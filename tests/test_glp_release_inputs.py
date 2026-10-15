"""Complete release inputs, not a changed-path/test-only heuristic."""
import datetime as dt
import importlib
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))


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
                                         sheet_sha256=kw.get('sheet', 'SHEET'), now=kw.get('now', now))
    baseline = capture()
    assert capture() == baseline
    for name in ['taxonomy.json', 'package-lock.json']:
        p = data / name
        old = p.read_bytes()
        p.write_bytes(old + b' ')
        assert capture()['sha256'] != baseline['sha256']
        p.write_bytes(old)
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
    pointer = {'site_head':'HEAD','release_root':str(root),'deployment_id':'DEP'}
    key = {'sha256':'INPUTS','source_tree':'TREE'}
    baseline = {'status':'PASS_PRODUCTION_TERMINAL','pointer':pointer,'inputs':key,
                'release_tree_sha256':inputs.path_digest(root)}
    job.save(base_report / 'SUCCESSFUL-INPUTS.json', baseline)
    calls = []
    monkeypatch.setattr(release, 'fresh_release_inputs', lambda *a: key)
    monkeypatch.setattr(release, 'validate_cached_release', lambda *a: calls.append('validated'))
    monkeypatch.setattr(hook, 'attach_sensor_result', lambda *a, **k: {'status':'PASS','failure_count':0})
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
    monkeypatch.setattr(job, 'ci', lambda *a: {})
    monkeypatch.setattr(job, 'namespace', lambda *a: ['bwrap','--bind',str(root),'/site'])
    monkeypatch.setattr(absence, 'audit', lambda *a: {'status':'PASS'})
    calls = []
    monkeypatch.setattr(job, 'run', lambda argv, **kw: calls.append(argv) or '{}')
    release.validate_cached_release({'namespace':'/site'}, report, root, {'url':'PR'})
    assert any('assertRawHeldOutput' in ' '.join(a) for a in calls)
    assert any(a[-3:] == ['npm','run','validate'] for a in calls)
    assert not any('build' in ' '.join(a[-3:]) for a in calls)
    assert all('--ro-bind' in a for a in calls)


def test_only_a_successful_publication_records_a_reusable_baseline(tmp_path):
    import glp_release_main as release
    import glp_affiliate_sync_job as job
    report = tmp_path / 'run'
    root = report / 'prepared/release'
    root.mkdir(parents=True)
    (root / 'file').write_text('real output')
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
        inputs.capture(config, source_tree='T', sheet_sha256='S')
    config['base_argv'][1:1] = ['--clearenv', '--unshare-all', '--cap-drop', 'ALL']
    config['base_argv'] += ['--ro-bind',str(tmp_path/'missing'),'/evidence']
    with pytest.raises(FileNotFoundError):
        inputs.capture(config, source_tree='T', sheet_sha256='S')
