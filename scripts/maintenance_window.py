#!/usr/bin/env python3
"""Journaled GLP-44/73 maintenance: plan, enter, status, exit.

Enter starts a bounded keeper which owns the existing flocks across CLI calls.
Every pause has a durable intent and preimage. A dead keeper is never ACTIVE;
repeat enter resumes the journal, or exit restores it. No jobs run from this tool.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import sqlite3
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import time

SCHEMA = 'glp44-glp73-maintenance-window-v1'
# Preserve the retained exit wrapper's pre-effect ceiling, but admit atomically
# behind at most the current owner, not repeated keeper inventories.
OPERATION_ADMISSION_SECONDS = 300.0
CONTROL_FIELDS = ('enabled', 'state', 'paused_at', 'paused_reason', 'pending_slot', 'next_run_at')
RUNTIME_FIELDS = {'fire_claim', 'run_claim', 'running', 'last_run_at', 'last_status',
                  'last_dispatch', 'last_error', 'last_delivery_error', 'last_delivery_unverified',
                  'failure_streak', 'monitor_state', 'quota_hold_until', 'model_snapshot',
                  'provider_snapshot'}


def json_sha(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def job_configuration(job):
    data = {key: value for key, value in job.items()
            if key not in set(CONTROL_FIELDS) | RUNTIME_FIELDS | {'script_sha256'}}
    if isinstance(data.get('repeat'), dict):
        data['repeat'] = {k: v for k, v in data['repeat'].items() if k != 'completed'}
    return json_sha(data)


def control_tools():
    directory = Path(__file__).resolve().parent
    names = (Path(__file__).name, 'glp44_scoped_adoption.py', 'glp73_paired_adoption.py',
             'glp73_sync_rebaseline.py')
    return {str(directory / name): hashlib.sha256((directory / name).read_bytes()).hexdigest()
            for name in names if (directory / name).is_file()}


def control_process(argv, data):
    """Exempt only these immutable, target-bound controller/adopter CLIs."""
    try:
        if not argv or not Path(argv[0]).name.startswith('python'):
            return False
        args = argv[1:]
        while args and args[0] in ('-B', '-u'):
            args = args[1:]
        if not args:
            return False
        path = str(Path(args[0]).resolve())
        expected = data.get('control_tools', {}).get(path)
        if not expected or hashlib.sha256(Path(path).read_bytes()).hexdigest() != expected:
            return False
        options = args[1:]
        def selected(flag, default=None):
            if flag not in options:
                return default
            require(options.count(flag) == 1, 'duplicate_controller_flag')
            return options[options.index(flag) + 1]
        if str(Path(selected('--root', '/home/ubuntu/business')).resolve()) != data['root']:
            return False
        name = Path(path).name
        if name == 'glp73_sync_rebaseline.py':
            return all(option in {'--root', data['root']} for option in options)
        if str(Path(selected('--hermes', '/home/ubuntu/.hermes')).resolve()) != data['hermes']:
            return False
        if name.startswith(('glp44_scoped_adoption', 'glp73_paired_adoption')):
            return str(Path(selected('--window')).absolute()) == data['journal']
        return (options and options[0] in {'plan', 'status', 'enter', 'exit'}
                and str(Path(selected('--journal')).absolute()) == data['journal'])
    except (OSError, ValueError, TypeError, IndexError, WindowError):
        return False


class WindowError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise WindowError(message)


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    require(not path.is_symlink(), 'journal_symlink')
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_journal(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as handle:
        require(stat.S_ISREG(os.fstat(handle.fileno()).st_mode), 'journal_not_regular')
        data = json.load(handle)
    require(data.get('schema') == SCHEMA, 'unknown_window_journal')
    return data


def identity(pid=None):
    pid = os.getpid() if pid is None else int(pid)
    base = Path('/proc') / str(pid)
    return {'pid': pid, 'start_ticks': int((base / 'stat').read_text().rsplit(') ', 1)[1].split()[19]),
            'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip()}


def locks_verified(data):
    """Bind each kernel EX flock to the keeper's descriptor AND current inode."""
    try:
        owner = data['keeper']
        if identity(owner['pid']) != owner or not data.get('locks'):
            return False
        for row in data['locks']:
            path = Path(row['path'])
            if path.is_symlink():
                return False
            current = path.stat()
            held = (Path('/proc') / str(owner['pid']) / 'fd' / str(row['fd'])).stat()
            if (current.st_dev, current.st_ino) != (row['device'], row['inode']):
                return False
            if (held.st_dev, held.st_ino) != (row['device'], row['inode']):
                return False
            lines = (Path('/proc') / str(owner['pid']) / 'fdinfo' / str(row['fd'])).read_text().splitlines()
            wanted = f'{os.major(current.st_dev):02x}:{os.minor(current.st_dev):02x}:{current.st_ino}'
            if not any(len(fields := line.split()) == 9 and fields[2] == 'FLOCK'
                       and fields[4] == 'WRITE' and fields[6].lower() == wanted.lower()
                       for line in lines if line.startswith('lock:')):
                return False
        return True
    except (OSError, ValueError, KeyError):
        return False


@contextmanager
def operation_lock(journal, *, timeout=0):
    """Bounded pre-effect admission; a queued foreground owner beats the keeper.

    All owners take the admission gate before the existing operation lock. A
    waiter retains the gate until acquisition, so the nonblocking keeper cannot
    barge into another full verification. Neither lock is a source/fire fence:
    those remain continuously held by the keeper. Never retry the yielded body.
    """
    path = Path(str(journal) + '.operation.lock')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    deadline = time.monotonic() + timeout
    gate = fd = None

    def acquire(handle):
        require(stat.S_ISREG(os.fstat(handle).st_mode), 'operation_lock_not_regular')
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError as exc:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WindowError('window_operation_busy') from exc
                time.sleep(min(0.05, remaining))
                require(time.monotonic() < deadline, 'window_operation_busy')

    try:
        gate = os.open(str(path) + '.admission', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        acquire(gate)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        acquire(fd)
        os.close(gate)
        gate = None
        yield  # Outside acquisition handling: ambiguous effects propagate once.
    finally:
        if fd is not None:
            os.close(fd)
        if gate is not None:
            os.close(gate)


def entry_key(entry):
    return entry['kind'], entry['id']


def inventory_shape(report):
    # State is allowed to change only through journaled pause/adoption. Source
    # content changes during adoption, but commands and coverage must not change.
    return sorted([e['kind'], e['id'], e.get('command'), e.get('affected')]
                  for e in report['entries'])


@contextmanager
def native_registry_lock(scheduler):
    """Wait only before acquisition; never replay a yielded save/effect.

    Native locking can degrade on failure. Maintenance must instead fail closed
    after a short bounded wait, then re-read/CAS under the acquired lock.
    """
    deadline = time.monotonic() + 5.0
    require(scheduler._jobs_file_lock.acquire(timeout=5.0), 'native_registry_lock_timeout')
    fd = None
    handle = None
    state = getattr(scheduler, '_jobs_lock_state', None)
    entered = False
    try:
        require(state is None or not getattr(state, 'depth', 0), 'nested_registry_lock_refused')
        fd = os.open(scheduler._jobs_lock_file(), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                require(remaining > 0, 'native_registry_lock_timeout')
                time.sleep(min(0.05, remaining))
                require(time.monotonic() < deadline, 'native_registry_lock_timeout')
        handle = os.fdopen(fd, 'a+')
        fd = None  # The file object now owns the genuinely flocked descriptor.
        if state is not None:
            state.depth = 1  # Native tool calls reuse this held descriptor.
            state.cross_process = True
            state.custody = (scheduler._jobs_lock_file(), handle)
            state.load_stamp = None
            entered = True
        yield  # Outside the contention handler: ambiguous failures propagate.
    finally:
        if entered:
            state.depth = 0
            state.cross_process = False
            state.custody = None
            state.load_stamp = None
        if handle is not None:
            handle.close()
        if fd is not None:
            os.close(fd)
        scheduler._jobs_file_lock.release()


class MaintenanceWindow:
    def __init__(self, backend, journal, lock_paths, *, drain_timeout=300,
                 poll_interval=0.25, max_window_seconds=3600, clock=time.monotonic,
                 sleeper=time.sleep):
        require(0 <= drain_timeout <= 86400 and 0 < max_window_seconds <= 86400, 'invalid_deadline')
        self.backend, self.journal = backend, Path(journal).absolute()
        self.lock_paths = [Path(p).absolute() for p in lock_paths]
        self.drain_timeout, self.poll_interval = drain_timeout, poll_interval
        self.max_window_seconds, self.clock, self.sleeper = max_window_seconds, clock, sleeper
        self.handles = []
        self.controller = None
        self._recovery = None

    def plan(self):
        report = self.backend.inventory()
        require(report.get('complete', not report.get('errors')) and not report.get('errors'),
                'inventory_incomplete')
        require(len({entry_key(e) for e in report['entries']}) == len(report['entries']), 'duplicate_inventory_entry')
        return report

    def _save(self, data):
        atomic_json(self.journal, data)

    def release_locks(self):
        for fd in reversed(self.handles):
            os.close(fd)
        self.handles = []

    def _take_locks(self, data, *, persist=True):
        self.release_locks()
        data['locks'] = []
        for path in self.lock_paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            self.handles.append(fd)
            st = os.fstat(fd)
            require(stat.S_ISREG(st.st_mode), 'maintenance_lock_not_regular')
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            data['locks'].append({'path': str(path), 'fd': fd, 'device': st.st_dev, 'inode': st.st_ino})
        data['keeper'] = identity()
        if persist:
            self._save(data)

    def _verify(self, data):
        if (data.get('pre_adoption_recovery') or data['state'] != 'active'
                or time.time() >= data['expires_at'] or not locks_verified(data)):
            return False
        if sorted(row['path'] for row in data['locks']) != sorted(str(p) for p in self.lock_paths):
            return False
        report = self.plan()
        if inventory_shape(report) != data['inventory_shape']:
            return False
        if not self._sources_valid(data):
            return False
        for entry in report['entries']:
            for path, actual in entry.get('source_sha256', {}).items():
                permitted = data.get('source_updates', {}).get(path)
                expected = data.get('source_sha256', {}).get(path)
                if actual not in ([permitted['before'], permitted['after']] if permitted else [expected]):
                    return False
        rows = data['entries']
        if not all(self.backend.paused(row['entry'], row['prior']) for row in rows):
            return False
        if self.backend.inflight([row['entry'] for row in rows]):
            return False
        return True

    def _sources_valid(self, data, for_exit=False):
        if data.get('source_switch', {}).get('state', 'published') not in {'published', 'rolled_back'}:
            return False
        try:
            for jid, pin in data.get('job_outcomes', {}).items():
                if self.backend.job(jid).get('script_sha256') != pin:
                    return False
            for path, old in data.get('source_sha256', {}).items():
                source = Path(path)
                if source.is_symlink():
                    return False
                actual = hashlib.sha256(source.read_bytes()).hexdigest() if source.exists() else None
                update = data.get('source_updates', {}).get(path)
                if actual not in ([update['before'], update['after']] if update else [old]):
                    return False
            transaction = data.get('source_transaction')
            if transaction:
                outcome = transaction.get('outcome')
                if outcome == 'uncertain':
                    return False
                # A half-written multi-file cohort is never safe to re-enable.
                sides = (['after'] if outcome == 'applied' else ['before'] if outcome == 'rolled_back'
                         or for_exit else ['before', 'after'])
                cohorts = [transaction[side] for side in sides]
                if not any(all(source_digest(path) == digest
                               for path, digest in cohort.items()) for cohort in cohorts):
                    return False
                if for_exit and outcome == 'pending':
                    if not all(json_sha(self.backend.job(jid)) == data['job_updates'][jid]['before_sha256']
                               for jid in transaction['job_ids']):
                        return False
            return True
        except OSError:
            return False

    def status(self):
        if not self.journal.exists():
            return {'state': 'absent', 'verified': False, 'journal': str(self.journal)}
        data = read_journal(self.journal)
        error = None
        try:
            verified = self._verify(data)
        except Exception as exc:
            verified, error = False, type(exc).__name__
        result = {'state': data['state'], 'verified': verified, 'journal': str(self.journal),
                'keeper_alive': locks_verified(data), 'expires_at': data['expires_at'],
                'recovery_required': data['state'] != 'closed' and not verified,
                'error': error or data.get('error')}
        transaction = data.get('source_transaction')
        if transaction and transaction.get('outcome') in {'pending', 'uncertain'}:
            try:
                result['outcome_proposal'] = self.outcome_proposal(data)
            except OSError:
                result['cohort_recovery'] = data.get('source_switch')
        return result

    def outcome_proposal(self, data):
        transaction = data['source_transaction']
        sources = {path: source_digest(path)
                   for path in transaction['before']}
        jobs = {jid: json_sha(self.backend.job(jid)) for jid in transaction['job_ids']}
        sides = [side for side in ('before', 'after') if sources == transaction[side]
                 and all(digest == data['job_updates'][jid][side + '_sha256'] for jid, digest in jobs.items())]
        reviewed = {key: value for key, value in data.items() if key not in {'exit_requested', 'outcome_review'}}
        return {'schema': 'glp-maintenance-outcome-proposal-v1', 'journal_sha256': json_sha(reviewed),
                'side': sides[-1] if sides else None, 'source_sha256': sources, 'job_sha256': jobs}

    def reconcile_outcome(self, data, review):
        proposal = self.outcome_proposal(data)
        require(review.get('schema') == 'glp-maintenance-outcome-review-v1'
                and review.get('decision') == 'ACCEPT_EXACT_OUTCOME'
                and review.get('prepared_by') and review.get('reviewer')
                and review['prepared_by'] != review['reviewer'] and review.get('proposal') == proposal
                and proposal['side'] is not None, 'independent_exact_outcome_review_required')
        require(inventory_shape(self.plan()) == data['inventory_shape'], 'reconcile_inventory_changed')
        require(all(self.backend.paused(row['entry'], row['prior']) for row in data['entries']),
                'reconcile_pause_ownership_changed')
        require(not self.backend.inflight([row['entry'] for row in data['entries']]), 'reconcile_not_drained')
        if not locks_verified(data) or data['keeper'] != identity():
            self._take_locks(data)
        require(self.outcome_proposal(data)['source_sha256'] == proposal['source_sha256']
                and self.outcome_proposal(data)['job_sha256'] == proposal['job_sha256'], 'reconcile_preimage_changed')
        side = proposal['side']
        transaction = data['source_transaction']
        transaction['outcome'] = 'applied' if side == 'after' else 'rolled_back'
        data.setdefault('source_sha256', {}).update(transaction[side])
        for path in transaction[side]:
            data.get('source_updates', {}).pop(path, None)
        for jid in transaction['job_ids']:
            data.setdefault('job_outcomes', {})[jid] = data['job_updates'][jid][side + '_pin']
        data['outcome_review_sha256'] = json_sha(review)
        self._save(data)

    def _recovery_basis(self, data):
        basis = copy.deepcopy(data)
        for key in ('state', 'keeper', 'locks', 'error', 'restore_errors',
                    'closed_at', 'pre_adoption_recovery'):
            basis.pop(key, None)
        for row in basis['entries']:
            for key in ('restore_intent', 'restored', 'recovery_resolution'):
                row.pop(key, None)
        return basis

    def _recovery_target(self, data):
        require(data['journal'] == str(self.journal)
                and data['root'] == str(self.backend.root)
                and data['hermes'] == str(self.backend.hermes), 'recovery_target_changed')
        require(data.get('source_transaction') is None
                and not any(data.get(key) for key in
                            ('source_switch', 'source_updates', 'job_updates',
                             'job_outcomes', 'adopter', 'outcome_review')),
                'recovery_requires_pre_adoption_window')
        expected_locks = [str(p.absolute()) for p in
                          default_locks(self.backend.root, self.backend.hermes)]
        require([str(p) for p in self.lock_paths] == expected_locks,
                'recovery_lock_scope_changed')
        origin = data.get('pre_adoption_recovery', {}).get('origin', data)
        retained = origin.get('locks', [])
        require(not retained or sorted(row['path'] for row in retained) == sorted(expected_locks),
                'recovery_retained_lock_scope_changed')

    def _recovery_scope(self, data):
        self._recovery_target(data)
        report = self.plan()
        # Native discover() reports per-backend coverage, not complete=True.
        coverage = report.get('coverage', {})
        complete = (report.get('complete') is True or
                    set(coverage) == {'hermes', 'crontab', 'systemd', 'n8n'}
                    and all(value is True for value in coverage.values()))
        require(complete and inventory_shape(report) == data['inventory_shape'],
                'recovery_inventory_changed')
        rows = data['entries']
        require(len({entry_key(row['entry']) for row in rows}) == len(rows)
                and {entry_key(row['entry']) for row in rows}
                == {entry_key(e) for e in report['entries'] if e['affected']},
                'recovery_control_cohort_changed')
        current = {entry_key(e): e for e in report['entries']}
        require(all(set(row['entry'].get('source_sha256', {}))
                    == set(current[entry_key(row['entry'])].get('source_sha256', {}))
                    for row in rows), 'recovery_source_coverage_changed')
        sources = {}
        for entry in report['entries']:
            for path, digest in entry.get('source_sha256', {}).items():
                require(path not in sources or sources[path] == digest,
                        'recovery_conflicting_source_inventory')
                sources[path] = digest
        require(set(sources) == set(data['source_sha256']), 'recovery_source_scope_changed')
        for path, digest in sources.items():
            source = Path(path)
            require(source.is_absolute() and source.resolve() == source
                    and not source.is_symlink()
                    and (not source.exists() or source.is_file()), 'recovery_source_type_changed')
            require(source_digest(path) == digest, 'recovery_source_inventory_stale')
        return sources

    def _recovery_dead_owners(self, data):
        for key in ('keeper', 'controller', 'adopter'):
            owner = data.get(key)
            if not owner:
                continue
            # A second call in this process still has to reacquire every flock.
            if key == 'keeper' and data.get('pre_adoption_recovery') and owner == identity():
                continue
            try:
                current = identity(owner['pid'])
            except FileNotFoundError:
                require(not (Path('/proc') / str(owner['pid'])).exists(),
                        'recovery_owner_visibility_unknown')
                continue
            require(current != owner, 'recovery_owner_alive:' + key)

    def pre_adoption_proposal(self, data):
        require(not data.get('pre_adoption_recovery')
                and data['state'] == 'recovery_required', 'recovery_initial_state_required')
        require(data['entries'] and all(row['intent'] and row['paused']
                and not row['restore_intent'] and not row['restored']
                for row in data['entries']), 'recovery_initial_custody_required')
        self._recovery_dead_owners(data)
        sources = self._recovery_scope(data)
        changes = {path: {'before': old, 'after': sources[path]}
                   for path, old in data['source_sha256'].items() if old != sources[path]}
        require(changes and all(change['before'] is not None and change['after'] is not None
                                for change in changes.values()), 'recovery_existing_sources_only')
        tools = control_tools()
        retained = {Path(path).name: digest for path, digest in data['control_tools'].items()}
        current_tools = {Path(path).name: digest for path, digest in tools.items()}
        require(len(retained) == len(data['control_tools'])
                and len(current_tools) == len(tools) and set(retained) == set(current_tools)
                and all(name == Path(__file__).name or digest == retained[name]
                        for name, digest in current_tools.items()), 'recovery_other_control_tool_changed')
        jobs = {}
        for row in data['entries']:
            entry, prior = row['entry'], row['prior']
            if entry['kind'] != 'hermes':
                require(self.backend.paused(entry, prior), 'recovery_pause_ownership_changed')
                continue
            job = self.backend.job(entry['id'])
            require(job_configuration(job) == prior['configuration_sha256'], 'recovery_job_configuration_changed')
            controls = {key: job[key] for key in CONTROL_FIELDS if key in job}
            if controls == prior.get('paused_fields') and job.get('enabled') is False:
                disposition = 'owned_pause'
            elif controls == prior['fields']:
                disposition = 'external_preimage'
            else:
                # Only an independently authorized owner release/re-hold of an
                # original native hold may be preserved. Never normalize its
                # cursor, replay its run, or write the old hold over its outcome.
                cursors = ('pending_slot', 'next_run_at')
                require(prior['fields'].get('enabled') is False
                        and {k: controls[k] for k in cursors if k in controls}
                            == {k: prior['fields'][k] for k in cursors if k in prior['fields']}
                        and ((controls.get('enabled') is True and controls.get('state') == 'scheduled'
                              and controls.get('paused_at') is None and controls.get('paused_reason') is None)
                             or (controls.get('enabled') is False and controls.get('state') == 'paused'
                                 and isinstance(controls.get('paused_at'), str) and controls['paused_at']
                                 and isinstance(controls.get('paused_reason'), str) and controls['paused_reason'])),
                        'recovery_control_conflict')
                disposition = 'external_owner_controls'
            pin = prior.get('script_sha256')
            paths = [path for path, digest in entry.get('source_sha256', {}).items()
                     if pin is not None and digest == pin]
            pin_path = paths[0] if len(paths) == 1 else None
            if pin_path is not None:
                require(job.get('script_sha256') == sources[pin_path], 'recovery_pin_invalid')
            else:
                # Retain already-unpinned/legacy-stale pins without claiming
                # source adoption. No previously unbound executable may change
                # before our native restore. An externally restored exact
                # preimage is observation only, separately reviewed below.
                require(job.get('script_sha256') == pin, 'recovery_pin_invalid')
                command_path = entry.get('command')
                require(disposition == 'external_preimage' or
                        command_path not in entry.get('source_sha256', {}) or
                        sources[command_path] == entry['source_sha256'][command_path],
                        'recovery_pin_binding_unproven')
            after = copy.deepcopy(job)
            if disposition != 'external_owner_controls':
                for key in CONTROL_FIELDS:
                    if key in prior['fields']:
                        after[key] = copy.deepcopy(prior['fields'][key])
                    else:
                        after.pop(key, None)
            jobs[entry['id']] = {'before_sha256': json_sha(job), 'after_sha256': json_sha(after),
                                'pin': job.get('script_sha256'), 'pin_path': pin_path,
                                'pin_changed': job.get('script_sha256') != pin,
                                'disposition': disposition}
            if disposition == 'external_owner_controls':
                jobs[entry['id']]['preserved_controls'] = copy.deepcopy(controls)
        require(not self.backend.inflight([row['entry'] for row in data['entries']]), 'recovery_not_drained')
        return {'schema': 'glp-maintenance-pre-adoption-proposal-v1',
                'journal_sha256': json_sha(data), 'source_sha256': sources, 'changes': changes,
                'tools_sha256': tools,
                'helper_sha256': {str(Path(__file__).with_name(name).resolve()):
                    source_digest(Path(__file__).with_name(name))
                    for name in ('maintenance_admission.py', 'sync_from_repo.py')},
                'jobs': jobs, 'lock_paths': [str(p) for p in self.lock_paths]}

    def _recovery_review(self, review, proposal):
        require(review.get('schema') == 'glp-maintenance-pre-adoption-review-v1'
                and review.get('decision') == 'ACCEPT_EXACT_PRE_ADOPTION_EXIT'
                and isinstance(review.get('prepared_by'), str) and review['prepared_by'].strip()
                and isinstance(review.get('reviewer'), str) and review['reviewer'].strip()
                and review['prepared_by'].strip() != review['reviewer'].strip()
                and review.get('proposal') == proposal, 'independent_exact_recovery_review_required')
        required = {'source_approvals': set(proposal['changes']),
                    'pin_approvals': {jid for jid, job in proposal['jobs'].items() if job['pin_changed']},
                    'external_restores': {jid for jid, job in proposal['jobs'].items()
                                          if job['disposition'] == 'external_preimage'},
                    'external_controls': {jid for jid, job in proposal['jobs'].items()
                                          if job['disposition'] == 'external_owner_controls'}}
        for key, identities in required.items():
            references = review.get(key, {} if key == 'external_controls' else None)
            require(isinstance(references, dict) and set(references) == identities
                    and all(isinstance(value, str) and value.strip() for value in references.values()),
                    'recovery_approval_references_required:' + key)

    def _recovery_source_view(self, data):
        record = data.get('pre_adoption_recovery')
        require(record is not None and self._recovery is record, 'use_recover_pre_adoption')
        require(locks_verified(data) and data['keeper'] == identity()
                and [row['path'] for row in data['locks']] == [str(p) for p in self.lock_paths],
                'recovery_fences_lost')
        proposal = record['review']['proposal']
        require(control_tools() == proposal['tools_sha256'], 'recovery_executor_changed')
        require(proposal['helper_sha256'] == {
            str(Path(__file__).with_name(name).resolve()): source_digest(Path(__file__).with_name(name))
            for name in ('maintenance_admission.py', 'sync_from_repo.py')}, 'recovery_helper_changed')
        for path in proposal['source_sha256']:
            source = Path(path)
            require(source.resolve() == source and not source.is_symlink()
                    and (not source.exists() or source.is_file()), 'recovery_source_type_changed')
        # An exit-only comparison view, never a journal baseline or a job write.
        view = copy.deepcopy(data)
        view['source_sha256'] = copy.deepcopy(proposal['source_sha256'])
        require(self._sources_valid(view, for_exit=True), 'recovery_sources_changed')
        return view

    def _recovery_row(self, data, row):
        entry, prior = row['entry'], row['prior']
        proposal = data['pre_adoption_recovery']['review']['proposal']
        if entry['kind'] == 'hermes':
            expected = proposal['jobs'][entry['id']]
            job = self.backend.job(entry['id'])
            require(job.get('script_sha256') == expected['pin']
                    and job_configuration(job) == prior['configuration_sha256'], 'recovery_job_changed')
            if row['restored']:
                require(row.get('recovery_resolution') in
                        {'native_readback', 'landed_intent', 'external_preimage', 'external_owner_controls'}, 'recovery_receipt_missing')
                return 'released'
            actual = json_sha(job)
            if row['restore_intent'] and actual == expected['after_sha256']:
                return 'landed_intent'
            require(actual == expected['before_sha256'], 'recovery_job_preimage_changed')
            return expected['disposition']
        if row['restored']:
            require(row.get('recovery_resolution') in {'native_readback', 'landed_intent'},
                    'recovery_receipt_missing')
            return 'released'
        if self.backend.paused(entry, prior):
            return 'owned_pause'
        require(row['restore_intent'] and self.backend.recovery_restored(entry, prior),
                'recovery_pause_ownership_changed')
        return 'landed_intent'

    def _recovery_check(self, data):
        record = data['pre_adoption_recovery']
        origin, review = record['origin'], record['review']
        proposal = review['proposal']
        require(record['schema'] == 'glp-maintenance-pre-adoption-recovery-v1'
                and json_sha(origin) == proposal['journal_sha256']
                and self._recovery_basis(data) == self._recovery_basis(origin), 'recovery_journal_changed')
        self._recovery_review(review, proposal)
        require(self._recovery_scope(data) == proposal['source_sha256'], 'recovery_preimage_changed')
        self._recovery_source_view(data)
        for row in data['entries']:
            self._recovery_row(data, row)
        require(not self.backend.inflight([row['entry'] for row in data['entries']]), 'recovery_not_drained')

    def recover_pre_adoption(self, review=None):
        if review is None:
            return self._recover_pre_adoption()
        from sync_from_repo import source_lock
        self._recovery_target(read_journal(self.journal))
        # Other live install owners keep their lane. Wait/refuse the shared
        # importer fence, never race their sources or terminate their work.
        with source_lock(self.backend.root):
            return self._recover_pre_adoption(review)

    def _recover_pre_adoption(self, review=None):
        with operation_lock(self.journal, timeout=OPERATION_ADMISSION_SECONDS):
            data = read_journal(self.journal)
            record = data.get('pre_adoption_recovery')
            if review is None:
                require(record is None, 'recovery_started_reuse_original_review')
                return self.pre_adoption_proposal(data)
            if record is not None:
                require(review == record['review'], 'recovery_review_changed')
                if data['state'] == 'closed':
                    return self.status()  # No effects and no renewed authority.
                require(data['state'] in {'restoring', 'recovery_required'}, 'recovery_state_changed')
            self._recovery_target(data)
            self._recovery_dead_owners(data)
            held = copy.deepcopy(data)
            try:
                self._take_locks(held, persist=False)
                if record is None:
                    proposal = self.pre_adoption_proposal(data)
                    self._recovery_review(review, proposal)
                    record = {'schema': 'glp-maintenance-pre-adoption-recovery-v1',
                              'origin': copy.deepcopy(data), 'review': copy.deepcopy(review)}
                    data['pre_adoption_recovery'] = record
                data['keeper'], data['locks'] = held['keeper'], held['locks']
                self._recovery = record
                self._recovery_check(data)  # Entire cohort before the first control effect.
                self._save(data)
                return self._exit(data)
            finally:
                self._recovery = None
                self.release_locks()

    def enter(self):
        with operation_lock(self.journal):
            return self._enter()

    def _enter(self):
        if self.journal.exists():
            data = read_journal(self.journal)
            require(not data.get('pre_adoption_recovery'), 'recovery_is_exit_only')
            if self._verify(data):
                return self.status()
            if data['state'] == 'closed':
                raise WindowError('closed_journal_use_new_path_for_new_window')
            require(not locks_verified(data), 'live_keeper_requires_exit_or_reconciliation')
            require(data['state'] not in {'restoring', 'recovery_required'}, 'restore_required_use_exit')
            if time.time() >= data['expires_at']:
                self._exit(data)
                raise WindowError('expired_window_restored')
            require(inventory_shape(self.plan()) == data['inventory_shape'], 'inventory_changed_resume_requires_exit')
            data['keeper'] = identity()
            if self.controller:
                data['controller'] = self.controller
            self._save(data)
        else:
            report = self.plan()  # Fail before any snapshot or mutation.
            rows = [{'entry': e, 'prior': self.backend.snapshot(e), 'intent': False,
                     'paused': False, 'restore_intent': False, 'restored': False}
                    for e in report['entries'] if e['affected']]
            data = {'schema': SCHEMA, 'state': 'entering', 'entries': rows,
                    'journal': str(self.journal), 'control_tools': control_tools(),
                    'root': str(getattr(self.backend, 'root', '')),
                    'hermes': str(getattr(self.backend, 'hermes', '')),
                    'inventory_shape': inventory_shape(report), 'locks': [], 'keeper': identity(),
                    'created_at': time.time(), 'expires_at': time.time() + self.max_window_seconds}
            data['source_sha256'] = {path: digest for entry in report['entries']
                                     for path, digest in entry.get('source_sha256', {}).items()}
            if self.controller:
                data['controller'] = self.controller
            self._save(data)  # ALL exact preimages are durable before the first pause.
        try:
            for row in data['entries']:
                if not row['intent']:
                    row['intent'] = True
                    self._save(data)
                if not self.backend.paused(row['entry'], row['prior']):
                    self.backend.pause(row['entry'], row['prior'])
                require(self.backend.paused(row['entry'], row['prior']), 'pause_readback_failed')
                row['paused'] = True
                self._save(data)
            data['state'] = 'draining'
            self._save(data)
            deadline = self.clock() + self.drain_timeout
            while self.backend.inflight([row['entry'] for row in data['entries']]):
                require(self.clock() < deadline, 'drain_timeout')
                self.sleeper(min(self.poll_interval, max(0, deadline - self.clock())))
            self._take_locks(data)
            data['state'] = 'active'
            self._save(data)
            require(self._verify(data), 'active_window_verification_failed')
            return self.status()
        except Exception as exc:
            data['error'] = str(exc) if isinstance(exc, WindowError) else type(exc).__name__
            self._save(data)
            self._exit(data)
            raise

    def exit(self, outcome_review=None):
        with operation_lock(self.journal):
            if not self.journal.exists():
                return self.status()
            data = read_journal(self.journal)
            if outcome_review is not None:
                self.reconcile_outcome(data, outcome_review)
            return self._exit(data)

    def _exit(self, data):
        if data['state'] == 'closed':
            self.release_locks()
            return self.status()
        recovery = data.get('pre_adoption_recovery')
        source_view = self._recovery_source_view(data) if recovery is not None else data
        # Only the keeper or a controller after its death can release locks.
        if locks_verified(data):
            require(data['keeper'] == identity(), 'exit_must_be_requested_from_live_keeper')
        data['state'] = 'restoring'
        self._save(data)
        if not self._sources_valid(source_view, for_exit=True):
            data['state'], data['error'] = 'recovery_required', 'source_or_adoption_outcome_requires_reconciliation'
            self._save(data)
            raise WindowError('restore_refused_source_or_adoption_outcome_requires_reconciliation')
        failures = []
        # Source locks stay held during restore; scheduler fire locks in the
        # backend prevent launching until an exact schedule repair is complete.
        for row in reversed(data['entries']):
            if not row['intent'] or row['restored']:
                continue
            prior = row['prior']
            if recovery is not None:
                self._recovery_source_view(data)
                disposition = self._recovery_row(data, row)
                if disposition in {'landed_intent', 'external_preimage', 'external_owner_controls'}:
                    row['restored'], row['recovery_resolution'] = True, disposition
                    self._save(data)
                    continue
                if row['entry']['kind'] == 'hermes':
                    expected = recovery['review']['proposal']['jobs'][row['entry']['id']]
                    prior = copy.deepcopy(prior)
                    prior['script_sha256'] = expected['pin']
                    prior['recovery_expected_sha256'] = expected['before_sha256']
            row['restore_intent'] = True
            self._save(data)
            try:
                self.backend.restore(row['entry'], prior)
                if recovery is not None and row['entry']['kind'] != 'hermes':
                    require(self.backend.recovery_restored(row['entry'], prior), 'recovery_restore_readback_failed')
                row['restored'] = True
                if recovery is not None:
                    row['recovery_resolution'] = 'native_readback'
                self._save(data)
            except Exception as exc:
                failures.append({'kind': row['entry']['kind'], 'id': row['entry']['id'],
                                 'error': type(exc).__name__})
                if recovery is not None:
                    break  # An ambiguous effect is not permission to restore more controls.
        if failures:
            data['state'], data['restore_errors'] = 'recovery_required', failures
            self._save(data)
            raise WindowError('restore_failed_use_recover_pre_adoption' if recovery is not None
                              else 'restore_failed_use_exit_to_reconcile_journal')
        if recovery is not None:
            self._recovery_source_view(data)
        data['state'] = 'closed'
        data['closed_at'] = time.time()
        self._save(data)
        self.release_locks()
        return self.status()


class LinuxBackend:
    """Native controls only. Never directly writes Hermes jobs.json."""
    def __init__(self, root, hermes, journal):
        self.root, self.hermes, self.journal = Path(root).resolve(), Path(hermes).resolve(), Path(journal).absolute()
        self._report = None
        self._bridge = None

    def command(self, argv, payload=None):
        environment = dict(os.environ)
        environment['XDG_RUNTIME_DIR'] = '/run/user/' + str(self.root.stat().st_uid)
        proc = subprocess.run(argv, input=payload.encode() if payload is not None else None,
                              capture_output=True, timeout=30,
                              env=environment)
        require(proc.returncode == 0, 'host_control_failed:' + Path(argv[0]).name)
        return proc.stdout.decode()  # Preserve crontab CRLF and final newline exactly.

    def _require_scheduler_namespace(self):
        if self.root == Path('/home/ubuntu/business'):
            require(Path('/proc/1/comm').read_text().strip() != 'codex', 'host_pid_namespace_unavailable')
        # Controls must operate in the workspace owner's scheduler namespace.
        # Run the whole controller as that user; sudo is used ONLY for nft.
        require(os.geteuid() == self.root.stat().st_uid, 'run_window_as_workspace_owner')

    def inventory(self):
        from maintenance_inventory import discover, InventoryError
        self._require_scheduler_namespace()
        def probe(argv):
            if argv[0] in {'systemctl', 'crontab'}:
                argv = ['/usr/bin/' + argv[0]] + argv[1:]
            try:
                return self.command(argv)
            except WindowError:
                if argv == ['/usr/bin/crontab', '-l']:
                    result = subprocess.run(argv, text=True, capture_output=True, timeout=20)
                    if result.returncode == 1 and 'no crontab for' in result.stderr:
                        return ''
                raise InventoryError(['scheduler_probe_failed:' + Path(argv[0]).name]) from None
        self._report = discover(self.root, self.hermes, probe=probe)
        return self._report

    def scheduler(self):
        # Select a profile before importing its supported management tool.
        os.environ['HERMES_HOME'] = str(self.hermes)
        sys.path.insert(0, str(self.hermes / 'hermes-agent'))
        from cron import jobs
        require(jobs._current_cron_store().jobs_file == self.hermes / 'cron/jobs.json', 'wrong_scheduler_store')
        return jobs

    def job(self, jid):
        document = json.loads((self.hermes / 'cron/jobs.json').read_text())
        found = [j for j in document['jobs'] if j.get('id') == jid]
        require(len(found) == 1, 'job_identity_changed')
        return found[0]

    def cron_tool(self, action, jid):
        self.scheduler()
        from tools.cronjob_tools import cronjob
        result = json.loads(cronjob(action=action, job_id=jid,
                                   reason='GLP44/73 maintenance; restore from ' + str(self.journal)))
        require(result.get('success') is True, 'hermes_' + action + '_failed')

    def bridge(self):
        # Existing external TCP gate and exact socket/cgroup census are usable
        # on the old resident. Never call get/health (credentials) or restart.
        if self._bridge is None:
            import glp44_safe_bridge_activation as activation
            require(self.root == activation.BUSINESS, 'bridge_fixed_host_required')
            backend = self
            class CredentialFreeBridge(activation.LinuxHost):
                def command(self, argv, payload=None):
                    if argv[:len(activation.SYSTEMCTL)] == activation.SYSTEMCTL:
                        argv = ['/usr/bin/systemctl', '--user'] + argv[len(activation.SYSTEMCTL):]
                    elif argv[0] == activation.NFT and os.geteuid() != 0:
                        argv = ['/usr/bin/sudo', '-n'] + argv
                    return backend.command(argv, payload)
            self._bridge = CredentialFreeBridge()
        return self._bridge

    def nft_table_absent(self):
        argv = ['/usr/sbin/nft', '--json', 'list', 'table', 'inet', 'glp44_bridge_activation']
        if os.geteuid() != 0:
            argv = ['/usr/bin/sudo', '-n'] + argv
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=10)
        return proc.returncode != 0 and 'No such file or directory' in proc.stderr

    def pause_reason(self):
        return 'GLP44/73 maintenance; restore from ' + str(self.journal)

    def timer_unchanged(self, entry, prior, refresh=False):
        if refresh:
            from maintenance_inventory import timer_metadata
            self._require_scheduler_namespace()
            # Fresh targeted configuration checks must not rescan every other
            # scheduler/source or replace the complete cohort's cached report.
            def probe(argv):
                return self.command(['/usr/bin/' + argv[0], *argv[1:]])
            current, _ = timer_metadata(entry['id'], self.root, probe=probe)
        else:
            report = self.inventory() if self._report is None else self._report
            current = next((e for e in report['entries'] if entry_key(e) == entry_key(entry)), None)
        require(current is not None and current['command'] == entry['command'], 'timer_command_changed')
        for field in ('timer_enabled', 'service', 'service_enabled',
                      'timer_configuration_sha256', 'service_configuration_sha256'):
            require(current['prior'].get(field) == prior.get(field), 'timer_configuration_changed:' + field)

    def snapshot(self, entry):
        kind = entry['kind']
        if kind == 'hermes':
            job = self.job(entry['id'])
            # Snapshot exact presence as well as values, and keep all unmutated
            # job data outside the journal (it may contain prompt text).
            fields = {key: copy.deepcopy(job[key]) for key in CONTROL_FIELDS if key in job}
            paused_fields = copy.deepcopy(fields)
            if fields.get('enabled') is not False:
                paused_fields.update(enabled=False, state='paused',
                                     paused_at=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                                     paused_reason=self.pause_reason())
                paused_fields.pop('pending_slot', None)
            # The expected pause is durable BEFORE native normalization/save,
            # including optional-field absence and the exact scheduling cursor.
            return {'fields': fields, 'paused_fields': paused_fields,
                    'script_sha256': job.get('script_sha256'), 'configuration_sha256': job_configuration(job),
                    'repeat': copy.deepcopy(job.get('repeat')), 'captured_at': time.time()}
        if kind == 'crontab':
            require(not entry['prior'].get('maintenance_marker'), 'foreign_maintenance_crontab_refused')
            text = self.command(['/usr/bin/crontab', '-l'])
            return {'text': text, **entry['prior']}
        if kind == 'systemd':
            self.timer_unchanged(entry, entry['prior'], refresh=True)
            require(entry['prior']['timer_active'] in {'active', 'inactive'}, 'transitional_timer_state')
            return copy.deepcopy(entry['prior'])
        if kind == 'n8n':
            host = self.bridge()
            owner = host.identity()
            require(self.nft_table_absent(),
                    'existing_or_unreadable_bridge_gate_refused')
            return {'identity': list(owner), 'gate_was_absent': True}
        raise WindowError('unknown_backend_kind')

    def _cron_paused(self, prior):
        lines = prior['text'].splitlines(keepends=True)
        marker = '# GLP44/73 maintenance ' + hashlib.sha256(str(self.journal).encode()).hexdigest()[:16] + ' '
        original = lines[prior['index']]
        require(original.rstrip('\r\n') == prior['line'], 'crontab_preimage_changed')
        lines[prior['index']] = marker + original
        return ''.join(lines)

    def _crontab_candidate(self, prior, restoring=False):
        # Multiple lines share one initial text. Derive the only admissible
        # current bytes from the journal's durable pause/restore intents.
        data = read_journal(self.journal)
        lines = prior['text'].splitlines(keepends=True)
        marker = '# GLP44/73 maintenance ' + hashlib.sha256(str(self.journal).encode()).hexdigest()[:16] + ' '
        for row in data['entries']:
            if row['entry']['kind'] != 'crontab' or not row['intent'] or row['restored']:
                continue
            index = row['prior']['index']
            lines[index] = marker + lines[index]
        return ''.join(lines)

    def completed_cursor_progress(self, entry, prior, job, data, scheduler):
        """Only a fenced, completed native occurrence chain may move the cursor.

        No lifecycle/config/pin/field-presence relaxation. Missing, pruned,
        manual, failed or ambiguous execution evidence stays a refusal.
        """
        fields = {k: job[k] for k in CONTROL_FIELDS if k in job}
        before = prior['fields']
        require(before.get('enabled') is True and before.get('state') == 'scheduled'
                and {k: v for k, v in fields.items() if k != 'next_run_at'}
                == {k: v for k, v in before.items() if k != 'next_run_at'}
                and all(not job.get(k) for k in ('fire_claim', 'run_claim', 'pending_slot')),
                'native_cursor_controls_changed')
        repeat = prior.get('repeat', entry.get('prior', {}).get('repeat'))
        current_repeat = job.get('repeat')
        require(isinstance(repeat, dict) and isinstance(current_repeat, dict)
                and {k: v for k, v in repeat.items() if k != 'completed'}
                == {k: v for k, v in current_repeat.items() if k != 'completed'},
                'native_cursor_repeat_changed')
        old_count, new_count = repeat.get('completed'), current_repeat.get('completed')
        require(type(old_count) is int and type(new_count) is int
                and 0 < new_count - old_count <= 10000, 'native_cursor_count_unproven')
        schedule = job.get('schedule', {})
        require(schedule.get('kind') in {'cron', 'interval'} and job.get('last_status') == 'ok',
                'native_cursor_schedule_unproven')

        def instant(value):
            try:
                result = dt.datetime.fromisoformat(value)
                require(result.tzinfo is not None, 'native_cursor_timestamp_unproven')
                return result.astimezone(dt.timezone.utc)
            except (TypeError, ValueError):
                raise WindowError('native_cursor_timestamp_unproven') from None

        cursor = before.get('next_run_at')
        first = instant(cursor)
        captured = dt.datetime.fromtimestamp(prior.get('captured_at', data['created_at']), dt.timezone.utc)
        database = scheduler._current_cron_store().cron_dir / 'executions.db'
        require(database.is_file(), 'native_cursor_ledger_missing')
        try:
            with sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True) as db:
                db.execute('PRAGMA query_only=ON')
                db.row_factory = sqlite3.Row
                rows = db.execute(
                    'SELECT id, status, scheduled_instant, claimed_at, started_at, finished_at, '
                    'handoff_pending FROM executions WHERE job_id=? AND scheduled_instant>=? '
                    'ORDER BY scheduled_instant LIMIT ?',
                    (entry['id'], first.isoformat(), new_count - old_count + 1)).fetchall()
        except sqlite3.Error:
            raise WindowError('native_cursor_ledger_unreadable') from None
        require(len(rows) == new_count - old_count, 'native_cursor_occurrences_unproven')
        previous_finish = captured
        for index, row in enumerate(rows):
            slot, claimed, started, finished = (instant(row[k]) for k in
                ('scheduled_instant', 'claimed_at', 'started_at', 'finished_at'))
            require(row['status'] == 'completed' and not row['handoff_pending']
                    and slot == instant(cursor)
                    and slot - dt.timedelta(seconds=scheduler.FIRE_CLAIM_SKEW_SECONDS) <= claimed
                    and previous_finish <= claimed <= started <= finished
                    and finished <= scheduler._hermes_now(), 'native_cursor_execution_unproven')
            # mark_job_run precedes finish_execution. For an intermediate cron
            # occurrence require both ends of that interval compute the SAME
            # next slot; interval chains cannot be reconstructed without marks.
            if index == len(rows) - 1:
                marked = job.get('last_run_at')
                require(started <= instant(marked) <= finished, 'native_cursor_mark_unproven')
            else:
                require(schedule['kind'] == 'cron' and
                        scheduler.compute_next_run(schedule, row['started_at']) ==
                        scheduler.compute_next_run(schedule, row['finished_at']),
                        'native_cursor_intermediate_mark_unproven')
                marked = row['finished_at']
            cursor = scheduler.compute_next_run(schedule, marked)
            require(instant(cursor) > slot, 'native_cursor_not_advanced')
            previous_finish = finished
        require(cursor == fields.get('next_run_at'), 'native_cursor_next_unproven')
        return {'from': before['next_run_at'], 'to': cursor, 'executions': [r['id'] for r in rows]}

    def post_pause_cursor_progress(self, entry, prior, job, data, scheduler):
        """Authenticate one already-running interval completion, for exit only.

        The retained snapshot has a native fire claim and its advanced dispatch
        cursor. Completion may advance that cursor again *after* our pause.
        Never admit ACTIVE, a new occurrence, replay, or configuration drift.
        """
        before, paused = prior['fields'], prior.get('paused_fields', {})
        row = next(r for r in data['entries'] if entry_key(r['entry']) == entry_key(entry))
        require(row.get('intent') is True and row.get('paused') is True
                and data.get('error') == 'active_window_verification_failed'
                and not data.get('source_transaction') and not data.get('job_updates')
                and before.get('enabled') is True and before.get('state') == 'scheduled'
                and paused.get('enabled') is False and paused.get('state') == 'paused'
                and paused.get('paused_reason') == self.pause_reason(),
                'post_pause_owned_lifecycle_required')
        controls = {k: job[k] for k in CONTROL_FIELDS if k in job}
        restored = copy.deepcopy(before)
        restored['next_run_at'] = controls.get('next_run_at')
        receipt = prior.get('post_pause_cursor_progress')
        require(({k: v for k, v in controls.items() if k != 'next_run_at'} ==
                 {k: v for k, v in paused.items() if k != 'next_run_at'})
                or (receipt is not None and controls == restored),
                'post_pause_controls_changed')
        require(job.get('script_sha256') == prior['script_sha256']
                and job_configuration(job) == prior['configuration_sha256']
                and all(not job.get(k) for k in ('fire_claim', 'run_claim', 'running', 'pending_slot'))
                and job.get('last_status') == 'ok' and job.get('last_error') is None,
                'post_pause_configuration_pin_or_claim_changed')
        repeat, current_repeat = prior.get('repeat'), job.get('repeat')
        require(isinstance(repeat, dict) and isinstance(current_repeat, dict)
                and type(repeat.get('completed')) is int
                and type(current_repeat.get('completed')) is int
                and current_repeat == dict(repeat, completed=repeat['completed'] + 1)
                and repeat.get('times') is None, 'post_pause_repeat_unproven')
        schedule = job.get('schedule', {})
        require(schedule.get('kind') == 'interval'
                and type(schedule.get('minutes')) is int and schedule['minutes'] > 0,
                'post_pause_interval_required')

        def instant(value):
            try:
                parsed = dt.datetime.fromisoformat(value)
                require(parsed.tzinfo is not None, 'post_pause_timestamp_unproven')
                return parsed.astimezone(dt.timezone.utc)
            except (TypeError, ValueError):
                raise WindowError('post_pause_timestamp_unproven') from None

        captured = dt.datetime.fromtimestamp(prior['captured_at'], dt.timezone.utc)
        pause_at = instant(paused.get('paused_at'))
        claim = entry.get('prior', {}).get('fire_claim')
        require(isinstance(claim, dict) and isinstance(claim.get('by'), str),
                'post_pause_snapshot_claim_required')
        database = scheduler._current_cron_store().cron_dir / 'executions.db'
        require(database.is_file(), 'post_pause_ledger_missing')
        try:
            with sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True) as db:
                db.execute('PRAGMA query_only=ON')
                db.row_factory = sqlite3.Row
                rows = db.execute('SELECT * FROM executions WHERE job_id=? LIMIT 10001',
                                  (entry['id'],)).fetchall()
        except sqlite3.Error:
            raise WindowError('post_pause_ledger_unreadable') from None
        require(len(rows) < 10001, 'post_pause_ledger_bound_exceeded')
        # Account for every attempt touching or following the snapshot. A second
        # attempt, missing terminal time, handoff or malformed row is uncertain.
        relevant = [r for r in rows if r['finished_at'] is None
                    or instant(r['finished_at']) >= captured or instant(r['claimed_at']) >= captured]
        require(len(relevant) == 1, 'post_pause_single_execution_required')
        execution = relevant[0]
        slot, claimed, started, finished = (instant(execution[k]) for k in
            ('scheduled_instant', 'claimed_at', 'started_at', 'finished_at'))
        marked, heartbeat = instant(job.get('last_run_at')), instant(claim.get('at'))
        dispatch = instant(before.get('next_run_at')) - dt.timedelta(minutes=schedule['minutes'])
        require(execution['source'] == 'builtin' and execution['status'] == 'completed'
                and execution['error'] is None and execution['handoff_pending'] == 0
                and execution['delivery_outcome'] == 'suppressed'
                and len(claim['by'].split(':')) == 3
                and claim['by'].split(':')[1].isdigit() and claim['by'].split(':')[2]
                # Dispatcher owns the claim; an isolated execution may have a
                # different PID. Bind by job/occurrence/time, not PID equality.
                and type(execution['pid']) is int and execution['pid'] > 0
                and isinstance(execution['process_id'], str) and execution['process_id']
                and slot - dt.timedelta(seconds=scheduler.FIRE_CLAIM_SKEW_SECONDS) <= claimed
                and claimed <= dispatch <= started <= min(captured, pause_at)
                and dispatch <= heartbeat <= captured
                and max(captured, pause_at) <= marked <= finished <= scheduler._hermes_now()
                and finished > max(captured, pause_at)
                and slot < instant(before['next_run_at']) < instant(controls.get('next_run_at'))
                and scheduler.compute_next_run(schedule, job['last_run_at']) == controls.get('next_run_at'),
                'post_pause_execution_unproven')
        paused_record = copy.deepcopy(job)
        for key in CONTROL_FIELDS:
            paused_record.pop(key, None)
        paused_record.update(copy.deepcopy(paused))
        paused_record['next_run_at'] = controls['next_run_at']
        restored_record = copy.deepcopy(paused_record)
        for key in CONTROL_FIELDS:
            restored_record.pop(key, None)
        restored_record.update(restored)
        progress = {'from': before['next_run_at'], 'to': controls['next_run_at'],
                    'executions': [execution['id']], 'execution_sha256': json_sha(dict(execution)),
                    'paused_record_sha256': json_sha(paused_record),
                    'restored_record_sha256': json_sha(restored_record)}
        require(receipt is None or receipt == progress, 'post_pause_receipt_changed')
        return progress

    def legacy_pause_not_started(self, data, row):
        """Authenticate the old pre-save refusal, not merely paused=False.

        This digest is the reviewed controller at receiving HEAD 09ca37b4.
        Its pause_preimage_changed callsite is before native pause/save; its
        sequential entry frontier binds that durable error to exactly one row.
        New/unknown controllers or ambiguous saver errors are NOT equivalent.
        """
        legacy = 'ff238691b508fdfefe8d546fd4d5c2dc7f7ceaa396aca14b72eec563374490d7'
        rows = data['entries']
        frontier = [r for r in rows if r.get('intent') is True and r.get('paused') is False]
        if (data.get('error') != 'pause_preimage_changed' or frontier != [row]
                or row.get('intent') is not True or row.get('paused') is not False
                or data.get('source_transaction') or data.get('job_updates')):
            return False
        index = rows.index(row)
        if (any(r.get('intent') is not True or r.get('paused') is not True for r in rows[:index])
                or any(r.get('intent') is not False for r in rows[index + 1:])):
            return False
        return any(Path(path).name == 'maintenance_window.py' and digest == legacy
                   and Path(path).is_file() and hashlib.sha256(Path(path).read_bytes()).hexdigest() == legacy
                   for path, digest in data.get('control_tools', {}).items())

    def pause(self, entry, prior):
        kind = entry['kind']
        if kind == 'hermes':
            scheduler = self.scheduler()
            with scheduler._fire_job_lock(entry['id']) as acquired, native_registry_lock(scheduler):
                require(acquired, 'native_pause_fire_lock_required')
                job = self.job(entry['id'])
                require(job.get('script_sha256') == prior['script_sha256'], 'pause_pin_changed')
                require(job_configuration(job) == prior['configuration_sha256'], 'pause_configuration_changed')
                fields = {k: job[k] for k in CONTROL_FIELDS if k in job}
                if fields != prior['fields']:
                    data = read_journal(self.journal)
                    progress = self.completed_cursor_progress(entry, prior, job, data, scheduler)
                    # Write the accepted cursor/expected pause BEFORE invoking
                    # native pause. Preserve every other snapshot field exactly.
                    prior['cursor_progress'] = progress
                    prior['fields']['next_run_at'] = fields['next_run_at']
                    prior['paused_fields']['next_run_at'] = fields['next_run_at']
                    prior['repeat'] = copy.deepcopy(job['repeat'])
                    row = next(r for r in data['entries'] if entry_key(r['entry']) == entry_key(entry))
                    row['prior'] = copy.deepcopy(prior)
                    atomic_json(self.journal, data)
                require('paused_fields' in prior, 'pause_exact_preimage_required')
                saver = scheduler._save_jobs_unlocked
                def exact_pause(jobs, **kwargs):
                    require(not kwargs.get('removed_ids') and not kwargs.get('replace'),
                            'destructive_scheduler_save_refused')
                    require(self.job(entry['id']) == job, 'pause_record_changed')
                    target = next(j for j in jobs if j['id'] == entry['id'])
                    # Native update normalizes legacy records. Preserve their
                    # original non-control fields in the SAME atomic save.
                    target.clear()
                    target.update(copy.deepcopy(job))
                    for key in CONTROL_FIELDS:
                        target.pop(key, None)
                    target.update(copy.deepcopy(prior['paused_fields']))
                    return saver(jobs, **kwargs)
                scheduler._save_jobs_unlocked = exact_pause
                try:
                    self.cron_tool('pause', entry['id'])
                finally:
                    scheduler._save_jobs_unlocked = saver
        elif kind == 'crontab':
            current = self.command(['/usr/bin/crontab', '-l'])
            target = self._crontab_candidate(prior)
            # Current row intent may be recorded before its actual mutation.
            undo = target.splitlines(keepends=True)
            undo[prior['index']] = prior['text'].splitlines(keepends=True)[prior['index']]
            require(current in {target, ''.join(undo)}, 'crontab_concurrent_edit')
            if current != target:
                self.command(['/usr/bin/crontab', '-'], target)
        elif kind == 'systemd':
            self.timer_unchanged(entry, prior, refresh=True)
            if prior['timer_active'] == 'active':
                self.command(['/usr/bin/systemctl', '--user', 'stop', entry['id']])
        elif kind == 'n8n':
            host = self.bridge()
            require(list(host.identity()) == prior['identity'], 'bridge_identity_changed')
            host.close_ingress()
        else:
            raise WindowError('unknown_backend_kind')

    def paused(self, entry, prior):
        kind = entry['kind']
        if kind == 'hermes':
            job = self.job(entry['id'])
            expected = prior.get('paused_fields')
            data = read_journal(self.journal)
            update = data.get('job_updates', {}).get(entry['id'])
            pins = {prior['script_sha256']}
            if update:
                pins.add(update['after_pin'])
            return (job.get('script_sha256') in pins and expected is not None and job.get('enabled') is False
                    and {k: job[k] for k in CONTROL_FIELDS if k in job} == expected
                    and job_configuration(job) == prior['configuration_sha256'])
        if kind == 'crontab':
            current = self.command(['/usr/bin/crontab', '-l'])
            expected = self._crontab_candidate(prior)
            # Compare the same complete bytes restoration requires: line
            # endings, final newline and unrelated rows are custody too.
            return (current == expected and
                    current.splitlines(keepends=True)[prior['index']] !=
                    prior['text'].splitlines(keepends=True)[prior['index']])
        if kind == 'systemd':
            self.timer_unchanged(entry, prior)
            return self.command(['/usr/bin/systemctl', '--user', 'show', entry['id'], '-p', 'ActiveState', '--value']).strip() == 'inactive'
        if kind == 'n8n':
            try:
                self.bridge().verify_ingress()
                return list(self.bridge().identity()) == prior['identity']
            except Exception:
                return False
        raise WindowError('unknown_backend_kind')

    def recovery_restored(self, entry, prior):
        if entry['kind'] == 'crontab':
            lines = self._crontab_candidate(prior).splitlines(keepends=True)
            lines[prior['index']] = prior['text'].splitlines(keepends=True)[prior['index']]
            return self.command(['/usr/bin/crontab', '-l']) == ''.join(lines)
        if entry['kind'] == 'systemd':
            self.timer_unchanged(entry, prior, refresh=True)
            return self.command(['/usr/bin/systemctl', '--user', 'show', entry['id'],
                                 '-p', 'ActiveState', '--value']).strip() == prior['timer_active']
        if entry['kind'] == 'n8n':
            return (prior.get('gate_was_absent') is True
                    and list(self.bridge().identity()) == prior['identity'] and self.nft_table_absent())
        raise WindowError('unsupported_recovery_readback')

    def restore(self, entry, prior):
        kind = entry['kind']
        if kind == 'hermes':
            scheduler = self.scheduler()
            with scheduler._fire_job_lock(entry['id']) as fenced, native_registry_lock(scheduler):
                require(fenced, 'restore_fire_fence_unavailable')
                current = self.job(entry['id'])
                if 'recovery_expected_sha256' in prior:
                    require(json_sha(current) == prior['recovery_expected_sha256'],
                            'recovery_restore_record_changed')
                data = read_journal(self.journal)
                update = data.get('job_updates', {}).get(entry['id'])
                pins = {prior['script_sha256']}
                if update:
                    pins.add(update['after_pin'])
                require(current.get('script_sha256') in pins, 'restore_unknown_pin')
                require(job_configuration(current) == prior['configuration_sha256'], 'restore_configuration_changed')
                controls = {k: current[k] for k in CONTROL_FIELDS if k in current}
                if controls == prior['fields']:
                    return  # Includes a crash after restore but before receipt.
                # Authenticate the legacy pre-save refusal, not paused=False.
                # Leave genuine native progress intact; closure needs no saver.
                row = next(r for r in data['entries'] if entry_key(r['entry']) == entry_key(entry))
                if self.legacy_pause_not_started(data, row):
                    self.completed_cursor_progress(entry, prior, current, data, scheduler)
                    return
                restore_fields = copy.deepcopy(prior['fields'])
                if not self.paused(entry, prior):
                    progress = self.post_pause_cursor_progress(entry, prior, current, data, scheduler)
                    restore_fields['next_run_at'] = progress['to']
                    if controls == restore_fields:
                        return  # Authenticated lost acknowledgement; no second save.
                    # Persist attribution BEFORE native save, without rewriting
                    # the original preimage, tool approvals, or runtime fields.
                    prior['post_pause_cursor_progress'] = progress
                    row['prior'] = copy.deepcopy(prior)
                    atomic_json(self.journal, data)
                # Restoration is an exact CAS, not a new scheduling request.
                # resume() rejects expired once jobs before saving and can
                # normalize legacy fields; use the native atomic saver under
                # the native registry + fire fences without rescheduling.
                jobs = scheduler.load_jobs()
                found = [j for j in jobs if j.get('id') == entry['id']]
                require(len(found) == 1 and found[0] == current, 'restore_record_changed')
                target = found[0]
                for key in CONTROL_FIELDS:
                    if key in restore_fields:
                        target[key] = copy.deepcopy(restore_fields[key])
                    else:
                        target.pop(key, None)
                expected = copy.deepcopy(target)
                scheduler._save_jobs_unlocked(jobs)
                require(self.job(entry['id']) == expected, 'restore_hermes_readback_failed')
        elif kind == 'crontab':
            current = self.command(['/usr/bin/crontab', '-l'])
            expected = self._crontab_candidate(prior)
            lines = expected.splitlines(keepends=True)
            lines[prior['index']] = prior['text'].splitlines(keepends=True)[prior['index']]
            restored = ''.join(lines)
            require(current in {expected, restored}, 'crontab_restore_conflict')
            if current != restored:
                self.command(['/usr/bin/crontab', '-'], restored)
            require(self.command(['/usr/bin/crontab', '-l']) == restored, 'crontab_restore_readback_failed')
        elif kind == 'systemd':
            self.timer_unchanged(entry, prior, refresh=True)
            if prior['timer_active'] == 'active':
                self.command(['/usr/bin/systemctl', '--user', 'start', entry['id']])
            require(self.command(['/usr/bin/systemctl', '--user', 'show', entry['id'], '-p', 'ActiveState', '--value']).strip()
                    == prior['timer_active'], 'timer_restore_readback_failed')
        elif kind == 'n8n':
            host = self.bridge()
            require(list(host.identity()) == prior['identity'], 'bridge_restore_identity_changed')
            if self.paused(entry, prior):
                host.open_ingress()
            else:
                # An absent table can mean crash AFTER successful delete. Never
                # silently release a changed or unverifiable table.
                require(self.nft_table_absent(), 'bridge_restore_gate_uncertain')
        else:
            raise WindowError('unknown_backend_kind')

    def inflight(self, entries):
        busy = []
        if any(e['kind'] == 'n8n' for e in entries):
            data = read_journal(self.journal)
            owner = next(row['prior']['identity'] for row in data['entries'] if row['entry']['kind'] == 'n8n')
            host = self.bridge()
            require(list(host.identity()) == owner, 'bridge_identity_changed')
            if not host.quiet(tuple(owner)) or any(r['status'] in {'queued', 'accepted', 'running'} for r in host.jobs().values()):
                busy.append({'kind': 'n8n', 'reason': 'socket_thread_cgroup_or_job_not_drained'})
        from maintenance_admission import native_runs
        native_busy, owners = native_runs(self.scheduler(), entries, self.job) if any(
            entry['kind'] == 'hermes' for entry in entries) else ([], set())
        busy.extend(native_busy)
        for entry in entries:
            if entry['kind'] == 'systemd':
                service = entry['prior']['service']
                value = self.command(['/usr/bin/systemctl', '--user', 'show', service, '-p', 'ActiveState', '--value']).strip()
                if value not in {'inactive', 'failed'}:
                    busy.append({'kind': 'systemd', 'id': service})
        # Bound admission to affected task launches/trees and actual source
        # references. Gateway residency, cwd and interpreter capability are not
        # source-reader evidence. Unknown unrelated PIDs do not block.
        from maintenance_inventory import affected_paths
        from maintenance_admission import process_readers
        data = read_journal(self.journal)
        excluded = {os.getpid()}
        for key in ('keeper', 'controller', 'adopter'):
            owner = data.get(key)
            if owner:
                try:
                    if identity(owner['pid']) == owner:
                        excluded.add(owner['pid'])
                except (OSError, ValueError, KeyError):
                    pass
        # The independently fenced bridge drains its actual action threads,
        # sockets and cgroup through host.quiet(), rather than its resident PID.
        excluded.update(row['prior']['identity'][0] for row in data['entries']
                        if row['entry']['kind'] == 'n8n')
        tasks = {p for entry in entries for p in entry.get('trace', [])}
        busy.extend(process_readers(affected_paths(self.root, self.hermes), tasks, owners, excluded))
        return busy


def default_locks(root, hermes):
    root, hermes = Path(root), Path(hermes)
    return [root / 'ops/glp/glp44-source-admission.lock',
            root / 'reports/ops/n8n-to-cron-20260924/runs/analytics_brain.lock',
            root / 'reports/ops/n8n-to-cron-20260924/runs/analytics_brain.state.lock',
            root / 'reports/ops/n8n-to-cron-20260924/runs/article_carrier_consume.lock',
            root / 'reports/glp-clinic-scout/affiliate-machine/weekly-reporting/.weekly-reporting.lock',
            hermes / 'locks/glp-price-index-weekly.lock',
            hermes / 'runstate/reports-archiver.lock']


def source_digest(path):
    """Presence-aware source CAS; only ENOENT is absence, never a symlink."""
    path = Path(path)
    require(not path.is_symlink(), 'source_symlink_refused')
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return None


class ActiveWindow:
    def __init__(self, journal, backend):
        self.journal, self.backend = Path(journal), backend

    def original_job(self, jid):
        data = read_journal(self.journal)
        row = next((row for row in data['entries'] if row['entry']['kind'] == 'hermes' and row['entry']['id'] == jid), None)
        require(row is not None, 'adoption_job_not_in_window')
        current = self.backend.job(jid)
        for key in CONTROL_FIELDS:
            if key in row['prior']['fields']:
                current[key] = copy.deepcopy(row['prior']['fields'][key])
            else:
                current.pop(key, None)
        current['script_sha256'] = row['prior']['script_sha256']
        return current

    def current_job(self, jid):
        return self.backend.job(jid)

    def accept_job_update(self, jid, before, after):
        require({k: v for k, v in before.items() if k != 'script_sha256'} ==
                {k: v for k, v in after.items() if k != 'script_sha256'}, 'window_job_update_must_be_hash_only')
        require(self.current_job(jid) == before, 'window_job_update_preimage_moved')
        data = read_journal(self.journal)
        require(any(row['entry']['kind'] == 'hermes' and row['entry']['id'] == jid for row in data['entries']),
                'adoption_job_not_in_window')
        data.setdefault('job_updates', {})[jid] = {'before_sha256': json_sha(before), 'after_sha256': json_sha(after),
                                                'before_pin': before.get('script_sha256'),
                                                'after_pin': after.get('script_sha256')}
        if data.get('source_transaction'):
            data['source_transaction'].setdefault('job_ids', []).append(jid)
        atomic_json(self.journal, data)

    def accept_file_updates(self, before, after, *, absent=()):
        from maintenance_inventory import affected_paths
        absent = set(absent)
        if absent:
            from glp331_carrier_adoption import ABSENT_PATHS
            permitted = {str((self.backend.root / rel).resolve()) for rel in ABSENT_PATHS}
            require(absent <= permitted and absent == {p for p, v in before.items() if v is None},
                    'window_unreviewed_absence')
        allowed = {str(p) for p in affected_paths(self.backend.root, self.backend.hermes)}
        require(set(before) == set(after) and set(before) <= allowed, 'window_file_update_outside_adoption_scope')
        data = read_journal(self.journal)
        for path, old in before.items():
            actual = source_digest(path) if path in absent else hashlib.sha256(Path(path).read_bytes()).hexdigest()
            require(actual == old, 'window_file_update_preimage_moved')
            data.setdefault('source_updates', {})[path] = {'before': old, 'after': after[path]}
        data['source_transaction'] = {'before': before, 'after': after, 'outcome': 'pending',
                                      'adopter': identity(), 'job_ids': []}
        atomic_json(self.journal, data)

    def switch_files(self, stage, before, *, commit=None):
        from maintenance_cohort import switch_files
        data = read_journal(self.journal)
        transaction = data.get('source_transaction')
        require(transaction and transaction['adopter'] == identity(), 'cohort_transaction_required')
        old = {str(Path(p).resolve()): hashlib.sha256(value[0]).hexdigest() for p, value in before.items()}
        new = {str(Path(p).resolve()): hashlib.sha256(body).hexdigest() for p, body in stage.items()}
        require((old == transaction['before'] and new == transaction['after']) or
                (old == transaction['after'] and new == transaction['before']), 'cohort_unreviewed_bytes')
        def drain():
            current = read_journal(self.journal)
            require(locks_verified(current), 'keeper_lost_during_cohort_switch')
            require(all(self.backend.paused(row['entry'], row['prior']) for row in current['entries']),
                    'cohort_pause_ownership_changed')
            return self.backend.inflight([row['entry'] for row in current['entries']])
        receipt = switch_files(self.journal, stage, before, drain, commit=commit)
        data = read_journal(self.journal)
        data['source_transaction']['switch_verified'] = True
        atomic_json(self.journal, data)
        return receipt

    def switch_glp331_files(self, stage, before, modes, *, commit):
        """Fixed public absence CAS via sealed aliases in the existing cohort.

        Existing switch_files/callback semantics are untouched. The transaction
        names only eight public outputs; alias inode custody stays in source_switch.
        """
        import tempfile
        import maintenance_cohort as cohort
        from glp331_carrier_adoption import PATHS, WRAPPER, ABSENT_PATHS
        expected = {self.backend.root / rel for rel in PATHS} | {self.backend.hermes / 'scripts' / WRAPPER}
        require(set(stage) == set(before) == expected, 'glp331_cohort_scope_mismatch')
        transaction = read_journal(self.journal).get('source_transaction')
        old = {str(p): None if v is None else hashlib.sha256(v[0]).hexdigest() for p, v in before.items()}
        new = {str(p): hashlib.sha256(body).hexdigest() for p, body in stage.items()}
        require(transaction and transaction['adopter'] == identity()
                and transaction['before'] == old and transaction['after'] == new, 'cohort_unreviewed_bytes')
        missing = {p for p, value in before.items() if value is None}
        require(missing <= {self.backend.root / rel for rel in ABSENT_PATHS}, 'window_unreviewed_absence')
        if not missing:
            return self.switch_files(stage, before, commit=commit)
        aliases, cohort_before, cohort_stage = {}, {}, {}
        for path in stage:
            if path not in missing:
                cohort_before[path], cohort_stage[path] = before[path], stage[path]
        def drain():
            current = read_journal(self.journal)
            require(locks_verified(current), 'keeper_lost_during_cohort_switch')
            require(all(self.backend.paused(row['entry'], row['prior']) for row in current['entries']),
                    'cohort_pause_ownership_changed')
            return self.backend.inflight([row['entry'] for row in current['entries']])
        try:
            for path in sorted(missing):
                require(source_digest(path) is None, 'absent_source_moved')
                fd, name = tempfile.mkstemp(prefix='.glp331-new-', dir=path.parent)
                alias = Path(name); aliases[path] = alias
                with os.fdopen(fd, 'wb') as out:
                    out.write(stage[path]); out.flush(); os.fsync(out.fileno())
                    os.fchmod(out.fileno(), modes[str(path)])
                cohort_before[alias] = (stage[path], modes[str(path)])
                cohort_stage[alias] = stage[path]
                cohort.sync_dir(path.parent)
            data = read_journal(self.journal)
            data['new_source_aliases'] = {str(k): str(v) for k, v in aliases.items()}
            atomic_json(self.journal, data)
            def linked_commit():
                for path, alias in aliases.items():
                    require(alias.stat().st_mode & 0o777 == 0, 'new_source_not_sealed')
                    os.link(alias, path, follow_symlinks=False)  # absent-path CAS
                    cohort.sync_dir(path.parent)
                    require(cohort.metadata(path.stat()) == cohort.metadata(alias.stat())
                            and path.stat().st_mode & 0o777 == 0, 'new_source_link_readback')
                commit()
            receipt = cohort.switch_files(self.journal, cohort_stage, cohort_before, drain, commit=linked_commit)
            for alias in aliases.values():
                os.unlink(alias); cohort.sync_dir(alias.parent)
            for path, body in stage.items():
                require(source_digest(path) == hashlib.sha256(body).hexdigest()
                        and path.stat().st_mode & 0o777 == modes[str(path)]
                        and path.stat().st_nlink == 1, 'glp331_postcommit_readback')
            data = read_journal(self.journal)
            data['source_transaction']['switch_verified'] = True
            atomic_json(self.journal, data)
            return receipt
        except BaseException:
            data = read_journal(self.journal)
            switch = data.get('source_switch', {})
            if switch.get('commit_started'):
                for path in set(stage) | set(aliases.values()):
                    if path.exists():
                        os.chmod(path, 0)
                switch['state'] = 'commit_uncertain'
                data['source_switch'] = switch
                atomic_json(self.journal, data)
            elif switch.get('state') not in {'recovery_required', 'recovering'}:
                for alias in aliases.values():
                    if alias.exists():
                        os.unlink(alias); cohort.sync_dir(alias.parent)
            raise

    def finish(self, success):
        data = read_journal(self.journal)
        transaction = data.get('source_transaction')
        if not transaction or transaction['adopter'] != identity() or transaction['outcome'] != 'pending':
            return
        side = 'after' if success else 'before'
        switch = data.get('source_switch', {})
        if switch.get('commit_started') and switch.get('state') == 'commit_uncertain':
            # The maintained switch left sealed inodes: never reopen/hash them
            # or certify rollback after an external commit may have landed.
            transaction['outcome'] = 'uncertain'
            atomic_json(self.journal, data)
            require(False, 'adoption_outcome_uncertain_reconcile_before_exit')
        matched = all(source_digest(path) == digest
                      for path, digest in transaction[side].items())
        matched = matched and all(json_sha(self.backend.job(jid)) == data['job_updates'][jid][side + '_sha256']
                                  for jid in transaction['job_ids'])
        transaction['outcome'] = ('applied' if success else 'rolled_back') if matched else 'uncertain'
        if matched:
            # Advance exact verified baselines. A later adoption must not make
            # the earlier adoption's old bytes admissible again.
            data.setdefault('source_sha256', {}).update(transaction[side])
            for path in transaction[side]:
                data.get('source_updates', {}).pop(path, None)
            for jid in transaction['job_ids']:
                data.setdefault('job_outcomes', {})[jid] = data['job_updates'][jid][side + '_pin']
        atomic_json(self.journal, data)
        require(matched, 'adoption_outcome_uncertain_reconcile_before_exit')


@contextmanager
def require_active_window(journal, root, hermes):
    require(journal is not None, 'active_maintenance_window_required')
    journal = Path(journal)
    with operation_lock(journal, timeout=OPERATION_ADMISSION_SECONDS):
        data = read_journal(journal)
        require(data['root'] == str(Path(root).resolve()) and data['hermes'] == str(Path(hermes).resolve()),
                'window_target_mismatch')
        backend = LinuxBackend(root, hermes, journal)
        window = MaintenanceWindow(backend, journal, default_locks(root, hermes))
        # Exempt exactly this adopter during its operation; the keeper's
        # operation lock prevents expiry/exit from restoring jobs underneath it.
        data['adopter'] = identity()
        atomic_json(journal, data)
        try:
            require(window.status()['verified'], 'active_maintenance_window_required')
            active = ActiveWindow(journal, backend)
            try:
                yield active
                current = read_journal(journal)
                require(locks_verified(current), 'keeper_lost_during_adoption')
                require(not backend.inflight([row['entry'] for row in current['entries']]),
                        'source_reader_admitted_during_adoption')
                require(all(backend.paused(row['entry'], row['prior']) for row in current['entries']),
                        'paused_custody_changed_during_adoption')
                transaction = current.get('source_transaction')
                require(not transaction or transaction['outcome'] != 'pending' or
                        transaction.get('switch_verified'), 'source_cohort_switch_required')
                active.finish(True)
            except BaseException:
                active.finish(False)
                raise
            data = read_journal(journal)
            for jid, update in data.get('job_updates', {}).items():
                require(json_sha(backend.job(jid)) in (update['before_sha256'], update['after_sha256']),
                        'adoption_job_outcome_uncertain')
        finally:
            data = read_journal(journal)
            data.pop('adopter', None)
            atomic_json(journal, data)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('plan', 'enter', 'status', 'exit', 'readers', 'recover-cohort', 'recover-pre-adoption'))
    parser.add_argument('--root', type=Path, default=Path('/home/ubuntu/business'))
    parser.add_argument('--hermes', type=Path, default=Path('/home/ubuntu/.hermes'))
    parser.add_argument('--journal', type=Path, required=True)
    parser.add_argument('--drain-seconds', type=float, default=300)
    parser.add_argument('--window-seconds', type=float, default=3600)
    parser.add_argument('--keeper', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--controller-pid', type=int, help=argparse.SUPPRESS)
    parser.add_argument('--outcome-review', type=Path, help='exit only: independently reviewed exact landed/rolled-back outcome')
    parser.add_argument('--cohort-review', type=Path, help='recover-cohort only: independent exact reconstruction review')
    parser.add_argument('--recovery-review', type=Path, help='recover-pre-adoption only: exact external approvals')
    args = parser.parse_args(argv)
    backend = LinuxBackend(args.root, args.hermes, args.journal)
    window = MaintenanceWindow(backend, args.journal, default_locks(args.root, args.hermes),
                               drain_timeout=args.drain_seconds, max_window_seconds=args.window_seconds)
    try:
        require(args.outcome_review is None or args.command == 'exit', 'outcome_review_is_exit_only')
        require(args.cohort_review is None or args.command == 'recover-cohort', 'cohort_review_is_recovery_only')
        require(args.recovery_review is None or args.command == 'recover-pre-adoption', 'recovery_review_is_exit_only')
        if args.command == 'recover-pre-adoption':
            review = json.loads(args.recovery_review.read_text()) if args.recovery_review else None
            result = window.recover_pre_adoption(review)
        elif args.command == 'readers':
            from maintenance_admission import process_readers
            from maintenance_inventory import affected_paths
            visible = Path('/proc/1/comm').read_text().strip() != 'codex'
            result = {'blockers': process_readers(affected_paths(args.root, args.hermes), excluded={os.getpid()}),
                      'host_process_visibility': 'available' if visible else 'unavailable_private_pid_namespace',
                      'host_blockers': 'see blockers' if visible else 'UNKNOWN'}
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0 if visible else 2
        elif args.command == 'recover-cohort':
            from maintenance_cohort import recovery_proposal, recover
            with operation_lock(args.journal):
                if args.cohort_review is None:
                    result = recovery_proposal(args.journal)
                else:
                    data = read_journal(args.journal)
                    if not locks_verified(data):
                        window._take_locks(data)
                    def drain():
                        current = read_journal(args.journal)
                        require(locks_verified(current), 'cohort_recovery_locks_unavailable')
                        require(all(backend.paused(row['entry'], row['prior']) for row in current['entries']),
                                'cohort_recovery_pause_changed')
                        return backend.inflight([row['entry'] for row in current['entries']])
                    try:
                        result = recover(args.journal, json.loads(args.cohort_review.read_text()), drain)
                    finally:
                        window.release_locks()
        elif args.command == 'plan':
            result = window.plan()
        elif args.command == 'status':
            result = window.status()
        elif args.command == 'enter' and args.keeper:
            if args.controller_pid:
                window.controller = identity(args.controller_pid)
            stopping = []
            signal.signal(signal.SIGTERM, lambda *_: stopping.append(True))
            signal.signal(signal.SIGINT, lambda *_: stopping.append(True))
            window.enter()
            while True:
                try:
                    with operation_lock(args.journal):
                        data = read_journal(args.journal)
                        if data['state'] == 'closed':
                            window.release_locks()
                            return 0
                        if stopping or data.get('exit_requested') or time.time() >= data['expires_at']:
                            if data.get('outcome_review'):
                                window.reconcile_outcome(data, data.pop('outcome_review'))
                            window._exit(data)
                            return 0
                        require(window._verify(data), 'keeper_verification_failed')
                except Exception as exc:
                    if not isinstance(exc, WindowError) or str(exc) != 'window_operation_busy':
                        window.exit()
                        raise
                time.sleep(0.5)
        elif args.command == 'enter':
            existing = window.status()
            if existing['verified']:
                result = existing
            else:
                # A fresh child owns every flock; the launching shell can exit.
                child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), 'enter', '--keeper',
                    '--root', str(args.root), '--hermes', str(args.hermes), '--journal', str(args.journal),
                    '--drain-seconds', str(args.drain_seconds), '--window-seconds', str(args.window_seconds),
                    '--controller-pid', str(os.getpid())],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True)
                deadline = time.monotonic() + args.drain_seconds + 60
                while True:
                    result = window.status()
                    if result['verified']:
                        break
                    require(child.poll() is None, 'keeper_enter_failed_inspect_journal_and_plan')
                    require(time.monotonic() < deadline, 'keeper_start_timeout_inspect_journal')
                    time.sleep(0.25)
        else:
            review = json.loads(args.outcome_review.read_text()) if args.outcome_review else None
            keeper_exit_requested = False
            # Route from a fresh journal under admission, not a stale dead-owner
            # sample taken before a concurrent keeper resume. Never retry effects.
            with operation_lock(args.journal, timeout=OPERATION_ADMISSION_SECONDS):
                data = read_journal(args.journal) if args.journal.exists() else None
                if data and locks_verified(data) and data['keeper'] != identity():
                    if review is not None:
                        # Review refers to the pre-request journal. Verify it
                        # before adding the durable keeper handoff fields.
                        require(review.get('proposal') == window.outcome_proposal(data), 'outcome_review_preimage_changed')
                        data['outcome_review'] = review
                    data['exit_requested'] = True
                    atomic_json(args.journal, data)
                    keeper_exit_requested = True
                elif data is not None:
                    if review is not None:
                        window.reconcile_outcome(data, review)
                    result = window._exit(data)
                else:
                    result = window.status()
            if keeper_exit_requested:
                deadline = time.monotonic() + 120
                while True:
                    result = window.status()
                    if result['state'] == 'closed':
                        break
                    if not result['keeper_alive']:
                        # Status is not an atomic owner/phase snapshot: the
                        # keeper may close and release its fences while that
                        # read is in flight, but still own the operation lock.
                        # Re-admit and read fresh before recovery, just as for
                        # the initial exit route. Never replay a completed exit.
                        with operation_lock(args.journal, timeout=OPERATION_ADMISSION_SECONDS):
                            data = read_journal(args.journal)
                            if data['state'] == 'closed':
                                result = window.status()
                                break
                            if not locks_verified(data):
                                if review is not None:
                                    window.reconcile_outcome(data, review)
                                result = window._exit(data)
                                break
                    require(time.monotonic() < deadline, 'keeper_exit_timeout_reconcile_journal')
                    time.sleep(0.25)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if args.command != 'status' or result['state'] in {'absent', 'closed'} or result['verified'] else 2
    except Exception as exc:
        failure = {'status': 'REFUSED', 'reason': str(exc) if isinstance(exc, WindowError) else type(exc).__name__,
                   'journal': str(args.journal),
                   'recovery': 'inspect status; follow the maintained route named by the refusal; never edit the journal'}
        if getattr(exc, 'errors', None):
            failure['inventory_errors'] = exc.errors
            failure['coverage'] = (getattr(exc, 'report', None) or {}).get('coverage', {})
        print(json.dumps(failure, indent=2, sort_keys=True))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
