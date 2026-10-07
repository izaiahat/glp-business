"""Exact builder AST contracts; synthetic authority, no host imports or build.

Producer fixtures use the actual native row/hash algorithm. Effect boundaries
are explicit doubles; these tests do not accept a deployment or receiving run.
"""
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import types
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / 'systems/seo-blog-machine/scripts/build_opus_article_release.py'


def builder_scope(path=BUILDER):
    names = {'sha', 'checked', 'rows', 'closure', 'require_current_base',
             'accepted_base_inventory', 'accepted_base_closure',
             'retain_price_index_predecessor', 'payload_hash'}
    nodes = [n for n in ast.parse(path.read_text()).body
             if isinstance(n, ast.FunctionDef) and n.name in names]
    assert {'sha', 'checked', 'rows', 'closure', 'require_current_base'} <= {n.name for n in nodes}
    scope: dict[str, Any] = dict(Path=Path, Any=Any, hashlib=hashlib, json=json, re=re,
                 os=os, stat=stat, SHA256_PATTERN=re.compile(r'[0-9a-f]{64}'),
                 SITE_RELEASES_ROOT=Path('/never/live/releases'))
    scope['ensure_accepted_base_sealed'] = lambda *args: None
    scope['materialize_accepted_prepared_base'] = lambda *args: (_ for _ in ()).throw(
        AssertionError('unexpected custody effect'))
    scope['unsealed_base_entries'] = lambda *args: []
    scope['load_env'] = lambda: {'CF_API_TOKEN': 'synthetic-not-a-credential'}
    scope['CF_API'] = 'https://invalid.example/fixture'
    scope['requests'] = types.SimpleNamespace(get=lambda *a, **k: types.SimpleNamespace(
        raise_for_status=lambda: None,
        json=lambda: {'success': True, 'result': {'canonical_deployment': {'id': 'fixture'}}}))
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), scope)
    return scope


def native_closure(rows):
    return hashlib.sha256(''.join(f"{r['path']}\0{r['size']}\0{r['sha256']}\n"
        for r in sorted(rows, key=lambda r: r['path'].encode())).encode()).hexdigest()


def put(path, document):
    path.write_text(json.dumps(document))
    return {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


@pytest.fixture
def base(tmp_path):
    scope = builder_scope()
    release = tmp_path / 'release'
    (release / 'public').mkdir(parents=True)
    (release / 'public/index.html').write_text('synthetic public')
    (release / 'input.json').write_text('{}')
    rows = [{'path': r['path'], 'size': r['bytes'], 'sha256': r['sha256']}
            for r in scope['rows'](release)]
    manifest = {'schema': 'glp-opus-release-deployment-manifest-v1', 'release_root': str(release),
                'release_rows': rows, 'release_tree_sha256': native_closure(rows),
                'site_head': 'a' * 40,
                'deploy_directory': {'tree_sha256': 'b' * 64, 'file_count': 1, 'total_bytes': 1}}
    receipt = {'schema': 'glp-opus-production-terminal-receipt-v1', 'status': 'PASS_PRODUCTION_TERMINAL',
               'deployment': {'deployment_id': 'fixture'}, 'site_head': 'a' * 40,
               'immutable_closure': {'release_tree_sha256': manifest['release_tree_sha256'],
                                     'public_tree_sha256': native_closure([
                                         dict(r, path=r['path'][7:]) for r in rows if r['path'].startswith('public/')])}}
    job = {'base_release_root': str(release)}
    def bind():
        job['base_manifest'] = put(tmp_path / 'manifest.json', manifest)
        receipt['evidence_sha256'] = {'manifest.json': job['base_manifest']['sha256']}
        job['base_final_receipt'] = put(tmp_path / 'terminal.json', receipt)
    bind()
    return scope, release, manifest, receipt, job, bind


def test_native_producer_representation_and_order(base):
    scope, release, manifest, _, job, bind = base
    manifest['release_rows'].reverse()
    bind()
    assert scope['require_current_base'](job)[0] == release


def test_legacy_representation_keeps_its_digest_and_seal(base):
    scope, release, manifest, receipt, job, bind = base
    manifest['release_rows'] = scope['rows'](release)
    manifest['release_tree_sha256'] = scope['closure'](manifest['release_rows'])
    receipt['immutable_closure'] = {'release_tree_sha256': manifest['release_tree_sha256'],
                                  'public_tree_sha256': scope['closure'](scope['rows'](release / 'public'))}
    calls = []
    scope['ensure_accepted_base_sealed'] = lambda *args: calls.append(args)
    bind()
    assert scope['require_current_base'](job)[0] == release
    assert calls == [(release, scope['rows'](release), manifest['release_tree_sha256'])]


@pytest.mark.parametrize('damage', ['mixed', 'both', 'missing', 'extra-field', 'empty', 'duplicate',
    'absolute', 'dot', 'traversal', 'double-slash', 'backslash', 'nul', 'bool', 'negative', 'float',
    'bad-hash', 'upper-hash', 'wrong-schema', 'missing-head', 'bad-deploy-directory'])
def test_malformed_or_mixed_manifest_is_refused_before_effects(base, damage):
    scope, _, manifest, _, job, bind = base
    row = manifest['release_rows'][0]
    if damage == 'mixed': row['bytes'] = row.pop('size')
    elif damage == 'both': row['bytes'] = row['size']
    elif damage == 'missing': row.pop('sha256')
    elif damage == 'extra-field': row['extra'] = 1
    elif damage == 'empty': manifest['release_rows'] = []
    elif damage == 'duplicate': manifest['release_rows'].append(dict(row))
    elif damage in {'absolute', 'dot', 'traversal', 'double-slash', 'backslash', 'nul'}:
        row['path'] = {'absolute': '/a', 'dot': './a', 'traversal': 'a/../b',
                       'double-slash': 'a//b', 'backslash': 'a\\b', 'nul': 'a\0b'}[damage]
    elif damage in {'bool', 'negative', 'float'}: row['size'] = {'bool': True, 'negative': -1, 'float': 1.0}[damage]
    elif damage == 'bad-hash': row['sha256'] = 'z' * 64
    elif damage == 'upper-hash': row['sha256'] = 'A' * 64
    elif damage == 'wrong-schema': manifest['schema'] = 'unexpected'
    elif damage == 'missing-head': manifest.pop('site_head')
    elif damage == 'bad-deploy-directory': manifest['deploy_directory']['file_count'] = True
    bind()
    scope['requests'].get = lambda *a, **k: (_ for _ in ()).throw(AssertionError('network reached'))
    with pytest.raises(RuntimeError): scope['require_current_base'](job)


@pytest.mark.parametrize('damage', ['file', 'release-digest', 'public-digest', 'terminal-tree',
                                   'terminal-manifest', 'terminal-status', 'head', 'canonical'])
def test_authenticated_content_and_terminal_guards_remain(base, damage):
    scope, release, manifest, receipt, job, bind = base
    if damage == 'file': (release / 'input.json').write_text('changed')
    elif damage == 'release-digest': manifest['release_tree_sha256'] = 'c' * 64
    elif damage == 'public-digest': receipt['immutable_closure']['public_tree_sha256'] = 'c' * 64
    elif damage == 'terminal-tree': receipt['immutable_closure']['release_tree_sha256'] = 'c' * 64
    elif damage == 'terminal-status': receipt['status'] = 'PASS'
    elif damage == 'head': receipt['site_head'] = 'c' * 40
    elif damage == 'canonical': scope['requests'].get = lambda *a, **k: types.SimpleNamespace(
        raise_for_status=lambda: None, json=lambda: {'success': True, 'result': {'canonical_deployment': {'id': 'wrong'}}})
    bind()
    if damage == 'terminal-manifest':
        receipt['evidence_sha256']['manifest.json'] = 'c' * 64
        job['base_final_receipt'] = put(Path(job['base_final_receipt']['path']), receipt)
    with pytest.raises(RuntimeError): scope['require_current_base'](job)


@pytest.mark.parametrize('field', ['base_manifest', 'base_final_receipt'])
def test_descriptor_drift_during_canonical_read_refused(base, field):
    scope, _, _, _, job, _ = base
    original = scope['requests'].get
    def drift(*a, **k):
        p = Path(job[field]['path'])
        p.write_bytes(p.read_bytes() + b' ')
        return original(*a, **k)
    scope['requests'].get = drift
    with pytest.raises(RuntimeError): scope['require_current_base'](job)


@pytest.mark.parametrize('field', ['base_manifest', 'base_final_receipt'])
@pytest.mark.parametrize('alias_kind', ['symlink', 'ancestor'])
@pytest.mark.parametrize('boundary', ['canonical-get', 'materialize'])
def test_same_byte_evidence_alias_rebinding_is_refused(base, field, alias_kind, boundary):
    scope, release, _, _, job, _ = base
    target = Path(job[field]['path'])
    original_info = target.stat()
    alternate_dir = release.parent / 'alternate'
    alternate_dir.mkdir()
    alternate = alternate_dir / target.name
    alternate.write_bytes(target.read_bytes())
    alias = release.parent / ('evidence-alias' if alias_kind == 'ancestor' else 'evidence-alias.json')
    alias.symlink_to(target.parent if alias_kind == 'ancestor' else target,
                     target_is_directory=alias_kind == 'ancestor')
    job[field]['path'] = str(alias / target.name if alias_kind == 'ancestor' else alias)
    def rebind():
        alias.unlink()
        alias.symlink_to(alternate_dir if alias_kind == 'ancestor' else alternate,
                         target_is_directory=alias_kind == 'ancestor')
    if boundary == 'canonical-get':
        original = scope['requests'].get
        def get(*args, **kwargs):
            rebind()
            return original(*args, **kwargs)
        scope['requests'].get = get
    else:
        scope['unsealed_base_entries'] = lambda *args: ['.']
        def materialize(source, destination, inventory):
            rebind()
            return destination
        scope['materialize_accepted_prepared_base'] = materialize
    with pytest.raises(RuntimeError, match='^base_evidence_descriptor_drift$'):
        scope['require_current_base'](job)
    assert target.stat() == original_info
    assert alternate.read_bytes() == target.read_bytes()


def test_native_writable_base_uses_supported_producer_not_legacy_reseal(base):
    scope, release, _, _, job, _ = base
    scope['unsealed_base_entries'] = lambda *args: ['.']
    scope['ensure_accepted_base_sealed'] = lambda *args: (_ for _ in ()).throw(AssertionError('native reseal forbidden'))
    calls = []
    immutable = release.parent / 'immutable'
    def materialize(source, destination, inventory):
        calls.append((source, destination, inventory))
        return destination
    scope['materialize_accepted_prepared_base'] = materialize
    selected = scope['require_current_base'](job)[0]
    assert selected.parent == scope['SITE_RELEASES_ROOT']
    assert len(calls) == 1 and calls[0][0] == release
    assert calls[0][2] == scope['rows'](release)


def test_unsupported_immutable_production_failure_stays_failure(base):
    scope, _, _, _, job, _ = base
    scope['unsealed_base_entries'] = lambda *args: ['.']
    def refused(*args): raise ValueError('immutable_base_writable:.')
    scope['materialize_accepted_prepared_base'] = refused
    with pytest.raises(ValueError, match='immutable_base_writable'): scope['require_current_base'](job)


def test_price_retention_consumes_only_the_bound_native_snapshot(base):
    import shutil
    scope, release, manifest, _, job, bind = base
    html = release / 'public/glp-1-price-index/index.html'
    html.parent.mkdir()
    html.write_text('retained fixture html')
    price = release / 'artifacts/ops/price-index-manifest.json'
    price.parent.mkdir(parents=True)
    value = {'schema': 'glp-clinic-scout-price-index-build-v1', 'route': '/glp-1-price-index/',
             'html_sha256': scope['sha'](html)}
    value['payload_sha256'] = scope['payload_hash'](value, 'payload_sha256')
    put(price, value)
    inventory = scope['rows'](release)
    manifest['release_rows'] = [{'path': r['path'], 'size': r['bytes'], 'sha256': r['sha256']} for r in inventory]
    manifest['release_tree_sha256'] = native_closure(manifest['release_rows'])
    bind()
    scope['SITE_RELEASES_ROOT'] = release.parent / 'site-releases'
    snapshot = scope['SITE_RELEASES_ROOT'] / ('accepted-prepared-' + manifest['release_tree_sha256'])
    target = release.parent / 'candidate'
    shutil.copytree(release, snapshot)
    shutil.copytree(release, target)
    result = scope['retain_price_index_predecessor'](job, snapshot, target)
    assert result['status'] == 'PASS_EXACT_SEALED_PREDECESSOR_PAIR'
    assert (target / price.relative_to(release)).read_bytes() == price.read_bytes()
    with pytest.raises(RuntimeError, match='base_identity_drift'):
        scope['retain_price_index_predecessor'](job, snapshot.with_name('arbitrary-alias'), target)
    scope['unsealed_base_entries'] = lambda *args: ['.']
    with pytest.raises(RuntimeError, match='base_identity_drift'):
        scope['retain_price_index_predecessor'](job, snapshot, target)


@pytest.fixture
def producer(tmp_path):
    """Execute checkout materializer only; root handoff is a synthetic double.

    Actual fd copy/hash/mode/immutability code runs on tiny tmp fixtures. The
    privilege double checks exact fd-only 0:0 handoff, never invokes sudo, and
    cannot certify real root ownership or a live installed producer.
    """
    path = ROOT / 'scripts/site_tree_materializer.py'
    scope: dict[str, Any] = {'__name__': 'isolated_materializer_fixture'}
    exec(compile(ast.parse(path.read_text()), str(path), 'exec'), scope)
    scope['_SITE_ROOT'] = tmp_path / 'site'
    scope['_CANONICAL_MACHINE_ROOT'] = tmp_path / 'machine'
    release_parent = scope['_SITE_ROOT'] / 'releases'
    release_parent.mkdir(parents=True)
    source = scope['_CANONICAL_MACHINE_ROOT'] / 'registry-site-sync/20261003T030854Z/prepared/release'
    (source / 'public').mkdir(parents=True)
    (source / 'public/index.html').write_text('fixture public')
    (source / 'tool.sh').write_text('fixture executable')
    (source / 'tool.sh').chmod(0o755)
    inventory = builder_scope()['rows'](source)
    native = native_closure([{'path': r['path'], 'size': r['bytes'], 'sha256': r['sha256']} for r in inventory])
    destination = release_parent / ('accepted-prepared-' + native)
    original_manifest = scope['_tree_manifest_fd']
    scope['_fixture_real_manifest'] = original_manifest
    scope['_fixture_real_handoff'] = scope['_sudo_chown_fds']
    handoff = []
    def fixture_manifest(fd, **kwargs):
        if kwargs.get('require_owner') is not None:
            assert kwargs.pop('require_owner') == (0, 0)
        return original_manifest(fd, **kwargs)
    def fixture_handoff(fds, uid, gid):
        assert (uid, gid) == (0, 0)
        assert all(not stat.S_IMODE(os.fstat(fd).st_mode) & 0o222 for fd in fds)
        handoff.append(len(fds))
    scope['_tree_manifest_fd'] = fixture_manifest
    scope['_sudo_chown_fds'] = fixture_handoff
    scope['os'] = types.SimpleNamespace(**{name: getattr(os, name) for name in dir(os)})
    scope['os'].geteuid = lambda: 1000
    return scope, source, destination, inventory, handoff


@pytest.mark.parametrize('relative', ['.', 'public'])
@pytest.mark.parametrize('donor_mode', [0o700, 0o755])
def test_mkdir_open_namespace_substitution_refused_before_target_effects(producer, relative, donor_mode):
    """Real rename/metadata/readback; ownership remains explicitly synthetic."""
    import resource
    scope, source, destination, inventory, handoff = producer
    target = destination if relative == '.' else destination / relative
    donor = source.parent / 'unrelated-donor'
    donor.mkdir(mode=donor_mode)
    donor_before = donor.stat()
    displaced = destination.parent / 'displaced-invocation-directory'
    before_fds = len(os.listdir('/proc/self/fd'))
    before_limit = resource.getrlimit(resource.RLIMIT_NOFILE)
    source_before = {p: p.stat() for p in [source, *source.rglob('*')]}
    native_mkdir = os.mkdir
    def mkdir(path, mode=0o777, *, dir_fd=None):
        native_mkdir(path, mode, dir_fd=dir_fd)
        if path == target.name and dir_fd is not None:
            os.rename(target, displaced)
            os.rename(donor, target)
    scope['os'].mkdir = mkdir
    with pytest.raises(RuntimeError, match='^accepted_prepared_creation_custody_lost$'):
        scope['materialize_accepted_prepared_base'](source, destination, inventory)
    assert not handoff
    after = target.stat()
    assert (after.st_dev, after.st_ino) == (donor_before.st_dev, donor_before.st_ino)
    # Rename changes ctime, but no copy/chmod/chown may touch the donor.
    assert (after.st_mode, after.st_uid, after.st_gid, after.st_mtime_ns) == (
        donor_before.st_mode, donor_before.st_uid, donor_before.st_gid, donor_before.st_mtime_ns)
    assert list(target.iterdir()) == []
    assert list(displaced.iterdir()) == []
    assert all(p.stat() == info for p, info in source_before.items())
    assert len(os.listdir('/proc/self/fd')) == before_fds
    assert resource.getrlimit(resource.RLIMIT_NOFILE) == before_limit


@pytest.mark.parametrize('fault', ['overflow', 'watch-loss', 'truncated'])
def test_creation_watch_uncertainty_refuses_before_target_effects(producer, fault):
    import resource
    import struct
    scope, source, destination, inventory, handoff = producer
    before_fds = len(os.listdir('/proc/self/fd'))
    before_limit = resource.getrlimit(resource.RLIMIT_NOFILE)
    native_read = os.read
    def read(fd, count):
        if os.readlink(f'/proc/self/fd/{fd}') == 'anon_inode:inotify':
            return {'overflow': struct.pack('iIII', -1, 0x4000, 0, 0),
                    'watch-loss': struct.pack('iIII', 1, 0x8000, 0, 0),
                    'truncated': b'x'}[fault]
        return native_read(fd, count)
    scope['os'].read = read
    with pytest.raises(RuntimeError, match='^accepted_prepared_creation_custody_lost$'):
        scope['materialize_accepted_prepared_base'](source, destination, inventory)
    assert not handoff and list(destination.iterdir()) == []
    assert len(os.listdir('/proc/self/fd')) == before_fds
    assert resource.getrlimit(resource.RLIMIT_NOFILE) == before_limit


def test_prepared_ingestion_produces_private_immutable_bytes_and_keeps_source(producer):
    scope, source, destination, inventory, handoff = producer
    before = {p.relative_to(source).as_posix(): os.lstat(p) for p in [source, *source.rglob('*')]}
    assert scope['materialize_accepted_prepared_base'](source, destination, inventory) == destination
    assert builder_scope()['rows'](destination) == inventory
    for path, info in before.items():
        p = source if path == '.' else source / path
        assert os.lstat(p) == info
        target = destination if path == '.' else destination / path
        assert (os.lstat(target).st_dev, os.lstat(target).st_ino) != (info.st_dev, info.st_ino)
        assert not stat.S_IMODE(os.lstat(target).st_mode) & 0o222
    assert handoff == [4]
    assert scope['materialize_accepted_prepared_base'](source, destination, inventory) == destination
    assert handoff == [4]  # existing qualified base is not resealed


def test_real_root_immutable_ingestion_and_protected_hardlink_consumer(producer):
    """Real sudo, only descriptor-bound synthetic scratch roots; no live base."""
    scope, source, destination, inventory, _ = producer
    scope['os'] = os
    scope['_tree_manifest_fd'] = scope['_fixture_real_manifest']
    scope['_sudo_chown_fds'] = scope['_fixture_real_handoff']
    candidates = scope['_SITE_ROOT'] / 'candidates'
    candidates.mkdir()
    candidate = candidates / 'fixture-candidate'
    scope['_PRIVILEGED_SOURCE_PARENTS'] = (destination.parent, candidates)
    scope['_PRIVILEGED_DESTINATION_PARENTS'] = (destination.parent, candidates)
    commands = []
    import subprocess
    native_run = subprocess.run
    def captured_run(argv, **kwargs):
        assert argv[0] == '/usr/bin/sudo'
        assert argv[1:3] == ['-n', '--']
        assert argv[3] in {'/usr/bin/chown', '/usr/bin/cp'}
        paths = argv[argv.index('--', 3) + 1:]
        assert paths and all(p.startswith(f'/proc/{os.getpid()}/fd/') for p in paths)
        commands.append(argv)
        return native_run(argv, **kwargs)
    scope['subprocess'] = types.SimpleNamespace(run=captured_run, PIPE=subprocess.PIPE)
    try:
        scope['materialize_accepted_prepared_base'](source, destination, inventory)
        for p in [destination, *destination.rglob('*')]:
            assert (os.lstat(p).st_uid, os.lstat(p).st_gid) == (0, 0)
            assert not stat.S_IMODE(os.lstat(p).st_mode) & 0o222
        assert builder_scope()['rows'](destination) == inventory
        scope['copytree_linked'](destination, candidate)
        for row in inventory:
            original, immutable, linked = (os.lstat(root / row['path']) for root in (source, destination, candidate))
            assert original.st_ino != immutable.st_ino == linked.st_ino
            assert (original.st_uid, original.st_gid) == (os.getuid(), os.getgid())
        assert any(argv[3] == '/usr/bin/cp' for argv in commands)
    finally:
        for root in (candidate, destination):
            if not root.exists(): continue
            parent_fd = scope['_open_tree_root'](root.parent)
            root_fd = scope['_open_tree_root'](root)
            try:
                scope['_cleanup_destination'](parent_fd, root.name, root_fd, privileged=True)
            finally:
                os.close(root_fd)
                os.close(parent_fd)


@pytest.mark.parametrize('damage', ['source-symlink', 'source-byte-drift', 'destination-root',
                                   'source-root', 'existing-writable', 'seal-denied', 'destination-rebind',
                                   'source-drift-during-seal'])
def test_immutable_base_producer_fails_closed(producer, damage):
    scope, source, destination, inventory, _ = producer
    if damage == 'source-symlink':
        (source / 'public/index.html').unlink()
        (source / 'public/index.html').symlink_to(source / 'tool.sh')
    elif damage == 'source-byte-drift': (source / 'tool.sh').write_text('changed')
    elif damage == 'destination-root': destination = source.parent / destination.name
    elif damage == 'source-root': source = source.parent
    elif damage == 'existing-writable':
        import shutil
        shutil.copytree(source, destination)
    elif damage == 'seal-denied':
        def refuse(*args): raise PermissionError('fixture seal denied')
        scope['_sudo_chown_fds'] = refuse
    elif damage == 'destination-rebind': scope['_destination_binding_matches'] = lambda *args: False
    elif damage == 'source-drift-during-seal':
        scope['_sudo_chown_fds'] = lambda *args: (source / 'tool.sh').write_text('changed during seal')
    with pytest.raises((ValueError, RuntimeError, PermissionError)):
        scope['materialize_accepted_prepared_base'](source, destination, inventory)
