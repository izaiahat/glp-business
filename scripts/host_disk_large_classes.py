#!/usr/bin/env python3
"""Additive large regenerable classes; no broad-root or evidence deletion.

This module deliberately has no import-time effects. Codex package releases are
reinstallable bytes; exact running 0.162.0 and process-referenced versions stay.
Caller must hold the existing host cleanup singleton before scheduler adoption.
"""
import os
from pathlib import Path
import re
import stat
import subprocess
import time

CODEX_RELEASES=Path('/home/ubuntu/.codex/packages/app-server-daemon/releases')
CURRENT='0.162.0-x86_64-unknown-linux-musl'
VERSION=re.compile(r'0\.(\d+)\.(\d+)-x86_64-unknown-linux-musl\Z')

def snapshot(root,deadline):
    """No-follow allocated inode inventory, including exact descendant names."""
    rows=[]; stack=[root]; blocks={}
    device=root.lstat().st_dev
    while stack:
        if time.monotonic()>=deadline: raise TimeoutError('inventory deadline')
        p=stack.pop(); s=p.lstat()
        if s.st_dev!=device: raise ValueError('cross-device')
        if not (stat.S_ISREG(s.st_mode) or stat.S_ISDIR(s.st_mode) or stat.S_ISLNK(s.st_mode)):
            raise ValueError('special file')
        if p.suffix in {'.db','.sqlite','.sqlite3','.zst','.tar','.tgz','.gz'} or any(t in p.name.lower() for t in ('backup','recovery','credential','secret')):
            raise ValueError('protected descendant')
        rows.append((str(p.relative_to(root)),s.st_dev,s.st_ino,s.st_mode,s.st_size,s.st_mtime_ns,s.st_ctime_ns,s.st_nlink))
        blocks[(s.st_dev,s.st_ino)]=(s.st_blocks*512,s.st_nlink)
        if stat.S_ISDIR(s.st_mode):
            with os.scandir(p) as entries: stack.extend(Path(e.path) for e in entries)
    # External hardlinks are conservatively counted as zero reclaimable bytes.
    counts={}
    for row in rows: counts[(row[1],row[2])]=counts.get((row[1],row[2]),0)+1
    amount=sum(b for key,(b,n) in blocks.items() if n==1 or counts[key]>=n)
    return sorted(rows),amount

def lsof_inactive(path):
    """Required independent live open-file check; unknown/timeout refuses."""
    try:
        r=subprocess.run(['sudo','-n','lsof','-nP','+D',str(path)],capture_output=True,timeout=15)
        return r.returncode==1 and not r.stdout and not r.stderr
    except (OSError,subprocess.TimeoutExpired): return False

def codex_releases(api,*,root=CODEX_RELEASES,dry_run,deadline):
    root=Path(root); rows=[]
    if root.is_symlink() or not root.is_dir() or root.resolve()!=root.absolute(): return rows
    for p in sorted(root.iterdir()):
        match=VERSION.fullmatch(p.name)
        if not match or (int(match[1]),int(match[2]))>=(162,0) or p.name==CURRENT or p.is_symlink() or not p.is_dir(): continue
        row={'rule':'old_codex_daemon_release','path':str(p),'ok':True,'bytes_freed':0,'before_bytes':0,'after_bytes':0}
        try:
            if api.path_is_active_privileged(p):
                rows.append(dict(row,status='ACTIVE_OR_UNKNOWN')); continue
            before,amount=snapshot(p,deadline); row['before_bytes']=row['after_bytes']=amount
            # Exact names/metadata/identity and two independent live checks before effect.
            if not lsof_inactive(p) or api.path_is_active_privileged(p) or snapshot(p,deadline)[0]!=before:
                rows.append(dict(row,status='REFERENCE_OR_IDENTITY_REFUSED')); continue
            if dry_run: row.update(status='ELIGIBLE',eligible_bytes=amount)
            elif api.remove_disposable(p,deadline=deadline) and not p.exists():
                row.update(status='REMOVED',after_bytes=0,bytes_freed=amount)
            else: row.update(status='PARTIAL_REMOVAL_UNKNOWN',ok=False)
        except (OSError,ValueError,TimeoutError) as exc: row.update(status='REFUSED',reason=str(exc))
        rows.append(row)
    return rows

# Additive explicit-target classes. The running controller is deliberately not
# edited or restarted; adoption occurs only after its existing singleton exits.
from host_disk_cold_classes import node_modules, cold_archive
