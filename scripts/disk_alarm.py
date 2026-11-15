#!/usr/bin/env python3
"""No-LLM disk alarm. See ops/disk-alarm.md before parent-owned installation."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import subprocess
import sys
import tempfile
import time
import uuid

BUSINESS = Path('/home/ubuntu/business')
STATE = BUSINESS / 'ops/runtime/disk-alarm'
LOG = Path('/srv/kael-mailbox/disk-alerts.log')
CHANNEL = '1487300132054765629'
OPERATOR = '392067741500309517'
LEVELS = ('warning', 'critical', 'error')


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
    if (len(rows) != 2 or rows[0].strip() != 'Avail'
            or not rows[1].strip().isascii() or not rows[1].strip().isdigit()):
        raise RuntimeError('invalid df available bytes')
    available = int(rows[1])
    filesystem = os.statvfs('/')
    if available > filesystem.f_blocks * filesystem.f_frsize:
        raise RuntimeError('implausible df available bytes')
    return available


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


def read_state(path):
    state = json.loads(path.read_text())
    if not isinstance(state, dict) or not isinstance(state.get('sent'), list):
        raise ValueError('invalid alarm state')
    if any(item not in LEVELS for item in state['sent']):
        raise ValueError('invalid sent levels')
    pending = state['pending']
    # Preserve predecessor uncertain intents, assigning a stable reconciliation ID.
    if pending is None:
        pending = {}
    elif isinstance(pending, dict) and 'level' in pending:
        pending['id'] = hashlib.sha256(json.dumps(pending, sort_keys=True).encode()).hexdigest()
        pending = {pending['level']: pending}
    if not isinstance(pending, dict):
        raise ValueError('invalid pending intents')
    for key, intent in pending.items():
        if (key not in LEVELS or not isinstance(intent, dict) or intent.get('level') != key
                or not isinstance(intent.get('message'), str) or not intent.get('id')
                or type(intent.get('at')) is not int):
            raise ValueError('invalid pending intent')
    state['pending'] = pending
    latest = state.get('latest')
    if latest is None:
        state['latest'] = None  # Legacy state: timer must supply the first observation.
        return state
    if (not isinstance(latest, dict) or type(latest.get('at')) is not int
            or latest.get('level') not in (*LEVELS, 'healthy')):
        raise ValueError('invalid latest observation')
    available = latest.get('available_bytes')
    if latest['level'] != 'error' and (type(available) is not int or available < 0
                                      or level(available) != latest['level']):
        raise ValueError('invalid latest measurement')
    return state


def backup_pause(root, current, observation):
    marker = root / 'backup-paused'
    if current in ('critical', 'error') and not os.path.lexists(marker):
        atomic_json(marker, {**observation, 'owner': 'disk-alarm'})
    if current == 'healthy' and marker.is_file() and not marker.is_symlink():
        try:
            owned = json.loads(marker.read_text()).get('owner') == 'disk-alarm'
        except (ValueError, AttributeError):
            owned = False
        if owned:
            marker.unlink()  # Only our hold; never an operator/legacy/unknown marker.
            fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            observation['backup_recovery'] = 'alarm hold lifted'
    if os.path.lexists(marker) or os.path.lexists(root / 'operator-backup-paused'):
        observation['backup_pause'] = 'paused'


def append_log(path, observation):
    with path.open('a') as stream:
        stream.write(json.dumps(observation, sort_keys=True) + '\n')
        stream.flush()
        os.fsync(stream.fileno())


def tick(root, available, send=notify, log_path=None, measurement_error=None):
    """Caller holds singleton lock; callbacks are injectable for offline tests."""
    path = root / 'state.json'
    state_error = None
    try:
        state = read_state(path) if path.exists() else {'sent': [], 'pending': {}}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # Preserve corrupt bytes and unknown deliveries; error alerts have a separate intent.
        state_error = type(exc).__name__
        path = root / 'monitoring-error.json'
        state = read_state(path) if path.exists() else {'sent': [], 'pending': {}}
    current = 'error' if measurement_error or state_error else level(available)
    observation = {'at': int(time.time()), 'available_bytes': available, 'level': current}
    if measurement_error:
        observation['measurement_error'] = measurement_error
    if state_error:
        observation['state_error'] = state_error
    backup_pause(root, current, observation)
    state['latest'] = observation
    if current == 'healthy':
        state['sent'] = []  # Pending severities remain latched independently.
        sidecar = root / 'monitoring-error.json'
        if sidecar.exists():
            error_state = read_state(sidecar)
            error_state['sent'] = []  # Preserve uncertain intents and reconciliation evidence.
            atomic_json(sidecar, error_state)
    message = None
    should_send = current != 'healthy' and current not in state['sent'] and current not in state['pending']
    if should_send:
        space = 'UNKNOWN' if available is None else f'{available / 2**30:.2f} GiB'
        message = (f'<@{OPERATOR}> DISK {current.upper()}: {space} available on /. '
                   'Operator + assistant: inspect disk pressure/monitoring. '
                   + ('Backup schedule gated. ' if current in ('critical', 'error')
                      else 'Below 60 GiB warning; no schedules changed. ')
                   + 'No active jobs stopped; AHS untouched. Alarm hold lifts at 60 GiB; operator holds remain.')
        state['pending'][current] = {'id': uuid.uuid4().hex, 'level': current,
                                     'message': message, 'at': observation['at']}
    atomic_json(path, state)  # Intent and observation BEFORE log/sender; no send on failed writes.
    append_log(log_path or root / 'disk-alerts.log', observation)
    if should_send:
        try:
            state['last_delivery'] = send(message)
            state['sent'].append(current)
            del state['pending'][current]
        except Exception as exc:
            observation['delivery_error'] = type(exc).__name__
    if state['pending']:
        observation['delivery_pending'] = True
    atomic_json(path, state)
    return observation


def admitted(root, available):
    state = read_state(root / 'state.json')
    latest = state['latest']
    if latest is None or latest['level'] == 'error' or not 0 <= time.time() - latest['at'] <= 2 * 15 * 60:
        raise RuntimeError('alarm unhealthy or stale; backup admission refused')
    if type(available) is not int or available < 0:
        raise RuntimeError('invalid backup measurement')
    if available < 60 * 2**30:
        # Do not refresh latest: only a persisted timer tick proves monitor freshness.
        state['backup_observation'] = {'at': int(time.time()),
                                       'available_bytes': available, 'level': level(available)}
        try:
            # Persist the critical hold before an observation failure can return.
            if level(available) == 'critical':
                backup_pause(root, 'critical', {'at': int(time.time()), 'available_bytes': available})
            atomic_json(root / 'state.json', state)
        except OSError:
            return False
    # Recovery is timer-owned: a fresh backup measurement cannot erase a hold.
    if level(available) == 'critical':
        return False  # Refuse independently of whether the pause marker persisted.
    return not (os.path.lexists(root / 'backup-paused')
                or os.path.lexists(root / 'operator-backup-paused'))


@contextmanager
def admission_directory(root, create=False):
    directory = root / 'admissions'
    if create:
        directory.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError('unsafe admission directory')
        yield fd
    finally:
        os.close(fd)


def consume_admission(root, token):
    """Consume one private, fresh wrapper admission; invalid proof uses normal gating."""
    if not isinstance(token, str) or len(token) != 64 or any(c not in '0123456789abcdef' for c in token):
        return False
    try:
        with admission_directory(root) as directory:
            fd = os.open(token, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            with os.fdopen(fd) as stream:
                info = os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                    return False
                # Only the process that successfully unlinks may use this proof.
                os.unlink(token, dir_fd=directory)
                proof = json.loads(stream.read(1024))
        return (secrets.compare_digest(proof['token'], token)
                and 0 <= time.time() - proof['at'] < 60)
    except (OSError, ValueError, KeyError, TypeError):
        return False


def backup(root, available, launch):
    """Called under the alarm lock. Start only; caller waits outside that lock."""
    if not admitted(root, available):
        return None
    token = secrets.token_hex(32)
    with admission_directory(root, create=True) as directory:
        fd = os.open(token, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=directory)
        try:
            with os.fdopen(fd, 'w') as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump({'token': token, 'at': time.time()}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            return launch(['/bin/bash', str(BUSINESS / 'scripts/hermes-state-backup.sh')],
                          env={**os.environ, 'DISK_ALARM_BACKUP_TOKEN': token})
        except BaseException:
            os.unlink(token, dir_fd=directory)
            raise


def reconcile(root, filename, severity, intent_id, outcome, evidence, message_id=None):
    """Record operator-verified exact-effect disposition; never send or retry here."""
    path = root / filename
    state = read_state(path)
    intent = state['pending'].get(severity)
    if not intent or intent['id'] != intent_id or not evidence.strip():
        raise ValueError('exact pending identity and evidence required')
    if outcome == 'delivered' and not message_id:
        raise ValueError('confirmed message ID required')
    state.setdefault('reconciled', []).append({**intent, 'outcome': outcome,
        'evidence': evidence, 'message_id': message_id, 'reconciled_at': int(time.time())})
    if outcome == 'delivered' and severity not in state['sent']:
        state['sent'].append(severity)
    del state['pending'][severity]
    atomic_json(path, state)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['check', 'tick', 'backup', 'admit-backup', 'reconcile'])
    parser.add_argument('--state-file', choices=['state.json', 'monitoring-error.json'], default='state.json')
    parser.add_argument('--level', choices=LEVELS)
    parser.add_argument('--intent-id')
    parser.add_argument('--outcome', choices=['delivered', 'not-sent'])
    parser.add_argument('--evidence')
    parser.add_argument('--message-id')
    args = parser.parse_args()

    if args.action == 'admit-backup' and consume_admission(STATE, os.environ.pop('DISK_ALARM_BACKUP_TOKEN', None)):
        return 0  # Parent already admitted under its still-held singleton lock.
    if args.action == 'check':
        available = free_bytes()
        print(json.dumps({'available_bytes': available, 'level': level(available), 'effects': False}))
        return 0
    try:
        with lock(STATE):
            if args.action == 'reconcile':
                if not all((args.level, args.intent_id, args.outcome, args.evidence)):
                    parser.error('reconcile requires level, intent-id, outcome and evidence')
                reconcile(STATE, args.state_file, args.level, args.intent_id,
                          args.outcome, args.evidence, args.message_id)
                return 0
            if args.action in ('backup', 'admit-backup'):
                available = free_bytes()
                if args.action == 'admit-backup':
                    if admitted(STATE, available):
                        return 0
                    print('SKIP backup schedule paused')
                    return 3
                child = backup(STATE, available, subprocess.Popen)
            else:
                try:
                    available, error = free_bytes(), None
                except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                    available, error = None, type(exc).__name__
                result = tick(STATE, available, log_path=LOG, measurement_error=error)
                print(json.dumps(result, sort_keys=True))
                return int('delivery_pending' in result or result['level'] == 'error')
    except BlockingIOError:
        print('SKIP disk alarm lock busy')
        return 3 if args.action in ('backup', 'admit-backup') else 0
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        # Journald/stderr is the fallback when disk/permissions prevent our own log.
        failure = {'at': int(time.time()), 'level': 'error', 'monitoring_error': type(exc).__name__}
        if args.action == 'tick':
            try:
                with lock(STATE):
                    backup_pause(STATE, 'error', failure)
            except (OSError, ValueError):
                failure['pause_write_failed'] = True
        print(json.dumps(failure), file=sys.stderr)
        return 1
    if child is None:
        print('SKIP backup schedule paused by disk alarm')
        return 3
    return child.wait()  # no deadline or signal: never stop an active backup


def level(available: int) -> str:
    if available < 40 * 2**30:
        return 'critical'
    return 'warning' if available < 60 * 2**30 else 'healthy'


if __name__ == '__main__':
    raise SystemExit(main())
