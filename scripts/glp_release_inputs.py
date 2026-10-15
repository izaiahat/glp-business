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

SCHEMA = 'glp-main-build-inputs-v1'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


def path_digest(path, *, file_cache=None):
    """Hash names, types, modes, symlinks and bytes; fail on races/special files."""
    root = Path(path)
    cache = {} if file_cache is None else file_cache
    if root.is_symlink():
        # A bind source is dereferenced by bwrap before mounting it.
        return digest(['bound-link', os.readlink(root), path_digest(root.resolve(strict=True), file_cache=cache)])
    active = set()

    def walk(p):
        before = p.lstat()
        mode = stat.S_IMODE(before.st_mode)
        if stat.S_ISLNK(before.st_mode):
            # Nested links are resolved IN the namespace. Their destination
            # bytes are covered by its mounts, not arbitrary host paths.
            return digest(['link', mode, os.readlink(p)])
        if not os.access(p, os.R_OK | (os.X_OK if stat.S_ISDIR(before.st_mode) else 0)):
            # The capability-dropped namespace runs as this same uid/groups.
            # Record inaccessible entry identity/permissions, never read secrets.
            return digest(['unreadable', before.st_mode, before.st_uid, before.st_gid,
                           before.st_size, before.st_mtime_ns, before.st_ctime_ns])
        key = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        if stat.S_ISREG(before.st_mode) and key in cache:
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
        result = digest(value)
        if stat.S_ISREG(before.st_mode):
            cache[key] = result
        return result

    return walk(root)


def capture(config, *, source_tree, source_revision, sheet_sha256, now=None):
    """An unsupported/open environment or unreadable input cannot yield a key."""
    argv = list(config['base_argv'])
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
    arities = {'--die-with-parent': 0, '--new-session': 0, '--unshare-all': 0, '--clearenv': 0,
               '--cap-drop': 1, '--chdir': 1, '--dev': 1, '--proc': 1,
               '--dir': 1, '--tmpfs': 1, '--symlink': 2,
               '--bind': 2, '--ro-bind': 2, '--setenv': 2}
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
                mounts.append({'destination': destination, 'mode': token,
                               'sha256': path_digest(origin, file_cache=file_cache)})
        elif token == '--setenv':
            environment[argv[i + 1]] = hashlib.sha256(argv[i + 2].encode()).hexdigest()
        i += 1 + arities[token]
    if source_mounts != 1:
        raise ValueError('build_input_exactly_one_source_mount_required')
    tool = shutil.which(argv[0])
    if not tool:
        raise ValueError('build_input_namespace_tool_missing')
    producers = {}
    for name in ('glp_release_inputs.py', 'glp_release_main.py', 'glp_affiliate_sync_job.py',
                 'glp_affiliate_registry_sync.py', 'glp_affiliate_absence_audit.py',
                 'glp_affiliate_release.py', 'glp_deploy_glp34_authority_v1.py'):
        p = Path(__file__).with_name(name)
        producers[name] = hashlib.sha256(p.read_bytes()).hexdigest()
    body = {'schema': SCHEMA, 'source_tree': source_tree, 'source_revision': source_revision,
            'sheet_sha256': sheet_sha256,
            'execution_identity': [os.geteuid(), os.getegid(), sorted(os.getgroups())],
            'config_sha256': digest(config),
            'clock_contract': 'UTC_render_day_and_fresh_native_temporal_validation',
            'namespace_argv_sha256': digest(argv), 'namespace_tool_sha256': path_digest(tool),
            'environment_sha256': environment, 'mounts': mounts, 'producers': producers,
            'utc_build_day': (now or datetime.now(timezone.utc)).astimezone(timezone.utc).date().isoformat()}
    return {**body, 'sha256': digest(body)}
