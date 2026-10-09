"""No-follow, fail-closed bounded scans shared by the host disk pruner."""
import os
from pathlib import Path
import stat
import time

class ScanBudget(Exception):
    pass


def tree_stats(path, *, cutoff=None, deadline=None, protected=lambda p: False, allow_symlinks=False):
    """Check EVERY descendant age before whole-directory removal; young stops early.

    Never derive eligibility from a directory's mtime alone. An incomplete scan
    refuses removal. Count allocated blocks once per inode in the same pass.
    """
    p = Path(path)
    stack = [p]
    newest = amount = 0
    seen = set()
    while stack:
        if deadline is not None and time.monotonic() >= deadline:
            raise ScanBudget()
        p = stack.pop()
        if protected(p):
            return float('inf'), 0
        try:
            s = p.lstat()
            if stat.S_ISLNK(s.st_mode) and not allow_symlinks:
                return float('inf'), 0
            newest = max(newest, s.st_mtime)
            if cutoff is not None and newest >= cutoff:
                return newest, 0
            key = (s.st_dev, s.st_ino)
            if key not in seen:
                amount += s.st_blocks*512
                seen.add(key)
            if stat.S_ISDIR(s.st_mode):
                with os.scandir(p) as entries:
                    for e in entries:
                        if deadline is not None and time.monotonic() >= deadline:
                            raise ScanBudget()
                        stack.append(Path(e.path))
        except OSError:
            return float('inf'), 0
    return newest, amount


def discovery(root, cursor, *, deadline, skip):
    """No-follow DFS; durable progress is acknowledged names, never offsets.

    Retain each live parent's iterator and handled set across child visits, so
    wide sibling trees are not repeatedly enumerated from the beginning. These
    caches are invocation-local and closed on every exit. A fresh sweep after
    any progress discovers siblings added while visiting children; a sweep with
    no new names finishes the frame. Each resumed invocation enumerates afresh.
    Eligibility, skip checks and the caller's effect gates are never cached.
    """
    root = Path(root).absolute()
    frames = cursor.setdefault('frames', [[str(root), []]])
    if any(not isinstance(frame[1], list) for frame in frames):
        frames[:] = [[str(root), []]]
    live = {}

    def drop():
        frame = frames.pop()
        state = live.pop(id(frame), None)
        if state is not None:
            state['entries'].close()

    try:
        while frames:
            if time.monotonic() >= deadline:
                raise ScanBudget()
            frame = frames[-1]
            directory, handled = frame
            parent = Path(directory)
            if (parent != root and root not in parent.parents) or parent.is_symlink() or skip(parent):
                drop(); continue
            try:
                state = live.get(id(frame))
                if state is None:
                    state = {'entries': os.scandir(parent), 'visited': set(handled), 'advanced': False}
                    live[id(frame)] = state
                try:
                    entry = next(state['entries'])
                except StopIteration:
                    state['entries'].close()
                    if state['advanced']:
                        state['entries'] = os.scandir(parent)
                        state['advanced'] = False
                    else:
                        drop()
                    continue
                if time.monotonic() >= deadline:
                    raise ScanBudget()
                if entry.name in state['visited']:
                    continue
                path = Path(entry.path)
                # DirEntry type bits can predate a caller mutation while this
                # parent iterator was suspended. Read the current no-follow type.
                try:
                    mode = path.lstat().st_mode
                except FileNotFoundError:
                    handled.append(entry.name)
                    state['visited'].add(entry.name)
                    state['advanced'] = True
                    continue
                if stat.S_ISLNK(mode) or skip(path):
                    handled.append(entry.name)
                    state['visited'].add(entry.name)
                    state['advanced'] = True
                    continue
                is_dir = stat.S_ISDIR(mode)
                descend = yield path, is_dir
                # Advance only AFTER the caller completes the gate/effect.
                handled.append(entry.name)
                state['visited'].add(entry.name)
                state['advanced'] = True
                if is_dir and descend:
                    frames.append([str(path), []])
            except (FileNotFoundError, PermissionError):
                drop()
    finally:
        for state in live.values():
            state['entries'].close()

