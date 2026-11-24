#!/usr/bin/env python3
"""Scheduled, bounded cleanup of old regenerable dependencies only.

No report/run/backup/DB/profile deletion, publication, sends or writer suspension.
Reuses the registered pruner's reference, payload, lsof and identity vetoes.
"""
import argparse
import fcntl
import json
import os
import multiprocessing
import signal
from pathlib import Path
import stat
import time

BUS = Path('/home/ubuntu/business')
ROOTS = (BUS/'reports/glp-clinic-scout/affiliate-machine/registry-site-sync',
         BUS/'reports/ops', BUS/'site-system/flagship-v2')
LOCK = Path('/home/ubuntu/.hermes/runstate/host-disk-cleanup.lock')
EXCLUDE = {'.git', 'browser-profiles', 'browser-profile', 'Default', 'archives', 'backups'}


def dependencies(root, deadline, max_items=100000):
    """No symlink traversal; bounded dependency discovery, never reports."""
    stack = [Path(root)]
    visited = 0
    while stack:
        visited += 1
        if visited + len(stack) > max_items:
            raise ValueError('discovery item cap')
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
    remaining_bytes = 2 * 1024**3
    remaining_items = 100000
    attempted = 0
    def record(row):
        rows.append(row)
        # Journald receives each completed effect before any next discovery.
        print(json.dumps(row, sort_keys=True), flush=True)
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
                    if attempted >= 5 or remaining_bytes <= 0 or remaining_items <= 0:
                        record({'path':str(path), 'status':'RUN_CAP', 'bytes_freed':0})
                        return {'apply':apply, 'rows':rows,
                                'bytes_freed':sum(r.get('bytes_freed',0) for r in rows)}
                    attempted += 1
                    try:
                        from host_disk_large_classes import snapshot
                        members, amount = snapshot(path, deadline, max_items=remaining_items, max_bytes=remaining_bytes)
                        remaining_items -= len(members)
                        # Reserve the whole candidate before any partial retirement.
                        remaining_bytes -= amount
                        owned_tree(path, deadline)
                        for row in auto.rebuildable_node_modules(
                            api, cold, [path], pins=pins, deadline=deadline,
                            max_bytes=amount, max_items=len(members),
                            dry_run=not apply, reports_root=root):
                            record(row)
                    except (OSError, ValueError, TimeoutError) as error:
                        record({'path':str(path), 'status':'REFUSED',
                                'reason':str(error), 'bytes_freed':0})
        finally:
            os.close(fd)
    return {'apply':apply, 'rows':rows,
            'bytes_freed':sum(row.get('bytes_freed',0) for row in rows),
            'retention':'inactive, ignored/untracked node_modules with adjacent lockfile; all members older than 3 days',
            'protected_writer_caps_applied':False,
            'seconds':round(time.monotonic()-started,3)}


def run_bounded(action, seconds):
    """Supervise reference discovery AND deletion; retain flushed partial logs."""
    def worker():
        os.setsid()
        raise SystemExit(action())
    child = multiprocessing.get_context('fork').Process(target=worker)
    child.start()
    try:
        child.join(seconds)
        if not child.is_alive():
            return child.exitcode
        print(json.dumps({'status':'TIME_BUDGET_PARTIAL', 'partial_effects_possible':True}), flush=True)
        return 75
    finally:
        if child.is_alive():
            # The group includes read-only git/reference helpers. No effectful
            # deletion child is detached; retirement runs in the supervised PID.
            try: os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError: pass
            child.join(1)
            try: os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError: pass
            child.join()
        child.close()


def execute(apply):
    try:
        result = clean(apply=apply)
    except (OSError, ValueError, TimeoutError, RuntimeError) as error:
        print(json.dumps({'status':'REFUSED', 'reason':str(error), 'partial_effects_possible':apply}), flush=True)
        return 75
    print(json.dumps(result, sort_keys=True), flush=True)
    return int(any(row['status'] not in ('ELIGIBLE','REMOVED') for row in result['rows']))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    return run_bounded(lambda: execute(args.apply), 180)


if __name__ == '__main__':
    raise SystemExit(main())
