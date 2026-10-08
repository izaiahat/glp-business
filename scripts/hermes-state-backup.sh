#!/usr/bin/env bash
# Daily private config/auth/jobs/SOUL/memories + bounded cron-ledger backup.
# Registered entry: 0 4 * * * bash /home/ubuntu/business/scripts/hermes-state-backup.sh
# SQLite online backup API only: never raw tar of live ledgers/WAL/state.db.
# Current state.db is NOT included; historical archives are not its backup.
# Same-day atomic replacement and existing 14-day retention scope preserved.
set -Eeuo pipefail
umask 077
exec python3 - "${HERMES_HOME:-$HOME/.hermes}" \
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

def log(text):
    print('['+dt.datetime.now(dt.timezone.utc).isoformat()+'] '+text, flush=True)

def private_member(member):
    if not (member.isfile() or member.isdir()):
        raise ValueError('non-regular backup member: '+member.name)
    member.mode = 0o700 if member.isdir() else 0o600
    return member

# Fixed scope, no DB discovery. Growth refuses rather than raw-copy fallback.
MAX_LEDGER_BYTES = 8 * 1024**2
manifest = {'schema': 1, 'at': now.isoformat(),
    'producer_sha256': hashlib.sha256(producer.read_bytes()).hexdigest(),
    'sqlite_version': sqlite3.sqlite_version,
    'snapshot_api': 'sqlite3.Connection.backup (SQLite online backup API)',
    'consistency': 'independent per-ledger snapshots; not a cross-store checkpoint',
    'ledgers': {}, 'session_store': {'path': 'state.db', 'status': 'NOT_INCLUDED',
        'reason': 'Requires separately authorized destination and real full SQLite staging capacity.'}}
log('hermes-state-backup start (out='+str(out)+')')
with tempfile.TemporaryDirectory(prefix='.snapshot-', dir=root) as stage_name:
    stage = Path(stage_name)  # exclusive private 0700 staging
    snapshots = {}
    for name in ('executions.db', 'deliveries.db', 'notepad.db', 'cron.db'):
        rel = 'cron/'+name
        source = home / rel
        if not source.exists():
            if source.is_symlink():
                raise ValueError('dangling ledger symlink: '+rel)
            manifest['ledgers'][rel] = {'status': 'MISSING'}
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
            if p.exists() and p.stat().st_size > MAX_LEDGER_BYTES:
                raise ValueError('ledger exceeds bounded staging scope: '+rel)
        src = dst = None
        try:
            src = sqlite3.connect(source.as_uri()+'?mode=ro', uri=True, timeout=0.0)
            page_size = src.execute('pragma page_size').fetchone()[0]
            if page_size * src.execute('pragma page_count').fetchone()[0] > MAX_LEDGER_BYTES:
                raise ValueError('logical ledger exceeds staging bound: '+rel)
            dst = sqlite3.connect(snapshot)
            deadline = time.monotonic()+15
            def progress(status, remaining, total):
                if time.monotonic() >= deadline or total * page_size > MAX_LEDGER_BYTES:
                    raise TimeoutError('bounded ledger snapshot refused: '+rel)
            src.backup(dst, pages=256, progress=progress, sleep=0.1)
            # Flatten ONLY private destination; never checkpoint/VACUUM source.
            dst.execute('pragma journal_mode=delete')
            if [r[0] for r in dst.execute('pragma quick_check')] != ['ok'] or snapshot.stat().st_size > MAX_LEDGER_BYTES:
                raise ValueError('invalid/oversize ledger snapshot: '+rel)
        finally:
            if dst is not None:
                dst.close()
            if src is not None:
                src.close()
        snapshots[rel] = snapshot
        manifest['ledgers'][rel] = {'status': 'SNAPSHOT_OK', 'quick_check': 'ok',
            'bytes': snapshot.stat().st_size,
            'sha256': hashlib.sha256(snapshot.read_bytes()).hexdigest()}
    coverage = stage / 'backup-coverage.json'
    with os.fdopen(os.open(coverage, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'w') as f:
        os.fchmod(f.fileno(), 0o600)
        f.write(json.dumps(manifest, indent=2)+'\n')
    partial = stage / 'archive.tar.gz'
    includes = 0
    # Explicit creation mode, not umask alone: inherited default ACLs can ignore it.
    with os.fdopen(os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'wb') as target:
        os.fchmod(target.fileno(), 0o600)
        with tarfile.open(fileobj=target, mode='w:gz', dereference=False) as tar:
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
log('wrote '+str(out)+' ('+str(out.stat().st_size)+' bytes); state.db NOT_INCLUDED')
# Preserve find -mtime +N semantics: whole 24-hour days, strictly >N.
deleted = 0
for old in root.glob('hermes-state-????????.tar.gz'):
    if old.is_file() and not old.is_symlink() and int((time.time()-old.stat().st_mtime)/86400) > retention:
        old.unlink()
        deleted += 1
for partial in root.glob('hermes-state-*.tar.gz.partial.*'):
    if partial.is_file() and not partial.is_symlink() and int((time.time()-partial.stat().st_mtime)/86400) > 1:
        partial.unlink()
log('hermes-state-backup done (retention='+str(retention)+'d, pruned='+str(deleted)+')')
PY
