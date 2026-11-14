#!/usr/bin/env python3
"""No-LLM disk alarm. See ops/disk-alarm.md before parent-owned installation."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

BUSINESS = Path('/home/ubuntu/business')
STATE = BUSINESS / 'ops/runtime/disk-alarm'
LOG = Path('/srv/kael-mailbox/disk-alerts.log')
CHANNEL = '1487300132054765629'
OPERATOR = '392067741500309517'


def atomic_json(path, value):
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(value, stream, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def lock(root):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / '.lock').open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def free_bytes():
    result = subprocess.run(['/usr/bin/df', '-B1', '--output=avail', '/'],
                            check=True, capture_output=True, text=True, timeout=10,
                            env={**os.environ, 'LC_ALL': 'C'})
    rows = result.stdout.splitlines()
    if len(rows) != 2 or not rows[1].strip().isascii() or not rows[1].strip().isdigit():
        raise RuntimeError('invalid df available bytes')
    return int(rows[1])


def notify(message):
    result = subprocess.run([sys.executable, str(BUSINESS / 'scripts/post_discord_message.py'),
                             '--channel', CHANNEL, '--ping-user', OPERATOR, '--message', message],
                            capture_output=True, text=True, timeout=180)
    if result.returncode:
        # Do not log arbitrary sender stderr or retry an uncertain external effect.
        raise RuntimeError('Discord sender failed; reconcile pending intent, do not resend')
    receipt = json.loads(result.stdout)
    if receipt.get('exact_readback') is not True or not receipt.get('message_id'):
        raise RuntimeError('Discord receipt not confirmed')
    return {'message_id': receipt['message_id']}


def tick(root, available, send=notify, log_path=None):
    """Caller holds singleton lock; callbacks are injectable for offline tests."""
    path = root / 'state.json'
    state = json.loads(path.read_text()) if path.exists() else {'sent': [], 'pending': None}
    current = level(available)
    observation = {'at': int(time.time()), 'available_bytes': available, 'level': current}
    if current == 'critical':
        # Persistent scheduled-backup gate; no crontab rewrite or active-process stop.
        if not (root / 'backup-paused').exists():
            atomic_json(root / 'backup-paused', observation)
        observation['backup_pause'] = 'paused'
    state['latest'] = observation
    if current == 'healthy' and state['pending'] is None:
        state['sent'] = []
    should_send = current != 'healthy' and current not in state['sent'] and state['pending'] is None
    if should_send:
        message = (f'<@{OPERATOR}> DISK {current.upper()}: {available / 2**30:.2f} GiB available '
                   f'on /. Operator + assistant: inspect disk pressure. '
                   + ('Backup schedule gated. '
                      if current == 'critical' else 'Below 60 GiB warning; no schedules changed. ')
                   + 'No active jobs stopped; AHS untouched. Pauses never auto-resume.')
        state['pending'] = {'level': current, 'message': message, 'at': observation['at']}
        atomic_json(path, state)  # fsynced intent BEFORE sender; survives crashes and timeouts
        try:
            receipt = send(message)
            state['last_delivery'] = receipt
            state['sent'].append(current)
            state['pending'] = None
        except Exception as exc:
            observation['delivery_error'] = type(exc).__name__
    if state['pending'] is not None:
        observation['delivery_pending'] = True
    atomic_json(path, state)
    with (log_path or root / 'disk-alerts.log').open('a') as stream:
        stream.write(json.dumps(observation, sort_keys=True) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    return observation


def backup(root, available, launch):
    """Called under the alarm lock. Start only; caller waits outside that lock."""
    if not (root / 'state.json').is_file():
        raise RuntimeError('alarm not initialized; backup admission refused')
    if level(available) == 'critical' and not (root / 'backup-paused').exists():
        atomic_json(root / 'backup-paused', {'available_bytes': available, 'at': int(time.time())})
    if (root / 'backup-paused').exists():
        return None
    return launch(['/bin/bash', str(BUSINESS / 'scripts/hermes-state-backup.sh')])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['check', 'tick', 'backup'])
    args = parser.parse_args()

    if args.action == 'check':
        available = free_bytes()
        print(json.dumps({'available_bytes': available, 'level': level(available), 'effects': False}))
        return 0
    try:
        with lock(STATE):
            if args.action == 'backup':
                child = backup(STATE, free_bytes(), subprocess.Popen)
            else:
                result = tick(STATE, free_bytes(), log_path=LOG)
                print(json.dumps(result, sort_keys=True))
                return int('delivery_pending' in result)
    except BlockingIOError:
        print('SKIP disk alarm lock busy')
        return 0
    if child is None:
        print('SKIP backup schedule paused by disk alarm; operator-only resume')
        return 0
    return child.wait()  # no deadline or signal: never stop an active backup



def level(available: int) -> str:
    if available < 40 * 2**30:
        return 'critical'
    return 'warning' if available < 60 * 2**30 else 'healthy'


if __name__ == '__main__':
    raise SystemExit(main())
