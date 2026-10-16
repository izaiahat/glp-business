#!/usr/bin/env python3
"""GLP-110/349 default-profile offline maintenance. PREPARED, not registered.

No arguments means refusal. --dry-run observes and writes only its report.
Live entry is disabled: no maintained all-writer drain/admission lease exists.
No registration, acknowledgement flag or caller receipt can enable it.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from contextlib import closing

HOME = Path('/home/ubuntu/.hermes')
FORK = HOME / 'hermes-agent'
BACKUPS = Path('/home/ubuntu/business/backups/hermes-state')
ROOT = Path(__file__).resolve().parents[1]
CHANNEL = '1487300132054765629'
UNIT = 'hermes-gateway.service'
REPORT = ROOT / 'reports/ops/maintenance-20261015/RUN.json'
PLAN = [
    'DISABLED: missing authenticated all-writer/child drain and continuous writer/browser admission lease',
    'START via scripts/post_discord_message.py to ops-alerts (live only)',
    'Require free bytes >= ceil(1.5 * state.db bytes) + 2 GiB',
    'Refuse recent delegation logs; bind default-profile literal PID/start/argv',
    'Freeze exact TUI frontend PIDs; runtime-mask gateway restart; TERM backend PIDs',
    'Verify stopped/zero default-profile DB holders; recheck delegates/headroom',
    'Direct SQLite backup API (no producer deadline override exists); SHA256 + full integrity_check',
    'Offline VACUUM; retain journal_mode; full integrity_check',
    'Fetch upstream; merge --ff-only ONLY if HEAD is ancestor; editable venv pip install',
    'Unmask/start original gateway unit; CONT TUI parents for native IPC-preserving respawn; verify exact original argv',
    'END with measured before/after bytes and rc; append every step to RUN.json',
]


class Refusal(RuntimeError):
    pass


def command(argv, *, cwd=None, timeout=120):
    r = subprocess.run([str(x) for x in argv], cwd=cwd, text=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if r.returncode:
        # Never publish raw process environments, credential-bearing installer output or tracebacks.
        raise Refusal(f'command_failed:{Path(str(argv[0])).name}:rc={r.returncode}')
    return r.stdout


def required_space(size):
    return (3 * size + 1) // 2 + 2 * 1024**3


def has_space(size, free):
    return free >= required_space(size)


def recent_delegates(root, now):
    if not root.is_dir() or root.is_symlink():
        raise Refusal('delegation_live_directory_missing_or_alias')
    found = []
    for child in root.iterdir():
        if child.is_symlink():
            raise Refusal('delegation_alias_unknown')
        if not child.is_dir():
            continue
        for base, dirs, files in os.walk(child, followlinks=False):
            if any((Path(base) / name).is_symlink() for name in dirs + files):
                raise Refusal('delegation_log_alias_unknown')
            for name in files:
                p = Path(base) / name
                # Include all log-sidecar files; future mtimes also refuse.
                if p.stat().st_mtime >= now - 300:
                    found.append(str(p))
    return sorted(found)


def default_scope(env, argv):
    if env.get('HERMES_HOME', str(HOME)).rstrip('/') != str(HOME):
        return False
    if env.get('HERMES_PROFILE', 'default') not in ('', 'default'):
        return False
    return not any(x in ('--profile', '-p') or x.startswith('--profile=')
                   or '/.hermes/profiles/' in x for x in argv)


def proc(pid):
    p = Path('/proc') / str(pid)
    stat = (p / 'stat').read_text().rsplit(')', 1)[1].split()
    argv = (p / 'cmdline').read_bytes().rstrip(b'\0').decode().split('\0')
    env = dict(x.split('=', 1) for x in (p / 'environ').read_bytes().decode().split('\0') if '=' in x)
    return {'pid': int(pid), 'start': stat[19], 'state': stat[0], 'ppid': int(stat[1]),
            'uid': p.stat().st_uid,
            'parent_start': (Path('/proc') / stat[1] / 'stat').read_text().rsplit(')', 1)[1].split()[19],
            'argv': argv, 'cwd': str((p / 'cwd').readlink()),
            'default_profile': default_scope(env, argv),
            'cgroup': (p / 'cgroup').read_text().strip()}


def census():
    # Equivalent exact census to ps -eo pid,args | grep '[t]ui_gateway.entry'
    # and grep '[h]ermes_cli.main gateway run', but match argv tokens, not observers.
    rows = []
    for line in command(['ps', '-eo', 'pid,args']).splitlines()[1:]:
        pid, _, args = line.strip().partition(' ')
        if not ('tui_gateway.entry' in args or 'hermes_cli.main gateway run' in args):
            continue
        try:
            row = proc(int(pid))
        except FileNotFoundError:
            continue
        a = row['argv']
        if len(a) >= 3 and a[1:3] == ['-m', 'tui_gateway.entry']:
            row['kind'] = 'tui'
        elif len(a) >= 5 and a[1:5] == ['-m', 'hermes_cli.main', 'gateway', 'run']:
            row['kind'] = 'gateway'
        else:
            continue
        if row['default_profile']:
            rows.append(row)
    return rows


def same_process(row):
    try:
        current = proc(row['pid'])
        return (row.get('default_profile') is True and row.get('uid') == os.getuid() and current.get('default_profile') is True
                and all(k in row and k in current and current[k] == row[k]
                        for k in ('start', 'argv', 'cwd', 'uid', 'ppid', 'parent_start', 'cgroup')))
    except (OSError, ValueError, IndexError):
        return False


def protected_browser_descendants(rows):
    """Refuse if shutdown hooks could reap a Chrome/CDP descendant."""
    lines = command(['ps', '-eo', 'pid,ppid,args']).splitlines()[1:]
    processes = {}
    for line in lines:
        parts = line.strip().split(None, 2)
        if len(parts) == 3:
            processes[int(parts[0])] = (int(parts[1]), parts[2])
    owners = {r['pid'] for r in rows}
    descendants = set(owners)
    while True:
        more = {pid for pid, (ppid, _) in processes.items() if ppid in descendants}
        if more <= descendants:
            break
        descendants |= more
    return sorted(pid for pid in descendants - owners if pid in processes
                  and re.search(r'(?i)chrome|chromium|--remote-debugging', processes[pid][1]))


def literal_signal(row, sig):
    if not same_process(row):
        raise Refusal('pid_identity_changed:' + str(row['pid']))
    # pidfd binds the signal to this kernel process, eliminating PID reuse race.
    fd = os.pidfd_open(row['pid'])
    try:
        if not same_process(row):
            raise Refusal('pid_identity_changed_after_pidfd')
        signal.pidfd_send_signal(fd, sig)
    finally:
        os.close(fd)


def holders(db):
    # Read-only all-UID census. sd-pam is deliberately nondumpable even to its
    # own UID; do not skip it or turn unreadable /proc into false zero holders.
    code = '''import json, sys
from pathlib import Path
names = {sys.argv[1] + suffix for suffix in ('', '-wal', '-shm', '-journal')}
result = []
for p in Path('/proc').iterdir():
    if not p.name.isdigit():
        continue
    try:
        if any(str(fd.readlink()).removesuffix(' (deleted)') in names for fd in (p/'fd').iterdir()):
            result.append(int(p.name))
    except FileNotFoundError:
        continue
print(json.dumps(sorted(result)))
'''
    prefix = [] if os.geteuid() == 0 else ['sudo', '-n']
    return json.loads(command([*prefix, '/usr/bin/python3', '-c', code, str(db)], timeout=30))


def git(repo, *args):
    return command(['git', '-C', str(repo), *args]).strip()


def update_fork(repo):
    if git(repo, 'branch', '--show-current') != 'izaiah-main' or git(repo, 'status', '--porcelain'):
        raise Refusal('fork_not_clean_izaiah_main')
    upstream = git(repo, 'symbolic-ref', 'refs/remotes/upstream/HEAD')
    if not re.fullmatch(r'refs/remotes/upstream/[A-Za-z0-9_./-]+', upstream):
        raise Refusal('unknown_upstream_default_branch')
    branch = upstream.removeprefix('refs/remotes/upstream/')
    before = git(repo, 'rev-parse', 'HEAD')
    git(repo, 'fetch', 'upstream', f'+refs/heads/{branch}:{upstream}')
    try:
        git(repo, 'merge-base', '--is-ancestor', 'HEAD', upstream)
    except Refusal:
        return {'status': 'SKIPPED_NON_FAST_FORWARD', 'before': before,
                'after': git(repo, 'rev-parse', 'HEAD'), 'patches_preserved': True}
    git(repo, 'merge', '--ff-only', upstream)
    command([repo / 'venv/bin/python', '-m', 'pip', 'install', '-e', '.'], cwd=repo, timeout=3600)
    return {'status': 'UPDATED', 'before': before, 'after': git(repo, 'rev-parse', 'HEAD')}


def verified_backup(db, directory):
    directory.mkdir(mode=0o700)  # unique per invocation; never overwrite/retry a partial backup
    os.chmod(directory, 0o700)
    target = directory / 'state.db'
    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    os.fchmod(fd, 0o600)
    os.close(fd)
    with closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True, timeout=0)) as src:
        with closing(sqlite3.connect(target, timeout=0)) as dst:
            # SQLite's own .backup engine, not cp; no 10 MiB/s pacing/deadline.
            src.backup(dst, pages=256, sleep=0.1)
            if [r[0] for r in dst.execute('pragma integrity_check')] != ['ok']:
                raise Refusal('backup_integrity_failed')
    with target.open('rb') as f:
        os.fsync(f.fileno())
        digest = hashlib.file_digest(f, 'sha256').hexdigest()
    sha = directory / 'state.db.sha256'
    with os.fdopen(os.open(sha, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as f:
        os.fchmod(f.fileno(), 0o600)
        f.write(digest + '  state.db\n')
        f.flush()
        os.fsync(f.fileno())
    with target.open('rb') as f:
        if hashlib.file_digest(f, 'sha256').hexdigest() != digest:
            raise Refusal('backup_sha256_readback_failed')
    fsync_directory(directory)
    fsync_directory(directory.parent)  # persist this new invocation's name
    return {'path': str(target), 'bytes': target.stat().st_size,
            'sha256': digest, 'integrity_check': 'ok'}


def fsync_directory(directory):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def prepare_backup_root(directory):
    missing, parent = [], directory
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    for created in reversed(missing):
        fsync_directory(created)
        fsync_directory(created.parent)


def vacuum(db):
    with closing(sqlite3.connect(db, timeout=0)) as c:
        mode = c.execute('pragma journal_mode').fetchone()[0]
        c.execute('vacuum')
        after = c.execute('pragma journal_mode').fetchone()[0]
        if mode != after:
            raise Refusal('journal_mode_changed')
        if [r[0] for r in c.execute('pragma integrity_check')] != ['ok']:
            raise Refusal('vacuum_integrity_failed')
    return {'journal_before': mode, 'journal_after': after, 'integrity_check': 'ok'}


def save_report(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name(path.name + f'.{os.getpid()}.partial')
    fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as f:
        os.fchmod(f.fileno(), 0o600)
        json.dump(data, f, indent=2)
        f.write('\n')
        f.flush()
        os.fsync(f.fileno())
    os.replace(staging, path)
    fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def registered_context(unit):
    if not unit or not re.fullmatch(r'glp-hermes-maintenance[-A-Za-z0-9]*\.service', unit):
        raise Refusal('registered_user_unit_required')
    if '/' + unit not in Path('/proc/self/cgroup').read_text() or not os.environ.get('INVOCATION_ID'):
        raise Refusal('not_running_in_registered_user_unit')
    parent = os.getppid()
    while parent > 1:
        row = proc(parent)
        if 'tui_gateway.entry' in row['argv'] or row['argv'][1:5] == ['-m', 'hermes_cli.main', 'gateway', 'run']:
            raise Refusal('tui_or_gateway_ancestor_forbidden')
        parent = row['ppid']


def preflight():
    checks, blockers = [], []
    def check(name, fn):
        try:
            value = fn()
            checks.append({'step': name, 'value': value})
            return value
        except (OSError, Refusal, ValueError, subprocess.SubprocessError) as e:
            blockers.append(name + ':' + (str(e) if isinstance(e, Refusal) else type(e).__name__))
    check('offline_custody', require_offline_custody)
    db = HOME / 'state.db'
    size = check('state_bytes', lambda: db.stat().st_size)
    free = check('free_bytes', lambda: shutil.disk_usage(HOME).free)
    if size is not None and free is not None:
        checks.append({'step': 'required_free_bytes', 'value': required_space(size)})
        if not has_space(size, free):
            blockers.append('insufficient_headroom')
    recent = check('recent_delegate_logs', lambda: recent_delegates(HOME / 'cache/delegation/live', time.time()))
    if recent:
        blockers.append('active_delegates_last_5_minutes')
    rows = check('default_profile_processes', census) or []
    browsers = check('protected_browser_descendants', lambda: protected_browser_descendants(rows))
    if browsers:
        blockers.append('chrome_cdp_in_shutdown_tree_do_not_touch')
    if len([r for r in rows if r['kind'] == 'gateway']) != 1:
        blockers.append('expected_one_default_gateway')
    parents = []
    for row in rows:
        if row['kind'] == 'tui':
            def parent_check(row=row):
                p = proc(row['ppid'])
                if not p['default_profile'] or not any('/ui-tui/dist/entry.js' in x for x in p['argv']):
                    raise Refusal('unknown_tui_supervisor')
                if p['state'] in ('T', 't'):
                    raise Refusal('tui_parent_already_stopped')
                return p
            p = check('tui_parent_' + str(row['pid']), parent_check)
            if p:
                parents.append(p)
    check('db_regular_no_alias', lambda: _require(db.is_file() and not db.is_symlink(), 'db_alias_or_missing'))
    current_holders = check('db_holders', lambda: holders(db)) or []
    if set(current_holders) - {r['pid'] for r in rows}:
        blockers.append('other_state_db_holders')
    check('fork_clean_branch', lambda: _require(git(FORK, 'branch', '--show-current') == 'izaiah-main'
          and not git(FORK, 'status', '--porcelain'), 'fork_not_clean_izaiah_main'))
    check('upstream_default_ref', lambda: git(FORK, 'symbolic-ref', 'refs/remotes/upstream/HEAD'))
    check('editable_installer', lambda: command([FORK / 'venv/bin/python', '-m', 'pip', '--version']))
    service = check('gateway_unit', lambda: command(['systemctl', '--user', 'show', UNIT,
                    '--property=MainPID,ExecStart,Restart,LoadState'])) or ''
    gateway = [r for r in rows if r['kind'] == 'gateway']
    if gateway and (f"MainPID={gateway[0]['pid']}\n" not in service or '/' + UNIT not in gateway[0]['cgroup']):
        blockers.append('gateway_not_bound_to_expected_user_unit')
    check('runtime_mask_absent', lambda: _require(not os.path.lexists(Path('/run/user') / str(os.getuid()) / 'systemd/user' / UNIT), 'runtime_unit_override_exists'))
    return {'checks': checks, 'blockers': blockers, 'processes': rows, 'tui_parents': parents,
            'plan': PLAN, 'expected_reclaim_bytes': 'UNKNOWN',
            'backup': 'direct sqlite3.Connection.backup: producer line 162 has hardcoded 1800s; no env/flag override'}


def _require(condition, reason):
    if not condition:
        raise Refusal(reason)
    return True


def require_offline_custody():
    """Fail closed; snapshots/log age cannot supply continuous exclusion.

    Maintained gateway drain covers gateway turns, not every TUI/cron/direct DB
    writer. Host/delegate admission locks protect child reservations only. No
    new private flock can make uncooperative writers honour offline custody.
    Keep this refusal until that runtime primitive is independently implemented.
    """
    raise Refusal('offline_custody_unavailable:authenticated_all_writer_child_drain_and_'
                  'continuous_writer_browser_admission_lease_missing;live_path_disabled')


def execute(report, report_path, unit):
    try:
        require_offline_custody()
    except Refusal as exc:
        report.update(failure=str(exc), rc=2)
        return 2  # no START/END, service command, signal or DB access
    registered_context(unit)
    db = HOME / 'state.db'
    masked, stopped, frozen = False, [], []
    rc = 1
    def step(name, fn):
        entry = {'step': name, 'started_at': datetime.now(timezone.utc).isoformat(), 'status': 'STARTED'}
        report['steps'].append(entry)
        save_report(report_path, report)
        try:
            result = fn()
            entry.update(status='OK', result=result)
            return result
        except BaseException as e:
            entry.update(status='FAILED', error=str(e) if isinstance(e, Refusal) else type(e).__name__)
            raise
        finally:
            entry['ended_at'] = datetime.now(timezone.utc).isoformat()
            save_report(report_path, report)
    def post(text):
        return json.loads(command([sys.executable, ROOT / 'scripts/post_discord_message.py',
                                   '--channel', CHANNEL, '--message', text]))
    def recover(name, fn):
        # Safety recovery precedes evidence I/O and each action is independent.
        nonlocal rc
        entry = {'step': name, 'status': 'STARTED'}
        report['steps'].append(entry)
        try:
            entry.update(result=fn(), status='OK')
        except BaseException as exc:
            entry.update(status='FAILED', error=str(exc) if isinstance(exc, Refusal) else type(exc).__name__)
            report.setdefault('recovery_failures', []).append(dict(entry))
            rc = 1
        try:
            save_report(report_path, report)
        except BaseException as exc:
            report.setdefault('recovery_reporting_failures', []).append(type(exc).__name__)
            rc = 1
    def quiet():
        _require(not recent_delegates(HOME / 'cache/delegation/live', time.time()), 'active_delegates')
        _require(has_space(db.stat().st_size, shutil.disk_usage(HOME).free), 'insufficient_headroom')
        _require(not protected_browser_descendants(census()), 'chrome_cdp_in_shutdown_tree_do_not_touch')
        return True
    def signal_if_quiet(row, sig):
        quiet()  # after STARTED receipt: reject late Chrome, never signal it
        literal_signal(row, sig)
    def offline(fn):
        quiet()
        _require(not holders(db), 'late_state_db_holder_do_not_touch')
        return fn()
    try:
        step('START', lambda: post('START GLP-110/349 offline Hermes maintenance; verified backup before shrink; reclaim UNKNOWN.'))
        current = step('preflight', preflight)
        _require(not current['blockers'], 'preflight_blocked:' + ','.join(current['blockers']))
        report['before_bytes'] = db.stat().st_size
        step('quiet_check', quiet)
        for parent in current['tui_parents']:
            step('quiet_before_freeze_' + str(parent['pid']), quiet)
            frozen.append(parent)
            step('freeze_tui_parent_' + str(parent['pid']), lambda p=parent: signal_if_quiet(p, signal.SIGSTOP))
        # Mask only the exact gateway unit's automatic Restart=always ingress.
        masked = True  # conservative intent: timeout/lost acknowledgement must recover too
        step('mask_gateway_restart', lambda: command(['systemctl', '--user', '--runtime', 'mask', UNIT]))
        for row in current['processes']:
            # Recheck immediately before EVERY literal PID stop, including after disk/service work.
            step('quiet_before_term_' + str(row['pid']), quiet)
            stopped.append(row)
            step('term_' + str(row['pid']), lambda r=row: signal_if_quiet(r, signal.SIGTERM))
        deadline = time.monotonic() + 60
        while any(same_process(r) and proc(r['pid'])['state'] != 'Z' for r in stopped):
            if time.monotonic() >= deadline:
                raise Refusal('term_drain_timeout_no_kill_escalation')
            time.sleep(0.2)
        step('offline_db_holders', lambda: _require(not holders(db), 'state_db_still_open'))
        step('quiet_before_backup', quiet)
        backup_root = BACKUPS
        _require(not backup_root.is_symlink(), 'backup_root_alias_refused')
        prepare_backup_root(backup_root)
        backup = step('verified_backup', lambda: offline(lambda: verified_backup(db, backup_root / report['run_id'])))
        report['backup'] = backup
        step('offline_before_vacuum', lambda: _require(not holders(db), 'new_state_db_holder'))
        # VACUUM may need twice the original DB size in temporary disk space.
        step('vacuum_headroom', lambda: _require(shutil.disk_usage(HOME).free >= 2 * db.stat().st_size + 2 * 1024**3, 'insufficient_vacuum_headroom_after_backup'))
        step('vacuum', lambda: offline(lambda: vacuum(db)))
        report['after_bytes'] = db.stat().st_size
        report['measured_reclaim_bytes'] = report['before_bytes'] - report['after_bytes']
        step('fork_update', lambda: update_fork(FORK))
        rc = 0
    except BaseException as e:
        report['failure'] = str(e) if isinstance(e, Refusal) else type(e).__name__
    finally:
        # Restore even after a refused/failed backup or update; never restore old DB bytes automatically.
        if masked:
            recover('unmask_gateway', lambda: command(['systemctl', '--user', '--runtime', 'unmask', UNIT]))
            recover('start_original_gateway_unit', lambda: command(['systemctl', '--user', 'start', UNIT]))
        for parent in frozen:
            try:
                recover('resume_tui_parent_' + str(parent['pid']), lambda p=parent: literal_signal(p, signal.SIGCONT))
            except BaseException as e:
                report.setdefault('tui_restore_failures', []).append({'pid': parent['pid'], 'error': type(e).__name__})
                rc = 1
        if stopped:
            try:
                def readback():
                    deadline = time.monotonic() + 90
                    while True:
                        rows = census()
                        good = all(any(r['argv'] == old['argv'] and r['cwd'] == old['cwd']
                                       and r['pid'] != old['pid'] and (old['kind'] != 'tui' or r['ppid'] == old['ppid'])
                                       for r in rows) for old in stopped)
                        if good:
                            return rows
                        if time.monotonic() >= deadline:
                            raise Refusal('restart_exact_argv_readback_failed_operator_recovery_needed')
                        time.sleep(1)
                recover('restart_exact_argv_readback', readback)
            except BaseException as e:
                report['restart_failure'] = str(e) if isinstance(e, Refusal) else type(e).__name__
                rc = 1
        report['after_bytes'] = db.stat().st_size
        report['rc'] = rc
        try:
            step('END', lambda: post(f"END GLP-110/349 rc={rc}; state.db before={report.get('before_bytes', 'UNKNOWN')} after={report['after_bytes']} bytes; reclaim={report.get('measured_reclaim_bytes', 'UNKNOWN')}; fork={next((s.get('result', {}).get('status') for s in report['steps'] if s['step'] == 'fork_update'), 'NOT_UPDATED')}."))
        except BaseException:
            report['end_post_uncertain_do_not_resend'] = True
            rc = 1
        report['rc'] = rc
        save_report(report_path, report)
    return rc


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--dry-run', action='store_true')
    mode.add_argument('--execute', action='store_true')
    parser.add_argument('--registered-unit')
    args = parser.parse_args(argv)
    report = {'run_id': datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ'),
              'dry_run': args.dry_run, 'steps': [], 'expected_reclaim_bytes': 'UNKNOWN'}
    if args.dry_run:
        if REPORT.exists() and not json.loads(REPORT.read_text()).get('dry_run', False):
            raise Refusal('preserve_prior_live_RUN_use_new_reviewed_report_namespace')
        report.update(preflight())
        report['rc'] = 2 if report['blockers'] else 0
        save_report(REPORT, report)
        print(json.dumps(report, indent=2))
        return report['rc']
    registered_context(args.registered_unit)
    require_offline_custody()  # before creating even the private singleton lock
    lock = os.open(HOME / 'cache/hermes-maintenance-window.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if REPORT.exists() and not json.loads(REPORT.read_text()).get('dry_run', False):
            raise Refusal('prior_live_RUN_requires_parent_disposition_no_replay')
        return execute(report, REPORT, args.registered_unit)
    finally:
        os.close(lock)


if __name__ == '__main__':
    raise SystemExit(main())
