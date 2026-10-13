#!/usr/bin/env bash
# Daily private config/auth/jobs/SOUL/memories + WAL-safe session/cron backup.
# Registered entry: 0 4 * * * bash /home/ubuntu/business/scripts/hermes-state-backup.sh
# SQLite online backup API only: never raw tar of live ledgers/WAL/state.db.
# Current state.db uses online backup, never quiesced archival or live raw copy.
# Same-day atomic replacement and existing 14-day retention scope preserved.
# state.db is published separately as zstd hermes-state-db-YYYYMMDD.sqlite.zst;
# only the newest 2 such copies are kept (older pruned before a new snapshot).
set -Eeuo pipefail
umask 077
exec nice -n 19 ionice -c 3 python3 - "${HERMES_HOME:-$HOME/.hermes}" \
  "${HERMES_BACKUP_ROOT:-/home/ubuntu/business/backups/hermes-state}" \
  "${HERMES_BACKUP_RETENTION_DAYS:-14}" "$0" <<'PY'
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import time

home, root = (Path(p).absolute() for p in sys.argv[1:3])
retention = int(sys.argv[3])
if retention < 0:
    raise ValueError('negative retention')
producer = Path(sys.argv[4]).resolve(strict=True)
os.umask(0o077)
# Refuse alias/foreign output directories before changing their permissions.
for p in (root, *root.parents):
    if p.is_symlink():
        raise ValueError('symlink backup root')
root.mkdir(parents=True, exist_ok=True, mode=0o700)
root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
try:
    if os.fstat(root_fd).st_uid != os.getuid():
        raise ValueError('foreign backup root')
    os.fchmod(root_fd, 0o700)
finally:
    os.close(root_fd)
lock_fd = os.open(root / '.backup.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
lock_stat = os.fstat(lock_fd)
if not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_nlink != 1 or lock_stat.st_uid != os.getuid():
    raise ValueError('unsafe backup lock')
os.fchmod(lock_fd, 0o600)
# One producer owns publication/retention; never replace an in-flight backup.
fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
now = dt.datetime.now(dt.timezone.utc)
out = root / ('hermes-state-'+now.strftime('%Y%m%d')+'.tar.gz')
if out.is_symlink() or (out.exists() and not out.is_file()):
    raise ValueError('unsafe backup output')

STATE_KEEP = 2
state_out = root / ('hermes-state-db-'+now.strftime('%Y%m%d')+'.sqlite.zst')
if state_out.is_symlink() or (state_out.exists() and not state_out.is_file()):
    raise ValueError('unsafe state backup output')

def state_copies():
    return sorted(p for p in root.glob('hermes-state-db-????????.sqlite.zst')
                  if p.is_file() and not p.is_symlink())

def log(text):
    print('['+dt.datetime.now(dt.timezone.utc).isoformat()+'] '+text, flush=True)

def private_member(member):
    if not (member.isfile() or member.isdir()):
        raise ValueError('non-regular backup member: '+member.name)
    member.mode = 0o700 if member.isdir() else 0o600
    return member

# Fixed scope, no DB discovery. Growth/capacity refuses, never raw-copy fallback.
MAX_LEDGER_BYTES = 8 * 1024**2
MIN_FREE_BYTES = 2 * 1024**3

def free_bytes():
    v = os.statvfs(root)
    return v.f_bavail * v.f_frsize

def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024**2), b''):
            h.update(chunk)
    return h.hexdigest()

class HeadroomWriter:
    def __init__(self, target):
        self.target = target
    def write(self, data):
        if free_bytes() < MIN_FREE_BYTES + len(data):
            raise ValueError('archive write headroom refused')
        return self.target.write(data)

manifest = {'schema': 1, 'at': now.isoformat(),
    'producer_sha256': hashlib.sha256(producer.read_bytes()).hexdigest(),
    'sqlite_version': sqlite3.sqlite_version,
    'snapshot_api': 'sqlite3.Connection.backup (SQLite online backup API)',
    'consistency': 'independent per-database snapshots; not a cross-store checkpoint',
    'ledgers': {}, 'session_store': {'path': 'state.db', 'status': 'MISSING'}}
log('hermes-state-backup start (out='+str(out)+')')
with tempfile.TemporaryDirectory(prefix='.snapshot-', dir=root) as stage_name:
    stage = Path(stage_name)  # exclusive private 0700 staging
    snapshots = {}
    for name in ('executions.db', 'deliveries.db', 'notepad.db', 'cron.db', 'state.db'):
        is_state = name == 'state.db'
        rel = name if is_state else 'cron/'+name
        records = {'state.db': manifest['session_store']} if is_state else manifest['ledgers']
        source = home / rel
        if not source.exists():
            if source.is_symlink():
                raise ValueError('dangling ledger symlink: '+rel)
            records[rel] = {'status': 'MISSING'}
            continue
        if source.is_symlink() or source.parent.is_symlink() or not source.is_file():
            raise ValueError('unsafe ledger source: '+rel)
        snapshot = stage / name
        snapshot.touch(mode=0o600, exist_ok=False)
        if name == 'cron.db' and source.stat().st_size == 0:
            # Historical placeholder preserved, never certified as a valid database.
            manifest['ledgers'][rel] = {'status': 'EMPTY_LEGACY_PLACEHOLDER', 'bytes': 0}
            snapshots[rel] = snapshot
            continue
        for p in (source, Path(str(source)+'-wal')):
            if not is_state and p.exists() and p.stat().st_size > MAX_LEDGER_BYTES:
                raise ValueError('ledger exceeds bounded staging scope: '+rel)
        src = dst = None
        try:
            src = sqlite3.connect(source.as_uri()+'?mode=ro', uri=True, timeout=0.0)
            if is_state:
                if src.execute('pragma journal_mode').fetchone()[0] != 'wal':
                    raise ValueError('current state capture requires WAL for nonblocking snapshot')
                wal = Path(str(source)+'-wal')
                wal_before = wal.stat().st_size if wal.exists() else 0
                # Pin a coherent WAL read generation. Without this, unrelated
                # writer commits can restart a multi-GiB online backup forever.
                # WAL writers remain admitted; only checkpoint truncation waits.
                src.execute('begin')
                src.execute('select count(*) from sqlite_schema').fetchone()
            page_size = src.execute('pragma page_size').fetchone()[0]
            logical_bytes = page_size * src.execute('pragma page_count').fetchone()[0]
            # Reserve full staging + worst-case tar/gzip output (1% overhead)
            # plus 2 GiB for ordinary live growth. No source checkpoint or pause.
            # Permit up to 1 GiB of concurrent committed growth, pre-reserved.
            limit = logical_bytes + 1024**3 if is_state else MAX_LEDGER_BYTES
            if is_state:
                # Disk: keep at most STATE_KEEP copies including the one being made.
                for old in [p for p in state_copies() if p != state_out][:-(STATE_KEEP-1) or None]:
                    old.unlink()
                    log('pruned old state copy: '+old.name)
            # Staging + compressed output bounded at half the logical size.
            if is_state and free_bytes() < limit * 3 // 2 + MIN_FREE_BYTES:
                raise ValueError('insufficient private state snapshot/archive headroom')
            if logical_bytes > limit:
                raise ValueError('logical ledger exceeds staging bound: '+rel)
            dst = sqlite3.connect(snapshot)
            copy_started = time.monotonic()
            deadline = copy_started+(1800 if is_state else 15)
            def progress(status, remaining, total):
                if (time.monotonic() >= deadline or total * page_size > limit
                        or (is_state and free_bytes() < MIN_FREE_BYTES + remaining * page_size)
                        or (is_state and wal.exists() and wal.stat().st_size > wal_before + 1024**3)):
                    raise TimeoutError('bounded ledger snapshot refused: '+rel)
                if is_state and status in (sqlite3.SQLITE_OK, sqlite3.SQLITE_DONE):
                    # backup(sleep=...) sleeps ONLY on BUSY/LOCKED, not after
                    # successful copies. Pace real copied pages at 10 MiB/s;
                    # bounded 1 MiB steps also prevent a 32 MiB read burst.
                    expected = (total-remaining) * page_size / (10 * 1024**2)
                    delay = expected - (time.monotonic()-copy_started)
                    if delay > 0:
                        time.sleep(delay)
            src.backup(dst, pages=max(1, 1024**2 // page_size) if is_state else 256,
                       progress=progress, sleep=0.1)
            # Release the private source read generation before validation and
            # compression, minimizing checkpoint delay without signalling holders.
            src.close()
            src = None
            # Flatten ONLY private destination; never checkpoint/VACUUM source.
            dst.execute('pragma journal_mode=delete')
            if [r[0] for r in dst.execute('pragma quick_check')] != ['ok'] or snapshot.stat().st_size > limit:
                raise ValueError('invalid/oversize ledger snapshot: '+rel)
        finally:
            if dst is not None:
                dst.close()
            if src is not None:
                src.close()
        if not is_state:
            snapshots[rel] = snapshot
        record = {'status': 'SNAPSHOT_OK', 'quick_check': 'ok',
            'bytes': snapshot.stat().st_size,
            'sha256': digest(snapshot)}
        if is_state:
            manifest['session_store'] = {'path': rel, **record}
            manifest['session_store']['headroom_before_archive_bytes'] = free_bytes()
            if free_bytes() < snapshot.stat().st_size // 2 + MIN_FREE_BYTES:
                raise ValueError('insufficient state archive publication headroom')
            zpartial = stage / 'state.db.zst'
            zhash = hashlib.sha256()
            with os.fdopen(os.open(zpartial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'wb') as target:
                os.fchmod(target.fileno(), 0o600)
                writer = HeadroomWriter(target)
                proc = subprocess.Popen(['zstd', '-q', '-3', '-T1', '-c', str(snapshot)], stdout=subprocess.PIPE)
                try:
                    for chunk in iter(lambda: proc.stdout.read(1024**2), b''):
                        writer.write(chunk)
                        zhash.update(chunk)
                except BaseException:
                    proc.kill()
                    raise
                finally:
                    proc.stdout.close()
                    rc = proc.wait()
                if rc != 0:
                    raise ValueError('zstd compression failed')
                target.flush()
                os.fsync(target.fileno())
            if subprocess.run(['zstd', '-q', '-t', str(zpartial)]).returncode != 0:
                raise ValueError('zstd verification failed')
            os.replace(zpartial, state_out)
            snapshot.unlink()  # free staging before the config archive
            manifest['session_store'].update({'artifact': state_out.name,
                'compression': 'zstd -3', 'artifact_bytes': state_out.stat().st_size,
                'artifact_sha256': zhash.hexdigest()})
            log('wrote '+str(state_out)+' ('+str(state_out.stat().st_size)+' bytes)')
        else:
            manifest['ledgers'][rel] = record
    coverage = stage / 'backup-coverage.json'
    with os.fdopen(os.open(coverage, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'w') as f:
        os.fchmod(f.fileno(), 0o600)
        f.write(json.dumps(manifest, indent=2)+'\n')
    partial = stage / 'archive.tar.gz'
    includes = 0
    # Explicit creation mode, not umask alone: inherited default ACLs can ignore it.
    with os.fdopen(os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'wb') as target:
        os.fchmod(target.fileno(), 0o600)
        with tarfile.open(fileobj=HeadroomWriter(target), mode='w:gz', dereference=False) as tar:
            for rel in ('config.yaml', '.env', 'auth.json', 'cron/jobs.json', 'SOUL.md', 'memories'):
                source = home / rel
                if source.is_symlink() or source.parent.is_symlink():
                    raise ValueError('symlink config source: '+rel)
                if source.exists():
                    tar.add(source, arcname=rel, filter=private_member)
                    includes += 1
                else:
                    log('skip (not present): '+rel)
            if not includes and not snapshots:
                raise ValueError('no Hermes state found; refusing empty archive')
            for rel, snapshot in snapshots.items():
                tar.add(snapshot, arcname=rel, filter=private_member)
            tar.add(coverage, arcname='backup-coverage.json', filter=private_member)
        target.flush()
        os.fsync(target.fileno())
    os.replace(partial, out)  # only complete private archive published
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
log('wrote '+str(out)+' ('+str(out.stat().st_size)+' bytes); state.db '+manifest['session_store']['status'])
# Preserve find -mtime +N semantics: whole 24-hour days, strictly >N.
deleted = 0
for old in root.glob('hermes-state-????????.tar.gz'):
    if old.is_file() and not old.is_symlink() and int((time.time()-old.stat().st_mtime)/86400) > retention:
        old.unlink()
        deleted += 1
for partial in root.glob('hermes-state-*.tar.gz.partial.*'):
    if partial.is_file() and not partial.is_symlink() and int((time.time()-partial.stat().st_mtime)/86400) > 1:
        partial.unlink()
log('hermes-state-backup done (retention='+str(retention)+'d, pruned='+str(deleted)
    +', state copies='+','.join(p.name for p in state_copies())+')')
PY
