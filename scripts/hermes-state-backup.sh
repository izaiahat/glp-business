#!/usr/bin/env bash
# Private incremental snapshots of the existing selected Hermes state scope.
# Registered entry remains held; installation/resumption requires separate approval.
# rsync links ONLY verified prior backups, never live files or live SQLite.
set -Eeuo pipefail
umask 077
# Gate the registered script itself, including legacy cron/manual invocations.
# Admission runs before any backup directory, lock, snapshot or retention effect.
if python3 -B "$(dirname -- "$0")/disk_alarm.py" admit-backup; then
  :
else
  exit "$?"  # A skipped backup is not a completed backup.
fi
unset DISK_ALARM_BACKUP_ADMITTED DISK_ALARM_BACKUP_TOKEN
exec nice -n 19 ionice -c 3 python3 - "${HERMES_HOME:-$HOME/.hermes}" \
  "${HERMES_BACKUP_ROOT:-/home/ubuntu/business/backups/hermes-state}" "$0" <<'PY'
import datetime as dt
from contextlib import closing
import ctypes
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time

home, root = (Path(p).absolute() for p in sys.argv[1:3])
producer = Path(sys.argv[3]).resolve(strict=True)
os.umask(0o077)
MIN_FREE_BYTES = 50 * 1024**3
RESERVE_BYTES = 1024**3  # sampled runtime guard margin, not an OS disk quota
MAX_LEDGER_BYTES = 8 * 1024**2
KEEP = 3
ORDINARY = ('config.yaml', '.env', 'auth.json', 'cron/jobs.json', 'SOUL.md', 'memories')
DATABASES = ('cron/executions.db', 'cron/deliveries.db', 'cron/notepad.db', 'cron/cron.db', 'state.db')
SNAPSHOT_NAME = re.compile(r'snapshot-\d{8}T\d{12}Z-[a-z0-9_]{8}\Z')


def log(text):
    print('['+dt.datetime.now(dt.timezone.utc).isoformat()+'] '+text, flush=True)


def no_alias(path):
    for p in (path, *path.parents):
        if p.is_symlink():
            raise ValueError('symlink path refused')


for p in (home, root):
    no_alias(p)
home, root = home.resolve(), root.resolve()
if home == root or home in root.parents or root in home.parents:
    raise ValueError('source and destination must be disjoint')
if not home.is_dir():
    raise ValueError('Hermes source missing')


def free_bytes():
    existing = root
    while not existing.exists():
        existing = existing.parent
    v = os.statvfs(existing)
    return v.f_bavail * v.f_frsize


def headroom(needed=0):
    if free_bytes() < MIN_FREE_BYTES + RESERVE_BYTES + needed:
        raise TimeoutError('insufficient headroom; 50GiB floor plus staging/reserve required')


# Even root/lock metadata must be admitted before the first filesystem write.
missing_parents = sum(not p.exists() for p in (root, *root.parents))
try:
    headroom((missing_parents + 1) * 4096)
except TimeoutError as exc:
    log('SKIP '+str(exc))
    sys.exit(75)
root.mkdir(parents=True, exist_ok=True, mode=0o700)
root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
if os.fstat(root_fd).st_uid != os.getuid():
    raise ValueError('foreign backup root')
os.fchmod(root_fd, 0o700)
lock_fd = os.open(root / '.backup.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
lock_stat = os.fstat(lock_fd)
if not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_nlink != 1 or lock_stat.st_uid != os.getuid():
    raise ValueError('unsafe backup lock')
os.fchmod(lock_fd, 0o600)
try:
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    log('SKIP backup owner busy')
    sys.exit(75)


# Alarm stop authority is this live kernel flock plus PID/start identity, not a
# stale PID file. No credentials/source contents enter the private lock record.
owner = {'schema': 1, 'pid': os.getpid(),
         'start_ticks': int(Path('/proc/self/stat').read_text().rsplit(')', 1)[1].split()[19])}
os.ftruncate(lock_fd, 0)
os.write(lock_fd, json.dumps(owner).encode())
os.fsync(lock_fd)


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024**2), b''):
            headroom()
            h.update(chunk)
    return h.hexdigest()


def private_write(path, data=b''):
    headroom(len(data) + 4096)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as f:
        os.fchmod(f.fileno(), 0o600)
        headroom(len(data))
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def inventory(base, selected):
    files, dirs, missing = {}, set(), []
    def visit(rel):
        headroom()
        path = base / rel
        no_alias(path)
        s = path.lstat()
        if stat.S_ISDIR(s.st_mode):
            dirs.add(rel)
            for child in sorted(path.iterdir()):
                visit(child.relative_to(base).as_posix())
        elif stat.S_ISREG(s.st_mode):
            # Unknown SQLite in selected ordinary data is a refusal, not an
            # exclusion or a raw-copy fallback. Sidecars must never reach rsync.
            with path.open('rb') as f:
                header = f.read(16)
            if (header == b'SQLite format 3\x00' or path.suffix.lower() in ('.db', '.sqlite', '.sqlite3')
                    or path.name.endswith(('-wal', '-shm', '-journal'))):
                raise ValueError('unexpected SQLite in ordinary scope')
            sha = digest(path)
            after = path.stat()
            if (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns) != (
                    after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ValueError('ordinary source changed during inventory')
            files[rel] = {'bytes': s.st_size, 'sha256': sha, 'mtime_ns': s.st_mtime_ns}
        else:
            raise ValueError('non-regular selected source')
    for rel in selected:
        no_alias(base / rel)
        if not (base / rel).exists():
            missing.append(rel)
        else:
            visit(rel)
    return files, sorted(dirs), missing


def validate_snapshot(path):
    """Only this version's sealed, complete, exact-member snapshots are eligible."""
    no_alias(path)
    if not path.is_dir():
        raise ValueError('unsafe snapshot directory')
    for p in path.rglob('*'):
        if p.is_symlink() or not (p.is_dir() or p.is_file()):
            raise ValueError('unsafe snapshot member')
        if p.stat().st_uid != os.getuid() or p.stat().st_mode & 0o077:
            raise ValueError('non-private snapshot member')
    raw = (path / 'backup-coverage.json').read_bytes()
    if (path / 'COMPLETE').read_text().strip() != hashlib.sha256(raw).hexdigest():
        raise ValueError('invalid completion seal')
    m = json.loads(raw)
    if m['schema'] != 2 or m['status'] != 'COMPLETE' or m['selected_scope'] != list(ORDINARY + DATABASES):
        raise ValueError('unsupported snapshot coverage')
    actual = {p.relative_to(path / 'data').as_posix() for p in (path / 'data').rglob('*') if p.is_file()}
    if actual != set(m['files']):
        raise ValueError('snapshot member set mismatch')
    if set(p.name for p in path.iterdir()) != {'data', 'backup-coverage.json', 'COMPLETE'}:
        raise ValueError('unexpected snapshot root member')
    for rel, record in m['files'].items():
        if rel not in actual or Path(rel).is_absolute() or '..' in Path(rel).parts:
            raise ValueError('invalid snapshot member name')
        p = path / 'data' / rel
        if p.stat().st_size != record['bytes'] or digest(p) != record['sha256']:
            raise ValueError('snapshot content mismatch')
        source = home / rel
        if source.exists() and p.samefile(source):
            raise ValueError('backup aliases live source')
    return m


def copy_child_guard():
    # Linux clears PDEATHSIG on fork, so set it in the immediate rsync child,
    # before exec. Check the fork-to-prctl race against the captured guard PID.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
        os._exit(126)
    if os.getppid() != guard_pid:
        os._exit(126)


guard_pid = os.getpid()


def copy_ordinary(home, data, files, dirs, previous):
    parents = set(dirs)
    for rel in files:
        parents.update(str(p) for p in Path(rel).parents if str(p) != '.')
    for rel in sorted(parents, key=lambda r: len(Path(r).parts)):
        p = data / rel
        headroom(4096)
        p.mkdir(mode=0o700, exist_ok=True)
        p.chmod(0o700)
    # Explicit list, no recursion/discovery, symlinks/devices/specials disabled.
    # No --inplace/--append/-H: previous snapshots are immutable link targets.
    argv = ['rsync', '-t', '--checksum', '--relative', '--from0', '--files-from=-',
            '--no-links', '--no-devices', '--no-specials', '--perms', '--chmod=F600,D700',
            '--bwlimit=10240']
    if previous:
        argv += ['--link-dest='+str(previous / 'data')]
    argv += [str(home)+'/', str(data)+'/']
    # A private file avoids a pipe deadlock with a large selected-name inventory.
    headroom(4096 + sum(len(os.fsencode(r)) + 1 for r in files))
    with tempfile.TemporaryFile(dir=root) as listing:
        for rel in files:
            name = os.fsencode(rel) + b'\0'
            headroom(len(name) + 4096)
            listing.write(name)
        listing.flush()
        listing.seek(0)
        proc = subprocess.Popen(argv, stdin=listing, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                preexec_fn=copy_child_guard, start_new_session=True)
        try:
            deadline = time.monotonic() + 900 + sum(r['bytes'] for r in files.values()) / (10 * 1024**2)
            while proc.poll() is None:
                headroom()
                if time.monotonic() >= deadline:
                    raise TimeoutError('ordinary copy deadline')
                time.sleep(0.05)
            if proc.returncode:
                raise ValueError('rsync failed; no publication')
            headroom()
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)  # only this invocation's private copy tree
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()


def run():
    # The sole writer is locked and its prior copy tree dies with its guard.
    # Reclaim only this producer's private abandoned staging namespaces.
    for stale in sorted(root.glob('.snapshot-*')):
        no_alias(stale)
        if not stale.is_dir() or stale.stat().st_uid != os.getuid():
            raise ValueError('unsafe stale staging directory')
        shutil.rmtree(stale)
        os.fsync(root_fd)
        log('reclaimed stale staging='+stale.name)
    headroom()  # before inventory, SQLite reads or any staging allocation
    previous_set = sorted(p for p in root.iterdir() if SNAPSHOT_NAME.fullmatch(p.name)
                          and (p / 'COMPLETE').exists())
    previous = previous_set[-1] if previous_set else None
    prior = validate_snapshot(previous) if previous else None
    files, dirs, missing = inventory(home, ORDINARY)
    if prior and set(missing) - set(prior['ordinary']['missing']):
        raise ValueError('previously covered selected source disappeared')
    estimates = {}
    for rel in DATABASES:
        source = home / rel
        no_alias(source)
        for suffix in ('-wal', '-shm', '-journal'):
            no_alias(Path(str(source)+suffix))
        if not source.exists():
            if prior and rel in prior['files']:
                raise ValueError('previously covered database disappeared')
            continue
        if not source.is_file():
            raise ValueError('unsafe database source')
        if rel == 'cron/cron.db' and source.stat().st_size == 0:
            estimates[rel] = 0
            continue
        with closing(sqlite3.connect(source.as_uri()+'?mode=ro', uri=True, timeout=0.0)) as src:
            estimates[rel] = src.execute('pragma page_size').fetchone()[0] * src.execute('pragma page_count').fetchone()[0]
        if rel != 'state.db' and estimates[rel] > MAX_LEDGER_BYTES:
            raise ValueError('ledger exceeds bounded staging scope')
    if not files and not dirs and not estimates:
        raise ValueError('no Hermes state found; refusing empty snapshot')
    # Reserve complete online DB images (no compression-ratio assumption),
    # ordinary files that cannot link, plus one GiB of live growth/sampled margin.
    new_bytes = sum(r['bytes'] for rel, r in files.items()
                    if not prior or prior['ordinary']['files'].get(rel) != r)
    metadata_bytes = (len(dirs) + 2 * len(files) + 2 * len(estimates) + 6) * 4096
    listing_bytes = sum(len(os.fsencode(r)) + 1 for r in files)
    headroom(new_bytes + sum(estimates.values()) + metadata_bytes + listing_bytes)
    now = dt.datetime.now(dt.timezone.utc)
    manifest = {'schema': 2, 'status': 'COMPLETE', 'at': now.isoformat(),
        'producer_sha256': hashlib.sha256(producer.read_bytes()).hexdigest(),
        'minimum_free_bytes': MIN_FREE_BYTES, 'keep_complete': KEEP,
        'selected_scope': list(ORDINARY + DATABASES),
        'excluded_scope': 'All other paths, including cache, logs, sessions, skills, profiles and rebuildable indexes; unchanged selected scope, not a full-home backup.',
        'snapshot_api': 'sqlite3.Connection.backup (SQLite online backup API)',
        'sqlite_version': sqlite3.sqlite_version,
        'consistency': 'independent per-database snapshots; not a cross-store checkpoint',
        'ordinary': {'files': files, 'directories': dirs, 'missing': missing,
                     'count': len(files), 'bytes': sum(r['bytes'] for r in files.values()), 'new_bytes': new_bytes},
        'ledgers': {}, 'session_store': {'path': 'state.db', 'status': 'MISSING'}, 'files': {}}
    with tempfile.TemporaryDirectory(prefix='.snapshot-', dir=root) as stage_name:
        stage = Path(stage_name)
        stage.chmod(0o700)
        data = stage / 'data'
        headroom(4096)
        data.mkdir(mode=0o700)
        data.chmod(0o700)
        copy_ordinary(home, data, files, dirs, previous)
        if inventory(home, ORDINARY) != (files, dirs, missing):
            raise ValueError('ordinary source changed during copy')
        copied, copied_dirs, _ = inventory(data, ORDINARY)
        if copied != files or copied_dirs != dirs:
            raise ValueError('ordinary snapshot count/size/hash mismatch')
        manifest['files'].update(files)
        for rel in DATABASES:
            is_state = rel == 'state.db'
            source = home / rel
            if rel not in estimates:
                if source.exists():
                    raise ValueError('database appeared after preflight')
                if not is_state:
                    manifest['ledgers'][rel] = {'status': 'MISSING'}
                continue
            no_alias(source)
            snapshot = data / rel
            headroom(4096)
            snapshot.parent.mkdir(mode=0o700, exist_ok=True)
            private_write(snapshot)
            if rel == 'cron/cron.db' and source.stat().st_size == 0:
                record = {'status': 'EMPTY_LEGACY_PLACEHOLDER', 'bytes': 0, 'sha256': digest(snapshot)}
            else:
                for p in (source, Path(str(source)+'-wal')):
                    no_alias(p)
                    if not is_state and p.exists() and p.stat().st_size > MAX_LEDGER_BYTES:
                        raise ValueError('ledger exceeds bounded staging scope')
                src = dst = None
                try:
                    src = sqlite3.connect(source.as_uri()+'?mode=ro', uri=True, timeout=0.0)
                    if is_state:
                        if src.execute('pragma journal_mode').fetchone()[0] != 'wal':
                            raise ValueError('current state capture requires WAL for nonblocking snapshot')
                        wal = Path(str(source)+'-wal')
                        wal_before = wal.stat().st_size if wal.exists() else 0
                        src.execute('begin')
                        src.execute('select count(*) from sqlite_schema').fetchone()
                    page_size = src.execute('pragma page_size').fetchone()[0]
                    logical_bytes = page_size * src.execute('pragma page_count').fetchone()[0]
                    limit = logical_bytes + 1024**3 if is_state else MAX_LEDGER_BYTES
                    if logical_bytes > limit:
                        raise ValueError('logical ledger exceeds staging bound')
                    headroom(logical_bytes)
                    dst = sqlite3.connect(snapshot)
                    copy_started = time.monotonic()
                    pace_bytes_per_second = 10 * 1024**2
                    copy_budget_seconds = max(1800, logical_bytes / pace_bytes_per_second + 900) if is_state else 15
                    deadline = copy_started + copy_budget_seconds
                    heartbeat_at = copy_started
                    def progress(status, remaining, total):
                        nonlocal heartbeat_at
                        elapsed = time.monotonic() - copy_started
                        if is_state and (time.monotonic() - heartbeat_at >= 30 or status == sqlite3.SQLITE_DONE):
                            log('snapshot progress '+rel+': copied_bytes='+str((total-remaining)*page_size)
                                +' total_bytes='+str(total*page_size)+' wall_seconds='+str(round(elapsed, 3))
                                +' budget_seconds='+str(round(copy_budget_seconds, 3)))
                            heartbeat_at = time.monotonic()
                        reason = None
                        if time.monotonic() >= deadline:
                            reason = 'DEADLINE'
                        elif total * page_size > limit:
                            reason = 'SIZE'
                        elif free_bytes() < MIN_FREE_BYTES + RESERVE_BYTES + remaining * page_size:
                            reason = 'HEADROOM'
                        elif is_state and wal.exists() and wal.stat().st_size > wal_before + 1024**3:
                            reason = 'WAL_GROWTH'
                        if reason:
                            raise TimeoutError('bounded ledger snapshot refused: '+rel+' reason='+reason)
                        if is_state and status in (sqlite3.SQLITE_OK, sqlite3.SQLITE_DONE):
                            expected = (total-remaining) * page_size / pace_bytes_per_second
                            delay = expected - (time.monotonic()-copy_started)
                            if delay > 0:
                                time.sleep(delay)
                    src.backup(dst, pages=max(1, 1024**2 // page_size) if is_state else 256,
                               progress=progress, sleep=0.1)
                    src.close()  # release pinned generation before validation
                    src = None
                    dst.execute('pragma journal_mode=delete')  # private destination only
                    headroom()
                    if [r[0] for r in dst.execute('pragma quick_check')] != ['ok'] or snapshot.stat().st_size > limit:
                        raise ValueError('invalid/oversize ledger snapshot')
                finally:
                    if dst is not None:
                        dst.close()
                    if src is not None:
                        src.close()
                record = {'status': 'SNAPSHOT_OK', 'quick_check': 'ok', 'bytes': snapshot.stat().st_size,
                          'sha256': digest(snapshot)}
            # A byte-identical ONLINE snapshot may share a prior backup inode,
            # but it still required full staging/headroom. Never link a source DB.
            if prior and all(prior['files'].get(rel, {}).get(k) == record[k] for k in ('bytes', 'sha256')):
                snapshot.unlink()
                os.link(previous / 'data' / rel, snapshot, follow_symlinks=False)
            manifest['files'][rel] = {k: record[k] for k in ('bytes', 'sha256')}
            if is_state:
                manifest['session_store'] = {'path': rel, **record}
            else:
                manifest['ledgers'][rel] = record
        # Persist all data before the seal and directory rename. Publication is
        # all-or-nothing; interrupted directories never count toward retention.
        for rel in manifest['files']:
            with (data / rel).open('rb') as f:
                if stat.S_IMODE(os.fstat(f.fileno()).st_mode) != 0o600:
                    raise ValueError('non-private snapshot data')
                os.fsync(f.fileno())
        coverage = json.dumps(manifest, indent=2).encode()+b'\n'
        private_write(stage / 'backup-coverage.json', coverage)
        private_write(stage / 'COMPLETE', hashlib.sha256(coverage).hexdigest().encode()+b'\n')
        validate_snapshot(stage)
        for p in [*sorted((p for p in stage.rglob('*') if p.is_dir()), key=lambda p: len(p.parts), reverse=True), stage]:
            fd = os.open(p, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        headroom()
        out = root / ('snapshot-'+now.strftime('%Y%m%dT%H%M%S%fZ')+'-'+stage.name.removeprefix('.snapshot-'))
        if out.exists() or out.is_symlink():
            raise ValueError('snapshot destination collision')
        stage.rename(out)
        os.fsync(root_fd)
    log('COMPLETE snapshot='+out.name+' ordinary_count='+str(len(files))
        +' ordinary_bytes='+str(manifest['ordinary']['bytes'])+' new_ordinary_bytes='+str(new_bytes))
    # Only this format's COMPLETE snapshots, only after durable publication.
    # Legacy tar/zstd backups and unknown directories are NEVER pruned.
    for old in (previous_set + [out])[:-KEEP]:
        no_alias(old)
        if not old.is_dir() or old.stat().st_uid != os.getuid():
            raise ValueError('unsafe retirement directory')
        try:
            validate_snapshot(old)
        except TimeoutError:
            raise  # Pressure is not corruption.
        except (ValueError, KeyError, TypeError, OSError):
            log('retiring corrupt COMPLETE snapshot='+old.name)
        shutil.rmtree(old)
        os.fsync(root_fd)
        log('pruned COMPLETE snapshot='+old.name)


try:
    run()
except TimeoutError as exc:
    log('SKIP '+str(exc))
    sys.exit(75)
except Exception as exc:
    # Do not put credential contents or subprocess stderr in public cron logs.
    log('FAILED '+type(exc).__name__+'; no incomplete snapshot admitted')
    raise
finally:
    os.close(lock_fd)
    os.close(root_fd)
PY
