"""F03 is source-only; only the hourly main entry owns production."""
import json
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import glp_affiliate_sync_job as job
import glp_release_main as release


def test_registered_f03_ignores_legacy_publish_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(job, 'ROOT', tmp_path)
    config = tmp_path / 'config.json'
    config.write_text('{}')
    calls = []
    monkeypatch.setattr(job, 'execute', lambda *a, **kw: calls.append(kw) or {'status': 'PASS_SOURCE_NO_CHANGE'})
    monkeypatch.setattr(job, 'retire_disposable_clones', lambda *a, **kw: [])
    monkeypatch.setattr(sys, 'argv', ['sync', '--config', str(config), '--publish'])
    assert job.main() == 0
    assert calls[0]['publish'] is False
    assert calls[0]['source_only'] is True


def test_f03_merge_handoff_is_selected_by_hourly_release(tmp_path, monkeypatch):
    import glp_affiliate_registry_sync as registry
    import glp_affiliate_release as packaging
    monkeypatch.setattr(job, 'ROOT', tmp_path)
    monkeypatch.setattr(registry, 'sync', lambda *a, **kw: {'changed': 0})
    head, merged, tree = 'a' * 40, 'b' * 40, 'c' * 40
    url = 'https://github.com/izaiahat/glp-site-candidate/pull/59'
    calls = []
    def run(argv, **kw):
        calls.append(argv)
        if argv[:3] == ['git', 'rev-parse', 'HEAD']: return head
        if argv[:2] == ['git', 'rev-parse']: return tree
        if argv[:3] == ['gh', 'pr', 'view']:
            return json.dumps({'state': 'MERGED', 'headRefOid': head, 'mergeCommit': {'oid': merged}})
        return ''
    monkeypatch.setattr(job, 'run', run)
    monkeypatch.setattr(job, 'ci', lambda *a: {'state': 'OPEN', 'headRefOid': head, 'url': url})
    def forbidden(*a, **kw): raise AssertionError('F03 reached build/publisher')
    monkeypatch.setattr(job, 'build_gate', forbidden)
    monkeypatch.setattr(packaging, 'package_and_publish', forbidden)
    report = tmp_path / 'f03'
    report.mkdir()
    out = job.execute({}, report, source_ref=head, source_pr=url, publish=True)
    assert out['status'] == 'PASS_SOURCE_MERGED'
    assert out['production_publisher'] == 'glp_release_main.py'
    assert out['deployed'] is False
    assert ['gh', 'pr', 'merge', url, '--repo', job.REPO, '--merge', '--match-head-commit', head] in calls
    assert not any('candidate_build_lock.py' in ' '.join(c) for c in calls)
    assert not (report / 'publish-intent.json').exists()
    def gh(*argv):
        if argv[0] == 'pr':
            return [{'url': url, 'headRefOid': head, 'mergeCommit': {'oid': merged}}]
        return {'commit': {'tree': {'sha': tree}}}
    selected = release.merged_pr_for(merged, gh=gh)
    monkeypatch.setattr(release, 'ROOT', tmp_path)
    monkeypatch.setattr(release, 'RUNS', tmp_path / 'hourly')
    monkeypatch.setattr(release, 'merged_pr_for', lambda sha: selected)
    monkeypatch.setattr(release, 'pointer_head', lambda: ('old', 'old-dep'))
    pointer = tmp_path / 'reports/glp-clinic-scout/seo-machine/current-content-base.json'
    pointer.parent.mkdir(parents=True)
    pointer.write_text(json.dumps({'release_root': str(tmp_path / 'old')}))
    monkeypatch.setattr(job, 'rebuild_inputs_complete', lambda *a: True)
    hourly = []
    monkeypatch.setattr(job, 'execute', lambda *a, **kw: hourly.append(kw) or {'status': 'PASS_PRODUCTION_TERMINAL'})
    import glp_deploy_glp34_authority_v1 as carrier
    monkeypatch.setattr(carrier, 'MAIN_RELEASE_FRESH', False)
    monkeypatch.setattr(release, 'install_cache_convergence', lambda *a: None)
    monkeypatch.setattr(packaging, 'authenticated_current_base', packaging.authenticated_current_base)
    monkeypatch.setattr(carrier, 'current_route_matrix', carrier.current_route_matrix)
    monkeypatch.setattr(job, 'build_gate', job.build_gate)
    monkeypatch.setattr(job, 'CONFIG', config := tmp_path / 'config.json')
    config.write_text('{}')
    # main intentionally sets process-local carrier bindings; undo after proof.
    monkeypatch.setattr(carrier, 'MAIN_RELEASE_FRESH', carrier.MAIN_RELEASE_FRESH)
    assert release.main(['--sha', merged]) == 0
    assert hourly == [{'source_ref': head, 'source_pr': url, 'publish': True, 'source_only': False}]


def test_f03_resume_cannot_publish(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['sync', '--resume-report', str(tmp_path), '--publish'])
    with pytest.raises(ValueError, match='hourly_main_release_only'):
        job.main()
