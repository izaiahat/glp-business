#!/usr/bin/env python3
"""Scheduled, bounded cleanup of old regenerable dependencies only.

No report/run/backup/DB/profile deletion, publication, sends or writer suspension.
Reuses the registered pruner's reference, payload, lsof and identity vetoes.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import stat
import time

BUS = Path('/home/ubuntu/business')
ROOTS = (BUS/'reports/glp-clinic-scout/affiliate-machine/registry-site-sync',
         BUS/'reports/ops', BUS/'site-system/flagship-v2')
LOCK = Path('/home/ubuntu/.hermes/runstate/host-disk-cleanup.lock')
EXCLUDE = {'.git', 'browser-profiles', 'browser-profile', 'Default', 'archives', 'backups'}


def dependencies(root, deadline):
    """No symlink traversal; return dependency roots, never containing reports."""
    stack = [Path(root)]
    while stack:
        if time.monotonic() >= deadline:
            raise TimeoutError('dependency discovery incomplete')
        path = stack.pop()
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid():
            continue
        if not stat.S_ISDIR(info.st_mode):
            continue
        if path.name == 'node_modules':
            yield path
            continue
        if path.name in EXCLUDE or path.name.startswith(('browser-profile-', 'sealed', 'seal-')):
            continue
        with os.scandir(path) as entries:
            stack.extend(Path(e.path) for e in entries if e.is_dir(follow_symlinks=False))


def owned_tree(path, deadline):
    stack = [path]
    while stack:
        if time.monotonic() >= deadline:
            raise TimeoutError('ownership discovery incomplete')
        p = stack.pop()
        s = p.lstat()
        if s.st_uid != os.getuid():
            raise ValueError('foreign owner; AHS and root data preserved')
        if stat.S_ISDIR(s.st_mode):
            with os.scandir(p) as entries:
                stack.extend(Path(e.path) for e in entries)


def clean(*, apply=False, budget=180):
    # Imports are effect-free. The same workspace lease serializes maintained
    # source replacement with reference capture and every subsequent effect.
    from sync_from_repo import source_lock
    import host_disk_auto_classes as auto
    import host_disk_cold_classes as cold
    import host_disk_pruner as api
    started = time.monotonic()
    deadline = started + budget
    rows = []
    with source_lock(BUS):
        fd = os.open(LOCK, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
                raise ValueError('cleanup singleton identity')
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # No incomplete/cache-only reference inventory is deletion authority.
            pins = cold.custody_pins(auto.external_pins())
            for root in ROOTS:
                if not root.exists():
                    continue
                if root.resolve() != root.absolute():
                    raise ValueError('aliased scope root')
                for path in dependencies(root, deadline):
                    try:
                        owned_tree(path, deadline)
                        rows.extend(auto.rebuildable_node_modules(
                            api, cold, [path], pins=pins, deadline=deadline,
                            dry_run=not apply, reports_root=root))
                    except (OSError, ValueError, TimeoutError) as error:
                        rows.append({'path':str(path), 'status':'REFUSED',
                                     'reason':str(error), 'bytes_freed':0})
        finally:
            os.close(fd)
    return {'apply':apply, 'rows':rows,
            'bytes_freed':sum(row.get('bytes_freed',0) for row in rows),
            'retention':'inactive, ignored/untracked node_modules with adjacent lockfile; all members older than 3 days',
            'protected_writer_caps_applied':False,
            'seconds':round(time.monotonic()-started,3)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    try:
        result = clean(apply=args.apply)
    except (OSError, ValueError, TimeoutError, RuntimeError) as error:
        print(json.dumps({'status':'REFUSED', 'reason':str(error), 'partial_effects_possible':args.apply}))
        return 75
    print(json.dumps(result, sort_keys=True))
    return int(any(row['status'] not in ('ELIGIBLE','REMOVED') for row in result['rows']))


if __name__ == '__main__':
    raise SystemExit(main())
