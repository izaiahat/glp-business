import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import glp_release_main as rm  # noqa: E402
from test_codex_cloud_key_renewal import (
    test_renewal_monitor_deadline_and_exact_readback,
    test_renewal_monitor_uncertain_post_reconciles_without_repost,
    test_renewal_registry_preserves_existing_scope,
)
from test_glp_single_publisher import (
    test_registered_f03_ignores_legacy_publish_flag,
    test_f03_merge_handoff_is_selected_by_hourly_release,
    test_f03_resume_cannot_publish,
)
import glp_affiliate_sync_job as job  # noqa: E402


def test_rebuild_manifests_are_retained_and_missing_inputs_refuse_skip(tmp_path):
    names = ('artifacts/ops/build-report.json', 'artifacts/ops/price-index-manifest.json',
             'artifacts/ops/wave4-deal-ingestion-manifest.json')
    assert not job.rebuild_inputs_complete(tmp_path)
    for name in names:
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('{"fixture":true}')
    rows = job.build_authority_files(tmp_path)
    assert tuple(r['file'] for r in rows) == names
    assert all(r['sha256'] == job.sha(tmp_path / r['file']) for r in rows)
    assert job.rebuild_inputs_complete(tmp_path)
    p.unlink()
    assert not job.rebuild_inputs_complete(tmp_path)
    p.symlink_to(tmp_path / names[0])
    assert not job.rebuild_inputs_complete(tmp_path)


def test_equivalent_rebuild_refuses_source_worker_public_or_pin_drift(tmp_path, monkeypatch):
    import hashlib
    import glp_affiliate_release as release
    from glp_deploy_glp34_authority_v1 import file_closure
    monkeypatch.setattr(job, 'ROOT', tmp_path)
    monkeypatch.setattr(release, 'validate_build_evidence', lambda *a: None)
    runs = tmp_path / 'runs'
    base = runs / 'main-old/prepared/release'
    donor = runs / 'main-new/prepared/release'
    base.mkdir(parents=True)
    donor.mkdir(parents=True)
    def put(path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
        return {'path': str(path), 'sha256': job.sha(path)}
    rows = []
    for name in job.BUILD_AUTHORITY_FILES[1:]:
        put(donor / name, {'fixture': True})
        rows.append({'file': name, 'sha256': job.sha(donor / name)})
    old = {'schema': 'glp43-current-prepared-v1', 'site_head': 'head', 'public_tree_sha256': 'public',
           'compiled': {'worker_sha256': 'worker'}, 'deploy_directory': {'tree_sha256': 'deployed'},
           'source': {'tracked_files': [['a', 'hash']], 'authority_files': []}, 'release': file_closure(base)}
    old_spec = put(base.parent / 'PREPARED.json', old)
    terminal = tmp_path / 'reports/glp-clinic-scout/seo-machine/current-release-head/CURRENT-RECEIPT.json'
    put(terminal, {'status': 'PASS_PRODUCTION_TERMINAL', 'binding': {'prepared_manifest_sha256': old_spec['sha256']}})
    gate = put(donor.parent / 'gate.json', {'fixture': True})
    package = put(donor.parent / 'PACKAGE-MANIFEST.json', {'files': []})
    new = dict(old, source={'tracked_files': old['source']['tracked_files'], 'authority_files': rows},
               release=file_closure(donor), build_receipt=gate, package_manifest_sha256=package['sha256'])
    def bind(data):
        spec = put(donor.parent / 'PREPARED.json', data)
        put(donor.parents[1] / 'publish-intent.json', {'base': {'release_root': str(base)}, 'prepared': spec})
    bind(new)
    assert len(job.equivalent_main_rebuild_inputs(base)) == 2
    monkeypatch.setattr(rm, 'ROOT', tmp_path)
    put(donor.parents[1] / 'FAILURE.json', {'status': 'FAIL', 'scope': rm.SCOPE,
        'error': 'current_release_consumed_or_binding_changed'})
    assert rm.main_pre_effect_refusal(donor.parents[1])
    put(terminal, {'status': 'ACTIVE', 'binding': {'prepared_manifest_sha256': old_spec['sha256']}})
    assert not rm.main_pre_effect_refusal(donor.parents[1])
    assert job.equivalent_main_rebuild_inputs(base) == {}
    put(terminal, {'status': 'PASS_PRODUCTION_TERMINAL', 'binding': {'prepared_manifest_sha256': old_spec['sha256']}})
    for field, bad in (('public_tree_sha256', 'changed'), ('compiled', {'worker_sha256': 'different'}),
                       ('source', {'tracked_files': [['a', 'changed']], 'authority_files': rows})):
        bind(dict(new, **{field: bad}))
        assert job.equivalent_main_rebuild_inputs(base) == {}
    bind(new)
    put(donor / rows[0]['file'], {'changed': True})
    assert job.equivalent_main_rebuild_inputs(base) == {}


def test_purge_urls_only_bare_routes_never_hashed_assets():
    specs = [{'url_path': p} for p in ('/', '/assets/site.css', '/assets/analytics.js',
                                       '/assets/analytics.0a0a7729cb628fc7.js', '/sitemap.xml', '/assets/site.css')]
    assert rm.purge_urls(specs, 'https://glpclinicscout.com') == [
        'https://glpclinicscout.com/', 'https://glpclinicscout.com/assets/analytics.js',
        'https://glpclinicscout.com/assets/site.css', 'https://glpclinicscout.com/sitemap.xml']


def test_purger_refuses_wildcard_or_foreign_zone_and_uses_files_only(monkeypatch):
    calls = []

    class Resp:
        def __init__(self, payload): self.payload = payload
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps(self.payload).encode()

    def urlopen(req, timeout):
        calls.append((req.get_method(), req.full_url, req.data))
        if req.get_method() == 'GET':
            return Resp({'success': True, 'result': [{'id': 'z1', 'name': 'glpclinicscout.com'}]})
        return Resp({'success': True, 'result': {'id': 'x'}})
    monkeypatch.setattr(rm.urllib.request, 'urlopen', urlopen)
    purge = rm.make_purger(lambda: 'tok', lambda row: None)
    with pytest.raises(RuntimeError, match='outside_zone_or_wildcard'):
        purge(['https://example.com/x'])
    with pytest.raises(RuntimeError, match='outside_zone_or_wildcard'):
        purge(['https://glpclinicscout.com/*'])
    urls = [f'https://glpclinicscout.com/p{i}' for i in range(65)]
    assert purge(urls) == [30, 30, 5]
    posts = [json.loads(c[2]) for c in calls if c[0] == 'POST']
    assert all(set(body) == {'files'} for body in posts)
    assert not any('purge_everything' in json.dumps(body) for body in posts)


def test_secrets_scan(tmp_path):
    (tmp_path / 'a.html').write_text('<p>ok sk_test_abc</p>')
    assert rm.secrets_scan(tmp_path)['status'] == 'PASS'
    (tmp_path / 'b.js').write_text('var k="ghp_' + 'a' * 36 + '"')
    with pytest.raises(RuntimeError, match='secrets_scan_failed'):
        rm.secrets_scan(tmp_path)


def test_merged_pr_for_requires_exact_merge_and_tree():
    trees = {'H': 'T', 'M': 'T', 'H2': 'T2'}

    def gh(*args):
        if args[0] == 'pr':
            return [{'url': 'u', 'headRefOid': 'H', 'mergeCommit': {'oid': 'M'}}]
        return {'commit': {'tree': {'sha': trees[args[1].rsplit('/', 1)[1]]}}}
    assert rm.merged_pr_for('M', gh=gh)['headRefOid'] == 'H'
    with pytest.raises(RuntimeError, match='not_a_merged_pr'):
        rm.merged_pr_for('X', gh=gh)
    trees['H'] = 'other'
    with pytest.raises(RuntimeError, match='tree_differs'):
        rm.merged_pr_for('M', gh=gh)


def test_cache_convergence_purges_once_then_runs_unchanged_matrix(tmp_path):
    seen = []
    carrier = SimpleNamespace(SITE='https://glpclinicscout.com', checkpoint=lambda p, v: None,
                              current_route_matrix=lambda *a, **k: seen.append('matrix') or ['ok'])
    purged = []
    rm.install_cache_convergence(carrier, lambda urls: purged.append(urls), None)
    receipt = {'stages': {}}
    specs = [{'url_path': '/assets/site.css'}]
    for _ in range(2):
        assert carrier.current_route_matrix(None, {'id': 'd1'}, specs, tmp_path, tmp_path / 'r', receipt) == ['ok']
    assert purged == [['https://glpclinicscout.com/assets/site.css']]
    assert seen == ['matrix', 'matrix']
    assert receipt['stages']['cache_purge_by_url'] == {'deployment_id': 'd1', 'count': 1}


def test_explicit_main_source_never_shortcuts_on_unchanged_affiliate_surfaces(tmp_path, monkeypatch):
    import glp_affiliate_registry_sync as registry
    import glp_affiliate_absence_audit as absence
    monkeypatch.setattr(job, 'ROOT', tmp_path)
    monkeypatch.setattr(job, 'run', lambda *a, **k: '')
    monkeypatch.setattr(registry, 'sync', lambda *a, **k: {'changed': 0})
    monkeypatch.setattr(job, 'read', lambda p: {'release_root': str(tmp_path / 'old'), 'deployment_id': 'old-deployment'})
    monkeypatch.setattr(job, 'rebuild_inputs_complete', lambda p: True)
    monkeypatch.setattr(job, 'sha', lambda p: 'same-affiliate-surface')
    # The accepted-guide source changes, although these five affiliate surfaces do not.
    for rel in ['data/program-registry.json','src/affiliate-relationship-sync.mjs',
                'src/go-runtime-selection.mjs','scripts/build-staging.mjs','functions/go/program-manifest.js']:
        p = tmp_path / 'old' / rel
        p.parent.mkdir(parents=True, exist_ok=True); p.write_text('same')
    monkeypatch.setattr(absence, 'audit', lambda *a, **k: {
        'remaining_unmapped': 0,'evidenced_no_live_offer_presence': 0,'unaccounted': 0})
    monkeypatch.setattr(job, 'save', lambda *a: None)
    def ci(*args):
        raise RuntimeError('exact-head-ci-gate-reached')
    monkeypatch.setattr(job, 'ci', ci)
    assert job.execute({},tmp_path / 'auto',source_only=False)['status'] == 'PASS_ALREADY_CURRENT'
    for ref, pr in [('guide-head','guide-pr'), (None,'guide-pr')]:
        with pytest.raises(RuntimeError,match='exact-head-ci-gate-reached'):
            job.execute({},tmp_path / 'explicit',source_ref=ref,source_pr=pr,publish=True)


def test_affiliate_sync_consumes_exact_main_pre_effect_reconciliation(tmp_path, monkeypatch):
    """The hourly sync must share the maintained main-release no-effect refusal."""
    monkeypatch.setattr(job, 'ROOT', tmp_path)
    monkeypatch.setattr(rm, 'ROOT', tmp_path)
    config = tmp_path / 'config.json'
    config.write_text('{}')
    runs = tmp_path / 'reports/glp-clinic-scout/affiliate-machine/registry-site-sync'
    prior = runs / 'main-20261009T063150Z'
    prior.mkdir(parents=True)
    (prior / 'release').mkdir()
    prepared = prior / 'prepared/PREPARED.json'
    prepared.parent.mkdir()
    prepared.write_text(json.dumps({'site_head': 'H', 'release_root': str(prior / 'release')}))
    (prior / 'publish-intent.json').write_text(json.dumps({'prepared': {
        'path': str(prepared), 'sha256': job.sha(prepared)}}))
    (prior / 'FAILURE.json').write_text(json.dumps({'status':'FAIL', 'scope': rm.SCOPE,
        'error': 'current_release_consumed_or_binding_changed'}))
    native = tmp_path / 'reports/glp-clinic-scout/seo-machine/current-release-H/CURRENT-RECEIPT.json'
    native.parent.mkdir(parents=True)
    monkeypatch.setattr(job, 'equivalent_main_rebuild_inputs', lambda p: {'one':{},'two':{}})
    monkeypatch.setattr(job, 'retire_disposable_clones', lambda *a, **k: [])
    calls = []
    def execute(*args, **kwargs):
        calls.append(kwargs)
        return {'status':'PASS_ALREADY_CURRENT'}
    monkeypatch.setattr(job, 'execute', execute)
    for status, accepted in [('ACTIVE',False),('PASS_PRODUCTION_TERMINAL',True)]:
        native.write_text(json.dumps({'status':status, 'binding': {
            'prepared_manifest_sha256': 'different-consumed-prepared-binding'}}))
        output = tmp_path / ('manual-' + status)
        monkeypatch.setattr(sys, 'argv', ['glp_affiliate_sync_job.py', '--config', str(config), '--output', str(output)])
        if not accepted:
            with pytest.raises(RuntimeError, match='prior_publication_uncertain'):
                job.main()
            assert calls == []
            assert not output.exists()
        else:
            assert job.main() == 0
            assert len(calls) == 1
    assert not (prior / 'PUBLISH.json').exists()  # never invent a publication receipt


def test_sync_job_skips_pending_heads_already_in_production(tmp_path, monkeypatch):
    pointer = tmp_path / 'reports/glp-clinic-scout/seo-machine/current-content-base.json'
    pointer.parent.mkdir(parents=True)
    pointer.write_text(json.dumps({'site_head': 'NEW'}))
    monkeypatch.setattr(job, 'ROOT', tmp_path)
    assert job.superseded('NEW') is True
    assert job.superseded('OLD', gh=lambda path: {'status': 'ahead'}) is True
    assert job.superseded('OTHER', gh=lambda path: {'status': 'diverged'}) is False


def test_main_release_base_skips_stale_prepared_chain_but_requires_terminal_pointer(tmp_path):
    import hashlib
    import glp_affiliate_release as release
    final = tmp_path / 'final.json'
    final.write_text(json.dumps({'status': 'PASS_PRODUCTION_TERMINAL',
                                 'deployment': {'deployment_id': 'dep-1'}, 'site_head': 'H'}))
    pointer = tmp_path / 'pointer.json'
    good = {'status': 'PASS', 'deployment_id': 'dep-1', 'site_head': 'H',
            'release_root': str(tmp_path / 'stale/release'),  # no PREPARED.json: never read
            'final_receipt': {'path': str(final), 'sha256': hashlib.sha256(final.read_bytes()).hexdigest()}}
    pointer.write_text(json.dumps(good))
    carrier = SimpleNamespace(POINTER=pointer)
    assert release.main_release_base(carrier)['deployment_id'] == 'dep-1'
    for drift in ({'deployment_id': 'dep-2'}, {'status': 'FAIL'}, {'site_head': 'X'}):
        pointer.write_text(json.dumps({**good, **drift}))
        with pytest.raises(ValueError, match='main_release_base_not_terminal'):
            release.main_release_base(carrier)
    pointer.write_text(json.dumps({**good, 'final_receipt': {**good['final_receipt'], 'sha256': '0' * 64}}))
    with pytest.raises(ValueError, match='evidence_drift'):
        release.main_release_base(carrier)
    # The fresh-main flag defaults off so legacy affiliate-sync callers keep the D1 replay.
    import glp_deploy_glp34_authority_v1 as carrier_module
    assert carrier_module.MAIN_RELEASE_FRESH is False
    runs = carrier_module.BUSINESS / 'reports/glp-clinic-scout/affiliate-machine/registry-site-sync'
    assert carrier_module.main_release_fresh(runs / 'main-20261009T045825Z/prepared') is True
    assert carrier_module.main_release_fresh(runs / '20261003T030854Z/prepared') is False
    assert carrier_module.main_release_fresh(runs / 'main-20261009T045825Z/other') is False
    assert carrier_module.main_release_fresh(Path('/tmp/main-20261009T045825Z/prepared')) is False
