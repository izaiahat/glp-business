#!/usr/bin/env python3
"""Automatic reclaim classes for the registered large sweep (2026-10-09).

Each class reuses the existing custody guards from host_disk_cold_classes:
protected()/custody_pins() (live symlink targets), dirty-worktree, privileged
/proc reference veto, lsof open-file veto, database-payload veto, identity
re-snapshot before effect. External references (open PRs, open Linear issues,
live release, R8, worktrees, PREPARED files) are added as pins; an incomplete
reference inventory raises, so no new class runs without it.
"""
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time

BUS = Path('/home/ubuntu/business')
SCRATCH = Path('/home/ubuntu/.hermes/cache/scratch')
TMP = Path('/tmp')
SITE = BUS / 'site-system/flagship-v2'
REGISTRY_RUNS = BUS / 'reports/glp-clinic-scout/affiliate-machine/registry-site-sync'
CONTENT_BASE = BUS / 'reports/glp-clinic-scout/seo-machine/current-content-base.json'
PATH_RE = re.compile(r'(?:/home/ubuntu/(?:business/|\.hermes/cache/scratch/)|/tmp/|(?:reports|site-system|\.worktrees|worktrees)/)'
                     r'[^\s"\'<>`,;\)\]\}]+')
# /tmp names owned by live system/browser/TUI processes; never candidates.
TMP_NEVER = ('systemd-private-', 'snap-private-', '.X', '.ICE', '.font', '.XIM', '.Test-unix', 'tmux-',
             'com.google.Chrome', '.com.google.Chrome', '.org.chromium', 'claude-', 'hsperfdata',
             'pyright', 'vscode', 'ssh-', 'pip-', 'node-compile-cache')
R8_RE = re.compile(r'(?:^|[-_.])r8(?:$|[-_.])', re.I)


def text_paths(text):
    out = set()
    for s in PATH_RE.findall(text):
        s = s.rstrip("'/:.")
        out.add(Path(s) if s.startswith('/') else BUS / s)
    return out


def worktree_pins(roots=(BUS / '.worktrees', BUS / 'worktrees')):
    """Every registered worktree of every repo that owns a business worktree (any location)."""
    pins = set()
    for root in roots:
        if not root.is_dir():
            continue
        pins.add(root)
        for wt in root.iterdir():
            marker = wt / '.git'
            if not marker.is_file():
                continue
            gitdir = Path(marker.read_text().split('gitdir:', 1)[-1].strip())
            common = gitdir.parent.parent  # <repo>/.git/worktrees/<name> -> <repo>/.git
            pins.add(common.parent)
            for record in (common / 'worktrees').glob('*/gitdir'):
                pins.add(Path(record.read_text().strip()).parent)
    return pins


def prepared_pins(roots=(BUS / 'reports', SITE), timeout=180):
    pins = set()
    for root in roots:
        r = subprocess.run(['find', str(root), '-xdev', '-maxdepth', '7', '-type', 'f', '-name', 'PREPARED*.json'],
                           capture_output=True, text=True, timeout=timeout)
        if r.returncode not in (0, 1):
            raise RuntimeError('PREPARED inventory incomplete')
        for line in r.stdout.splitlines():
            p = Path(line)
            pins.add(p.parent)
            pins.update(text_paths(p.read_text(errors='replace')))
    return pins


def pr_pins(repos=('izaiahat/glp-business', 'izaiahat/glp-site-candidate')):
    pins = set()
    for repo in repos:
        r = subprocess.run(['gh', 'pr', 'list', '-R', repo, '--state', 'open', '--limit', '200',
                            '--json', 'number,headRefName,body,title'], capture_output=True, text=True, timeout=60)
        if r.returncode:
            raise RuntimeError('open PR inventory unavailable')
        for pr in json.loads(r.stdout):
            pins.update(text_paths((pr.get('body') or '') + ' ' + (pr.get('title') or '')))
    return pins


def linear_pins(client=BUS / 'scripts/linear_client.py'):
    r = subprocess.run([sys.executable, str(client), 'list', 'GLP'], capture_output=True, text=True, timeout=120)
    if r.returncode:
        raise RuntimeError('Linear inventory unavailable')
    pins = set()
    for issue in json.loads(r.stdout):
        if issue['state']['type'] in ('completed', 'canceled'):
            continue
        g = subprocess.run([sys.executable, str(client), 'get', issue['identifier']],
                           capture_output=True, text=True, timeout=60)
        if g.returncode:
            raise RuntimeError('Linear issue unavailable')
        pins.update(text_paths(json.loads(g.stdout).get('description') or ''))
    return pins


def live_release_pins():
    pins = set()
    base = json.loads(CONTENT_BASE.read_text())
    pins.add(Path(base['release_root']).parent.parent)  # whole live registry run dir
    for name in ('current', 'live'):
        p = SITE / name
        if p.exists() or p.is_symlink():
            pins.add(p.resolve(strict=False))
    releases = SITE / 'releases'
    if releases.is_dir():
        for p in releases.iterdir():
            if p.is_symlink():
                pins.add(p.resolve(strict=False))
            if p.name.startswith('accepted-prepared-'):
                pins.add(p)  # immutable bases a live release may hardlink
    return pins


def r8_pins(roots=(BUS, BUS / 'reports/ops', SITE / 'candidates', SITE / 'releases', SITE / 'qa',
                   SITE / 'work', SCRATCH, TMP)):
    pins = set()
    for root in roots:
        if root.is_dir():
            pins.update(p for p in root.iterdir() if R8_RE.search(p.name) or 'glp347' in p.name)
    return pins


LAYOUT = {Path('/'), Path('/home/ubuntu'), Path('/home/ubuntu/.hermes'), Path('/home/ubuntu/.hermes/cache'), BUS,
          BUS / 'reports', BUS / 'reports/ops', BUS / 'reports/glp-clinic-scout', BUS / 'site-system', SITE,
          SITE / 'candidates', SITE / 'qa', SITE / 'releases', SITE / 'work', BUS / '.worktrees', BUS / 'worktrees',
          SCRATCH, TMP, REGISTRY_RUNS, REGISTRY_RUNS.parent}


def external_pins():
    """All live reference sources; any failure propagates (no effects without it).

    Bare layout roots (a reference to '/tmp' or 'reports/ops' itself) name no
    specific artifact; they are retained, never retired, but do not pin children.
    """
    pins = set()
    for source in (worktree_pins, prepared_pins, pr_pins, linear_pins, live_release_pins, r8_pins):
        pins |= source()
    return pins - LAYOUT


def _guarded_remove(api, m, p, pins, deadline, rule, dry_run, extra_check=None, cutoff=None, check_atime=False):
    """Shared effect path: every existing veto, then a single bounded removal."""
    from host_disk_large_classes import snapshot, lsof_inactive
    from host_disk_payload_guard import reject_database_payloads
    row = {'rule': rule, 'path': str(p), 'bytes_freed': 0}
    try:
        if p.is_symlink() or p.resolve() != p.absolute() or m.protected(p, pins):
            raise ValueError('scope/protection')
        if extra_check:
            extra_check(p)
        if not m.inactive(api, p):
            raise ValueError('dirty/active')
        before, amount = snapshot(p, deadline)
        reject_database_payloads(p, before)
        for r in before:
            q = p / r[0]; s = q.lstat()
            stamp = max(s.st_mtime, s.st_ctime, s.st_atime if check_atime and stat.S_ISREG(s.st_mode) else 0)
            if cutoff is not None and stamp > cutoff:
                raise ValueError('young descendant')
        fresh = m.custody_pins(pins)
        if any(m.protected(p / r[0], fresh) for r in before):
            raise ValueError('new live symlink custody')
        if not lsof_inactive(p) or not m.inactive(api, p) or snapshot(p, deadline)[0] != before:
            raise ValueError('reference/identity')
        row['eligible_bytes'] = amount
        if dry_run:
            row['status'] = 'ELIGIBLE'
        elif api.remove_disposable(p, deadline=deadline) and not p.exists():
            row.update(status='REMOVED', bytes_freed=amount)
        else:
            row['status'] = 'PARTIAL_UNKNOWN'
    except (OSError, ValueError, TimeoutError) as e:
        row.update(status='REFUSED', reason=str(e))
    return row


def rebuildable_node_modules(api, m, targets, *, pins, deadline, dry_run=True, reports_root=BUS / 'reports'):
    """node_modules under reports/ that git ignores and never tracks, with a lockfile to rebuild it.

    The checkout may be dirty elsewhere; only the ignored dependency tree is removed.
    """
    def rebuildable(p):
        if reports_root not in p.parents or p.name != 'node_modules':
            raise ValueError('scope/protection')
        if not any((p.parent / f).is_file() for f in ('package-lock.json', 'npm-shrinkwrap.json', 'pnpm-lock.yaml', 'yarn.lock')):
            raise ValueError('no lockfile')
        g = ['git', '-c', 'safe.directory=*', '-c', 'core.fsmonitor=false', '-C', str(p.parent)]
        top = subprocess.run(g + ['rev-parse', '--show-toplevel'], capture_output=True, text=True, timeout=20)
        if top.returncode == 0:
            tracked = subprocess.run(g + ['ls-files', '--', 'node_modules'], capture_output=True, text=True, timeout=60)
            ignored = subprocess.run(g + ['check-ignore', '-q', 'node_modules/'], capture_output=True, timeout=20)
            if tracked.returncode or tracked.stdout.strip() or ignored.returncode != 0:
                raise ValueError('tracked or not ignored')
        elif m.dirty_worktree(p):
            raise ValueError('dirty/active')
    cutoff = time.time() - 3 * 86400
    return [_guarded_remove(api, m, Path(t), pins, deadline, 'rebuildable_report_node_modules', dry_run,
                            rebuildable, cutoff) for t in targets if time.monotonic() < deadline]


def stale_tmp(api, m, *, pins, deadline, dry_run=True, root=TMP, min_age=86400):
    """Top-level /tmp directories idle >24h (atime/mtime/ctime of every member), not held open."""
    cutoff = time.time() - min_age
    def scope(p):
        if p.parent != root or p.name.startswith(TMP_NEVER) or not p.is_dir():
            raise ValueError('scope/protection')
        if m.dirty_worktree(p):
            raise ValueError('dirty/active')
    rows = []
    for p in sorted(root.iterdir()):
        if time.monotonic() > deadline:
            break
        try:
            s = p.lstat()
        except OSError:
            continue
        if max(s.st_mtime, s.st_ctime) > cutoff or p.name.startswith(TMP_NEVER) or p.is_symlink() or not p.is_dir():
            continue
        rows.append(_guarded_remove(api, m, p, pins, deadline, 'stale_tmp_24h', dry_run, scope, cutoff, check_atime=True))
    return rows


def site_cold_archive(api, m, report, *, pins, deadline, dry_run=True, site=SITE, min_bytes=128 * 1024 ** 2):
    """Old (>7d) site candidates/releases/work -> verified tar.zst, then retire members.

    Reuses cold_archive (exact member set + SHA256 re-verified from the archive,
    hardlink sets reclaimed only when every link is selected, durable ledger).
    qa/ remains a policy-protected root; sealed-*, live release, accepted-prepared
    bases, R8 and every external pin are refused by protected().
    """
    rows = []
    for kind in ('candidates', 'releases', 'work'):
        parent = site / kind
        if not parent.is_dir():
            continue
        for p in sorted(parent.iterdir()):
            if time.monotonic() + 120 > deadline:
                return rows
            if p.is_symlink() or not p.is_dir() or m.protected(p, pins):
                continue
            if max(p.lstat().st_mtime, p.lstat().st_ctime) > time.time() - 7 * 86400:
                continue
            dest = Path(report) / 'archives' / f'site-{kind}-{p.name}.tar.zst'
            try:
                rows.append(m.cold_archive(api, p, dest, pins=pins, dry_run=dry_run, deadline=deadline,
                                           min_bytes=min_bytes, reserve_bytes=1024 ** 3))
            except Exception as e:
                rows.append({'rule': 'cold_evidence_archive', 'path': str(p), 'archive': str(dest),
                             'status': 'ERROR_PRESERVED_OR_PARTIAL', 'reason': str(e), 'bytes_freed': 0, 'ok': False})
    return rows


def registry_clones(api, m, *, pins, dry_run=True, root=REGISTRY_RUNS):
    """Producer-owned retention for hourly registry-site-sync clones (same function the job runs)."""
    sys.path.insert(0, str(BUS / 'scripts'))
    import glp_affiliate_sync_job as job
    if dry_run or not root.is_dir():
        return []
    if m.protected(root, tuple(pins)):
        return [{'rule': 'registry_sync_clone_retention', 'path': str(root), 'status': 'REFUSED',
                 'reason': 'scope/protection', 'bytes_freed': 0}]
    inner = [p for p in m.custody_pins(tuple(pins)) if root in p.parents]
    import fcntl
    with (root / 'run.lock').open('a') as lock:  # the producer's own singleton
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return [{'rule': 'registry_sync_clone_retention', 'path': str(root), 'status': 'DEFERRED_BUSY', 'bytes_freed': 0}]
        rows = job.retire_disposable_clones(root, pins=inner, active=lambda p: not m.inactive(api, p))
    retired = [r for r in rows if r['status'] == 'RETIRED']
    return [{'rule': 'registry_sync_clone_retention', 'path': str(root), 'status': 'COMPLETED',
             'retired': len(retired), 'kept': len(rows) - len(retired), 'runs': rows,
             'bytes_freed': sum(r.get('bytes_freed', 0) for r in retired)}]
