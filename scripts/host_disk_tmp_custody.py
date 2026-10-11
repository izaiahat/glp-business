"""Fail-closed custody checks for the explicit aeo-declared temporary class.

Run only inside the cleanup singleton. These checks authorize no deletion:
callers retain their age, payload, policy, open-file and inode rechecks.
"""
import fnmatch
import json
import os
from pathlib import Path
import re
import stat
import time

PROC = Path('/proc')
SYMLINK_ROOTS = tuple(map(Path, ('/home/ubuntu', '/root', '/etc', '/tmp')))
SCHEDULE_ROOTS = tuple(map(Path, ('/run/systemd', '/usr/lib/systemd',
                                '/usr/local/lib/systemd', '/var/spool/cron')))
REQUIRED_FILES = (Path('/home/ubuntu/.hermes/cron/jobs.json'),)


def declared_directory(path, root):
    return (path.parent == root and path.name.startswith('aeo-declared-')
            and path.name != 'aeo-declared-' and path.is_dir() and not path.is_symlink())


def _budget(deadline):
    if time.monotonic() >= deadline:
        raise TimeoutError('tmp custody deadline')


def _overlaps(target, path):
    target = Path(str(target).removesuffix(' (deleted)'))
    return target == path or path in target.parents or target in path.parents


def _under(target, path):
    target = Path(str(target).removesuffix(' (deleted)'))
    return target == path or path in target.parents


def _identity(path):
    s = path.lstat()
    return (s.st_dev, s.st_ino, s.st_mode, s.st_size, s.st_mtime_ns, s.st_ctime_ns)


def _schedule(path):
    return any(part == 'systemd' or part == 'cron' or part == 'crontab'
               or part.startswith('cron.') for part in path.parts)


def _text_reference(text, path):
    # Include basename/relative and glob references; ambiguous mentions retain.
    if str(path) in text or path.name in text:
        return True
    return any(fnmatch.fnmatchcase(path.name, token.rsplit('/', 1)[-1])
               for token in re.findall(r"[^\s\"'<>;=]+", text)
               if 'aeo-declared-' in token)


def _reference_scan(path, deadline, inodes):
    identities = {}
    stack = [(root, False, True) for root in SYMLINK_ROOTS]
    # Some distributions omit these optional scheduler directories. Record
    # their absence and recheck it, rather than swallowing permission failures.
    stack += [(root, True, False) for root in SCHEDULE_ROOTS]
    stack += [(p, True, True) for p in REQUIRED_FILES]
    seen = set()
    while stack:
        _budget(deadline)
        source, schedule, required = stack.pop()
        mode_key = (source, schedule or _schedule(source), required)
        if mode_key in seen:
            continue
        seen.add(mode_key)
        try:
            expected = _identity(source)
        except FileNotFoundError:
            if required:
                raise ValueError('missing tmp custody source: ' + str(source))
            identities[source] = None
            continue
        if source in identities and identities[source] != expected:
            raise ValueError('tmp custody source changed between scan modes: ' + str(source))
        identities[source] = expected
        mode = expected[2]
        if stat.S_ISLNK(mode):
            target = source.resolve(strict=False)
            if _overlaps(target, path):
                raise ValueError('symlink tmp custody: ' + str(source))
            try:
                linked = source.stat()
            except FileNotFoundError:
                linked = None  # Positively dangling links have no live inode.
            if linked is not None and (linked.st_dev, linked.st_ino) in inodes:
                raise ValueError('symlink inode tmp custody: ' + str(source))
            # Unit/crontab symlinks may point outside the scanned trees.
            if schedule or _schedule(source):
                stack.append((target, True, True))
        elif stat.S_ISDIR(mode):
            if source == path:
                continue  # Caller inventories the candidate without following links.
            with os.scandir(source) as entries:
                stack.extend((Path(e.path), schedule or _schedule(source), True) for e in entries)
        elif schedule or _schedule(source):
            if not stat.S_ISREG(mode) or expected[3] > 32 * 1024 ** 2:
                raise ValueError('unreadable tmp schedule source: ' + str(source))
            text = source.read_text(errors='strict')
            if source in REQUIRED_FILES:
                # Parse required native scheduler evidence before accepting its
                # absence of references, including JSON-escaped path strings.
                data = json.loads(text)
                if not isinstance(data, (dict, list)):
                    raise ValueError('invalid native cron custody evidence')
                text = json.dumps(data, ensure_ascii=False)
            if _text_reference(text, path):
                raise ValueError('cron/systemd tmp custody: ' + str(source))
        if _identity(source) != expected:
            raise ValueError('tmp custody source changed: ' + str(source))
    _validate_references(identities, deadline)
    return identities


def _validate_references(identities, deadline):
    for source, expected in identities.items():
        _budget(deadline)
        try:
            actual = _identity(source)
        except FileNotFoundError:
            actual = None
        if actual != expected:
            raise ValueError('tmp custody inventory changed: ' + str(source))


def _process_stat(proc):
    text = (proc / 'stat').read_text()
    fields = text.rsplit(')', 1)[1].split()
    return fields[0], int(fields[6]), fields[19]  # state, flags, starttime


def _process_scan(path, deadline, inodes):
    # A private execution namespace cannot establish host-wide process custody.
    if os.geteuid() != 0 or (PROC / '1/comm').read_text().strip() not in {'systemd', 'init'}:
        raise ValueError('host process custody unavailable')
    mounts = (PROC / 'mounts').read_text().splitlines()
    proc_mounts = [line.split() for line in mounts if len(line.split()) >= 4
                   and line.split()[1:3] == ['/proc', 'proc']]
    if len(proc_mounts) != 1 or any(option.startswith(('hidepid=', 'subset='))
                                  for option in proc_mounts[0][3].split(',')):
        raise ValueError('incomplete proc mount visibility')
    with os.scandir(PROC) as entries:
        processes = [Path(e.path) for e in entries if e.name.isdigit()]
    if not processes:
        raise ValueError('empty process custody inventory')
    for proc in processes:
        _budget(deadline)
        try:
            before = _process_stat(proc)
            if before[0] in {'Z', 'X'} or before[1] & 0x00200000:  # proven zombie/kernel thread
                continue
            if _under(os.readlink(proc / 'cwd'), path):
                raise ValueError('live process cwd tmp custody')
            with os.scandir(proc / 'fd') as entries:
                descriptors = [Path(e.path) for e in entries]
            for descriptor in descriptors:
                _budget(deadline)
                try:
                    target = os.readlink(descriptor)
                except FileNotFoundError:
                    continue  # Closed descriptor; unreadable descriptors still refuse.
                if target.startswith('/') and _under(target, path):
                    raise ValueError('live process open file tmp custody')
                try:
                    opened = descriptor.stat()
                except FileNotFoundError:
                    continue
                if (opened.st_dev, opened.st_ino) in inodes:
                    raise ValueError('live process open inode tmp custody')
            if _process_stat(proc) != before:
                raise ValueError('process identity changed during tmp custody scan')
        except FileNotFoundError:
            # Only a positively vanished PID is an exited process.
            try:
                proc.lstat()
            except FileNotFoundError:
                continue
            raise ValueError('incomplete process tmp custody: ' + proc.name)
    with os.scandir(PROC) as entries:
        after = {Path(e.path) for e in entries if e.name.isdigit()}
    if after - set(processes):
        raise ValueError('new process during tmp custody scan')


def require_unreferenced(path, deadline):
    """Missing, unreadable, changed or timed-out evidence always raises."""
    from host_disk_large_classes import snapshot
    members, _ = snapshot(path, deadline)
    inodes = {(r[1], r[2]) for r in members}
    identities = _reference_scan(path, deadline, inodes)
    _process_scan(path, deadline, inodes)
    _validate_references(identities, deadline)
