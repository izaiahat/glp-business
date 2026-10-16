#!/usr/bin/env python3
"""Deterministic hourly inbox/PLAN loop. No scheduler installation or activation.

The existing fleet inbox is read-only. Receipts under auto-coordinator are effect
fences, not another task queue: only PLAN and INGEST can introduce work. Missing
owner/worktree/routing bindings fail closed, never inferred from a model name.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path('/home/ubuntu/business')
PLAN = 'reports/ops/fable-plan-2-20261009/PLAN.json'
# Exact registered fleet_ingest --live default; no other profile or runtime tree.
LIVE = Path('/home/ubuntu/.hermes/cache/delegation/live')
ISSUE = re.compile(r'GLP-[1-9][0-9]*')
DONE = {'Done', 'Canceled', 'Cancelled', 'Duplicate'}
SPLIT = {'reviewer': 45, 'batch': 30, 'opus': 100, 'haiku': 5, 'reserve': 10}
ROUTES = {
    'sol': dict(provider='openai-codex', model='gpt-6.1-sol', effort='medium', speed='fast'),
    'astra': dict(provider='openai-codex', model='gpt-6-astra-900k', effort='high', speed='standard'),
}


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def load(path):
    return json.loads(Path(path).read_bytes())


def save(path, data):
    # Caller holds the loop singleton; fsync intent BEFORE any effect.
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    with temp.open('w') as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(data, stream, sort_keys=True, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)
    fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def route(executor):
    if executor not in ROUTES:
        raise ValueError('executor needs an explicit supported PLAN routing binding')
    return dict(ROUTES[executor])


def api_allowed(credit, category, cost):
    """Admission oracle only, never a paid fallback or spending authorization.

    Dispatcher must supply a fresh authenticated tracker observation and reserve
    its cost under its existing single-writer budget lock before issuing a call.
    The $10 reserve is not an executable category.
    """
    try:
        remaining = credit['remaining_usd']
        spent = credit['spent'].get(category, 0)
        return bool(category in SPLIT and category != 'reserve'
                    and all(type(x) in (int, float) and math.isfinite(x) for x in (remaining, spent, cost))
                    and cost >= 0 and spent >= 0 and remaining >= 5 and remaining - cost >= 5
                    and spent + cost <= SPLIT[category])
    except (KeyError, TypeError, AttributeError):
        return False


def select(plan, inbox, board, *, slots):
    if type(slots) is not int or not 0 <= slots <= 7:
        raise ValueError('eight host slots includes the coordinator')
    if (not isinstance(plan, list) or any(not isinstance(p, dict)
            or not ISSUE.fullmatch(str(p.get('issue', ''))) or type(p.get('order')) is not int for p in plan)):
        raise ValueError('invalid PLAN order/issue')
    if len({p['issue'] for p in plan}) != len(plan) or len({p['order'] for p in plan}) != len(plan):
        raise ValueError('duplicate PLAN identity/order')
    actions, skipped = [], []
    children, writers, write_areas, writing_checkouts = 0, 0, set(), set()
    for p in sorted(plan, key=lambda x: x['order']):
        issue = p['issue']
        b = board.get(issue, {})
        reason = None
        if b.get('state') in DONE:
            reason = 'DONE'
        elif b.get('held') is not False or p.get('held') or p.get('parked'):
            reason = 'HOLD_OR_UNKNOWN'
        elif b.get('live_owner') is not False:
            reason = 'LIVE_OWNER_OR_UNKNOWN'
        elif not isinstance(p.get('depends_on', []), list) or any(
                board.get(dep, {}).get('state') != 'Done' for dep in p.get('depends_on', [])):
            reason = 'DEPENDENCY_OR_UNKNOWN'
        elif p.get('executor') in {'pro', 'rook', 'finch'}:
            reason = 'SUPPORT_TICKET_REMAINS_OPEN'
        elif not p.get('owner') or not isinstance(p['owner'], str):
            reason = 'MISSING_EXPLICIT_OWNER'
        # Pro/Rook/Finch advisory rows remain open and byte-stable. Never pretend
        # an advisory's completion means the consuming issue is ready to close.
        rows = [r for r in inbox if r.get('issue') == issue and r.get('source') not in {'pro', 'dot'}]
        if not reason and len(rows) > 1:
            reason = 'MULTIPLE_ROWS_REQUIRE_OWNER_RECONCILIATION'
        elif not reason and not rows:
            reason = 'NO_ELIGIBLE_INBOX_ROW'
        r = rows[0] if len(rows) == 1 else None
        if not reason and r:
            if r.get('status') not in {'READY_TO_CLOSE', 'REDISPATCH'}:
                reason = 'REVIEW_OPERATOR_OR_UNKNOWN'
            elif r.get('owner') != p['owner']:
                reason = 'OWNER_MISMATCH_OR_UNKNOWN'
            elif not re.fullmatch(r'[a-f0-9]{64}', str(r.get('fingerprint', ''))):
                reason = 'INVALID_FINGERPRINT'
        if reason:
            skipped.append({'issue': issue, 'reason': reason})
            continue
        assert r is not None  # exactly one eligible row is required above
        kind = 'close' if r['status'] == 'READY_TO_CLOSE' else 'redispatch'
        a = {'issue': issue, 'owner': p['owner'], 'kind': kind, 'plan': p,
             'fingerprint': r['fingerprint']}
        a['inbox'] = r
        a['failure_text'] = r.get('failure_text', '')
        if kind != 'close':
            try:
                a['route'] = route(p.get('executor'))
            except ValueError as exc:
                skipped.append({'issue': issue, 'reason': str(exc)})
                continue
            if not p.get('worktree') or not p.get('next_action'):
                skipped.append({'issue': issue, 'reason': 'MISSING_OWNED_WORKTREE_OR_BRIEF'})
                continue
            # No heavy build/browser is admitted implicitly under disk/memory
            # pressure. Unknown resource classification is not read-only.
            area = p.get('write_area')
            writing = p.get('read_only') is not True
            if p.get('heavy') is not False:
                skipped.append({'issue': issue, 'reason': 'HEAVY_REQUIRES_SEPARATE_ADMISSION'})
                continue
            if writing and (not isinstance(area, str) or not area or writers >= 3
                            or p['worktree'] in writing_checkouts
                            or any(area == a or area.startswith(a + '/') or a.startswith(area + '/') for a in write_areas)):
                skipped.append({'issue': issue, 'reason': 'WRITER_CAPACITY_OR_MISSING_DISJOINT_AREA'})
                continue
            if children >= slots:
                skipped.append({'issue': issue, 'reason': 'HOST_CAPACITY'})
                continue
            if writing:
                writers += 1
                write_areas.add(area)
                writing_checkouts.add(p['worktree'])
            children += 1
        actions.append(a)
    return actions, skipped


def summary(result):
    return ('⚠️ **GLP Auto-Coordinator** — ' + result['status'] + '\n\n'
            + 'closure-ready: %s · closed: %s · dispatched: %s · uncertain: %s\n' % (
                result.get('closure_ready', 0), result.get('closed', 0),
                result.get('dispatched', 0), result.get('uncertain', 0))
            + 'Skipped: %s; input rows: %s\n' % (len(result.get('skipped', [])), result.get('rows', 0))
            + 'No TUI input, scheduler changes, support consumption or article-carrier activation.')


def render_log(path, rc):
    """Bound native cron delivery on success AND error; never emit a raw traceback."""
    lines = Path(path).read_text(errors='replace').splitlines()
    starts = [i for i, text in enumerate(lines) if text.startswith('⚠️ **GLP Auto-Coordinator** — ')]
    if starts:
        card = lines[starts[-1]:starts[-1] + 5]
    else:
        card = ['⚠️ **GLP Auto-Coordinator** — inspect exact run', '',
                'Effect counts: UNKNOWN; do not replay an ambiguous effect.']
    if rc:
        card.append('→ rc %s; reconcile the exact target before retrying.' % rc)
    card.append('-# artifacts: ' + str(path))
    return '\n'.join(card)


def run_pass(directory, plan, inbox, backend, *, slots=7):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    result = {'status': 'COMPLETE', 'started_at': now(), 'closed': 0, 'dispatched': 0,
              'closure_ready': 0, 'uncertain': 0, 'rows': len(inbox), 'skipped': [], 'actions': []}
    with (directory / '.loop.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return dict(result, status='BUSY')
        state = backend.guard()
        if state['status'] != 'IDLE':
            return dict(result, status=state['status'])
        ids = sorted({p['issue'] for p in plan} | {d for p in plan for d in p.get('depends_on', []) if ISSUE.fullmatch(str(d))})
        actions, result['skipped'] = select(plan, inbox, backend.observe(ids), slots=slots)
        for a in actions:
            key = digest(json.dumps([a['issue'], a['owner'], a['kind'], a['fingerprint']]).encode())
            # Issue-level latch also stops a different fingerprint from replaying
            # an unresolved prior effect. Only an exact terminal output can release
            # a successful dispatch; uncertain intents require human reconciliation.
            fence = directory / 'effects' / (a['issue'] + '.json')
            prior = load(fence) if fence.exists() else None
            if prior:
                bound_result = (a.get('inbox') or {}).get('worker_result')
                terminal = prior.get('effect', {}).get('worker_result')
                reconciled = (prior.get('status') == 'verified' and a['kind'] in {'redispatch', 'close'}
                              and bound_result and bound_result == terminal and prior['key'] != key)
                if not reconciled:
                    result['skipped'].append({'issue': a['issue'], 'reason': 'EXISTING_EFFECT_NO_REPLAY', 'prior': prior['key']})
                    continue
            if a['kind'] == 'close' and not backend.preflight(a):
                result['skipped'].append({'issue': a['issue'], 'reason': 'PREFLIGHT_REFUSED'})
                continue
            # Fresh board/guard after proof, never trust the beginning-of-pass snapshot.
            fresh = backend.observe(ids)
            refreshed, _ = select([a['plan']], [a['inbox']] if 'inbox' in a else [], fresh, slots=slots)
            if not refreshed or backend.guard()['status'] != 'IDLE':
                result['skipped'].append({'issue': a['issue'], 'reason': 'PRE_EFFECT_STATE_CHANGED'})
                continue
            if a['kind'] == 'close':
                # Readiness is not coordinator acceptance or permission to mutate
                # Linear. No effect intent/latch is created for a check-only pass.
                result['closure_ready'] += 1
                result['actions'].append({'status': 'ready_for_coordinator', 'action': a})
                continue
            record = {'key': key, 'status': 'intent', 'action': a, 'created_at': now()}
            save(fence, record)
            try:
                record['effect'] = backend.effect(a)
                record['status'] = 'verified'
                result['dispatched'] += 1
            except Exception as exc:
                record.update(status='uncertain', error_class=type(exc).__name__)
                result['uncertain'] += 1
            save(fence, record)
            result['actions'].append(record)
        result['finished_at'] = now()
        return result


class InstalledBackend:
    """Only fixed argv effects; no shell, prompt-coordinator or TUI transport."""
    def __init__(self, root, run_dir, token):
        self.root, self.run_dir, self.token = Path(root), Path(run_dir), token
        self.children = []
        self.read_index = 0

    def guard(self):
        import auto_coordinator_guard as guard
        # Children from THIS pass are permitted; all other live work defers the
        # initial pass. Subsequent guards still check active TUI turns explicitly.
        return guard.inspect_idle_tui(owned_token=self.token, owned_tokens=[t for _, t in self.children if t])

    def observe(self, ids):
        import linear_orchestrator as lo
        values = {}
        for issue in ids:
            row = lo.issue(issue)
            state = (row.get('state') or {}).get('name')
            # Only explicitly admitted In Progress issues can be refilled. This
            # loop never changes roots, admission, priority, ownership or order.
            labels = {r['name'] for r in (row.get('labels') or {}).get('nodes', [])}
            values[issue] = {'state': state, 'held': state != 'In Progress' and state not in DONE
                             or bool(labels & {'held', 'parked', 'operator-hold'}), 'live_owner': False,
                             'raw': row}
        self.read_index += 1
        save(self.run_dir / ('BOARD-%04d.json' % self.read_index), values)
        return values

    def call(self, argv):
        if '--apply' in argv:
            raise ValueError('loop permits check-only preflight, never apply')
        cp = subprocess.run(argv, cwd=self.root, env=dict(os.environ, LINEAR_ROLE='worker'),
                            capture_output=True, text=True, timeout=120)
        self.read_index += 1
        save(self.run_dir / ('CALL-%04d.json' % self.read_index),
             {'argv': argv, 'rc': cp.returncode, 'stdout': cp.stdout, 'stderr': cp.stderr})
        return cp

    def current(self, action):
        plan, inbox = inputs(self.root)  # rehash current artifacts, not row prose
        if [p for p in plan if p['issue'] == action['issue']] != [action['plan']]:
            raise ValueError('PLAN changed before effect')
        rows = [r for r in inbox if r['issue'] == action['issue'] and r.get('source') not in {'pro', 'dot'}]
        if rows != ([action['inbox']] if 'inbox' in action else []):
            raise ValueError('INGEST changed before effect')

    def preflight(self, action):
        self.current(action)
        from fleet_ingest import proof_argv, completion_preflight
        p, r = action['plan'], action['inbox']
        if p.get('proof_complete') is not True or not r.get('worker_result'):
            return False
        if self.call(proof_argv(p.get('proof_cmd'))).returncode:
            return False
        def runner(argv, cwd, timeout):
            cp = self.call(argv)
            return {'rc': cp.returncode, 'stdout': cp.stdout, 'stderr': cp.stderr}
        return completion_preflight(self.root, action['issue'], r['worker_result'], p,
                                    runner, 120)['rc'] == 0

    def effect(self, action):
        if action['kind'] == 'close':
            raise ValueError('closure is coordinator-owned; loop reports readiness only')
        self.current(action)
        request = self.run_dir / (action['issue'] + '-WORK.json')
        # The existing owned checkout MUST already exist: no competing worktree,
        # owner assignment, queue or installer is created by this coordinator.
        worktree = Path(action['plan']['worktree'])
        if not worktree.is_dir() or not (worktree / '.git').exists():
            raise ValueError('explicit owned worktree missing')
        save(request, action)
        # Keep the singleton descriptor alive through the entire native child.
        fd = os.environ.get('GLP_AUTO_COORDINATOR_LOCK_FD')
        if fd is None:
            raise ValueError('registered singleton descriptor missing')
        cp = subprocess.Popen([sys.executable, '-B', str(self.root / 'scripts/auto_coordinator_loop.py'),
                               '--worker', str(request), '--lock-fd', fd], cwd=self.root,
                              stdout=subprocess.PIPE, text=True, pass_fds=(int(fd),))
        self.children.append((cp, None))  # track even an ambiguous/missing ACK
        assert cp.stdout is not None
        # A missing ACK is ambiguous. Never terminate the worker to clear a slot.
        import select as io_select
        ready, _, _ = io_select.select([cp.stdout], [], [], 30)
        if not ready:
            raise TimeoutError('worker admission ACK unknown')
        ack = json.loads(cp.stdout.readline())
        if ack.get('status') != 'ADMITTED' or ack.get('issue') != action['issue']:
            raise ValueError('worker admission refused or unknown')
        self.children[-1] = (cp, ack['token'])
        return ack

    def finish(self):
        # Natural exit only; no timeout that could kill an in-flight effect.
        for child, _ in self.children:
            child.wait()


def inputs(root):
    from auto_coordinator_receipt import binding
    root = Path(root)
    plan = binding(root, PLAN)
    policies = plan['data']['issues']
    # CONFIG is the same coordinator-owned override used by the ingester.
    # Bind only contract selection here; PLAN's proof_complete gate is retained.
    config_path = root / 'reports/ops/fleet-inbox/CONFIG.json'
    if config_path.exists():
        config = binding(root, config_path)
        overrides = config['data'].get('issues', {})
        policies = [dict(p, _completion_config_sha256=config['sha256']) for p in policies]
        for p in policies:
            override = overrides.get(p['issue'], {})
            if 'completion_contract' in override:
                p['completion_contract'] = override['completion_contract']
    inbox = []
    planned = {p['issue'] for p in plan['data']['issues']}
    for p in sorted((root / 'reports/ops/fleet-inbox').glob('*/*/INGEST.json')):
        if p.parent.parent.name not in planned:
            continue  # historical non-PLAN rows cannot become work or block it
        ref = binding(root, p)
        row = ref['data']
        if row.get('issue') != p.parent.parent.name:
            raise ValueError('INGEST directory/issue mismatch')
        if row.get('schema') != 'fleet-ingest-v1' or p.parent.parent.name != row.get('issue'):
            raise ValueError('invalid fleet row binding')
        # Authenticate every artifact, including worker owner; stale rows cannot
        # drive dispatch/closure before the independent ingester revokes them.
        for name, sha in row.get('evidence', {}).items():
            path = Path(name)
            if not path.is_absolute():
                path = root / path
            native_manifest = (row.get('source') == 'native' and path.name == 'manifest.json'
                               and re.fullmatch(r'deleg_[A-Za-z0-9]+', path.parent.name)
                               and path.resolve().parent.parent == LIVE.resolve())
            if (path.is_symlink() or not (path.resolve().is_relative_to(root.resolve()) or native_manifest)
                    or digest(path.read_bytes()) != sha):
                raise ValueError('fleet evidence changed or escaped root')
        worker = row.get('worker_result')
        if worker:
            wref = binding(root, worker)
            if row.get('evidence', {}).get(str(root / wref['path'])) != wref['sha256']:
                raise ValueError('worker RESULT not hash-bound by INGEST')
            if wref['data'].get('issue', row['issue']) != row['issue']:
                raise ValueError('worker RESULT issue mismatch')
            row = dict(row, owner=wref['data'].get('owner'))
            from fleet_ingest import alive
            if wref['data'].get('pid') and alive(wref['data']['pid']):
                row['status'] = 'LIVE_OWNER'
        inbox.append(row)
    return policies, inbox


def worker_command(plan, prompt):
    r = route(plan.get('executor'))
    return ['/home/ubuntu/.hermes/hermes-agent/venv/bin/hermes', 'chat', '--cli', '--oneshot',
            '--query-file', str(prompt), '--provider', r['provider'], '-m', r['model'],
            '--reasoning', r['effort'], '--toolsets', 'terminal,file,skills,web']


def worker(request, lock_fd):
    # Fresh noninteractive native executor, never an existing operator session.
    sys.path.insert(0, '/home/ubuntu/.hermes/hermes-agent')
    from tools.delegation_admission import admission_problem, try_reserve_host_children
    action = load(request)
    os.fstat(lock_fd)  # refuse before admission if singleton inheritance is absent
    if admission_problem():
        print(json.dumps({'status': 'REFUSED'}), flush=True)
        return 10
    leases, _, _ = try_reserve_host_children(1)
    if leases is None:
        print(json.dumps({'status': 'REFUSED'}), flush=True)
        return 10
    out, ack, rc = Path(request).with_suffix(''), None, 127
    try:
        import yaml
        p = action['plan']
        # CLI has no supported per-call speed switch. Check inherited config,
        # never mutate a shared profile to obtain the requested routing tier.
        config = yaml.safe_load((Path.home() / '.hermes/config.yaml').read_text())
        tier = config.get('agent', {}).get('service_tier')
        expected = 'fast' if route(p['executor'])['speed'] == 'fast' else 'normal'
        if tier not in ({'fast', 'priority'} if expected == 'fast' else {'normal', '', None}):
            print(json.dumps({'status': 'REFUSED', 'reason': 'ROUTING_TIER_MISMATCH'}), flush=True)
            return 10
        out.mkdir()
        result_path, prompt = out / 'RESULT.json', out / 'BRIEF.md'
        text = ('Issue: %s\nSame end-to-end owner: %s\nOwned checkout: %s\n' % (action['issue'], action['owner'], p['worktree'])
                + 'Task (PLAN only):\n' + p['next_action'] + '\nAcceptance:\n' + str(p.get('acceptance_rewrite', ''))
                + '\nExact prior failure:\n' + action.get('failure_text', '')
                + '\nLeaf executor: no delegation, no TUI input, no Linear root/order/admission/closure changes. '
                  'Use ops/linear/SKILL-INDEX.md; load matching skills; preserve locks, cursors, holds and uncertain effects. '
                  'No paid fallback, scheduler enablement, self-merge or installation. Stop for distinct review. '
                  'Keep n8n article-carrier inactive. '
                + '\nWrite <=20-line RESULT.json with issue, owner, pid=%s and genuine output receipt at %s.\n' % (os.getpid(), result_path))
        prompt.write_text(text)
        ack = {'status': 'ADMITTED', 'issue': action['issue'], 'owner': action['owner'],
               'pid': os.getpid(), 'token': leases[0].token, 'worker_result': str(result_path), 'started_at': now()}
        save(out / 'RUN.json', ack)
        print(json.dumps(ack), flush=True)
        env = dict(os.environ, LINEAR_ROLE='worker')
        with (out / 'RESPONSE.md').open('w') as log:
            rc = subprocess.run(worker_command(p, prompt), cwd=p['worktree'], env=env,
                                stdout=log, stderr=log, pass_fds=(lock_fd,)).returncode
        return rc
    finally:
        try:
            if ack:
                save(out / 'RUN.json', dict(ack, finished_at=now(), exit_code=rc))
        finally:
            for lease in leases:
                lease.release()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--worker', type=Path, required=True)
    p.add_argument('--lock-fd', type=int, required=True)
    a = p.parse_args()
    return worker(a.worker, a.lock_fd)


if __name__ == '__main__':
    sys.exit(main())
