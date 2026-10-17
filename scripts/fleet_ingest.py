#!/usr/bin/env python3
"""Deterministic fleet completion inbox. Never dispatches, sends, or writes Linear.

Coordinator-owned PLAN.json proof commands are evaluated without a shell. Prose,
missing commands and unsafe effect-capable proofs fail closed to REDISPATCH.
Only the coordinator closes a READY_TO_CLOSE row through linear_close.py --apply.
"""
from __future__ import annotations
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

ISSUE = re.compile(r'GLP-[1-9][0-9]*\Z')
LANE = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,100}\Z')
# Leave room for local discovery, durable receipts and the wrapper's 240s ceiling.
MAX_COMMAND_SECONDS = 15
POLL_COMMAND_BUDGET_SECONDS = 120
TIMEOUT_CLEANUP_SECONDS = .1


def now():
    return datetime.now(timezone.utc).isoformat()


def read(path):
    try:
        data = json.loads(Path(path).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_hashed(path):
    """Parse and authenticate one buffer; callers recheck for subsequent drift."""
    raw = Path(path).read_bytes()
    try:
        data = json.loads(raw)
    except ValueError:
        data = {}
    return (data if isinstance(data, dict) else {}), hashlib.sha256(raw).hexdigest()


def atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.ingest-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def event(source, issue, lane, identity, files, worker_result=None, failure=''):
    if not ISSUE.fullmatch(issue) or not LANE.fullmatch(lane):
        raise ValueError('invalid issue/lane identity')
    evidence = {str(p): digest(p) for p in files}
    return {'source': source, 'issue': issue, 'lane': lane,
            'identity': str(identity), 'evidence': evidence,
            'worker_result': str(worker_result) if worker_result else None,
            'source_failure': failure}


def run(argv, cwd, timeout, output_limit=4000):
    env = dict(os.environ, LINEAR_ROLE='fleet-ingest')
    try:
        # Own only this read-only command's process group. Killing just its Python
        # parent can leave a network helper holding stdout open past the deadline.
        # Do not use Popen's context manager: its implicit wait is unbounded.
        cp = subprocess.Popen(argv, cwd=cwd, env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            try:
                stdout, stderr = cp.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                deadline = time.monotonic() + TIMEOUT_CLEANUP_SECONDS
                try:
                    os.killpg(cp.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    cp.communicate(timeout=max(0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    # A detached descendant can still own the write ends. Never
                    # wait for its EOF or kill processes outside our owned group.
                    cp.stdout.close()
                    cp.stderr.close()
                    try:
                        cp.wait(timeout=max(0, deadline - time.monotonic()))
                    except subprocess.TimeoutExpired:
                        pass  # Popen's nonblocking reaper retains an unreaped child.
                return {'rc': 124, 'stdout': '', 'stderr': 'REFUSED: read-only proof timed out; not verified'}
            return {'rc': cp.returncode, 'stdout': stdout[-output_limit:], 'stderr': stderr[-4000:],
                    'output_truncated': len(stdout) > output_limit}
        finally:
            cp.stdout.close()
            cp.stderr.close()
    except OSError as exc:
        return {'rc': 127, 'stdout': '', 'stderr': str(exc)}


class CommandBudget:
    """One poll-wide allowance; policy timeouts are upper bounds, never grants."""
    def __init__(self, runner):
        self.runner = runner
        self.remaining = POLL_COMMAND_BUDGET_SECONDS

    def __call__(self, argv, cwd, timeout, output_limit=4000):
        if self.remaining <= 0:
            return {'rc': 124, 'stdout': '', 'stderr': 'REFUSED: poll read-only command budget exhausted; deferred',
                    'deferred': True}
        start = time.monotonic()
        try:
            return self.runner(argv, cwd, min(timeout, MAX_COMMAND_SECONDS, self.remaining), output_limit)
        finally:
            self.remaining -= time.monotonic() - start


BUSINESS_PROOF_ISSUES = frozenset({'GLP-125', 'GLP-320', 'GLP-327', 'GLP-328',
    'GLP-349', 'GLP-47', 'GLP-334', 'GLP-93', 'GLP-354', 'GLP-358',
    'GLP-359', 'GLP-88', 'GLP-250', 'GLP-253'})


def proof_argv(value):
    if isinstance(value, str):
        if any(x in value for x in (';', '|', '&', '`', '$', '\n', '>', '<')):
            raise ValueError('proof_cmd requires a read-only argv, not shell/prose')
        value = shlex.split(value)
    if not isinstance(value, list) or not value or not all(isinstance(x, str) for x in value):
        raise ValueError('missing executable proof_cmd')
    # These commands cannot mutate business/Linear or send/deploy. Other commands
    # need an explicit coordinator-owned read-only contract, not a worker claim.
    base = Path(value[0]).name
    if value[0] != base and value[0] not in {'/usr/bin/' + base, '/bin/' + base, sys.executable}:
        raise ValueError('proof executable path not trusted')
    safe = base in {'stat', 'test', 'sha256sum'}
    safe |= base == 'gh' and len(value) > 2 and (value[1:3] in [['pr', 'view'], ['pr', 'checks'], ['issue', 'view']])
    if base == 'curl':
        options = {'--fail', '--silent', '--show-error', '--head', '-I', '-f', '-s', '-S'}
        urls = [x for x in value[1:] if x.startswith('https://')]
        safe = (len(urls) == 1 and re.fullmatch(r'https://glpclinicscout\.com/(?:guides/[^?#]*|sitemap\.xml|robots\.txt)', urls[0]) is not None
                and all(x in options or x in urls for x in value[1:]))
    safe |= base in {'python3', 'python'} and len(value) > 2 and value[1:3] == ['-m', 'unittest']
    safe |= (base in {'python3', 'python'} and len(value) == 3
             and value[1] == 'scripts/fleet_plan_proof.py' and ISSUE.fullmatch(value[2]) is not None)
    safe |= (base in {'python3', 'python'} and value[1:] == ['scripts/glp_cogny_queue_drain.py', '--mode', 'status'])
    safe |= (base == 'python3' and len(value) == 4
             and value[1:3] == ['-B', 'scripts/linear_business_proof.py']
             and value[3] in BUSINESS_PROOF_ISSUES)
    if not safe:
        raise ValueError('proof_cmd not in read-only allowlist; coordinator must supply a safe proof')
    return value


def alive(pid):
    try:
        pid = int(pid)
        if pid <= 0:
            return True
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except (ValueError, TypeError, PermissionError, OverflowError):
        return True


def generated_tree(directory, dirs, files):
    """Positive producer evidence, not a basename blacklist. Unknown stays visible."""
    path = Path(directory)
    if {'RESULT.json', 'RUN.json'} & set(files):
        return False
    if 'CACHEDIR.TAG' in files:
        try:
            with (path / 'CACHEDIR.TAG').open('rb') as tag:
                if tag.read(43) == b'Signature: 8a477f597d28d172789f06886806bc55':
                    return True
        except OSError:
            pass
    if not re.fullmatch(r'pytest(?:-.*)?', path.name):
        return False
    # pytest tmp_path/basetemp creates numbered siblings and a matching current
    # symlink. The name alone (including cases/native-output) proves nothing.
    for name in dirs:
        if not re.fullmatch(r'(?:test_.+|pytest-)current', name):
            continue
        try:
            target = (path / name).readlink()
            if not target.is_absolute():
                target = path / target
            prefix = name[:-len('current')]
            if (target.parent == path and target.name in dirs
                    and re.fullmatch(re.escape(prefix) + r'[0-9]+', target.name)
                    and not target.is_symlink()):
                return True
        except OSError:
            pass
    return False


def walk(root, names):
    # Retain the predecessor's copy/state namespace exclusions. Additional
    # generated-tree pruning requires producer evidence, never a name alone.
    for directory, dirs, files in os.walk(root, followlinks=False):
        if '.git' in dirs or '.git' in files or generated_tree(directory, dirs, files):
            dirs[:] = []
            continue
        dirs[:] = sorted(x for x in dirs if x not in {
            '.git', 'checkout', 'source', 'repo', 'node_modules', '.venv',
            '.worktrees', 'fleet-inbox', 'artifacts', 'history', 'cache',
            'site-system', 'prepared', 'release', 'evidence'})
        for name in sorted(set(files) & set(names)):
            path = Path(directory) / name
            if not path.is_symlink():
                yield path


def binding(data, path):
    value = data.get('issue') or data.get('linear_issue') or data.get('issue_id')
    if isinstance(value, str) and ISSUE.fullmatch(value):
        return value
    issues = data.get('issues')
    if isinstance(issues, list) and len(issues) == 1 and isinstance(issues[0], str) and ISSUE.fullmatch(issues[0]):
        return issues[0]
    # Legacy Codex lanes do not store issue: accept only a named glpNN lane dir,
    # never a mention in arbitrary reply text or paths under another issue.
    matches = set(re.findall(r'(?i)(?:^|[/_-])glp-?([1-9][0-9]*)(?=[/_-]|$)', str(path)))
    return 'GLP-' + next(iter(matches)) if len(matches) == 1 else None


def lane_name(data, source, identity):
    lane = data.get('lane') or data.get('executor') or data.get('model') or source
    lane = re.sub(r'[^a-zA-Z0-9_.-]', '-', str(lane))[:60]
    return lane + '-' + hashlib.sha256(str(identity).encode()).hexdigest()[:12]


def result_pointer(root, data, fallback=None):
    raw = data.get('worker_result') or data.get('result_path')
    p = Path(raw) if isinstance(raw, str) else fallback
    if p is None:
        return None
    if not p.is_absolute():
        p = root / p
    p = p.resolve()
    return p if root.resolve() in p.parents and p.is_file() else None


def verified_outbox(root, job):
    if not LANE.fullmatch(job):
        raise ValueError('OUTBOX_INVALID_JOB')
    parent = root / 'reports/ops/gpt6pro-relay/jobs' / job
    st = read(parent / 'JOB.json')
    receipt = st.get('fetch_verification', {})
    dest = parent / 'outbox'
    manifest = dest / 'OUTPUT-MANIFEST.json'
    files = read(manifest).get('files')
    if (receipt.get('state') != 'VERIFIED' or not isinstance(files, dict)
            or not manifest.is_file() or digest(manifest) != receipt.get('manifest_sha256')
            or not (parent / 'RESPONSE.md').is_file()
            or digest(parent / 'RESPONSE.md') != st.get('source_reply_sha256')):
        raise ValueError('OUTBOX_NOT_VERIFIED_OR_CHANGED')
    if set(p.name for p in dest.iterdir()) != set(files) | {'OUTPUT-MANIFEST.json'}:
        raise ValueError('OUTBOX_FILE_SET_CHANGED')
    paths = [manifest, parent / 'JOB.json', parent / 'RESPONSE.md']
    for name, sha in files.items():
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,159}', name):
            raise ValueError('OUTBOX_INVALID_FILE')
        p = dest / name
        if p.is_symlink() or digest(p) != sha:
            raise ValueError('OUTBOX_BYTES_CHANGED')
        paths.append(p)
    return paths


def registered_root_native(root, live, config):
    """Exact coordinator-configured root lanes only; never glob worker receipts."""
    registered, completed, active = {}, {}, set()
    for issue, policy in config.get('issues', {}).items():
        raw = policy.get('worker_result') if isinstance(policy, dict) else None
        if not ISSUE.fullmatch(str(issue)) or not isinstance(raw, str):
            continue
        path = Path(raw) if Path(raw).is_absolute() else root / raw
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        if (len(rel.parts) != 3 or rel.parts[0] != 'lanes'
                or not LANE.fullmatch(rel.parts[1]) or rel.parts[2] != 'RESULT.json'
                or path.resolve() != path or path.is_symlink()):
            continue
        registered.setdefault(path, set()).add(issue)
    if not registered:
        return []
    for manifest in sorted(live.glob('*/manifest.json')):
        if manifest.is_symlink() or manifest.parent.is_symlink():
            continue
        data, sha = read_hashed(manifest)
        for task in data.get('tasks', []):
            goal = task.get('goal', '')
            ids = {'GLP-' + n for n in re.findall(r'\bGLP-?([1-9][0-9]*)\b', goal)}
            for path, issues in registered.items():
                named = re.search(re.escape(str(path)) + r'(?=$|[\s{},.;)])', goal)
                owned = re.match(re.escape(path.parent.name) + r'(?:\s|/|$)', goal)
                if not named and not (owned and ids == issues):
                    continue
                # Live PIDs and unknown/unfinalized states override history;
                # finalized failures are terminal, but cannot authorize success.
                terminal_failure = (data.get('completed') and task.get('status')
                                    in {'failed', 'error', 'interrupted', 'timeout'})
                if ((task.get('status') != 'completed' and not terminal_failure)
                        or (data.get('pid') and alive(data['pid']))
                        or (task.get('pid') and alive(task['pid']))):
                    active.add(path)
                    continue
                if (task.get('status') == 'completed'
                        and named and owned and len(issues) == 1 and ids == issues
                        and data.get('completed') and task.get('exit_reason') == 'completed'):
                    if path not in completed or manifest.stat().st_mtime_ns > completed[path][0].stat().st_mtime_ns:
                        completed[path] = (manifest, sha)
    events = []
    for path, (manifest, manifest_sha) in completed.items():
        if path in active or not path.is_file() or path.is_symlink():
            continue
        data, result_sha = read_hashed(path)
        if (binding(data, path) not in registered[path]
                or (data.get('pid') and alive(data['pid']))
                or path.stat().st_mtime_ns > manifest.stat().st_mtime_ns):
            continue
        issue = next(iter(registered[path]))
        failure = '' if str(data.get('status', '')).upper() in {'DONE', 'DONE_READY_TO_CLOSE', 'PASS', 'COMPLETED', 'COMPLETE', 'CLOSED'} else 'native executor stopped without a complete worker RESULT'
        item = event('native', issue, lane_name(data, 'sol', path), path,
                     [path, manifest], path, failure)
        if item['evidence'] == {str(path): result_sha, str(manifest): manifest_sha}:
            events.append(item)
    return events


def auto_deployer_events(root):
    """Consume the deployer's finite append-only inbox; still require PLAN proof.

    A source install or merge receipt is not a genuine completed worker RESULT
    and never supplies its own executable proof or Linear closure authority.
    """
    journal = root / 'reports/ops/fleet-inbox/auto-deployer.jsonl'
    events, errors, seen = [], [], set()
    if not journal.exists():
        return events, errors
    for line_number, line in enumerate(journal.read_text().splitlines(), 1):
        try:
            entry = json.loads(line)
            rel = entry['receipt']
            if not re.fullmatch(r'reports/ops/auto-deployer/[0-9]{8}T[0-9]{6}\.[0-9]{6}Z/glp-(?:business|site-candidate)-[1-9][0-9]*\.json', rel):
                raise ValueError('invalid auto-deployer receipt path')
            path = root / rel
            if path.is_symlink() or not path.resolve().is_relative_to((root / 'reports/ops/auto-deployer').resolve()):
                raise ValueError('receipt escapes maintained output root')
            data, sha = read_hashed(path)
            if sha != entry['sha256'] or data.get('schema') != 'auto-deployer-v1':
                raise ValueError('receipt digest/schema mismatch')
            repo, number, head, issue = data['repo'], data['pr'], data['head'], data['issue']
            if (repo not in {'izaiahat/glp-business', 'izaiahat/glp-site-candidate'}
                    or type(number) is not int or number < 1 or not ISSUE.fullmatch(issue)
                    or not re.fullmatch('[0-9a-f]{40}', head)
                    or path.name != f'{repo.split("/")[1]}-{number}.json'):
                raise ValueError('invalid receipt identity')
            identity = f'auto-deployer:{repo}#{number}:{head}'
            if identity in seen:
                continue
            seen.add(identity)
            failure = '' if (data['status'] == 'WAITING_LIVE_OUTPUT'
                and data.get('merge', {}).get('head') == head
                and re.fullmatch('[0-9a-f]{40}', data.get('merge', {}).get('merge_commit', ''))
                and data.get('deployment', {}).get('status') in
                    {'INSTALLED_SOURCE', 'NO_RUNTIME_PATHS', 'WAITING_REGISTERED_RELEASE'}) else 'auto-deployer requires owner reconciliation'
            item = event('github', issue, f'auto-deployer-{repo.split("/")[1]}-{number}', identity, [path], failure=failure)
            if item['evidence'] != {str(path): sha}:
                raise ValueError('receipt changed during discovery')
            events.append(item)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors.append({'path': str(journal), 'line': line_number, 'error': str(exc)})
    return events, errors


def discover(root, live, config, prs):
    events, errors = auto_deployer_events(root)
    completed = {}
    try:
        events.extend(registered_root_native(root, live, config))
    except (OSError, ValueError) as exc:
        errors.append({'path': str(live), 'error': 'registered root native: ' + str(exc)})
    # A terminal native manifest is the runtime-owned equivalent of dead PID.
    # Explicit PID, if present, must still be dead; partial live RESULT is ignored.
    for manifest in live.glob('*/manifest.json'):
        data = read(manifest)
        if data.get('pid') and alive(data['pid']):
            continue
        for task in data.get('tasks', []):
            if task.get('status') != 'completed':
                continue
            for raw in re.findall(r'/home/ubuntu/business/[^\s{},]*RESULT\.json', task.get('goal', '')):
                completed[str(Path(raw).resolve())] = (manifest, task)
    for path in walk(root / 'reports/ops', {'RESULT.json', 'RUN.json'}):
        data = read(path)
        try:
            if path.name == 'RUN.json':
                if not data.get('finished_at') or 'exit_code' not in data:
                    continue
                issue = binding(data, path)
                if not issue:
                    continue
                response = path.parent / 'RESPONSE.md'
                evidence = [path] + ([response] if response.is_file() else [])
                failure = '' if data['exit_code'] == 0 and response.is_file() else 'Codex executor failed or RESPONSE.md absent'
                events.append(event('codex', issue, lane_name(data, 'codex', path), path, evidence,
                    result_pointer(root, data, path.parent / 'RESULT.json'), failure))
                continue
            completion = completed.get(str(path.resolve()))
            if data.get('pid'):
                if alive(data['pid']):
                    continue
            elif not completion:
                continue
            if not data:
                continue
            failure = '' if str(data.get('status', '')).upper() in {'DONE', 'DONE_READY_TO_CLOSE', 'PASS', 'COMPLETED', 'COMPLETE', 'CLOSED'} else 'native executor stopped without a complete worker RESULT'
            issue = binding(data, path)
            if not issue and completion:
                ids = set(re.findall(r'GLP-[1-9][0-9]*', completion[1].get('goal', '')))
                issue = next(iter(ids)) if len(ids) == 1 else None
            if issue:
                evidence = [path] + ([completion[0]] if completion else [])
                events.append(event('native', issue, lane_name(data, 'sol', path), path, evidence, path, failure))
        except (OSError, ValueError) as exc:
            errors.append({'path': str(path), 'error': str(exc)})
    for path in sorted((root / 'ops/gpt6pro-relay/route/queue').glob('*.json')):
        data = read(path)
        if data.get('state') != 'complete' or not binding(data, path):
            continue
        try:
            files = verified_outbox(root, data['job'])
            events.append(event('pro', binding(data, path), lane_name(data, 'pro', path),
                path, [path] + files, result_pointer(root, data)))
        except (KeyError, OSError, ValueError) as exc:
            errors.append({'path': str(path), 'error': str(exc)})
    for path in sorted((root / 'reports/ops/dot-tickets').glob('*/REPLY.md')):
        data = read(path.parent / 'CONFIG.json')
        issue = data.get('issue') or data.get('issue_id')
        if not isinstance(issue, str) or not ISSUE.fullmatch(issue) or not path.read_text().strip():
            continue
        events.append(event('dot', issue, lane_name({'lane': data.get('dot', data.get('lane', 'dot'))}, 'dot', path),
            path, [path, path.parent / 'CONFIG.json'], result_pointer(root, data, path.parent / 'RESULT.json')))
    for pr in prs:
        ids = {label['name'].split(':', 1)[1] for label in pr.get('labels', [])
               if re.fullmatch(r'fleet-ticket:GLP-[1-9][0-9]*', label.get('name', ''))}
        if len(ids) != 1 or pr.get('state') not in {'MERGED', 'CLOSED'}:
            continue
        issue = next(iter(ids))
        identity = pr['url']
        # Snapshot only read-only GitHub data. Never claim PR alone is installed.
        snapshot = root / 'reports/ops/fleet-inbox/.github' / (hashlib.sha256(identity.encode()).hexdigest() + '.json')
        if read(snapshot) != pr:
            atomic(snapshot, pr)
        bad = pr.get('state') != 'MERGED' or not pr.get('statusCheckRollup') or any(
            c.get('conclusion', c.get('state')) not in {'SUCCESS', 'SKIPPED', 'NEUTRAL'} for c in pr.get('statusCheckRollup', []))
        events.append(event('github', issue, lane_name({'lane': 'pr'}, 'github', identity),
            identity, [snapshot], None, 'PR not merged on passing exact-head CI' if bad else ''))
    # Only coordinator-bound registered machine identities, never infer ownership
    # from arbitrary receipt text or issue mentions in an unrelated artifact.
    latest = {}
    for base in ('reports/ops/n8n-to-cron-20260924/runs', 'reports/ops/job-receipts'):
        for path in walk(root / base, {p.name for p in (root / base).glob('*.json')}):
            data = read(path)
            bridge = data.get('bridge', {})
            if not isinstance(bridge, dict):
                continue
            jid = bridge.get('job_id') or data.get('job_id') or data.get('action')
            policy = config.get('cron_bindings', {}).get(jid)
            if not policy or not ISSUE.fullmatch(str(policy.get('issue', ''))):
                continue
            terminal = bridge.get('status') or data.get('status')
            if terminal not in {'done', 'complete', 'completed', 'PASS', 'SUCCESS', 'failed', 'FAIL', 'error'}:
                continue
            key = str(jid)
            if key not in latest or path.stat().st_mtime_ns > latest[key][0].stat().st_mtime_ns:
                latest[key] = (path, data, policy)
    for jid, (path, data, policy) in sorted(latest.items()):
        bridge = data.get('bridge', {})
        failure = '' if (bridge.get('rc', data.get('rc', 0)) == 0 and (bridge.get('status') or data.get('status')) not in {'failed', 'FAIL', 'error'}) else 'registered cron receipt failed'
        # Stable machine lane; source revision hashes still retain every receipt.
        events.append(event('cron', policy['issue'], lane_name({'lane': policy.get('lane', 'cron')}, 'cron', jid),
            'cron:' + jid, [path], result_pointer(root, policy), failure))
    return events, errors


def completion_preflight(root, issue, result, policy, runner=run, timeout=30):
    """Dispatch a coordinator-selected contract; never infer completion from prose.

    Protected retained contracts have no schema discriminator in the maintained
    writer; require its exact scope and reject foreign schema labels. All arms
    still run their authenticated acceptance/evidence validators, check-only.
    """
    try:
        root = Path(root).resolve()
        if not ISSUE.fullmatch(str(issue)):
            raise ValueError('exact GLP issue required')
        contract = policy.get('completion_contract', 'worker')
        if contract not in ('worker', 'ordinary', 'protected-runtime', 'protected-noncode'):
            raise ValueError('unknown coordinator completion_contract')
        if not result:
            raise ValueError('completion has no genuine worker RESULT.json for closure')
        path = Path(result)
        if not path.is_absolute():
            path = root / path
        if contract == 'worker':
            argv = [sys.executable, str(root / 'scripts/linear_close.py'), issue,
                    str(path), '--check-only']
        else:
            if (path.name != 'RESULT.json' or path.is_symlink() or not path.is_file()
                    or not path.resolve().is_relative_to((root / 'reports').resolve())):
                raise ValueError('typed completion needs existing RESULT.json under reports')
            path = path.resolve()
            data = json.loads(path.read_bytes())
            if not isinstance(data, dict) or data.get('issue') != issue or data.get('status') != 'COMPLETE':
                raise ValueError('typed completion requires exact issue and COMPLETE')
            if contract == 'ordinary':
                if (data.get('schema') != 'linear-ordinary-completion-v1'
                        or data.get('scope') not in ('ordinary-code', 'ordinary-runtime', 'ordinary-readonly-runtime')):
                    raise ValueError('ordinary completion scope/schema mismatch')
            elif data.get('scope') != contract or 'schema' in data:
                raise ValueError('protected completion scope/schema mismatch')
            argv = [sys.executable, '-B', str(root / 'scripts/linear_orchestrator.py'),
                    'pass', issue, str(path), '--' + contract, '--check-only']
        check = {'argv': argv, **runner(argv, root, timeout)}
        if check['rc'] != 0:
            return check
        if contract == 'worker':
            valid = re.search(r'^' + re.escape(issue) + r' PASS(?:\s|$)',
                              check.get('stdout', ''), re.M)
            error = 'no-write closer did not return explicit PASS'
        else:
            try:
                response = json.loads(check.get('stdout', ''))
            except (ValueError, TypeError):
                response = None
            valid = (isinstance(response, dict) and response.get('identifier') == issue
                     and response.get('validated') is True
                     and type(response.get('writes')) is int and response['writes'] == 0)
            error = 'no-write typed validator did not return exact identifier/validated:true/writes:0'
        if not valid:
            check.update(rc=3, stderr=error)
        return check
    except (ValueError, TypeError, OSError) as exc:
        return {'rc': 2, 'stdout': '', 'stderr': str(exc)}


def ingest(root, item, policy, runner=run, timeout=30, retry=False):
    inbox = root / 'reports/ops/fleet-inbox'
    target = inbox / item['issue'] / item['lane'] / 'INGEST.json'
    closer = root / 'scripts/linear_close.py'
    item = dict(item, evidence=dict(item['evidence']))
    if item['source'] == 'native' and any(
            not Path(p).is_file() or Path(p).is_symlink() or digest(p) != sha
            for p, sha in item['evidence'].items()):
        item['source_failure'] = 'native evidence changed since discovery'
    supplied_result = policy.get('worker_result') or item.get('worker_result')
    result = result_pointer(root, {'worker_result': supplied_result})
    item['worker_result'] = str(result) if result else None
    if supplied_result and not result:
        item['source_failure'] = 'worker RESULT absent or outside workspace'
    if result:
        item['worker_result'] = str(result)
        item['evidence'][str(result)] = digest(result)
        bound_issue = read(result).get('issue')
        if bound_issue and bound_issue != item['issue']:
            item['source_failure'] = 'worker RESULT issue mismatch'
    # Completion identity + trusted command + closer bytes are the replay key.
    fingerprint = hashlib.sha256(json.dumps({'event': item, 'policy': policy,
        'closer': digest(closer) if closer.is_file() else None,
        'gate': digest(root / 'scripts/linear_orchestrator.py') if (root / 'scripts/linear_orchestrator.py').is_file() else None,
        'dispatcher': digest(root / 'scripts/fleet_ingest.py') if (root / 'scripts/fleet_ingest.py').is_file() else None,
        'typed_lint': digest(root / 'scripts/result_lint.py') if (root / 'scripts/result_lint.py').is_file() else None,
        'proof_verifier': digest(root / 'scripts/fleet_plan_proof.py') if (root / 'scripts/fleet_plan_proof.py').is_file() else None,
        'business_verifier': digest(root / 'scripts/linear_business_proof.py') if (root / 'scripts/linear_business_proof.py').is_file() else None}, sort_keys=True).encode()).hexdigest()
    prior = read(target)
    transient = any(check.get('rc') == 124 for check in prior.get('checks', {}).values()
                    if isinstance(check, dict))
    if prior.get('fingerprint') == fingerprint and not retry and not transient:
        return prior
    if prior.get('identity') not in (None, item['identity']):
        raise ValueError('lane collision: source identities must have separate lanes')
    checks = {}
    failures = [item['source_failure']] if item['source_failure'] else []
    if policy.get('proof_complete') is False:
        failures.append('proof incomplete: ' + str(policy.get('proof_incomplete_reason') or 'prerequisite-only command'))
    try:
        argv = proof_argv(policy.get('proof_cmd'))
        proof_cwd, proof_timeout = root, timeout
        if argv[1:3] == ['-B', 'scripts/linear_business_proof.py']:
            if argv[-1] != item['issue'] or policy.get('issue', item['issue']) != item['issue']:
                raise ValueError('business proof issue mismatch')
            if not isinstance(policy.get('proof_cmd'), list):
                raise ValueError('business proof_cmd requires nonempty argv list, not shell text')
            proof_cwd = Path(policy.get('proof_cwd', str(root)))
            proof_timeout = policy.get('proof_timeout_s', timeout)
            if proof_cwd != root or type(proof_timeout) is not int or not 1 <= proof_timeout <= 300:
                raise ValueError('business proof cwd/timeout outside read-only contract')
        proof = runner(argv, proof_cwd, min(proof_timeout, MAX_COMMAND_SECONDS))
        minimums = policy.get('proof_stdout_minimums')
        if proof['rc'] == 0 and minimums is not None:
            try:
                observed = [int(s) for s in proof.get('stdout', '').splitlines()]
                valid = isinstance(minimums, list) and len(observed) == len(minimums) and all(v >= int(m) for v, m in zip(observed, minimums))
            except (ValueError, TypeError):
                valid = False
            if not valid:
                proof = dict(proof, rc=3, stderr='proof stdout minimum thresholds not met')
        checks['proof'] = {'argv': argv, **proof}
    except ValueError as exc:
        checks['proof'] = {'rc': 2, 'stderr': str(exc), 'stdout': ''}
    if checks['proof']['rc']:
        failures.append('proof: ' + (checks['proof'].get('stderr') or checks['proof'].get('stdout') or 'nonzero exit'))
    checks['linear_close'] = completion_preflight(root, item['issue'], item['worker_result'],
                                                 policy, runner, min(timeout, MAX_COMMAND_SECONDS))
    if checks['linear_close']['rc']:
        failures.append('linear_close: ' + (checks['linear_close'].get('stderr') or checks['linear_close'].get('stdout') or 'nonzero exit'))
    row = {'schema': 'fleet-ingest-v1', **item, 'fingerprint': fingerprint,
           'ingested_at_utc': now(), 'status': 'REDISPATCH' if failures else 'READY_TO_CLOSE',
           'attempted_at_utc': (now() if any(c.get('argv') and not c.get('deferred') for c in checks.values())
                                else prior.get('attempted_at_utc', '')),
           'checks': checks, 'failure_text': '\n'.join(failures),
           'closure_authority': 'coordinator-only', 'linear_mutations': 0}
    # Preserve the original timeout/refusal when the same receipt is retried.
    revision = target.parent / 'history' / (fingerprint + '.json')
    if revision.exists() and read(revision) != row:
        attempt = hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()
        revision = revision.with_name(fingerprint + '-' + attempt + '.json')
    atomic(revision, row)
    atomic(target, row)
    return row


def revoke_stale(root, current_items):
    current = {item['identity'] for item in current_items}
    revoked = 0
    for path in (root / 'reports/ops/fleet-inbox').glob('GLP-*/*/INGEST.json'):
        row = read(path)
        if row.get('status') != 'READY_TO_CLOSE' or row.get('identity') in current:
            continue
        try:
            valid = all(Path(p).is_file() and not Path(p).is_symlink() and digest(Path(p)) == sha
                        for p, sha in row.get('evidence', {}).items())
        except OSError:
            valid = False
        if valid:
            continue
        row = dict(row, status='REDISPATCH', failure_text='source evidence changed or disappeared; terminal completion must be reconciled', revoked_at_utc=now())
        key = hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()
        row['fingerprint'] = key
        atomic(path.parent / 'history' / (key + '.json'), row)
        atomic(path, row)
        revoked += 1
    return revoked


def admission_observation(proc=Path('/proc'), budget=None):
    """Read host counters, never reserve/reclaim slots or call a spawn gate.

    A poll snapshot is not an admission decision or proof of continuous workload.
    Unknown measurements remain null; lease rows are not a live executor census.
    """
    budget = budget or Path('/home/ubuntu/.hermes/cache/delegation-host-budget.json')
    result = {'observed_at_utc': now(), 'mem_available_kb': None,
              'swap_free_kb': None, 'psi_full_avg10': None,
              'swap_io_pages': None, 'swappiness': None,
              'reserved_slots_unvalidated': None,
              'actual_memory_gate_refusals': None, 'errors': []}
    def text(path):
        try:
            return path.read_text()
        except OSError as exc:
            result['errors'].append(str(exc))
            return ''
    try:
        mem = {line.split(':', 1)[0]: int(line.split()[1])
               for line in text(proc / 'meminfo').splitlines() if ':' in line}
        result['mem_available_kb'] = mem.get('MemAvailable')
        result['swap_free_kb'] = mem.get('SwapFree')
        for line in text(proc / 'pressure/memory').splitlines():
            if line.startswith('full '):
                result['psi_full_avg10'] = float(next(x.split('=')[1] for x in line.split() if x.startswith('avg10=')))
        counters = dict(line.split() for line in text(proc / 'vmstat').splitlines())
        if 'pswpin' in counters and 'pswpout' in counters:
            result['swap_io_pages'] = int(counters['pswpin']) + int(counters['pswpout'])
        value = text(proc / 'sys/vm/swappiness').strip()
        result['swappiness'] = int(value) if value else None
        rows = json.loads(text(budget))
        if not isinstance(rows, list) or not all(isinstance(r, dict) and {'pid', 'start', 'token'} <= r.keys() for r in rows):
            raise ValueError('host budget schema invalid')
        result['reserved_slots_unvalidated'] = len(rows)
    except (ValueError, IndexError, StopIteration) as exc:
        result['errors'].append(str(exc))
    return result


def admission_metrics(previous, observation):
    """Retain actual poll observations in the existing receipt, not a new ledger."""
    cutoff = datetime.fromisoformat(observation['observed_at_utc']).timestamp() - 86400
    samples = []
    for row in previous.get('admission_metrics', {}).get('observations', []):
        try:
            if cutoff <= datetime.fromisoformat(row['observed_at_utc']).timestamp() <= cutoff + 86400:
                samples.append(row)
        except (ValueError, KeyError, TypeError):
            continue
    samples.append(observation)
    return {'schema': 'fleet-admission-observations-v1',
            'observations': samples[-300:], 'full_acceptance': 'UNPROVEN',
            'missing_evidence': ['actual admission decisions/refusals',
                                 '24h genuine eight-slot workload',
                                 'supported pruner and scheduled audit disposition',
                                 'continuous swap guard outcome']}


def poll(root, live, plan_path, config_path, timeout=30, retry=False):
    inbox = root / 'reports/ops/fleet-inbox'
    inbox.mkdir(parents=True, exist_ok=True)
    with (inbox / '.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'status': 'BUSY', 'changed': 0}
        plan_data, config = read(plan_path), read(config_path)
        policies = {p['issue']: p for p in plan_data.get('issues', []) if isinstance(p, dict) and ISSUE.fullmatch(str(p.get('issue', '')))}
        for issue, override in config.get('issues', {}).items():
            if ISSUE.fullmatch(issue):
                policies[issue] = {**policies.get(issue, {}), **override}
        prs, errors = [], []
        budget = CommandBudget(run)
        for repo in ('izaiahat/glp-business', 'izaiahat/glp-site-candidate'):
            cp = budget(['gh', 'api', '--paginate', 'repos/' + repo + '/labels', '--jq',
                      '[.[] | select(.name | test("^fleet-ticket:GLP-[1-9][0-9]*$")) | .name]'], root, timeout, 128000)
            if cp['rc'] or cp.get('output_truncated'):
                errors.append({'repo': repo, 'error': cp['stderr'] or 'label discovery failed'})
                continue
            try:
                # --paginate outputs a JSON array per page, often adjacent.
                labels = []
                decoder, text = json.JSONDecoder(), cp['stdout'].strip()
                while text:
                    page, end = decoder.raw_decode(text)
                    labels.extend(page)
                    text = text[end:].lstrip()
                for label in sorted(set(labels)):
                    cp = budget(['gh', 'pr', 'list', '--repo', repo, '--state', 'all', '--limit', '1000',
                        '--label', label, '--json', 'number,url,state,headRefOid,labels,statusCheckRollup'], root, timeout, 1000000)
                    if cp['rc'] or cp.get('output_truncated'):
                        raise ValueError('GitHub PR listing failed: ' + cp['stderr'])
                    prs.extend(json.loads(cp['stdout']))
            except ValueError as exc:
                errors.append({'repo': repo, 'error': str(exc)})
        items, discovery_errors = discover(root, live, config, prs)
        errors.extend(discovery_errors)
        revoked = revoke_stale(root, items)
        counts, statuses, rows, changed = {}, {}, [], 0
        # New/oldest attempts first: repeated slow refusals must not starve unseen
        # receipts. Timeout/deferred rows are retried; immutable refusals stay cached.
        def attempted(item):
            row = read(inbox / item['issue'] / item['lane'] / 'INGEST.json')
            return (row.get('attempted_at_utc', row.get('ingested_at_utc', '')), item['issue'], item['lane'])
        for item in sorted(items, key=attempted):
            path = inbox / item['issue'] / item['lane'] / 'INGEST.json'
            before = read(path)
            row = ingest(root, item, policies.get(item['issue'], {}), timeout=timeout, retry=retry, runner=budget)
            changed += before != row
            counts[item['source']] = counts.get(item['source'], 0) + 1
            statuses[row['status']] = statuses.get(row['status'], 0) + 1
            rows.append(str(path))
        summary = {'schema': 'fleet-ingest-run-v1', 'status': 'COMPLETE', 'finished_at_utc': now(),
                   'sources': counts, 'statuses': statuses, 'row_count': len(rows), 'changed': changed,
                   'rows': rows, 'errors': errors, 'linear_mutations': 0, 'revoked': revoked,
                   'admission_metrics': admission_metrics(read(inbox / 'LAST-RUN.json'), admission_observation())}
        atomic(inbox / 'LAST-RUN.json', summary)
        return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('/home/ubuntu/business'))
    parser.add_argument('--live', type=Path, default=Path('/home/ubuntu/.hermes/cache/delegation/live'))
    parser.add_argument('--plan', type=Path)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--timeout', type=int, default=30)
    parser.add_argument('--retry', action='store_true', help='explicit read-only proof re-evaluation')
    parser.add_argument('--json', action='store_true', help='local diagnostic, never a channel card')
    args = parser.parse_args()
    summary = poll(args.root, args.live,
        args.plan or args.root / 'reports/ops/fable-plan-2-20261009/PLAN.json',
        args.config or args.root / 'reports/ops/fleet-inbox/CONFIG.json', args.timeout, args.retry)
    if args.json:
        print(json.dumps(summary, sort_keys=True))
    # Scheduled success is silent. Durable rows are coordinator input, not alerts.
    return 0


if __name__ == '__main__':
    sys.exit(main())
