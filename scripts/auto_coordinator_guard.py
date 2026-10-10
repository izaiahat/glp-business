#!/usr/bin/env python3
"""Inspect the authoritative host child leases; never change admission state.

The hourly coordinator defers the entire dispatch/closure pass to live TUI
children. Corrupt/missing state is unknown, not idle. Match kernel start ticks
to avoid stale PID reuse. This readback grants no independent closure authority.
"""
import fcntl
import json
from pathlib import Path
import sys

BUDGET = Path.home() / '.hermes/cache/delegation-host-budget.json'


def start_tick(pid):
    try:
        fields = (Path('/proc') / str(int(pid)) / 'stat').read_text().rpartition(')')[2].split()
        return fields[19]  # /proc stat field 22 after comm (field 2)
    except (ValueError, OSError, IndexError):
        return None


def inspect(budget=BUDGET, tick=start_tick, owned_token=None):
    try:
        with Path(budget).open() as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            rows = json.load(f)
        if not isinstance(rows, list) or any(
                not isinstance(r, dict) or not {'pid', 'start', 'token'} <= set(r)
                or type(r['pid']) is not int or r['pid'] < 1
                or not str(r['start']).isdigit() or not isinstance(r['token'], str) or not r['token']
                for r in rows):
            raise ValueError('malformed host lease state')
        active = [r for r in rows if tick(r['pid']) == str(r['start']) and r['token'] != owned_token]
        return {'status': 'DEFERRED_TUI_CHILDREN' if active else 'IDLE',
                'live_child_slots': len(active), 'owner_pids': sorted({int(r['pid']) for r in active})}
    except (ValueError, TypeError, OSError) as exc:
        return {'status': 'DEFERRED_UNKNOWN', 'reason': type(exc).__name__}


def owned_token():
    """Ignore only the installed runner's lease in its own process ancestry."""
    import os
    token = os.environ.get('GLP_AUTO_COORDINATOR_LEASE_TOKEN')
    try:
        owner = int(os.environ.get('GLP_AUTO_COORDINATOR_LEASE_PID', '0'))
        pid = os.getppid()
        for _ in range(64):
            if pid < 1:
                break
            if pid == owner:
                args = Path('/proc/%s/cmdline' % pid).read_bytes().decode().split('\0')
                if '/home/ubuntu/business/scripts/auto_coordinator_runner.py' in args:
                    return token
                break
            tail = Path('/proc/%s/stat' % pid).read_text().rsplit(')', 1)[1].split()
            pid = int(tail[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def main():
    result = inspect(owned_token=owned_token())
    print(json.dumps(result, sort_keys=True))
    return 0 if result['status'] == 'IDLE' else 10


if __name__ == '__main__':
    sys.exit(main())
