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
    """Incremental no-follow scandir DFS with mutation-safe handled-name cursors."""
    root = Path(root).absolute()
    frames = cursor.setdefault('frames', [[str(root), []]])
    if any(not isinstance(frame[1], list) for frame in frames):
        frames[:] = [[str(root), []]]
    while frames:
        if time.monotonic() >= deadline:
            raise ScanBudget()
        directory, handled = frames[-1]
        parent = Path(directory)
        if (parent != root and root not in parent.parents) or parent.is_symlink() or skip(parent):
            frames.pop(); continue
        visited = set(handled)
        try:
            with os.scandir(parent) as entries:
                for entry in entries:
                    if time.monotonic() >= deadline:
                        raise ScanBudget()
                    if entry.name in visited:
                        continue
                    path = Path(entry.path)
                    if entry.is_symlink() or skip(path):
                        handled.append(entry.name); visited.add(entry.name); continue
                    is_dir = entry.is_dir(follow_symlinks=False)
                    descend = yield path, is_dir
                    # Advance only AFTER the caller completes the gate/effect.
                    handled.append(entry.name); visited.add(entry.name)
                    if is_dir and descend:
                        frames.append([str(path), []]); break
                else:
                    frames.pop()
        except (FileNotFoundError, PermissionError):
            frames.pop()
