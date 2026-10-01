#!/usr/bin/env python3
"""Journaled GLP-44/73 maintenance: plan, enter, status, exit.

Enter starts a bounded keeper which owns the existing flocks across CLI calls.
Every pause has a durable intent and preimage. A dead keeper is never ACTIVE;
repeat enter resumes the journal, or exit restores it. No jobs run from this tool.
"""
from __future__ import annotations

import argparse
import copy
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
def operation_lock(journal):
    path = Path(str(journal) + '.operation.lock')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        require(stat.S_ISREG(os.fstat(fd).st_mode), 'operation_lock_not_regular')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    except BlockingIOError as exc:
        raise WindowError('window_operation_busy') from exc
    finally:
        os.close(fd)


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
        if state is not None:
            state.depth = 1  # Native tool calls reuse this held descriptor.
            state.load_stamp = None
            entered = True
        yield  # Outside the contention handler: ambiguous failures propagate.
    finally:
        if entered:
            state.depth = 0
            state.load_stamp = None
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

    def _take_locks(self, data):
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
        self._save(data)

    def _verify(self, data):
        if data['state'] != 'active' or time.time() >= data['expires_at'] or not locks_verified(data):
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
                if not any(all(hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest
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
        sources = {path: hashlib.sha256(Path(path).read_bytes()).hexdigest()
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

    def enter(self):
        with operation_lock(self.journal):
            return self._enter()

    def _enter(self):
        if self.journal.exists():
            data = read_journal(self.journal)
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
        # Only the keeper or a controller after its death can release locks.
        if locks_verified(data):
            require(data['keeper'] == identity(), 'exit_must_be_requested_from_live_keeper')
        data['state'] = 'restoring'
        self._save(data)
        if not self._sources_valid(data, for_exit=True):
            data['state'], data['error'] = 'recovery_required', 'source_or_adoption_outcome_requires_reconciliation'
            self._save(data)
            raise WindowError('restore_refused_source_or_adoption_outcome_requires_reconciliation')
        failures = []
        # Source locks stay held during restore; scheduler fire locks in the
        # backend prevent launching until an exact schedule repair is complete.
        for row in reversed(data['entries']):
            if not row['intent'] or row['restored']:
                continue
            row['restore_intent'] = True
            self._save(data)
            try:
                self.backend.restore(row['entry'], row['prior'])
                row['restored'] = True
                self._save(data)
            except Exception as exc:
                failures.append({'kind': row['entry']['kind'], 'id': row['entry']['id'],
                                 'error': type(exc).__name__})
        if failures:
            data['state'], data['restore_errors'] = 'recovery_required', failures
            self._save(data)
            raise WindowError('restore_failed_use_exit_to_reconcile_journal')
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
                    'script_sha256': job.get('script_sha256'), 'configuration_sha256': job_configuration(job)}
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

    def pause(self, entry, prior):
        kind = entry['kind']
        if kind == 'hermes':
            scheduler = self.scheduler()
            with native_registry_lock(scheduler):
                job = self.job(entry['id'])
                require(job.get('script_sha256') == prior['script_sha256'], 'pause_pin_changed')
                require(job_configuration(job) == prior['configuration_sha256'], 'pause_configuration_changed')
                require({k: job[k] for k in CONTROL_FIELDS if k in job} == prior['fields'], 'pause_preimage_changed')
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

    def restore(self, entry, prior):
        kind = entry['kind']
        if kind == 'hermes':
            scheduler = self.scheduler()
            with scheduler._fire_job_lock(entry['id']) as fenced, native_registry_lock(scheduler):
                require(fenced, 'restore_fire_fence_unavailable')
                current = self.job(entry['id'])
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
                require(self.paused(entry, prior), 'restore_pause_ownership_changed')
                # Restoration is an exact CAS, not a new scheduling request.
                # resume() rejects expired once jobs before saving and can
                # normalize legacy fields; use the native atomic saver under
                # the native registry + fire fences without rescheduling.
                jobs = scheduler.load_jobs()
                found = [j for j in jobs if j.get('id') == entry['id']]
                require(len(found) == 1 and found[0] == current, 'restore_record_changed')
                target = found[0]
                for key in CONTROL_FIELDS:
                    if key in prior['fields']:
                        target[key] = copy.deepcopy(prior['fields'][key])
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

    def accept_file_updates(self, before, after):
        from maintenance_inventory import affected_paths
        allowed = {str(p) for p in affected_paths(self.backend.root, self.backend.hermes)}
        require(set(before) == set(after) and set(before) <= allowed, 'window_file_update_outside_adoption_scope')
        data = read_journal(self.journal)
        for path, old in before.items():
            actual = hashlib.sha256(Path(path).read_bytes()).hexdigest()
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
        matched = all(hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest
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
    with operation_lock(journal):
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
    parser.add_argument('command', choices=('plan', 'enter', 'status', 'exit', 'readers', 'recover-cohort'))
    parser.add_argument('--root', type=Path, default=Path('/home/ubuntu/business'))
    parser.add_argument('--hermes', type=Path, default=Path('/home/ubuntu/.hermes'))
    parser.add_argument('--journal', type=Path, required=True)
    parser.add_argument('--drain-seconds', type=float, default=300)
    parser.add_argument('--window-seconds', type=float, default=3600)
    parser.add_argument('--keeper', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--controller-pid', type=int, help=argparse.SUPPRESS)
    parser.add_argument('--outcome-review', type=Path, help='exit only: independently reviewed exact landed/rolled-back outcome')
    parser.add_argument('--cohort-review', type=Path, help='recover-cohort only: independent exact reconstruction review')
    args = parser.parse_args(argv)
    backend = LinuxBackend(args.root, args.hermes, args.journal)
    window = MaintenanceWindow(backend, args.journal, default_locks(args.root, args.hermes),
                               drain_timeout=args.drain_seconds, max_window_seconds=args.window_seconds)
    try:
        require(args.outcome_review is None or args.command == 'exit', 'outcome_review_is_exit_only')
        require(args.cohort_review is None or args.command == 'recover-cohort', 'cohort_review_is_recovery_only')
        if args.command == 'readers':
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
            data = read_journal(args.journal) if args.journal.exists() else None
            if data and locks_verified(data) and data['keeper'] != identity():
                with operation_lock(args.journal):
                    data = read_journal(args.journal)
                    if review is not None:
                        # Review refers to the pre-request journal. Verify it
                        # before adding the durable keeper handoff fields.
                        require(review.get('proposal') == window.outcome_proposal(data), 'outcome_review_preimage_changed')
                        data['outcome_review'] = review
                    data['exit_requested'] = True
                    atomic_json(args.journal, data)
                deadline = time.monotonic() + 120
                while True:
                    result = window.status()
                    if result['state'] == 'closed':
                        break
                    if not result['keeper_alive']:
                        result = window.exit(review)
                        break
                    require(time.monotonic() < deadline, 'keeper_exit_timeout_reconcile_journal')
                    time.sleep(0.25)
            else:
                result = window.exit(review)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if args.command != 'status' or result['state'] in {'absent', 'closed'} or result['verified'] else 2
    except Exception as exc:
        failure = {'status': 'REFUSED', 'reason': str(exc) if isinstance(exc, WindowError) else type(exc).__name__,
                   'journal': str(args.journal), 'recovery': 'inspect status; use exit to restore journaled preimages'}
        if getattr(exc, 'errors', None):
            failure['inventory_errors'] = exc.errors
            failure['coverage'] = (getattr(exc, 'report', None) or {}).get('coverage', {})
        print(json.dumps(failure, indent=2, sort_keys=True))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
