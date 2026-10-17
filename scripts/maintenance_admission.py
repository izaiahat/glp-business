"""Read-only census of bounded task trees and actual source-file references.

Interpreter capability and working directory alone are not evidence of a source
reader. Unreadable unrelated PIDs do not block; an identified task/reader whose
identity cannot be read does. No commands, environments or prompts are emitted.
"""
import os
from pathlib import Path
import shlex
import re
import hashlib


# The authenticated serve mode stores intents only; it never drains, imports
# action modules, or calls the bridge. Bind exact reviewed bytes and argv.
RECEIVE_ONLY_INGRESS = (
    '/home/ubuntu/business/scripts/glp_host_ingress.py',
    '2d5aa63b6bf9416d94e74928c7a21ff587cb2900e485351430aeeff0ca544b7d')


def receive_only_ingress(base, argv, row, owners):
    path, digest = RECEIVE_ONLY_INGRESS
    if (row['pid'] in owners or row.get('unreadable') or row['sources']
            or row['tasks'] != {path}
            or argv != ['/usr/bin/python3', '-B', path, 'serve']):
        return False
    try:
        source = Path(path)
        if source.is_symlink() or hashlib.sha256(source.read_bytes()).hexdigest() != digest:
            return False
        threads = list((base / 'task').iterdir())
        return (len(threads) == 1 and threads[0].name == str(row['pid'])
                and not (threads[0] / 'children').read_text().strip())
    except OSError:
        return False


def source_aliases(paths):
    result = {str(Path(p).absolute()) for p in paths}
    for path in list(result):
        p = Path(path)
        if p.suffix == '.py':
            result.add(str(p.with_suffix('.pyc')))
            result.update(str(q) for q in (p.parent / '__pycache__').glob(p.stem + '.*.pyc'))
    return result


def argv_paths(argv, cwd, paths):
    matches = set()
    for index, arg in enumerate(argv):
        if index and argv[index - 1] == '-c' and Path(argv[0]).name.startswith(('python', 'bash', 'sh', 'dash', 'zsh')):
            for target in paths:
                if re.search(r'(?<![A-Za-z0-9_./+-])' + re.escape(target) + r'(?![A-Za-z0-9_./+-])', arg):
                    matches.add(target)
        # Shell command bodies are operands too. Match complete lexical paths,
        # never a broad root substring (which also matches prompts and shells).
        try:
            words = shlex.split(arg)
        except ValueError:
            words = [arg]
        for word in words:
            if word in paths:
                matches.add(word)
            elif cwd and word and not word.startswith('-'):
                candidate = os.path.normpath(os.path.join(cwd, word))
                if candidate in paths:
                    matches.add(candidate)
            # E.g. --script=/absolute/target.py.
            if '=' in word:
                value = word.split('=', 1)[1]
                candidate = value if value.startswith('/') else os.path.normpath(os.path.join(cwd or '', value))
                if candidate in paths:
                    matches.add(candidate)
    return sorted(matches)


def process_readers(paths, task_paths=(), owners=(), excluded=(), proc=Path('/proc')):
    paths, tasks = source_aliases(paths), source_aliases(task_paths)
    excluded = set(excluded)
    records, roots = {}, set(owners)
    for base in proc.iterdir():
        if not base.name.isdigit() or int(base.name) in excluded:
            continue
        pid = int(base.name)
        row = {'pid': pid, 'sources': set(), 'tasks': set()}
        try:
            row['uid'] = base.stat().st_uid
            raw = (base / 'stat').read_text()
            fields = raw.rsplit(')', 1)[1].split()
            row.update(ppid=int(fields[1]), start_ticks=int(fields[19]))
            row['comm'] = raw.split('(', 1)[1].rsplit(')', 1)[0]
        except FileNotFoundError:
            continue
        except (OSError, ValueError, IndexError):
            if pid in roots:
                row['unreadable'] = True
            else:
                continue
        unreadable = False
        argv = []
        try:
            argv = (base / 'cmdline').read_bytes().decode(errors='replace').rstrip('\0').split('\0')
            try:
                cwd = os.readlink(base / 'cwd')
            except OSError:
                cwd = None
            row['sources'].update(argv_paths(argv, cwd, paths))
            row['tasks'].update(argv_paths(argv, cwd, tasks))
        except OSError:
            unreadable = True
        try:
            for fd in (base / 'fd').iterdir():
                try:
                    target = os.readlink(fd).removesuffix(' (deleted)')
                    if target in paths:
                        row['sources'].add(target)
                except OSError:
                    continue
        except OSError:
            unreadable = True
        try:
            for line in (base / 'maps').read_text().splitlines():
                fields = line.split(maxsplit=5)
                if len(fields) == 6 and fields[5].removesuffix(' (deleted)') in paths:
                    row['sources'].add(fields[5].removesuffix(' (deleted)'))
        except OSError:
            unreadable = True
        if unreadable and (row['sources'] or row['tasks'] or pid in roots):
            row['unreadable'] = True
        records[pid] = row
        if receive_only_ingress(base, argv, row, owners):
            continue  # Exact idle receive-only resident, never a drain/action.
        if row['tasks'] or row['sources']:
            roots.add(pid)
    changed = True
    while changed:
        expanded = roots | {pid for pid, row in records.items() if row.get('ppid') in roots}
        changed, roots = expanded != roots, expanded
    busy = []
    for pid in sorted(roots - excluded):
        row = records.get(pid)
        if row is None:
            continue  # Exited; registry claims/locks are checked separately.
        row['sources'], row['tasks'] = sorted(row['sources']), sorted(row['tasks'])
        row['kind'] = 'process'
        row['reason'] = ('identified_reader_unreadable' if row.get('unreadable') else
                         'adoption_source_reference' if row['sources'] else 'affected_task_tree')
        busy.append(row)
    return busy


def _native_process(pid, started_at, proc):
    """Never interpret an unreadable or changed fingerprint as a dead owner."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return 'unresolved', []
    base = proc / str(pid)
    try:
        fields = (base / 'stat').read_text().rsplit(')', 1)[1].split()
        current = int(fields[19])
    except FileNotFoundError:
        return 'gone', []
    except (OSError, ValueError, IndexError):
        return 'unreadable', []
    try:
        argv = (base / 'cmdline').read_bytes().decode(errors='replace').rstrip('\0').split('\0')
    except OSError:
        argv = []
    if started_at is not None and (not isinstance(started_at, int) or current != started_at):
        return 'identity_changed', argv
    return 'live', argv


def _native_timestamp(value):
    from datetime import datetime
    try:
        stamp = datetime.fromisoformat(value)
        return stamp if stamp.tzinfo is not None else None
    except (TypeError, ValueError):
        return None


def _native_claim_pid(claim):
    import socket
    if not isinstance(claim, dict):
        return None
    # Installed claims use hostname:pid[:unique-token], not a `pid` field.
    parts = str(claim.get('by') or '').split(':')
    if len(parts) >= 2 and parts[0] == socket.gethostname() and parts[1].isdigit():
        pid = int(parts[1])
        return pid if pid > 0 else None
    return None


def native_runs(scheduler, entries, job_lookup, *, proc=Path('/proc')):
    """Read native run ownership without importing the scheduler or mutating its ledger.

    TTL expiry alone cannot discharge a claim: require a dead attributed owner
    or a matching terminal execution. Ledger-owned in-process gateway runs block
    by job ID; only an exact external-worker argv roots a process-tree census.
    """
    import sqlite3
    selected = [entry for entry in entries if entry['kind'] == 'hermes']
    if not selected:
        return [], set()
    busy, owners = [], set()
    rows, ledger_error = {}, False
    cron_dir = scheduler._current_cron_store().cron_dir
    database = cron_dir / 'executions.db'
    try:
        # Do not use cron.executions._connect(): it initializes/prunes/migrates.
        database.stat()
        connection = sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=1)
        try:
            connection.execute('PRAGMA query_only=ON')
            connection.row_factory = sqlite3.Row
            for entry in selected:
                rows[entry['id']] = [dict(row) for row in connection.execute(
                    'SELECT id, job_id, pid, process_started_at, status, claimed_at, '
                    'finished_at, handoff_pending FROM executions WHERE job_id=?', (entry['id'],))]
        finally:
            connection.close()
    except FileNotFoundError:
        pass  # No ledger and no claims means there is no durable run to drain.
    except (OSError, sqlite3.Error):
        ledger_error = True

    for entry in selected:
        jid = entry['id']
        job = job_lookup(jid)
        reasons = set()
        job_rows = rows.get(jid, [])
        if ledger_error:
            reasons.add('native_execution_ledger_unreadable')
        for row in job_rows:
            state, argv = _native_process(row['pid'], row['process_started_at'], proc)
            payload = str(cron_dir / 'external-workers' / (row['id'] + '.json'))
            worker = any(argv[index:index + 2] == ['--external-worker-file', payload]
                         for index in range(len(argv) - 1))
            if state != 'gone' and worker:
                owners.add(row['pid'])
                reasons.add('native_external_worker_alive')
            if row['status'] not in {'completed', 'failed', 'unknown', 'claimed', 'running'}:
                reasons.add('native_execution_state_unresolved')
            elif row['handoff_pending']:
                reasons.add('native_execution_handoff_pending')
            elif row['status'] in {'claimed', 'running', 'unknown'} and state != 'gone':
                reasons.add('native_execution_owner_' + state)

        with scheduler._fire_job_lock(jid) as free:
            if not free or job.get('running'):
                reasons.add('native_fire_lock_or_running')
        now = scheduler._hermes_now()
        for key, ttl in [('fire_claim', scheduler.FIRE_CLAIM_TTL_SECONDS),
                         ('run_claim', scheduler._oneshot_run_claim_ttl_seconds())]:
            claim = job.get(key)
            if claim is None:
                continue
            if scheduler._claim_is_live(claim, now, ttl):
                reasons.add('native_live_' + key)
                continue
            at = _native_timestamp(claim.get('at')) if isinstance(claim, dict) else None
            if at is None or not 0 <= ttl <= (now - at).total_seconds():
                # Native considers future/malformed claims stale for availability;
                # maintenance requires affirmative evidence to discharge them.
                reasons.add('native_unresolved_' + key)
                continue
            pid = _native_claim_pid(claim)
            state, _ = _native_process(pid, None, proc)
            terminal = any(row['pid'] == pid and row['status'] in {'completed', 'failed'}
                           and not row['handoff_pending']
                           and (start := _native_timestamp(row['claimed_at'])) is not None
                           and (end := _native_timestamp(row['finished_at'])) is not None
                           and start <= at <= end for row in job_rows)
            if state != 'gone' and not terminal:
                reasons.add('native_unattributed_' + key)
        for reason in sorted(reasons):
            busy.append({'kind': 'hermes', 'id': jid, 'reason': reason})
    return busy, owners
