#!/usr/bin/env python3
"""One re-entrant process lock for every GLP content-base mutation."""
from __future__ import annotations

import fcntl
import os
import stat
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

CARRIER_LOCK = Path("/home/ubuntu/.hermes/state/glp-seo-carrier.lock")
LOCK_FD_ENV = "GLP_SEO_CARRIER_LOCK_FD"

_guard = threading.RLock()
_depth = 0
_fd: int | None = None
_owned_fd = False


def _same_file(fd: int, path: Path) -> bool:
    try:
        opened = os.fstat(fd)
        target = os.stat(path)
    except (OSError, ValueError):
        return False
    return stat.S_ISREG(opened.st_mode) and (opened.st_dev, opened.st_ino) == (
        target.st_dev,
        target.st_ino,
    )


def _inherited_fd(path: Path) -> int | None:
    raw = os.environ.get(LOCK_FD_ENV)
    if not raw or not raw.isdecimal():
        return None
    fd = int(raw)
    return fd if _same_file(fd, path) else None


@contextmanager
def carrier_lock(path: Path = CARRIER_LOCK) -> Iterator[int]:
    """Hold the standard lock, reusing an inherited apply-chain descriptor.

    ``apply_opus_article_release`` passes this descriptor into its builder and
    publisher children.  The lock therefore spans the current-pointer reread,
    candidate build, deploy, live verification, ledgers, and pointer advance.
    """
    global _depth, _fd, _owned_fd
    resolved = path.resolve()
    with _guard:
        if _depth:
            if _fd is None or not _same_file(_fd, resolved):
                raise RuntimeError("carrier_lock_reentrant_path_mismatch")
            _depth += 1
            try:
                yield _fd
            finally:
                _depth -= 1
            return

        resolved.parent.mkdir(parents=True, exist_ok=True)
        inherited = _inherited_fd(resolved)
        if inherited is None:
            descriptor = os.open(
                resolved,
                os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            owned = True
        else:
            descriptor = inherited
            # A duplicated inherited open-file description owns the same flock.
            # LOCK_NB proves it instead of trusting an environment variable.
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError("inherited_carrier_lock_not_held") from error
            owned = False

        _fd, _owned_fd, _depth = descriptor, owned, 1
        os.set_inheritable(descriptor, True)
        prior_env = os.environ.get(LOCK_FD_ENV)
        os.environ[LOCK_FD_ENV] = str(descriptor)
        try:
            yield descriptor
        finally:
            _depth = 0
            _fd = None
            _owned_fd = False
            if prior_env is None:
                os.environ.pop(LOCK_FD_ENV, None)
            else:
                os.environ[LOCK_FD_ENV] = prior_env
            if owned:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)


def inherited_lock_fd() -> int | None:
    """Return the verified standard-lock descriptor for child pass_fds."""
    with _guard:
        if _depth and _fd is not None and _same_file(_fd, CARRIER_LOCK.resolve()):
            return _fd
    return _inherited_fd(CARRIER_LOCK.resolve())
