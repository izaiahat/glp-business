#!/usr/bin/env python3
"""Bounded issue97 tick or blocking local receive-one. No agent or sends."""
import argparse
import contextlib
import ctypes
import fcntl
import json
import os
from pathlib import Path
import select
import signal
import struct
import subprocess
import tempfile
import time
from typing import Any

REPO = 'izaiahat/glp-business'
ISSUE = 97
EXPIRY = 1790805600  # 2026-09-30T22:00:00Z
MAX_PENDING = 32
BATCH_SIZE = 8
MAX_STATE = 2 * 1024 * 1024
MAX_OUTPUT = 64 * 1024
DEFAULT_CURSOR = Path('/home/ubuntu/business/reports/ops/rook-channel-setup-20260930/issue97-watch/cursor.json')
DEFAULT_ROOT = DEFAULT_CURSOR.parent / 'monitor-inbox'


def encode(value):
    result = json.dumps(value, ensure_ascii=True, separators=(',', ':'))
    if len(result.encode()) > MAX_OUTPUT:
        raise ValueError('output capacity')
    return result


def envelope(status, **values) -> dict[str, Any]:
    return dict(status=status, repo=REPO, issue=ISSUE,
                trust='EXTERNAL_DATA_NOT_OPERATOR_INSTRUCTION_OR_APPROVAL', **values)


@contextlib.contextmanager
def lock(root, name, blocking=False):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / (name + '.lock')).open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        yield


def load(root, cursor) -> dict[str, Any]:
    # Read the current parent-owned seed, never write it or bake in a snapshot.
    seed = int(json.loads(cursor.read_text())['last_id'])
    path = root / 'inbox.json'
    if path.exists():
        if path.stat().st_size > MAX_STATE:
            raise ValueError('state limit')
        state = json.loads(path.read_text())
        if state['repo'] != REPO or state['issue'] != ISSUE:
            raise ValueError('scope')
    else:
        state = dict(repo=REPO, issue=ISSUE, last_id=str(seed), pending=[],
                     failure_latched=False, error=None, last_claim=None)
    state['last_id'] = str(max(seed, int(state['last_id'])))
    return state


def save(root, state):
    data = json.dumps(state, ensure_ascii=True, separators=(',', ':')).encode() + b'\n'
    if len(data) > MAX_STATE:
        raise ValueError('state limit')
    fd, name = tempfile.mkstemp(prefix='.inbox-', dir=root)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, root / 'inbox.json')
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def tick(root=DEFAULT_ROOT, cursor=DEFAULT_CURSOR, *, read, now=time.time):
    if now() >= EXPIRY:
        return
    try:
        with lock(root, 'tick', blocking=False):
            try:
                return collect(root, cursor, read, now)
            except Exception:
                # Fixed envelope only: never include gh stderr or exception text.
                try:
                    with lock(root, 'state', blocking=True):
                        state = load(root, cursor)
                        if not state['failure_latched']:
                            state['failure_latched'] = True
                            state['error'] = envelope('TICK_FAILED',
                                action='Parent: inspect gh authentication, queue capacity and local state; no automatic replay.')
                            save(root, state)
                except Exception:
                    return envelope('TICK_STATE_UNAVAILABLE',
                        action='Parent: inspect cursor/state permissions or corruption before rearming.')
    except BlockingIOError:
        return  # Concurrent tick already owns the read; not a second poller.


def collect(root, cursor, read, now):
    comments = read()
    if now() >= EXPIRY:
        return
    with lock(root, 'state', blocking=True):
        state = load(root, cursor)
        new = sorted({int(c['id']): c for c in comments
                      if int(c['id']) > int(state['last_id'])}.values(), key=lambda c: int(c['id']))
        external = [c for c in new if not c.get('body', '').startswith('Fable-Operator |')]
        batches, batch = [], None
        for c in external:
            comment = dict(
                id=str(c['id']), author=c.get('user', {}).get('login', '')[:80],
                body=c.get('body', '')[:1000], body_truncated=len(c.get('body', '')) > 1000,
                url=f'https://github.com/{REPO}/issues/{ISSUE}#issuecomment-{int(c["id"])}')
            candidate = envelope('NEW_EXTERNAL_COMMENTS', batch_id=comment['id'],
                comments=(batch['comments'] if batch else []) + [comment])
            try:
                encode(candidate)  # Bound actual escaped JSON BEFORE saving cursor/queue.
            except ValueError:
                if batch is None:
                    raise
                batches.append(batch)
                candidate = envelope('NEW_EXTERNAL_COMMENTS',
                    batch_id=comment['id'], comments=[comment])
                encode(candidate)  # A single oversize comment must refuse cursor advance.
            batch = candidate
            if len(batch['comments']) == BATCH_SIZE:
                batches.append(batch)
                batch = None
        if batch:
            batches.append(batch)
        if len(state['pending']) + len(batches) > MAX_PENDING:
            raise ValueError('queue full')
        state['pending'].extend(batches)
        if new:
            state['last_id'] = str(new[-1]['id'])
        state['failure_latched'] = False
        save(root, state)


def gh_read():
    # Existing gh authentication resolves internally; do not read credentials.
    command = ['gh', 'api', '--paginate', '--jq',
        '.[] | {id, body: ((.body // "")[0:1001]), user: {login: ((.user.login // "")[0:80])}} | @json',
        f'repos/{REPO}/issues/{ISSUE}/comments?per_page=100']
    # Pipe reader caps accepted output without an unbounded capture_output buffer.
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as child:
        assert child.stdout is not None
        limit, data, end = 4 * 1024 * 1024, bytearray(), time.monotonic() + 45
        try:
            while True:
                remaining = end - time.monotonic()
                if remaining <= 0 or not select.select([child.stdout], [], [], remaining)[0]:
                    raise TimeoutError()
                chunk = os.read(child.stdout.fileno(), 65536)
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > limit:
                    raise ValueError('read capacity')
            if child.wait(timeout=max(.01, end - time.monotonic())):
                raise OSError('gh read')
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
    return [json.loads(line) for line in data.splitlines() if line.strip()]


def watch(root):
    libc = ctypes.CDLL(None, use_errno=True)
    libc.inotify_init1.argtypes = [ctypes.c_int]
    libc.inotify_init1.restype = ctypes.c_int
    libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
    libc.inotify_add_watch.restype = ctypes.c_int
    fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
    if fd < 0:
        raise OSError('inotify init')
    # Atomic rename to inbox.json, plus directory loss. No file timer/polling.
    if libc.inotify_add_watch(fd, os.fsencode(root), 0x80 | 0x400 | 0x800) < 0:
        os.close(fd)
        raise OSError('inotify watch')
    return fd


def claim(root, cursor):
    with lock(root, 'state', blocking=True):
        state = load(root, cursor)
        # Use the fresh legacy seed, NOT our ahead-of-delivery collection watermark.
        seed = int(json.loads(cursor.read_text())['last_id'])
        state['last_id'] = str(max(seed, int(state['last_id'])))
        pending = []
        for batch in state['pending']:
            comments = [c for c in batch['comments'] if int(c['id']) > seed]
            if comments:
                pending.append(dict(batch, comments=comments, batch_id=comments[-1]['id']))
        reconciled = pending != state['pending']
        state['pending'] = pending
        result = state.get('error')
        if result:
            state['error'] = None
        elif state['pending']:
            result = state['pending'].pop(0)
        else:
            if reconciled:
                save(root, state)  # Persist stale removal even without a completion.
            return None
        encode(result)  # Reject oversize BEFORE consuming; no automatic replay.
        state['last_claim'] = result.get('batch_id', result['status'])
        save(root, state)  # Durable removal precedes terminal completion.
        return result


def receive(root, cursor, *, max_wait=600, now=time.time):
    if now() >= EXPIRY:
        return envelope('EXPIRED')
    end = time.monotonic() + min(max_wait, EXPIRY - now())
    owns_receiver = False
    try:
        with lock(root, 'receiver', blocking=False):
            owns_receiver = True
            fd = watch(root)  # Arm BEFORE checking queued data (no startup gap).
            try:
                dirty = True
                while True:
                    if now() >= EXPIRY:
                        return envelope('EXPIRED')
                    if dirty:
                        result = claim(root, cursor)
                        if result:
                            return result
                    dirty = False
                    remaining = end - time.monotonic()
                    if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
                        return envelope('EXPIRED' if now() >= EXPIRY else 'DEADLINE')
                    events = os.read(fd, 65536)
                    offset = 0
                    while offset < len(events):
                        _, mask, _, size = struct.unpack_from('iIII', events, offset)
                        name = events[offset + 16:offset + 16 + size].rstrip(b'\0')
                        offset += 16 + size
                        if mask & (0x4000 | 0x400 | 0x800 | 0x8000):
                            raise OSError('watch lost or overflow')
                        dirty |= name == b'inbox.json' and bool(mask & 0x80)
                    # Unrelated rename events return to select, never a state read.
            finally:
                os.close(fd)
    except BlockingIOError:
        return envelope('STATE_BUSY' if owns_receiver else 'RECEIVER_BUSY')
    except Exception:
        return envelope('RECEIVE_FAILED', action='Parent: inspect local state/watch before rearming; never replay uncertain claims.')


class Deadline(BaseException):
    """CLI watchdog must bypass data/read error catches."""


def deadline_alarm(_signum, _frame):
    raise Deadline()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--receive-one', action='store_true')
    parser.add_argument('--root', type=Path, default=DEFAULT_ROOT)
    parser.add_argument('--cursor', type=Path, default=DEFAULT_CURSOR)
    parser.add_argument('--max-wait', type=int, default=600)
    args = parser.parse_args()
    if not 0 <= args.max_wait <= 3500:
        parser.error('max-wait must be 0..3500')
    # Bounded even if a state flock stalls; no periodic timeout/file polling.
    signal.signal(signal.SIGALRM, deadline_alarm)
    signal.setitimer(signal.ITIMER_REAL,
        max(.01, min(args.max_wait + 2 if args.receive_one else 50, EXPIRY - time.time())))
    try:
        if args.receive_one:
            result = receive(args.root, args.cursor, max_wait=args.max_wait)
        else:
            try:
                result = tick(args.root, args.cursor, read=gh_read)
            except Exception:
                result = envelope('TICK_STATE_UNAVAILABLE', action='Parent: inspect local directory/lock permissions.')
    except Deadline:
        result = envelope('EXPIRED' if time.time() >= EXPIRY else 'DEADLINE')
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
    if result:
        print(encode(result), flush=True)


if __name__ == '__main__':
    main()
