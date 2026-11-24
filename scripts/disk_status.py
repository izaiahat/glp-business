#!/usr/bin/env python3
"""Read-only known-writer disk monitor. No cleanup, quota, lifecycle or send effects."""
from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import time

MAILBOX = Path('/srv/kael-mailbox')
BUS = Path('/home/ubuntu/business')
# Measure retained allocation, not mtime footprints or process write throughput.
WRITERS = (
    (BUS / 'reports/glp-clinic-scout/affiliate-machine/registry-site-sync', 'GLP registry sync (paused)', 'frozen report/review evidence'),
    (BUS / 'reports/host-disk-prune', 'host-disk-cleanup', 'recovery and retention evidence'),
    (BUS / 'reports/ops', 'multiple report producers', 'historical deliverables/review evidence'),
    (BUS / 'backups/hermes-state', 'Hermes state backup (paused)', 'backup/recovery records'),
    (Path('/var/log'), 'system log producers', None),
    (Path('/home/ubuntu/.hermes/state.db'), 'Hermes gateway/API', 'protected live session store; metadata only'),
)


def level(free_gb):
    return 'critical' if free_gb < 15 else 'low' if free_gb < 30 else 'ok'


def checked_fd(path, flags):
    """Only our single-linked regular mailbox artifacts; no alias chmods."""
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o640)
    try:
        s = os.fstat(fd)
        if (not stat.S_ISREG(s.st_mode) or s.st_nlink != 1
                or s.st_uid != os.getuid() or s.st_gid != path.parent.stat().st_gid):
            raise ValueError('unexpected mailbox artifact identity')
        os.fchmod(fd, 0o640)
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_previous(path):
    try:
        fd = checked_fd(path, os.O_RDONLY)
    except FileNotFoundError:
        return {}
    with os.fdopen(fd) as stream:
        return json.load(stream)


def publish(path, data):
    if os.path.lexists(path):
        os.close(checked_fd(path, os.O_RDONLY))
    fd, name = tempfile.mkstemp(prefix='.disk-status-', dir=path.parent)
    try:
        os.fchmod(fd, 0o640)
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        dfd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        if os.path.exists(name):
            os.unlink(name)  # Only this invocation's unpublished temporary status.


def allocation(path):
    s = path.lstat()
    if stat.S_ISLNK(s.st_mode):
        raise ValueError('writer path is a symlink')
    if stat.S_ISREG(s.st_mode):
        return s.st_blocks * 512
    result = subprocess.run(['/usr/bin/du', '-sx', '-B1', '--', str(path)],
                            capture_output=True, text=True, timeout=180, check=True)
    return int(result.stdout.split('\t', 1)[0])


def observe(previous, sizes, now, geometry):
    total, used, available = geometry
    history = previous.get('history', [])
    old = [r for r in history if 1800 <= now - r['at'] <= 26 * 3600]
    # Prefer an actual ~24-hour sample; early rates explicitly expose their shorter window.
    baseline = min(old, key=lambda r: abs(now - r['at'] - 86400)) if old else None
    growers = []
    for path, owner, _ in WRITERS:
        key = str(path)
        hours = (now - baseline['at']) / 3600 if baseline and key in baseline['bytes'] else None
        rate = ((sizes[key] - baseline['bytes'][key]) / 1e9 * 24 / hours
                if baseline and hours and key in sizes else None)
        growers.append({'path': key, 'owner': owner, 'gb_per_day': round(rate, 6) if rate is not None else None,
                        'observed_hours': round(hours, 3) if hours else None,
                        'allocated_gb': round(sizes[key] / 1e9, 6) if key in sizes else None,
                        'over_5gb_per_day': rate > 5 if rate is not None else None,
                        'automatic_cap_applied': False})
    growers.sort(key=lambda r: r['gb_per_day'] if r['gb_per_day'] is not None else float('-inf'), reverse=True)
    stamp = dt.datetime.fromtimestamp(now, dt.timezone.utc)
    return {'free_gb': round(available / 1e9, 6), 'used_pct': round(100 * used / (used + available), 3),
            'last_run': stamp.isoformat(), 'freed_gb_this_run': 0,
            'state': level(available / 1e9), 'target_free_gb': 60,
            'below_target': available < 60e9, 'cleanup_enabled': False,
            'coverage': 'fixed known writers only; not a host-wide writer census; /home/ahs excluded',
            'growth_method': 'allocated-size delta / observed interval; GB/day normalized, not mtime',
            'top_growers': growers, 'history': [r for r in history if now - r['at'] < 49 * 3600][-48:] + [{'at': now, 'bytes': sizes}],
            'daily_key': stamp.strftime('%Y-%m-%d'), 'weekly_key': stamp.strftime('%G-W%V')}


def run(mailbox=MAILBOX):
    if mailbox.is_symlink() or mailbox.resolve() != mailbox.absolute() or mailbox.stat().st_uid != os.getuid():
        raise ValueError('unexpected mailbox directory')
    fd = checked_fd(mailbox / '.disk-status.lock', os.O_RDWR | os.O_CREAT)
    with os.fdopen(fd, 'r+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        previous = read_previous(mailbox / 'disk-status.json')
        now = time.time()
        sizes, errors = {}, []
        for path, _, _ in WRITERS:
            try:
                sizes[str(path)] = allocation(path)
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                errors.append({'path': str(path), 'error': type(exc).__name__})
        fs = os.statvfs('/')
        status = observe(previous, sizes, now, (fs.f_blocks * fs.f_frsize,
                         (fs.f_blocks - fs.f_bfree) * fs.f_frsize, fs.f_bavail * fs.f_frsize))
        status['measurement_errors'] = errors
        events = []
        if status['state'] != 'ok':
            events.append({'kind': 'disk_' + status['state'], 'free_gb': status['free_gb'], 'cleanup_enabled': False})
        if previous.get('daily_key') != status['daily_key']:
            events.append({'kind': 'daily', 'free_gb': status['free_gb'], 'freed_gb': 0, 'writers': status['top_growers']})
        if previous.get('weekly_key') != status['weekly_key']:
            events.append({'kind': 'candidates for Izaiah', 'candidates': [
                {'path': str(p), 'gb': round(sizes[str(p)] / 1e9, 6) if str(p) in sizes else None, 'why': why}
                for p, _, why in WRITERS if why]})
        if events:
            log_fd = checked_fd(mailbox / 'disk-events.jsonl', os.O_WRONLY | os.O_APPEND | os.O_CREAT)
            with os.fdopen(log_fd, 'a') as stream:
                for event in events:
                    stream.write(json.dumps({'at': status['last_run'], **event}, sort_keys=True) + '\n')
                stream.flush()
                os.fsync(stream.fileno())
        publish(mailbox / 'disk-status.json', status)
        print(json.dumps({k: status[k] for k in ('last_run', 'free_gb', 'used_pct', 'state', 'freed_gb_this_run', 'cleanup_enabled', 'measurement_errors')}, sort_keys=True))
        return status


if __name__ == '__main__':
    run()
