#!/usr/bin/env python3
"""Additive large regenerable classes; no broad-root or evidence deletion.

This module deliberately has no import-time effects. Codex package releases are
reinstallable bytes; exact running 0.162.0 and process-referenced versions stay.
Caller must hold the existing host cleanup singleton before scheduler adoption.
"""
import os
from contextlib import contextmanager
from pathlib import Path
import re
import stat
import subprocess
import time

CODEX_RELEASES=Path('/home/ubuntu/.codex/packages/app-server-daemon/releases')
CURRENT='0.162.0-x86_64-unknown-linux-musl'
VERSION=re.compile(r'0\.(\d+)\.(\d+)-x86_64-unknown-linux-musl\Z')

def mount_id(fd):
    """Linux mount identity detects same-device bind mounts too."""
    for line in Path(f'/proc/self/fdinfo/{fd}').read_text().splitlines():
        if line.startswith('mnt_id:'):
            return int(line.split()[1])
    raise ValueError('mount identity unavailable')


@contextmanager
def checked_directory(path):
    """Open every ancestor with O_NOFOLLOW; retain handles for final checks."""
    path = Path(path).absolute()
    handles = []
    edges = []
    try:
        fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        handles.append(fd)
        for name in path.parts[1:]:
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                            dir_fd=fd)
            handles.append(child)
            info = os.fstat(child)
            edges.append((fd, name, info.st_dev, info.st_ino))
            fd = child
        if not edges or mount_id(fd) != mount_id(edges[-1][0]):
            raise ValueError('candidate mount boundary')
        def validate():
            for parent, name, device, inode in edges:
                current = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if (current.st_dev, current.st_ino) != (device, inode) or not stat.S_ISDIR(current.st_mode):
                    raise ValueError('ancestor identity changed')
        validate()
        yield fd, edges[-1][0], path.name, validate
    finally:
        for fd in reversed(handles):
            os.close(fd)


def snapshot(root, deadline, *, max_items=100000, max_bytes=2*1024**3, include_directories=False):
    """Descriptor-relative, no-follow inventory; reject every mount boundary."""
    rows = []; blocks = {}; total = 0
    root = Path(root)
    directory = stat.S_ISDIR(root.lstat().st_mode)
    with checked_directory(root if directory else root.parent) as (root_fd, _, _, validate):
        device = os.fstat(root_fd).st_dev
        mounted = mount_id(root_fd)
        def walk(fd, relative):
            nonlocal total
            if time.monotonic() >= deadline: raise TimeoutError('inventory deadline')
            info = os.fstat(fd)
            add(relative, info)
            with os.scandir(fd) as entries:
                for entry in entries:
                    if time.monotonic() >= deadline: raise TimeoutError('inventory deadline')
                    name = str(Path(relative)/entry.name) if relative != '.' else entry.name
                    s = os.stat(entry.name, dir_fd=fd, follow_symlinks=False)
                    if stat.S_ISDIR(s.st_mode):
                        child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
                        try:
                            if mount_id(child) != mounted: raise ValueError('descendant mount boundary')
                            if (os.fstat(child).st_dev, os.fstat(child).st_ino) != (s.st_dev, s.st_ino):
                                raise ValueError('inventory identity changed')
                            walk(child, name)
                        finally: os.close(child)
                    else: add(name, s)
        def add(name, s):
            nonlocal total
            if len(rows) >= max_items: raise ValueError('inventory item cap')
            if s.st_dev != device: raise ValueError('cross-device mount')
            if not (stat.S_ISREG(s.st_mode) or stat.S_ISDIR(s.st_mode) or stat.S_ISLNK(s.st_mode)):
                raise ValueError('special file')
            p = Path(root.name if name == '.' else name)
            if p.suffix in {'.db','.sqlite','.sqlite3','.zst','.tar','.tgz','.gz'} or any(t in p.name.lower() for t in ('backup','recovery','credential','secret')):
                raise ValueError('protected descendant')
            rows.append((name,s.st_dev,s.st_ino,s.st_mode,s.st_size,s.st_mtime_ns,s.st_ctime_ns,s.st_nlink))
            key = (s.st_dev,s.st_ino)
            if key not in blocks:
                total += s.st_blocks*512
                if total > max_bytes: raise ValueError('inventory byte cap')
            blocks[key] = (s.st_blocks*512,s.st_nlink)
        if directory:
            walk(root_fd, '.')
        else:
            add('.', os.stat(root.name, dir_fd=root_fd, follow_symlinks=False))
        validate()
    counts = {}
    for row in rows: counts[(row[1],row[2])] = counts.get((row[1],row[2]),0)+1
    directory_keys = {(row[1], row[2]) for row in rows if include_directories and stat.S_ISDIR(row[3])}
    amount = sum(b for key,(b,n) in blocks.items() if key in directory_keys or n==1 or counts[key]>=n)
    return sorted(rows), amount

def lsof_inactive(path):
    """Required independent live open-file check; unknown/timeout refuses."""
    try:
        # The all-UID /proc veto is separate; lsof needs no root access here.
        r=subprocess.run(['lsof','-nP','+D',str(path)],capture_output=True,timeout=15)
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
