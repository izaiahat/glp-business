"""Fail-closed custody checks for the explicit aeo-declared temporary class.

Run only inside the cleanup singleton. These checks authorize no deletion:
callers retain their age, payload, policy, open-file and inode rechecks.
"""
import fnmatch
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
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
SYSTEMD_GENERATOR_ROOTS = tuple(map(Path, (
    '/run/systemd/generator', '/run/systemd/generator.early',
    '/run/systemd/generator.late')))


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
DECLARED_REBUILD_DIRS = ('scripts', 'src', 'data', 'public')
DECLARED_CONTENT_BASE = Path('/home/ubuntu/business/reports/glp-clinic-scout/seo-machine/current-content-base.json')
DECLARED_BUSINESS_ROOT = Path('/home/ubuntu/business')
DECLARED_BASE_LOCK = Path('/home/ubuntu/.hermes/locks/glp-price-index-weekly.lock')


def require_declared_retirement_authority():
    """Contain the unaccepted class until both outstanding contracts exist.

    Stable mutable files do not authenticate a compatible rebuilding producer.
    The current leases also do not exclude systemd/crontab/symlink publishers
    or dispatch readers throughout removal. Repeated snapshots cannot replace
    either obligation. There is deliberately no CLI/environment bypass; a later
    source change must establish both contracts before this class can retire.
    Other registered scratch/cache classes keep their existing admission.
    """
    raise ValueError('declared tmp retirement unavailable: authenticated compatible '
                     'producer and continuous reference/reader custody not established')


def _predecessor_sources(candidate, deadline):
    """External inputs read by the fixed producer's fixture/prepareLastmod.

    These are the real pointer -> receipt/manifest -> predecessor contracts.
    They authenticate the predecessor bytes, not the mutable producer/copy tree
    itself. A complete known-good producer admission remains a separate duty.
    """
    inventory = {}

    def read(path, digest=None):
        path = Path(path)
        if path.resolve(strict=True) != path or _overlaps(path, candidate):
            raise ValueError('aliased/overlapping rebuild predecessor')
        identity, actual, raw = _rebuild_content(path, deadline, retain_bytes=True)
        if digest is not None and actual != digest:
            raise ValueError('rebuild predecessor digest: ' + str(path))
        inventory[path] = (identity, actual)
        return raw

    pointer = json.loads(read(DECLARED_CONTENT_BASE))
    if pointer.get('schema') != 'glp-opus-current-content-base-v1' or pointer.get('status') != 'PASS':
        raise ValueError('rebuild predecessor pointer status')
    receipt = json.loads(read(pointer['final_receipt']['path'], pointer['final_receipt']['sha256']))
    manifest = json.loads(read(pointer['manifest']['path'], pointer['manifest']['sha256']))
    if (receipt.get('status') != 'PASS_PRODUCTION_TERMINAL' or
            not pointer.get('deployment_id') or
            receipt.get('deployment', {}).get('deployment_id') != pointer['deployment_id'] or
            manifest.get('release_root') != pointer['release_root']):
        raise ValueError('rebuild predecessor receipt/manifest binding')
    root = Path(pointer['release_root'])
    if not root.is_absolute() or root.resolve(strict=True) != root or _overlaps(root, candidate):
        raise ValueError('rebuild predecessor root')
    inventory[root] = _identity(root)
    rows = {}
    for row in manifest['release_rows']:
        relative = row['path']
        parts = relative.split('/')
        if (not relative or Path(relative).is_absolute() or any(p in ('', '.', '..') for p in parts)
                or relative in rows or not re.fullmatch('[0-9a-f]{64}', row['sha256'])):
            raise ValueError('rebuild predecessor manifest entry')
        rows[relative] = row['sha256']

    def verified(relative):
        if relative not in rows:
            raise ValueError('missing rebuild predecessor dependency: ' + str(relative))
        return read(root / relative, rows[relative])

    verified('scripts/postbuild-aeo-surface.mjs')
    provenance = json.loads(verified('data/page-content-provenance.json'))
    if provenance.get('schema') != 'glp-page-content-provenance-v1' or not isinstance(provenance.get('pages'), dict):
        raise ValueError('rebuild predecessor provenance schema')
    for route in ('/how-we-rate/', '/reviews/orderlymeds/'):
        verified('public/' + route[1:] + 'index.html')
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', provenance['pages'].get(route, {}).get('lastmod', '')):
            raise ValueError('rebuild predecessor route date')
    for slug in ('ezra-glp-1-reviews', 'top-rated-glp-1-providers'):
        carrier = json.loads(verified('data/carrier-articles/' + slug + '/manifest.json'))
        raw = verified(carrier['snapshot'])
        if hashlib.sha256(raw).hexdigest() != carrier['html_sha256']:
            raise ValueError('rebuild predecessor carrier snapshot')
        for binding in carrier['bindings']:
            verified(binding['path'])
        for item in carrier['files']:
            raw = verified(item['path'])
            if len(raw) != item['bytes'] or hashlib.sha256(raw).hexdigest() != item['sha256']:
                raise ValueError('rebuild predecessor carrier asset')
    return inventory


def declared_directory(path, root):
    return (path.parent == root and DECLARED_NAME.fullmatch(path.name) is not None
            and path.is_dir() and not path.is_symlink())


def _rebuild_content(source, deadline, *, retain_bytes=False):
    """Bind actual bytes from a stable no-follow descriptor, not directory mtime."""
    _budget(deadline)
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size == 0:
            raise ValueError('empty or invalid rebuild source: ' + str(source))
        digest = hashlib.sha256()
        chunks = []
        while True:
            _budget(deadline)
            block = os.read(fd, 1024 * 1024)
            if not block:
                break
            digest.update(block)
            if retain_bytes:
                chunks.append(block)
        after = os.fstat(fd)
        identity = lambda s: (s.st_dev, s.st_ino, s.st_mode, s.st_size,
                              s.st_mtime_ns, s.st_ctime_ns)
        if identity(before) != identity(after) or _identity(source) != identity(after):
            raise ValueError('rebuild source changed during read: ' + str(source))
        result = identity(after), digest.hexdigest()
        return (*result, b''.join(chunks)) if retain_bytes else result
    finally:
        os.close(fd)


def _rebuild_sources(candidate, deadline):
    """Bind all producer copy trees and the out-of-tree carrier dependencies.

    This stable content inventory is necessary but NOT sufficient authority to
    retire a target: neither it nor the cleanup singleton fences reference writers.
    """
    require_declared_retirement_authority()
    root = DECLARED_REBUILD_ROOT
    identities = {}
    paths = [root] + [root / p for p in DECLARED_REBUILD_FILES]
    paths += [root / p for p in DECLARED_REBUILD_DIRS]
    try:
        if root.resolve(strict=True) != root or _overlaps(root, candidate):
            raise ValueError('aliased or overlapping rebuild source: ' + str(root))
        for relative in DECLARED_REBUILD_FILES:
            if not stat.S_ISREG(_identity(root / relative)[2]):
                raise ValueError('invalid rebuild source entrypoint: ' + relative)
        for relative in DECLARED_REBUILD_DIRS:
            source = root / relative
            if not stat.S_ISDIR(_identity(source)[2]):
                raise ValueError('invalid rebuild source directory: ' + relative)
            with os.scandir(source) as entries:
                if next(entries, None) is None:
                    raise ValueError('empty rebuild source directory: ' + relative)
        for slug in ('ezra-glp-1-reviews', 'top-rated-glp-1-providers'):
            source = root / 'data/carrier-articles' / slug / 'manifest.json'
            manifest_identity, manifest_digest, manifest_bytes = _rebuild_content(source, deadline, retain_bytes=True)
            manifest = json.loads(manifest_bytes)
            if _rebuild_content(source, deadline) != (manifest_identity, manifest_digest):
                raise ValueError('rebuild source carrier manifest changed')
            relatives = [manifest['snapshot']]
            relatives += [r['path'] for r in manifest['bindings']]
            relatives += [r['path'] for r in manifest['files']]
            for relative in relatives:
                name = Path(relative)
                if name.is_absolute() or '..' in name.parts or not name.parts:
                    raise ValueError('invalid rebuild source carrier dependency')
                paths.append(root / name)
            for row in manifest['files']:
                dependency = root / row['path']
                identity, digest = _rebuild_content(dependency, deadline)
                if identity[3] != row['bytes'] or digest != row['sha256']:
                    raise ValueError('rebuild source carrier asset digest')
            if _rebuild_content(root / manifest['snapshot'], deadline)[1] != manifest['html_sha256']:
                raise ValueError('rebuild source carrier snapshot digest')
        while paths:
            _budget(deadline)
            source = paths.pop()
            if source in identities:
                continue
            if source.resolve(strict=True) != source or _overlaps(source, candidate):
                raise ValueError('aliased or overlapping rebuild source: ' + str(source))
            identity = _identity(source)
            if stat.S_ISDIR(identity[2]):
                identities[source] = identity
                # The root itself contains other, uncopied runtime/build output.
                if source != root:
                    with os.scandir(source) as entries:
                        paths.extend(Path(e.path) for e in entries)
                    if _identity(source) != identity:
                        raise ValueError('rebuild source directory changed: ' + str(source))
            elif stat.S_ISREG(identity[2]):
                identities[source] = _rebuild_content(source, deadline)
            else:
                raise ValueError('invalid rebuild source: ' + str(source))
        identities.update(_predecessor_sources(candidate, deadline))
    except (OSError, RuntimeError, KeyError, TypeError, UnicodeError) as error:
        raise ValueError('rebuild source unavailable or incomplete') from error
    return identities


def _validate_rebuild(identities, deadline):
    for source, expected in identities.items():
        _budget(deadline)
        actual = (_rebuild_content(source, deadline) if len(expected) == 2
                  else _identity(source))
        if actual != expected:
            raise ValueError('rebuild source inventory changed: ' + str(source))


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


class _ReferenceIdentities(dict):
    def __init__(self):
        super().__init__()
        self.systemd_units = {}
        self.systemd_paths = set()


def _systemd_show(properties, deadline, unit=None):
    """Query only the local system manager; no user/remote bus environment."""
    _budget(deadline)
    command = ['/usr/bin/systemctl', '--system', '--no-pager', 'show', '--all']
    command += ['--property=' + key for key in properties]
    if unit is not None:
        command += ['--', unit]
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, check=False,
                                timeout=min(5, max(0.001, deadline - time.monotonic())),
                                env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C',
                                     'SYSTEMD_COLORS': '0'})
    except (OSError, UnicodeError, subprocess.SubprocessError) as error:
        raise ValueError('systemd custody manager unavailable') from error
    _budget(deadline)
    if result.returncode or result.stderr or len(result.stdout) > 1024 * 1024:
        raise ValueError('systemd custody manager query refused')
    values = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition('=')
        if not separator or key not in properties or key in values:
            raise ValueError('malformed systemd custody properties')
        values[key] = value
    if set(values) != set(properties):
        raise ValueError('incomplete systemd custody properties')
    return values


def _systemd_paths(value, *, empty=False):
    if not value and empty:
        return ()
    names = value.split(' ')
    if not value or len(names) > 256 or len(names) != len(set(names)):
        raise ValueError('invalid systemd custody paths')
    paths = []
    for name in names:
        path = Path(name)
        if (not name or not path.is_absolute() or str(path) != name
                or '..' in path.parts or len(name) > 4096
                or any(char.isspace() or ord(char) < 32 or char == '\\' for char in name)):
            raise ValueError('invalid systemd custody path')
        paths.append(path)
    return tuple(paths)


def _loaded_systemd_unit(unit, deadline):
    roots = _systemd_paths(_systemd_show(('UnitPath',), deadline)['UnitPath'])
    keys = ('Id', 'LoadState', 'FragmentPath', 'SourcePath', 'DropInPaths', 'NeedDaemonReload')
    values = _systemd_show(keys, deadline, unit)
    if (values['Id'] != unit or values['LoadState'] != 'loaded'
            or values['NeedDaemonReload'] != 'no'):
        raise ValueError('unresolved or stale systemd custody unit')
    fragment = _systemd_paths(values['FragmentPath'])
    source = _systemd_paths(values['SourcePath'], empty=True)
    drops = _systemd_paths(values['DropInPaths'], empty=True)
    if (len(fragment) != 1 or len(source) > 1 or fragment[0].name != unit
            or fragment[0].parent not in roots or any(p.suffix != '.conf' for p in drops)):
        raise ValueError('unsupported systemd custody selection')
    return roots, tuple(values[key] for key in keys), tuple(dict.fromkeys(fragment + source + drops))


def _bind_systemd_path(identities, path, *, optional=False, directory=False):
    try:
        identity = _identity(path)
    except FileNotFoundError:
        if not optional:
            raise ValueError('missing loaded systemd custody source: ' + str(path))
        identity = None
    if identity is not None:
        if (path.resolve(strict=True) != path or
                not (stat.S_ISDIR(identity[2]) if directory else stat.S_ISREG(identity[2]))):
            raise ValueError('aliased or invalid systemd custody source: ' + str(path))
        identities.systemd_paths.add(path)
    if path in identities and identities[path] != identity:
        raise ValueError('systemd custody source changed: ' + str(path))
    identities[path] = identity


def _generated_systemd_dependency(source, target, identities, deadline):
    """Resolve only generated ../unit wants/requires links in real UnitPath.

    Their link name names a system-manager dependency, not an external file.
    Other dangling links retain the ordinary missing-source refusal.
    """
    root = source.parent.parent
    owner, separator, relationship = source.parent.name.rpartition('.')
    valid_unit = lambda name: (re.fullmatch(r'[A-Za-z0-9_.:@-]+', name) is not None
                               and Path(name).suffix in UNIT_SUFFIXES
                               and len(name) > len(Path(name).suffix))
    if (root not in SYSTEMD_GENERATOR_ROOTS or not separator
            or relationship not in {'wants', 'requires'} or not valid_unit(owner)
            or not valid_unit(source.name) or os.readlink(source) != '../' + source.name
            or target != root / source.name):
        return None
    _bind_systemd_path(identities, root, directory=True)
    _bind_systemd_path(identities, source.parent, directory=True)
    _bind_systemd_path(identities, target, optional=True)
    if identities[target] is not None:
        raise ValueError('generated systemd lexical target appeared')
    selection = _loaded_systemd_unit(source.name, deadline)
    roots, _, selected = selection
    if root not in roots:
        raise ValueError('generated systemd source outside manager UnitPath')
    if (source.name in identities.systemd_units
            and identities.systemd_units[source.name] != selection):
        raise ValueError('systemd custody selection changed')
    identities.systemd_units[source.name] = selection
    for directory in roots:
        _bind_systemd_path(identities, directory, optional=True, directory=True)
    for path in selected:
        _bind_systemd_path(identities, path)
    return selected


def _validate_systemd_units(identities, deadline):
    for unit, expected in getattr(identities, 'systemd_units', {}).items():
        if _loaded_systemd_unit(unit, deadline) != expected:
            raise ValueError('systemd custody selection changed: ' + unit)


def _text_reference(text, path):
    # Include basename/relative and glob references; ambiguous mentions retain.
    if str(path) in text or path.name in text:
        return True
    return any(fnmatch.fnmatchcase(path.name, token.rsplit('/', 1)[-1])
               for token in re.findall(r"[^\s\"'<>;=]+", text)
               if 'aeo-declared-' in token)


def _reference_scan(path, deadline, inodes):
    identities = _ReferenceIdentities()
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
                    selected = (_generated_systemd_dependency(source, target, identities, deadline)
                                if linked is None else None)
                    if selected is None:
                        stack.append((target, True, True))
                    else:
                        for loaded in selected:
                            if _overlaps(loaded, path) or identities[loaded][:2] in inodes:
                                raise ValueError('loaded systemd tmp custody: ' + str(loaded))
                            stack.append((loaded, True, True))
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
    _validate_systemd_units(identities, deadline)
    for source, expected in identities.items():
        _budget(deadline)
        try:
            actual = _identity(source)
        except FileNotFoundError:
            actual = None
        if actual != expected:
            raise ValueError('tmp custody inventory changed: ' + str(source))
    for source in getattr(identities, 'systemd_paths', ()):
        _budget(deadline)
        if source.resolve(strict=True) != source:
            raise ValueError('aliased systemd custody source: ' + str(source))
    _validate_systemd_units(identities, deadline)


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
    _validate_rebuild(rebuild, deadline)
    def validate():
        _validate_references(identities, deadline)
        _validate_rebuild(rebuild, deadline)
    return validate


@contextmanager
def declared_reference_lease():
    """Join the maintained importer, native saver and content-base CAS leases.

    sync_from_repo.source_lock, cron.jobs._jobs_lock and the release carrier's
    acquire_lock/atomic_pointer_cas are actual participants. This does not fence
    direct site-tree edits, crontab restoration, systemd or symlink publishers.
    """
    require_declared_retirement_authority()
    from contextlib import ExitStack
    from sync_from_repo import source_lock
    with ExitStack() as handles:
        handles.enter_context(source_lock(DECLARED_BUSINESS_ROOT))
        for path in [*(jobs.parent / '.jobs.lock' for jobs in REQUIRED_FILES), DECLARED_BASE_LOCK]:
            if path.resolve(strict=True) != path:
                raise ValueError('aliased native reference lease')
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
            handles.callback(os.close, fd)
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                raise ValueError('invalid native reference lease')
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if _identity(path)[:2] != (opened.st_dev, opened.st_ino):
                raise ValueError('native reference lease changed')
        yield


def _member_signature(s):
    return (s.st_dev, s.st_ino, s.st_mode, s.st_size,
            s.st_mtime_ns, s.st_ctime_ns, s.st_nlink)


def _restore_name(source_fd, source, destination_fd, destination):
    """Linux no-replace rollback: never overwrite a newly published name."""
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.renameat2(source_fd, os.fsencode(source), destination_fd,
                      os.fsencode(destination), 1) != 0:  # RENAME_NOREPLACE
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def retire_expected(path, expected, deadline, admission=None):
    """Retire only captured expected inodes; preserve replacements and unknowns.

    Rename into a private, same-filesystem namespace BEFORE each unlink. Holding
    parent/file descriptors keeps a pathname replacement from redirecting it.
    No recursive remover runs on a public name. A failed rollback retains its
    exact recovery location. This is namespace ownership, NOT a writer lease:
    open readers/source/reference publication remain separate admission duties.
    """
    from contextlib import ExitStack
    import tempfile
    from host_disk_large_classes import snapshot
    result = dict(status='REFUSED', bytes_freed=0, retired_members=0)
    stage = None
    moved = False
    pending = None
    deadline = time.monotonic() + 20 if deadline is None else deadline
    with ExitStack() as handles:
        def opened(name, parent=None, directory=False, owner=handles):
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK |
                         (os.O_DIRECTORY if directory else 0), dir_fd=parent)
            owner.callback(os.close, fd)
            return fd

        def matches(fd, row, *, renamed=False):
            actual = _member_signature(os.fstat(fd))
            want = tuple(row[1:])
            # rename changes ctime itself, never inode/content/mode/links/mtime.
            if (actual[:5] + actual[6:] if renamed else actual) != (
                    want[:5] + want[6:] if renamed else want):
                raise ValueError('expected member changed: ' + row[0])

        try:
            _budget(deadline)
            if admission:
                admission()
            rows = {row[0]: tuple(row) for row in expected}
            if len(rows) != len(expected) or '.' not in rows or not stat.S_ISDIR(rows['.'][3]):
                raise ValueError('invalid expected member inventory')
            for name in rows:
                if Path(name).is_absolute() or '..' in Path(name).parts:
                    raise ValueError('invalid expected member path')
                if not (stat.S_ISREG(rows[name][3]) or stat.S_ISDIR(rows[name][3])):
                    raise ValueError('non-regular expected member')
            if path.resolve(strict=True) != path.absolute():
                raise ValueError('aliased retirement path')
            parent = opened(path.parent, directory=True)
            root = opened(path.name, parent, directory=True)
            matches(root, rows['.'])
            if snapshot(path, deadline)[0] != list(expected):
                raise ValueError('expected member inventory changed')
            if admission:
                admission()
            stage = Path(tempfile.mkdtemp(prefix='.glp354-recovery-', dir=path.parent))
            private = opened(stage, directory=True)
            os.rename(path.name, 'tree', src_dir_fd=parent, dst_dir_fd=private)
            moved = True
            captured = os.stat('tree', dir_fd=private, follow_symlinks=False)
            if (captured.st_dev, captured.st_ino) != (rows['.'][1], rows['.'][2]):
                raise ValueError('root replaced during capture')
            matches(root, rows['.'], renamed=True)
            rows['.'] = ('.', *_member_signature(os.fstat(root)))
            # Verify the complete captured tree before the first payload unlink.
            if snapshot(stage / 'tree', deadline)[0] != sorted(rows.values()):
                raise ValueError('member replaced during root capture')
            directories = {'.': root}
            for name in sorted(rows, key=lambda n: (len(Path(n).parts), n)):
                if name == '.' or not stat.S_ISDIR(rows[name][3]):
                    continue
                fd = opened(Path(name).name, directories[str(Path(name).parent)], directory=True)
                matches(fd, rows[name])
                directories[name] = fd
            # Children first; directory metadata changes caused by our own
            # removals are tracked from the held parent descriptor.
            file_handles = handles.enter_context(ExitStack())
            for name in sorted(rows, key=lambda n: (len(Path(n).parts), n), reverse=True):
                _budget(deadline)
                file_handles.close()
                row = rows[name]
                is_dir = stat.S_ISDIR(row[3])
                if not is_dir and not stat.S_ISREG(row[3]):
                    raise ValueError('non-regular expected member')
                source_parent = private if name == '.' else directories[str(Path(name).parent)]
                source_name = 'tree' if name == '.' else Path(name).name
                fd = directories[name] if is_dir else opened(source_name, source_parent, owner=file_handles)
                matches(fd, row)
                if is_dir and os.listdir(fd):
                    raise ValueError('unexpected remaining directory member')
                os.rename(source_name, 'member', src_dir_fd=source_parent, dst_dir_fd=private)
                pending = (source_parent, source_name)
                captured = os.stat('member', dir_fd=private, follow_symlinks=False)
                if (captured.st_dev, captured.st_ino) != (row[1], row[2]):
                    raise ValueError('member replaced during capture: ' + name)
                matches(fd, row, renamed=True)
                _budget(deadline)
                before = os.fstat(fd)
                if is_dir:
                    os.rmdir('member', dir_fd=private)
                else:
                    os.unlink('member', dir_fd=private)
                pending = None
                after = os.fstat(fd)
                result['retired_members'] += 1
                # Only this successful last-link unlink earns file allocation.
                # Directory allocation and external hardlinks remain uncredited.
                if not is_dir and after.st_nlink == 0:
                    result['bytes_freed'] += before.st_blocks * 512
                if name == '.':
                    moved = False
                else:
                    parent_name = str(Path(name).parent)
                    rows[parent_name] = (parent_name, *_member_signature(os.fstat(source_parent)))
                    # Another selected name may share this inode; only update
                    # its link count/ctime for our own successful unlink.
                    for other, previous in list(rows.items()):
                        if other != name and previous[1:3] == row[1:3]:
                            rows[other] = (other, *_member_signature(after))
            result['status'] = 'REMOVED'
        except (OSError, ValueError, TimeoutError) as error:
            result['reason'] = str(error)
            if result['retired_members']:
                result['status'] = 'PARTIAL_UNKNOWN'
        finally:
            if stage is not None:
                try:
                    if pending:
                        _restore_name(private, 'member', *pending)
                    if moved:
                        _restore_name(private, 'tree', parent, path.name)
                    os.rmdir(stage)
                except OSError:
                    result['recovery_path'] = str(stage)
                    result['status'] = 'PARTIAL_UNKNOWN'
    return result
