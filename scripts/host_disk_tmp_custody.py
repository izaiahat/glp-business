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
SYMLINK_ROOTS = tuple(map(Path, ('/home/ubuntu', '/root', '/etc', '/tmp', '/run/user')))
CRON_ROOTS = tuple(map(Path, ('/etc/crontab', '/etc/anacrontab', '/etc/cron.d',
                            '/etc/cron.hourly', '/etc/cron.daily', '/etc/cron.weekly',
                            '/etc/cron.monthly', '/var/spool/cron')))
# Unit load directories, not systemd's executable/library or runtime-data trees.
SYSTEMD_ROOTS = tuple(map(Path, (
    '/etc/systemd/system', '/etc/systemd/user',
    '/etc/systemd/system.control', '/etc/systemd/user.control', '/etc/systemd/system.attached',
    '/run/systemd/system', '/run/systemd/user', '/run/systemd/transient',
    '/run/systemd/system.control', '/run/systemd/user.control', '/run/systemd/system.attached',
    '/run/systemd/generator', '/run/systemd/generator.early', '/run/systemd/generator.late',
    '/usr/lib/systemd/system', '/usr/lib/systemd/user',
    '/usr/local/lib/systemd/system', '/usr/local/lib/systemd/user',
)))
SCHEDULE_ROOTS = CRON_ROOTS + SYSTEMD_ROOTS
REQUIRED_FILES = (Path('/home/ubuntu/.hermes/cron/jobs.json'),)
UNIT_SUFFIXES = {'.service', '.socket', '.device', '.mount', '.automount',
                 '.swap', '.target', '.path', '.timer', '.slice', '.scope'}


# One explicit rebuildable-payload entry, not a generic name/suffix heuristic.
# Node fs.mkdtemp appends six ASCII alphanumerics. Only this producer and its
# external, retained rebuild inputs are admitted; a candidate cannot vouch for
# its own rebuildability. Existing age/process/payload/custody guards still apply.
DECLARED_NAME = re.compile(r'aeo-declared-[A-Za-z0-9]{6}\Z')
DECLARED_REBUILD_ROOT = Path('/home/ubuntu/business/site-system/flagship-v2/candidates/glp34-provider-families-20260912')
DECLARED_REBUILD_FILES = (
    'tests/lastmod-declared-routes.test.mjs',
    'scripts/postbuild-aeo-surface.mjs',
    'data/page-content-provenance.json',
    'data/llms-priority.json',
)
DECLARED_REBUILD_DIRS = ('src', 'public')


def declared_directory(path, root):
    return (path.parent == root and DECLARED_NAME.fullmatch(path.name) is not None
            and path.is_dir() and not path.is_symlink())


def _rebuild_sources(candidate, deadline):
    """Require fixed external rebuild inputs and retain identities for recheck."""
    root = DECLARED_REBUILD_ROOT
    identities = {}
    paths = [(root, True)] + [(root / p, False) for p in DECLARED_REBUILD_FILES]
    paths += [(root / p, True) for p in DECLARED_REBUILD_DIRS]
    for source, directory in paths:
        _budget(deadline)
        try:
            if source.resolve(strict=True) != source or _overlaps(source, candidate):
                raise ValueError('aliased or overlapping rebuild source: ' + str(source))
            identity = _identity(source)
            if not (stat.S_ISDIR(identity[2]) if directory else stat.S_ISREG(identity[2])):
                raise ValueError('invalid rebuild source: ' + str(source))
            identities[source] = identity
        except (OSError, RuntimeError) as error:
            raise ValueError('rebuild source unavailable: ' + str(source)) from error
    return identities


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


def _user_systemd_source(path):
    parts = path.parts
    if parts[:2] == ('/', 'home') and len(parts) >= 3:
        tail = parts[3:]
    elif parts[:2] == ('/', 'root'):
        tail = parts[2:]
    else:
        return False
    return (tail[:3] == ('.config', 'systemd', 'user') or
            tail[:4] == ('.local', 'share', 'systemd', 'user'))


def _systemd_source(path):
    parts = path.parts
    runtime = (len(parts) >= 6 and parts[:3] == ('/', 'run', 'user')
               and parts[3].isdigit() and parts[4] == 'systemd'
               and parts[5] in {'user', 'user.control', 'user.attached', 'transient',
                                'generator', 'generator.early', 'generator.late'})
    return runtime or _user_systemd_source(path) or any(_under(path, root) for root in SYSTEMD_ROOTS)


def _schedule(path):
    # An arbitrary package named cron/systemd is not a registered scheduler.
    return (path in REQUIRED_FILES or _systemd_source(path) or
            any(_under(path, root) for root in CRON_ROOTS))


def _masked_unit(source, target, linked):
    return (source.suffix in UNIT_SUFFIXES and _systemd_source(source) and
            target == Path('/dev/null') and os.readlink(source) == '/dev/null' and
            linked is not None and
            stat.S_ISCHR(linked.st_mode) and
            (os.major(linked.st_rdev), os.minor(linked.st_rdev)) == (1, 3))


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
                if _masked_unit(source, target, linked):
                    # A real masked unit has no executable scheduler content.
                    # Keep the null-device identity in the final drift check.
                    null_identity = _identity(target)
                    if target in identities and identities[target] != null_identity:
                        raise ValueError('masked unit target identity changed')
                    identities[target] = null_identity
                else:
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
    rebuild = _rebuild_sources(path, deadline)
    members, _ = snapshot(path, deadline)
    inodes = {(r[1], r[2]) for r in members}
    identities = _reference_scan(path, deadline, inodes)
    _process_scan(path, deadline, inodes)
    _validate_references(identities, deadline)
    _validate_references(rebuild, deadline)
