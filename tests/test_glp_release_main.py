import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import glp_release_main as rm  # noqa: E402
import glp_affiliate_sync_job as job  # noqa: E402


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
