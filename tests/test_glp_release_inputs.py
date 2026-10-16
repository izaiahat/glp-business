"""Complete release inputs, not a changed-path/test-only heuristic."""
import datetime as dt
import importlib
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))


@pytest.fixture(autouse=True)
def compiler_fixture(tmp_path, monkeypatch):
    import glp_deploy_glp34_authority_v1 as carrier
    root=tmp_path/'compiler';root.mkdir()
    (root/'compiler.js').write_text('compiler-v1')
    monkeypatch.setattr(carrier, 'TOOLCHAIN', root)
    return root


def test_package_compiler_is_an_input(tmp_path, compiler_fixture):
    import glp_release_inputs as inputs
    argv=['/bin/true','--clearenv','--unshare-all','--cap-drop','ALL','--bind',str(tmp_path),'/site']
    config={'namespace':'/site','base_argv':argv}
    capture=lambda: inputs.capture(config,source_tree='T',source_revision='H',sheet_sha256='S')
    old=capture()
    (compiler_fixture/'compiler.js').write_text('compiler-v2')
    assert capture()['sha256'] != old['sha256']


def test_local_lazy_imports_are_build_inputs(tmp_path):
    import glp_release_inputs as inputs
    scripts=tmp_path/'scripts';scripts.mkdir()
    (scripts/'entry.py').write_text('def run():\n import helper\n')
    helper=scripts/'helper.py';helper.write_text('VALUE=1\n')
    old=inputs.producer_closure(scripts, ('entry.py',))
    helper.write_text('VALUE=2\n')
    new=inputs.producer_closure(scripts, ('entry.py',))
    assert old['scripts/entry.py'] == new['scripts/entry.py']
    assert old['scripts/helper.py'] != new['scripts/helper.py']


def write_recipe(source):
    prefix = ('npm run generate:provider-logo-atlas && npm run generate:home-imagery && '
              'npm run generate:phase3-product-renders && npm run assemble:css && '
              'node scripts/build-staging.mjs && node scripts/build-lastmod-provenance.mjs')
    recipe = prefix + ' && npm run validate:coverage && node scripts/validate-affiliate-review-publication.mjs'
    (source/'package.json').write_text(json.dumps({'scripts':{'build:validated':recipe}}))


def test_native_validation_tail_is_the_registered_build_tail(tmp_path):
    import glp_release_main as release
    write_recipe(tmp_path)
    commands = release.native_validation_commands(tmp_path)
    assert commands[0] == ['npm','run','validate:coverage']
    assert commands[-1] == ['node','scripts/validate-affiliate-review-publication.mjs']


def test_exact_input_snapshot_tracks_source_data_deps_env_and_mount_bytes(tmp_path):
    inputs = importlib.import_module('glp_release_inputs')
    data = tmp_path / 'data'
    data.mkdir()
    (data / 'taxonomy.json').write_text('{"revision":1}')
    (data / 'package-lock.json').write_text('{"version":1}')
    source = tmp_path / 'old-source'
    source.mkdir()
    tool = tmp_path / 'bwrap'
    tool.write_bytes(b'tool-v1')
    tool.chmod(0o755)
    config = {'namespace': '/site', 'base_argv': [str(tool), '--clearenv', '--unshare-all', '--cap-drop', 'ALL',
              '--bind', str(source), '/site', '--ro-bind', str(data), '/data',
              '--setenv', 'GLP_PRIVATE_EVIDENCE_ROOT', '/data']}
    now = dt.datetime(2026, 10, 15, 21, tzinfo=dt.timezone.utc)
    capture = lambda **kw: inputs.capture(config, source_tree=kw.get('tree', 'TREE'),
                                         sheet_sha256=kw.get('sheet', 'SHEET'), now=kw.get('now', now),
                                         source_revision=kw.get('head', 'HEAD'))
    baseline = capture()
    assert capture() == baseline
    for name in ['taxonomy.json', 'package-lock.json']:
        p = data / name
        old = p.read_bytes()
        p.write_bytes(old + b' ')
        assert capture()['sha256'] != baseline['sha256']
        p.write_bytes(old)
    assert capture(head='DIFFERENT_HEAD_SAME_TREE')['sha256'] != baseline['sha256']
    assert capture(tree='OTHER')['sha256'] != baseline['sha256']
    assert capture(sheet='OTHER')['sha256'] != baseline['sha256']
    assert capture(now=now + dt.timedelta(days=1))['sha256'] != baseline['sha256']
    config['base_argv'][-1] = '/other-data'
    assert capture()['sha256'] != baseline['sha256']
    assert '/other-data' not in json.dumps(capture())
    config['base_argv'][-1] = '/data'
    (data / 'new-manifest.json').write_text('{}')
    assert capture()['sha256'] != baseline['sha256']


def test_mount_links_are_namespace_links_not_host_file_reads(tmp_path):
    import glp_release_inputs as inputs
    root = tmp_path / 'mount'
    root.mkdir()
    (root / 'private').symlink_to('/etc/ssl/private')
    first = inputs.path_digest(root)
    (root / 'private').unlink()
    (root / 'private').symlink_to('/not-mounted/private')
    assert inputs.path_digest(root) != first
    target = tmp_path / 'actual-library'
    target.write_text('library-v1')
    bound = tmp_path / 'bound-library'
    bound.symlink_to(target)
    first = inputs.path_digest(bound)
    target.write_text('library-v2')
    assert inputs.path_digest(bound) != first


def test_duplicate_file_bindings_hash_bytes_once_per_capture(tmp_path, monkeypatch):
    import os
    import glp_release_inputs as inputs
    data=tmp_path/'input'; data.write_text('same physical input')
    alias=tmp_path/'alias'; os.link(data, alias)
    reads=[]; original=Path.open
    def opened(path, *a, **kw):
        if path in (data, alias): reads.append(path)
        return original(path, *a, **kw)
    monkeypatch.setattr(Path, 'open', opened)
    argv=['/bin/true','--clearenv','--unshare-all','--cap-drop','ALL',
          '--bind',str(tmp_path),'/site','--ro-bind',str(data),'/a','--ro-bind',str(alias),'/b']
    inputs.capture({'namespace':'/site','base_argv':argv}, source_tree='T', source_revision='H', sheet_sha256='S')
    assert len(reads)==1


def test_unreadable_mount_entries_are_not_build_readable_inputs(tmp_path):
    import glp_release_inputs as inputs
    private = tmp_path / 'private'
    private.write_text('not readable by the build uid')
    private.chmod(0)
    try:
        first = inputs.path_digest(tmp_path)
        assert inputs.path_digest(tmp_path) == first
    finally:
        private.chmod(0o600)
    assert inputs.path_digest(tmp_path) != first


def test_identical_inputs_skip_requires_success_binding_and_passing_sensor(tmp_path, monkeypatch):
    import glp_release_main as release
    import glp_affiliate_sync_job as job
    import glp_deploy_sensor_hook as hook
    import glp_release_inputs as inputs
    base_report = tmp_path / 'old'
    root = base_report / 'prepared/release'
    root.mkdir(parents=True)
    (root / 'content').write_text('deployed')
    report = tmp_path / 'new'
    report.mkdir()
    import glp_deploy_glp34_authority_v1 as carrier
    terminal = tmp_path/'terminal.json'
    job.save(terminal, {'status':'PASS_PRODUCTION_TERMINAL', 'site_head':'HEAD',
                       'deployment':{'deployment_id':'DEP'}})
    pointer = {'schema':'glp-opus-current-content-base-v1', 'status':'PASS',
               'site_head':'HEAD','release_root':str(root),'deployment_id':'DEP',
               'reminted_at_utc':(dt.datetime.now(dt.timezone.utc)-dt.timedelta(minutes=1)).isoformat(),
               'final_receipt':{'path':str(terminal),'sha256':job.sha(terminal)}}
    pointer_path = tmp_path/'pointer.json'; job.save(pointer_path, pointer)
    monkeypatch.setattr(carrier, 'POINTER', pointer_path)
    key = {'sha256':'INPUTS','source_tree':'TREE'}
    source = base_report/'candidate'
    source.mkdir()
    (source/'function.js').write_text('generated runtime')
    baseline = {'status':'PASS_PRODUCTION_TERMINAL','pointer':pointer,'inputs':key,
                'release_tree_sha256':inputs.path_digest(root),
                'validation_source':str(source), 'validation_source_sha256':inputs.path_digest(source)}
    job.save(base_report / 'SUCCESSFUL-INPUTS.json', baseline)
    calls = []
    monkeypatch.setattr(release, 'fresh_release_inputs', lambda *a: key)
    monkeypatch.setattr(release, 'validate_cached_release', lambda *a: calls.append('validated'))
    import glp_deploy_completeness_sensor as sensor
    def sensor_raw(*a, **k):
        checks = [{'check':name,'status':'PASS'} for name in hook.LIVE_CHECKS]
        checks[-1].update(base_deployment_id='DEP',live_canonical_deployment_id='DEP')
        return {'schema':'glp-deploy-completeness-sensor-v1','status':'PASS',
                'release':root.name,'release_root':str(root),'live_programs':1,
                'checks':checks,'failed_checks':[],'skipped_checks':[],
                'failure_count':0,'failures':[],
                'captured_at_utc':dt.datetime.now(dt.timezone.utc).isoformat()}
    monkeypatch.setattr(sensor, 'run_sensor', sensor_raw)
    monkeypatch.setattr(hook, 'MACHINE_DIR', tmp_path/'sensors')
    attach = hook.attach_sensor_result
    out = release.try_identical_inputs({}, report, pointer, {'headRefOid':'HEAD','url':'PR'})
    assert out['status'] == 'SKIPPED_IDENTICAL_INPUTS'
    assert out['deployment_id'] == 'DEP' and calls == ['validated']
    assert json.loads((report/'MAIN-RELEASE-RESULT.json').read_text())['sensor_result']['status'] == 'PASS'
    (root / 'content').write_text('mutated')
    with pytest.raises(ValueError, match='release_tree_drift'):
        release.try_identical_inputs({}, report, pointer, {'headRefOid':'HEAD','url':'PR'})
    (root / 'content').write_text('deployed')
    monkeypatch.setattr(hook, 'attach_sensor_result', lambda *a, **k: {'status':'FAIL','failure_count':1})
    with pytest.raises(ValueError, match='completeness_sensor'):
        release.try_identical_inputs({}, report, pointer, {'headRefOid':'HEAD','url':'PR'})
    job.save(base_report / 'SUCCESSFUL-INPUTS.json', {**baseline,'status':'PREPARED'})
    assert release.try_identical_inputs({}, report, pointer, {'headRefOid':'HEAD','url':'PR'}) is None
    job.save(base_report / 'SUCCESSFUL-INPUTS.json', baseline)
    sequence=iter([key, {**key,'sha256':'changed while validating'}])
    monkeypatch.setattr(release, 'fresh_release_inputs', lambda *a: next(sequence))
    monkeypatch.setattr(hook, 'attach_sensor_result', attach)
    assert release.try_identical_inputs({}, report, pointer, {'headRefOid':'HEAD','url':'PR'}) is None
    monkeypatch.setattr(release, 'fresh_release_inputs', lambda *a:key)
    job.save(pointer_path, {**pointer,'deployment_id':'OTHER'})
    with pytest.raises(ValueError, match='current_pointer_changed|main_release_base_not_terminal'):
        release.try_identical_inputs({}, report, pointer, {'headRefOid':'HEAD','url':'PR'})
    job.save(pointer_path, pointer)
    terminal.write_text('{}')
    refused = release.try_identical_inputs({}, report, pointer, {'headRefOid':'HEAD','url':'PR'})
    assert refused['status'] == 'REFUSED_IDENTICAL_INPUTS'
    assert 'event missing' in refused['error']


def test_cached_validation_keeps_raw_held_oracle_without_build(tmp_path, monkeypatch):
    import glp_release_main as release
    import glp_affiliate_sync_job as job
    import glp_affiliate_absence_audit as absence
    root = tmp_path / 'release'
    (root / 'artifacts').mkdir(parents=True)
    (root / 'public').mkdir()
    report = tmp_path / 'report'
    (report / 'registry').mkdir(parents=True)
    for name in ('registry-sync.json', 'sheet-snapshot.json'):
        (report / 'registry' / name).write_text('{}')
    monkeypatch.setattr(job, 'ci', lambda *a: {'state':'MERGED','headRefOid':'H'})
    source = tmp_path/'candidate'
    (source/'artifacts').mkdir(parents=True)
    write_recipe(source)
    def namespace(config, selected, report):
        assert selected == source
        return ['bwrap','--bind',str(source),'/site']
    monkeypatch.setattr(job, 'namespace', namespace)
    monkeypatch.setattr(absence, 'audit', lambda *a, **kw: {'status':'PASS'})
    calls = []
    monkeypatch.setattr(job, 'run', lambda argv, **kw: calls.append(argv) or '{}')
    pr = {'url':'PR','headRefOid':'H'}
    release.validate_cached_release({'namespace':'/site'}, report, root, pr, source)
    assert any('assertRawHeldOutput' in ' '.join(a) for a in calls)
    assert any(a[-3:] == ['npm','run','validate:coverage'] for a in calls)
    assert not any('build' in ' '.join(a[-3:]) for a in calls)
    assert all('--ro-bind' in a for a in calls)
    monkeypatch.setattr(job, 'ci', lambda *a: {'state':'MERGED','headRefOid':'OTHER'})
    with pytest.raises(ValueError, match='exact_head_ci'):
        release.validate_cached_release({'namespace':'/site'}, tmp_path/'bad-ci', root, pr, source)


def test_only_a_successful_publication_records_a_reusable_baseline(tmp_path):
    import glp_release_main as release
    import glp_affiliate_sync_job as job
    report = tmp_path / 'run'
    root = report / 'prepared/release'
    root.mkdir(parents=True)
    (root / 'file').write_text('real output')
    (report/'candidate').mkdir()
    write_recipe(report/'candidate')
    job.save(report / 'BUILD-INPUTS.json', {'sha256':'inputs'})
    pointer = {'release_root':str(root),'deployment_id':'D','site_head':'H'}
    assert release.record_successful_inputs(report, {'status':'PREPARED'}, pointer) is None
    assert not (report / 'SUCCESSFUL-INPUTS.json').exists()
    result = release.record_successful_inputs(report, {'status':'PASS_PRODUCTION_TERMINAL'}, pointer)
    assert result['pointer'] == pointer
    assert result['inputs'] == {'sha256':'inputs'}
    assert result['release_tree_sha256']


def test_stop92_blocks_before_build_even_when_a_gigabyte_is_available(monkeypatch):
    import shutil
    from collections import namedtuple
    import glp_release_main as release
    usage = namedtuple('Usage', 'total used free')
    monkeypatch.setattr(shutil, 'disk_usage', lambda _: usage(1000, 930, 70))
    assert release.build_storage_status()['blocked'] is True
    monkeypatch.setattr(shutil, 'disk_usage', lambda _: usage(1000, 910, 90))
    assert release.build_storage_status()['blocked'] is False


def test_missing_or_unbounded_inputs_cannot_form_a_skip_key(tmp_path):
    inputs = importlib.import_module('glp_release_inputs')
    config = {'namespace':'/site', 'base_argv':['/bin/true', '--bind',str(tmp_path),'/site']}
    with pytest.raises(ValueError, match='clearenv'):
        inputs.capture(config, source_tree='T', source_revision='H', sheet_sha256='S')
    config['base_argv'][1:1] = ['--clearenv', '--unshare-all', '--cap-drop', 'ALL']
    config['base_argv'] += ['--ro-bind',str(tmp_path/'missing'),'/evidence']
    with pytest.raises(FileNotFoundError):
        inputs.capture(config, source_tree='T', source_revision='H', sheet_sha256='S')


@pytest.mark.parametrize('mutation', ['effective', 'seed', 'generated-only'])
def test_tiny_gate_retains_input_drift_in_existing_failure(tmp_path, monkeypatch, mutation):
    """Promoted retained fixture: real capture/namespace, unchanged nested gate AST.

    No build, namespace executable, network or subprocess is run. Compiler and
    producer identities are synthetic constants, not native release evidence.
    """
    import ast
    import hashlib
    import os
    import socket
    import subprocess
    import types
    import glp_release_inputs as inputs
    import glp_affiliate_sync_job as namespace_owner
    root = Path(__file__).resolve().parents[1]
    tree = ast.parse((root / 'scripts/glp_release_main.py').read_text())
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'main')
    gated = next(n for n in ast.walk(main) if isinstance(n, ast.FunctionDef) and n.name == 'gated')
    # Exercise the actual existing FAILURE writer too, not a test serializer of
    # the new diagnostics. Its final bare raise is replaced only by the caller.
    failure = next(n for n in ast.walk(main) if isinstance(n, ast.ExceptHandler)
                   and any(isinstance(c, ast.Constant) and c.value == 'FAILURE.json' for c in ast.walk(n)))
    failure_code = compile(ast.fix_missing_locations(ast.Module(body=failure.body[:-1], type_ignores=[])),
                           '<native-failure-writer>', 'exec')
    source, seed, report = (tmp_path / name for name in ('source', 'seed', 'report'))
    for directory in (source, seed / 'npm-cache/_logs', report / 'registry'):
        directory.mkdir(parents=True)
    (seed / 'npm-cache/_logs/old.log').write_text('fixture-old-log')
    (seed / 'npm-cache/_cacache').mkdir()
    (seed / 'npm-cache/_cacache/input.bin').write_text('genuine-cache-input')
    (report / 'registry/sheet-snapshot.json').write_text('{"fixture":true}')
    tool = tmp_path / 'namespace-tool'
    tool.write_text('fixture executable identity; NEVER RUN')
    tool.chmod(0o755)
    config = {'namespace': '/site', 'base_argv': [str(tool), '--clearenv', '--unshare-all', '--cap-drop', 'ALL',
              '--bind', str(source), '/site', '--bind', str(seed), '/home/test']}
    now = dt.datetime(2026, 10, 16, 2, 36, tzinfo=dt.timezone.utc)
    real_capture = inputs.capture
    def capture(*args, **kwargs):
        return real_capture(*args, **kwargs, now=now)
    def save(path, value):
        Path(path).write_text(json.dumps(value, sort_keys=True))
    def sha(path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    job = types.SimpleNamespace(namespace=namespace_owner.namespace, save=save, sha=sha,
        run=lambda argv, **kw: 'fixture-tree' if argv[1:3] == ['rev-parse', 'HEAD^{tree}'] else '')
    def original_gate(source, rep, config, head):
        namespace_owner.namespace(config, source, rep)
        before = json.loads((rep / 'BUILD-INPUTS.json').read_text())
        (source / 'public').mkdir()
        (source / 'public/index.html').write_text('fixture generated source output')
        assert capture(config, source_tree='fixture-tree', source_revision=head,
                       sheet_sha256=sha(rep / 'registry/sheet-snapshot.json')) == before
        effective = Path(config['_effective_base_argv'][-2])
        if mutation == 'effective':
            (effective / 'npm-cache/_logs/old.log').unlink()
            (effective / 'npm-cache/_logs/new.log').write_text('fixture npm log rotation')
        elif mutation == 'seed':
            (seed / 'npm-cache/_cacache/input.bin').write_text('mutated genuine input')
    def deny(*args, **kwargs):
        raise AssertionError('network/subprocess forbidden in this fixture')
    monkeypatch.setattr(inputs, 'producer_closure', lambda *a: {'fixture': 'constant-producer'})
    monkeypatch.setattr(inputs, 'capture', capture)
    monkeypatch.setattr(socket, 'socket', deny)
    monkeypatch.setattr(subprocess, 'Popen', deny)
    env = {'job': job, 'original_gate': original_gate, 'log': lambda row: None, 'Path': Path,
           'secrets_scan': lambda path: {'fixture': 'not-a-real-scan'}, 'os': os, 'SCOPE': 'fixture',
           'report': report}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[gated], type_ignores=[])), '<native-gated>', 'exec'), env)
    if mutation == 'generated-only':
        env['gated'](source, report, config, 'fixture-head')
        assert not (report / 'FAILURE.json').exists()
        return
    with pytest.raises(ValueError, match='^main_build_inputs_changed_during_build$') as error:
        env['gated'](source, report, config, 'fixture-head')
    env['exc'] = error.value
    exec(failure_code, env)
    saved = json.loads((report / 'FAILURE.json').read_text())
    assert saved['status'] == 'FAIL' and saved['error'] == 'main_build_inputs_changed_during_build'
    drift = saved['input_drift']
    assert drift['before'] == json.loads((report / 'BUILD-INPUTS.json').read_text())
    assert drift['after'] == capture(config, source_tree='fixture-tree', source_revision='fixture-head',
                                     sheet_sha256=sha(report / 'registry/sheet-snapshot.json'))
    paths = {row['path'] for row in drift['differing_fields']}
    field = 'sha256' if mutation == 'effective' else 'seed_sha256'
    assert paths == {'/sha256', '/mounts/0/' + field}
    for row in drift['differing_fields']:
        assert row['before'] != row['after']
    timing = drift['timing']
    stamps = [timing['before']['started'], timing['before']['finished'], timing['gate']['started'],
              timing['gate']['finished'], timing['after']['started'], timing['after']['finished']]
    assert [s['monotonic_ns'] for s in stamps] == sorted(s['monotonic_ns'] for s in stamps)
    assert all(dt.datetime.fromisoformat(s['utc']).tzinfo for s in stamps)
    entries = drift['differing_entries']
    assert {r['role'] for r in entries} == {mutation}
    assert all(r['destination'] == '/home/test' for r in entries)
    names = {r['path'] for r in entries}
    assert names == ({'npm-cache/_logs/old.log', 'npm-cache/_logs/new.log'} if mutation == 'effective'
                     else {'npm-cache/_cacache/input.bin'})
    assert drift['writer'] == 'UNKNOWN'
    # Diagnostic metadata must not participate in equality or hide any input.
    diagnostics = {}
    plain = capture(config, source_tree='fixture-tree', source_revision='fixture-head', sheet_sha256='S')
    assert capture(config, source_tree='fixture-tree', source_revision='fixture-head', sheet_sha256='S',
                   diagnostics=diagnostics) == plain
    with pytest.raises(ValueError, match='effective_writable_bind_preexisting_drift'):
        namespace_owner.namespace(config, source, report)


def test_diagnostic_leaf_hashes_and_json_pointer_fields_are_exact(tmp_path):
    import hashlib
    import glp_release_inputs as inputs
    root = tmp_path / 'tmp'
    root.mkdir()
    file = root / 'genuine-cache-input'
    file.write_bytes(b'input bytes')
    file.chmod(0o640)
    (root / 'link').symlink_to('/not-mounted/private')
    entries = {}
    old = inputs.path_digest(root, entries=entries)
    assert inputs.path_digest(root) == old
    assert entries['genuine-cache-input'] == ['file', 0o640, 11, hashlib.sha256(b'input bytes').hexdigest()]
    assert entries['link'] == ['link', 0o777, '/not-mounted/private']
    assert entries['.'] == ['dir', root.stat().st_mode & 0o7777]
    file.write_bytes(b'real mutation')
    assert inputs.path_digest(root) != old  # /tmp/cache names do not exempt inputs
    file.write_bytes(b'input bytes')
    file.chmod(0o600)
    assert inputs.path_digest(root) != old
    fields = inputs.differing_fields({'a/b~': [None, 'old'], 'gone': None}, {'a/b~': [None, 'new']})
    assert fields == [{'path': '/a~1b~0/1', 'before': 'old', 'after': 'new'},
                      {'path': '/gone', 'before': None, 'after': None,
                       'before_present': True, 'after_present': False}]


def test_runtime_outputs_use_only_supported_private_dev_and_are_fingerprinted(tmp_path):
    import hashlib
    import glp_release_inputs as inputs
    import glp_affiliate_sync_job as job
    source = tmp_path / 'source'
    source.mkdir()
    plain = {'namespace': '/site', 'base_argv': ['/bin/true', '--clearenv', '--unshare-all',
             '--cap-drop', 'ALL', '--bind', str(source), '/site']}
    assert inputs.build_environment_argv(plain) == plain['base_argv']
    config = {**plain, 'base_argv': plain['base_argv'] + ['--dev', '/dev']}
    raw = list(config['base_argv'])
    projected = inputs.build_environment_argv(config)
    expected = {'npm_config_logs_dir': '/dev/shm/glp-build-runtime/npm-logs',
                'NODE_COMPILE_CACHE': '/dev/shm/glp-build-runtime/node-compile-cache'}
    assert config['base_argv'] == raw
    assert projected[-6:] == ['--setenv', 'npm_config_logs_dir', expected['npm_config_logs_dir'],
                              '--setenv', 'NODE_COMPILE_CACHE', expected['NODE_COMPILE_CACHE']]
    report = tmp_path / 'report'
    report.mkdir()
    assert job.namespace(config, source, report) == projected
    snapshot = inputs.capture(config, source_tree='T', source_revision='H', sheet_sha256='S')
    assert snapshot['namespace_argv_sha256'] != inputs.capture(plain, source_tree='T', source_revision='H',
                                                             sheet_sha256='S')['namespace_argv_sha256']
    assert snapshot['environment_sha256'] == {k: hashlib.sha256(v.encode()).hexdigest() for k, v in expected.items()}
    config['_effective_base_argv'][-1] = '/tmp/not-a-private-output'
    with pytest.raises(ValueError, match='effective_namespace_recipe_drift'):
        inputs.capture(config, source_tree='T', source_revision='H', sheet_sha256='S')
    for suffix in (['--bind', str(source), '/dev/shm'], ['--ro-bind', str(source), '/dev'],
                   ['--symlink', '/home/test', '/dev/shm'], ['--tmpfs', '/dev/../dev/shm']):
        unsupported = {**plain, 'base_argv': raw + suffix}
        assert inputs.build_environment_argv(unsupported) == unsupported['base_argv']
