#!/usr/bin/env python3
"""Content-only build-input closure for the hourly exact-main publisher.

The source Git tree includes every tracked data/lockfile/taxonomy/manifest byte.
The namespace is --clearenv: hash its complete argv, effective environment and
ALL bound input bytes, including node_modules, tools, private evidence/history
and writable seed directories. Do not substitute path/mtime or a test-only diff.

A UTC build day is an output input (lastmod). Cached output still has to pass
fresh native temporal validators and the deploy-completeness sensor; a hash
match alone is never permission to skip those gates or change the pointer.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import shutil
import stat
import time
import posixpath

SCHEMA = 'glp-main-build-inputs-v1'
NAMESPACE_ARITIES = {'--die-with-parent': 0, '--new-session': 0, '--unshare-all': 0, '--clearenv': 0,
                    '--cap-drop': 1, '--chdir': 1, '--dev': 1, '--proc': 1,
                    '--dir': 1, '--tmpfs': 1, '--symlink': 2,
                    '--bind': 2, '--ro-bind': 2, '--setenv': 2}


def build_environment_argv(config):
    """Project outputs only into bwrap's existing fresh private /dev/shm.

    No input mount is omitted, remounted or masked. Recipes without that
    output contract stay unchanged; never redirect into a guarded seed.
    Both namespace execution and input capture use this exact projection.
    """
    argv = list(config['base_argv'])
    private_dev = False
    i = 1
    while i < len(argv):
        token = argv[i]
        if token not in NAMESPACE_ARITIES or i + NAMESPACE_ARITIES[token] >= len(argv):
            raise ValueError('build_input_unbounded_namespace_option:' + token)
        if token in ('--bind', '--ro-bind', '--symlink', '--dev', '--tmpfs', '--proc'):
            destination = posixpath.normpath(argv[i + NAMESPACE_ARITIES[token]])
            if token == '--dev' and destination == '/dev':
                private_dev = True
            elif destination == '/' or destination == '/dev' or destination.startswith('/dev/'):
                # Any overlaid/aliased dev path invalidates this narrow output
                # contract, even when another recipe might be safe.
                private_dev = False
                break
        i += 1 + NAMESPACE_ARITIES[token]
    if private_dev:
        argv += ['--setenv', 'npm_config_logs_dir', '/dev/shm/glp-build-runtime/npm-logs',
                 '--setenv', 'NODE_COMPILE_CACHE', '/dev/shm/glp-build-runtime/node-compile-cache']
    return argv


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


def path_digest(path, *, file_cache=None, entries=None):
    """Hash names, types, modes, symlinks and bytes; fail on races/special files."""
    root = Path(path)
    cache = {} if file_cache is None else file_cache
    if root.is_symlink():
        # A bind source is dereferenced by bwrap before mounting it.
        target = os.readlink(root)
        result = digest(['bound-link', target, path_digest(root.resolve(strict=True), file_cache=cache, entries=entries)])
        if entries is not None:
            entries['/'] = ['bound-link', target]  # impossible relative-entry key
        return result
    active = set()

    def walk(p):
        before = p.lstat()
        mode = stat.S_IMODE(before.st_mode)
        def recorded(value):
            if entries is not None:
                entries[p.relative_to(root).as_posix()] = value
            return digest(value)
        if stat.S_ISLNK(before.st_mode):
            # Nested links are resolved IN the namespace. Their destination
            # bytes are covered by its mounts, not arbitrary host paths.
            return recorded(['link', mode, os.readlink(p)])
        if not os.access(p, os.R_OK | (os.X_OK if stat.S_ISDIR(before.st_mode) else 0)):
            # The capability-dropped namespace runs as this same uid/groups.
            # Record inaccessible entry identity/permissions, never read secrets.
            return recorded(['unreadable', before.st_mode, before.st_uid, before.st_gid,
                             before.st_size, before.st_mtime_ns, before.st_ctime_ns])
        key = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        if entries is None and stat.S_ISREG(before.st_mode) and key in cache:
            return cache[key]
        if p in active:
            raise ValueError('build_input_symlink_cycle')
        active.add(p)
        if stat.S_ISDIR(before.st_mode):
            names = sorted(p.iterdir(), key=lambda q: q.name)
            value = ['dir', mode, [[q.name, walk(q)] for q in names]]
            if [q.name for q in sorted(p.iterdir(), key=lambda q: q.name)] != [q.name for q in names]:
                raise ValueError('build_input_directory_changed')
        elif stat.S_ISREG(before.st_mode):
            h = hashlib.sha256()
            with p.open('rb') as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    h.update(block)
            value = ['file', mode, before.st_size, h.hexdigest()]
        else:
            raise ValueError('build_input_special_file:' + str(p))
        after = p.lstat()
        if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError('build_input_changed_during_hash:' + str(p))
        active.remove(p)
        if entries is not None:
            # Leaf identity uses the very bytes hashed above, never a later
            # diagnostic re-read. Directory children are separate entries.
            entries[p.relative_to(root).as_posix()] = value[:2] if value[0] == 'dir' else value
        result = digest(value)
        if stat.S_ISREG(before.st_mode):
            cache[key] = result
        return result

    return walk(root)


def producer_closure(root, entrypoints):
    """Hash local Python imports, including lazy imports, without executing them."""
    import ast
    root=Path(root); project=root.parent
    pending=[root/name for name in entrypoints]; result={}
    while pending:
        path=pending.pop().resolve()
        key=str(path.relative_to(project))
        if key in result:
            continue
        raw=path.read_bytes(); result[key]=hashlib.sha256(raw).hexdigest()
        tree=ast.parse(raw, filename=str(path))
        for node in ast.walk(tree):
            names=[]; bases=[root,project]
            if isinstance(node, ast.Import):
                names=[a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                module=node.module or ''
                names=[module]+[module+'.'+a.name if module else a.name for a in node.names]
                if node.level:
                    bases=[path.parents[node.level-1]]
            for name in names:
                parts=name.split('.')
                for count in range(1,len(parts)+1):
                    for base in bases:
                        rel=base.joinpath(*parts[:count])
                        for candidate in (rel.with_suffix('.py'), rel/'__init__.py'):
                            if candidate.is_file() and candidate.resolve().is_relative_to(project):
                                pending.append(candidate)
    return dict(sorted(result.items()))


def stamp():
    return {'utc': datetime.now(timezone.utc).isoformat(), 'monotonic_ns': time.monotonic_ns()}


def differing_fields(before, after, path=''):
    """Exact JSON-pointer leaf differences, including absent-versus-null."""
    if before == after:
        return []
    if isinstance(before, dict) and isinstance(after, dict):
        rows = []
        for key in sorted(before.keys() | after.keys()):
            child = path + '/' + key.replace('~', '~0').replace('/', '~1')
            if key not in before or key not in after:
                rows.append({'path': child, 'before': before.get(key), 'after': after.get(key),
                             'before_present': key in before, 'after_present': key in after})
            else:
                rows.extend(differing_fields(before[key], after[key], child))
        return rows
    if isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
        return [row for i, (a, b) in enumerate(zip(before, after))
                for row in differing_fields(a, b, path + '/' + str(i))]
    return [{'path': path, 'before': before, 'after': after}]


def input_drift(before, after, pre, post, gate):
    rows = []
    for index in sorted(pre['writable_mounts'].keys() | post['writable_mounts'].keys(), key=int):
        old = pre['writable_mounts'].get(index, {})
        new = post['writable_mounts'].get(index, {})
        for role in ('effective', 'seed'):
            a, b = old.get(role, {}), new.get(role, {})
            rows.extend({'mount_index': int(index), 'destination': new.get('destination', old.get('destination')),
                         'role': role, 'path': name,
                         'before': a.get(name), 'after': b.get(name)}
                        for name in sorted(a.keys() | b.keys()) if a.get(name) != b.get(name))
    return {'before': before, 'after': after, 'differing_fields': differing_fields(before, after),
            'differing_entries': rows, 'timing': {'before': pre['timing'], 'gate': gate, 'after': post['timing']},
            'entry_encoding': 'file=[type,mode,size,raw_sha256]; dir=[type,mode]; link=[type,mode,target]; /=bound-link',
            'entry_scope': 'non-source writable binds, effective and seed; read-only inputs retain aggregate fields',
            'writer': 'UNKNOWN'}


def capture(config, *, source_tree, source_revision, sheet_sha256, now=None, diagnostics=None):
    """An unsupported/open environment or unreadable input cannot yield a key."""
    if diagnostics is not None:
        diagnostics.update(timing={'started': stamp()}, writable_mounts={})
    declared = build_environment_argv(config)
    argv = list(config.get('_effective_base_argv', declared))
    if len(argv) != len(declared):
        raise ValueError('build_input_effective_namespace_shape_drift')
    for index, (actual, original) in enumerate(zip(argv, declared)):
        if actual != original and (index < 2 or declared[index-1] != '--bind'):
            raise ValueError('build_input_effective_namespace_recipe_drift')
    if '--clearenv' not in argv:
        raise ValueError('build_inputs_require_clearenv')
    if not source_tree or not source_revision or not sheet_sha256:
        raise ValueError('build_inputs_missing_source_or_sheet')
    if '--unshare-all' not in argv:
        raise ValueError('build_inputs_require_network_isolation')
    if not any(argv[i:i + 2] == ['--cap-drop', 'ALL'] for i in range(len(argv))):
        raise ValueError('build_inputs_require_capability_drop')
    mounts, environment, file_cache = [], {}, {}
    source_mounts = 0
    arities = NAMESPACE_ARITIES
    i = 1
    while i < len(argv):
        token = argv[i]
        if token not in arities or i + arities[token] >= len(argv):
            raise ValueError('build_input_unbounded_namespace_option:' + token)
        if token in ('--bind', '--ro-bind'):
            origin, destination = argv[i + 1:i + 3]
            if destination == config['namespace']:
                argv[i + 1] = '<exact-source-tree>'
                source_mounts += 1
            else:
                effective_entries = seed_entries = None
                if diagnostics is not None and token == '--bind':
                    effective_entries, seed_entries = {}, {}
                    diagnostics['writable_mounts'][str(i)] = {'destination': destination,
                        'effective': effective_entries, 'seed': seed_entries}
                mounts.append({'destination': destination, 'mode': token,
                               'sha256': path_digest(origin, file_cache=file_cache, entries=effective_entries),
                               'seed_sha256': path_digest(declared[i+1], file_cache=file_cache, entries=seed_entries)})
                if token == '--bind':
                    argv[i+1] = '<effective-writable-bind-%d>' % i
        elif token == '--setenv':
            environment[argv[i + 1]] = hashlib.sha256(argv[i + 2].encode()).hexdigest()
        i += 1 + arities[token]
    if source_mounts != 1:
        raise ValueError('build_input_exactly_one_source_mount_required')
    tool = shutil.which(argv[0])
    if not tool:
        raise ValueError('build_input_namespace_tool_missing')
    producers = producer_closure(Path(__file__).parent, (
        'glp_release_inputs.py', 'glp_release_main.py', 'glp_affiliate_sync_job.py',
        'glp_affiliate_registry_sync.py', 'glp_affiliate_absence_audit.py',
        'glp_affiliate_release.py', 'glp_deploy_glp34_authority_v1.py'))
    import glp_deploy_glp34_authority_v1 as carrier
    package_compiler = carrier.toolchain_closure(carrier.TOOLCHAIN)
    body = {'schema': SCHEMA, 'source_tree': source_tree, 'source_revision': source_revision,
            'package_compiler': package_compiler,
            'sheet_sha256': sheet_sha256,
            'execution_identity': [os.geteuid(), os.getegid(), sorted(os.getgroups())],
            'config_sha256': digest({k:v for k,v in config.items() if k != '_effective_base_argv'}),
            'clock_contract': 'UTC_render_day_and_fresh_native_temporal_validation',
            'namespace_argv_sha256': digest(argv), 'namespace_tool_sha256': path_digest(tool),
            'environment_sha256': environment, 'mounts': mounts, 'producers': producers,
            'utc_build_day': (now or datetime.now(timezone.utc)).astimezone(timezone.utc).date().isoformat()}
    result = {**body, 'sha256': digest(body)}
    if diagnostics is not None:
        diagnostics['timing']['finished'] = stamp()
    return result
