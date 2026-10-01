"""Offline regressions for bounded, resumable Monday discovery."""
import importlib.util
import json
from pathlib import Path
import signal
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def discovery():
    return load('affiliate_supply_discovery')


def test_blocking_read_is_bounded_and_error_not_empty(discovery, tmp_path):
    checkpoint = discovery.DiscoveryCheckpoint(tmp_path / 'checkpoint.json', run_id='one',
                                               budget_s=1, source_timeout_s=.03)
    previous_handler = signal.getsignal(signal.SIGALRM)
    started = time.monotonic()
    log = []
    discovery.run_search(checkpoint.wrap('search', lambda *args: time.sleep(1)), ['slow'], log=log)
    assert time.monotonic() - started < .3
    assert log[0]['status'] == 'error:SourceUnavailable'
    receipt = checkpoint.finish()
    assert receipt['unavailable'][0]['reason'] == 'source_timeout'
    assert receipt['status'] == 'DISCOVERED_WITH_SOURCE_ERRORS'
    assert signal.getsignal(signal.SIGALRM) == previous_handler
    assert signal.getitimer(signal.ITIMER_REAL)[0] == 0


def test_deadline_inside_read_stays_retryable(discovery, tmp_path):
    path = tmp_path / 'checkpoint.json'
    checkpoint = discovery.DiscoveryCheckpoint(path, run_id='one', budget_s=.03, source_timeout_s=1)
    with pytest.raises(discovery.SourceDeferred):
        checkpoint.wrap('catalog', lambda: time.sleep(1))()
    assert checkpoint.finish()['status'] == 'PARTIAL_RESUMABLE'
    resumed = discovery.DiscoveryCheckpoint(path, run_id='two', budget_s=1)
    assert resumed.wrap('catalog', lambda: {'programs': []})() == {'programs': []}
    assert resumed.finish()['complete']


def test_resume_replays_completed_reads_and_preserves_entire_tail(discovery, tmp_path):
    path = tmp_path / 'checkpoint.json'
    queries = ['first', 'second', 'third', 'fourth', 'fifth']
    calls = []
    def search(query, limit):
        calls.append(query)
        return [{'url': 'https://example.invalid/' + query, 'title': query}]
    for run_id in ['one', 'two', 'three']:
        checkpoint = discovery.DiscoveryCheckpoint(path, run_id=run_id, budget_s=2, max_calls=2)
        hits = discovery.run_search(checkpoint.wrap('search', search), queries)
        receipt = checkpoint.finish()
        if run_id == 'one':
            assert len(receipt['pending']) == 3
    assert calls == queries
    assert len(hits) == 5
    assert receipt['complete']
    assert json.loads(path.read_text())['cycle_id'] == 'one'
    assert path.stat().st_mode & 0o777 == 0o600


def test_failed_source_does_not_starve_later_sources(discovery, tmp_path):
    path = tmp_path / 'checkpoint.json'
    calls = []
    def search(query, limit):
        calls.append(query)
        if query == 'broken':
            raise ValueError('provider exception must not be serialized')
        return []
    for run_id in ['one', 'two']:
        cp = discovery.DiscoveryCheckpoint(path, run_id=run_id, budget_s=1, max_calls=1)
        discovery.run_search(cp.wrap('search', search), ['broken', 'healthy'])
        receipt = cp.finish()
    assert calls == ['broken', 'healthy']
    assert receipt['complete']
    assert receipt['unavailable'][0]['reason'] == 'ValueError'
    assert 'provider exception' not in path.read_text()


@pytest.mark.parametrize('status', ['error:SourceUnavailable', 'error:SourceDeferred', 'skipped_time_budget'])
def test_failed_or_deferred_query_is_never_evidenced_empty(discovery, status):
    receipt = discovery.supply_expansion._receipt(
        discovery, 'query_expansion', {}, evidence=[], query_log=[{'status': status}])
    assert receipt['status'] == 'INCOMPLETE'
    assert receipt['evidenced_empty'] is False


def test_expansion_retains_more_than_old_query_cap(discovery):
    queries = [f'unique query {i}' for i in range(250)]
    assert discovery.supply_expansion._unique(queries, cap=8) == queries
    seen = []
    result = discovery.supply_expansion.query_expansion(
        discovery, lambda query, limit: seen.append(query) or [], lambda url: {},
        known=discovery.KnownSupply(),
        policy_snapshot=discovery.operator_rulings.load_snapshot(ROOT / 'nonexistent-fixture-rulings'),
        queries=queries)
    assert seen == queries
    assert len(result['queries_run']) == 250


@pytest.mark.parametrize('legacy_partial', [False, True])
def test_review_r2_resume_with_own_inventory_completes_enrichment(discovery, tmp_path, monkeypatch, legacy_partial):
    path = tmp_path / 'checkpoint.json'
    root = tmp_path / 'machine'
    monkeypatch.setattr(discovery, 'MACHINE_DIR', root)
    url = 'https://resumeclinic.example/affiliate'
    calls = []
    def search(query, limit):
        return [{'url': url, 'title': 'Resume Clinic',
                 'description': 'US telehealth clinic prescribes semaglutide medication.'}]
    def scrape(value):
        calls.append(value)
        return {'status': 'ok', 'markdown': 'Resume Clinic is a US telehealth clinic. '
                'Our clinicians prescribe semaglutide medication for weight loss. '
                'We serve patients in the United States. Our affiliate program is open for applications.'}
    snapshot = discovery.operator_rulings.load_snapshot(tmp_path / 'rulings')
    monkeypatch.setattr(discovery.operator_rulings, 'load_snapshot', lambda: snapshot)
    monkeypatch.setattr(discovery, 'DIRECT_QUERIES', ('query',))
    options = dict(search=search, scrape=scrape, seed_from_sheet=False,
                   do_five_routes=False, do_marketplaces=False, checkpoint_path=path,
                   source_timeout_s=1, time_budget_s=10)
    first = discovery.run(known=discovery.KnownSupply(), max_source_calls=1,
        out_dir=root / 'run-one', run_id='one', **options)
    assert first['source_coverage']['status'] == 'PARTIAL_RESUMABLE'
    own_inventory = json.loads(Path(first['apply_inventory']).read_text())
    if legacy_partial:
        # Emulate the old version's real builder publication, not a new approval.
        hit = discovery.Hit(url, 'Resume Clinic', search('query', 10)[0]['description'], 'query')
        own_inventory = discovery.build_apply_inventory([discovery.to_candidate(hit)])
        discovery.private_json(Path(first['apply_inventory']), own_inventory)
    # Include this exact run's real publication in the second dedupe universe.
    known = discovery.load_known_supply(sheet_export={}, inventories=[own_inventory], ledger={})
    second = discovery.run(known=known, max_source_calls=10,
        out_dir=root / 'run-two', run_id='two', **options)
    assert calls == [url]
    assert second['source_coverage']['complete'] is True
    inventory = json.loads(Path(second['apply_inventory']).read_text())
    assert inventory['programs'][0]['us_service_evidence']
    assert not any(r['status'] == 'retired' for r in json.loads(path.read_text())['operations'].values())


def test_review_r4_failed_platform_query_never_emits_empty_subreceipt(discovery, tmp_path):
    def failed(*args):
        raise RuntimeError('synthetic source failure')
    result = discovery.supply_expansion.platform_sweeps(
        discovery, failed, lambda url: {}, known=discovery.KnownSupply(),
        policy_snapshot=discovery.operator_rulings.load_snapshot(tmp_path / 'rulings'))
    assert result['status'] == 'INCOMPLETE'
    public = [r for r in result['platform_receipts'] if r['mode'] == 'public_search']
    assert public and all(r['status'] in {'UNKNOWN', 'FAILED'} for r in public)
    assert not result['evidenced_empty']


def test_review_r5_provider_plan_preserves_all_1002_rows(discovery, tmp_path):
    seeds = {'providers': [{'name': f'Provider {i}', 'official_url': f'https://seed{i}.example/'}
                           for i in range(1002)]}
    seen = []
    result = discovery.supply_expansion.provider_first(
        discovery, lambda q, limit: seen.append(q) or [], lambda url: {},
        known=discovery.KnownSupply(), source_docs=[seeds], enumeration_queries=['enumerate'],
        policy_snapshot=discovery.operator_rulings.load_snapshot(tmp_path / 'rulings'))
    assert result['evidence'][-1]['provider_seeds'] == 1002
    assert len(seen) == len(result['queries_run']) == 2005
    assert '"Provider 1001" affiliate program' in seen


def test_unvisited_pending_read_cannot_complete_partial_cycle(discovery, tmp_path):
    path = tmp_path / 'checkpoint.json'
    first = discovery.DiscoveryCheckpoint(path, run_id='one', budget_s=1, max_calls=0)
    with pytest.raises(discovery.SourceDeferred):
        first.wrap('scrape', lambda url: {} )('https://pending.example/affiliate')
    assert first.finish()['pending']
    resumed = discovery.DiscoveryCheckpoint(path, run_id='two', budget_s=1)
    assert resumed.finish()['status'] == 'PARTIAL_RESUMABLE'
    assert resumed.doc['operations'] == json.loads(json.dumps(first.doc['operations']))


def test_scheduled_budget_leaves_time_for_queue_and_receipts():
    wrapper = (ROOT / 'ops/hermes-cron-scripts/affiliate_supply_discovery_daily.sh').read_text()
    assert 'AFFILIATE_MONDAY_TIME_BUDGET_SECONDS:-360' in wrapper
    assert '--checkpoint ' in wrapper
    assert '--admit-applications' in wrapper


def test_marketplace_success_is_not_catalog_completion():
    module = load('affiliate_monday_marketplace_receipt')
    result = module.build({'networks': {'awin': {'status': 'error'}}},
                          {'status': 'success'}, joined={'awin', 'cj'})
    assert result['coverage']['completed'] == 0
    assert result['scope'].startswith('historical')
    assert not hasattr(module, 'latest_execution')


def test_card_exposes_resumable_coverage(tmp_path):
    module = load('affiliate_monday_discovery_card')
    (tmp_path / 'summary.json').write_text(json.dumps({
        'status': 'PARTIAL_RESUMABLE', 'source_coverage': {'pending': [{}, {}], 'unavailable': [{}]}}))
    result = module.compose(tmp_path, {})
    assert 'PARTIAL_RESUMABLE' in result['card']
    assert 'pending reads: **2**' in result['card']
    assert 'unavailable reads: **1**' in result['card']


def test_complete_run_resumes_into_reviewable_receipts(discovery, monkeypatch, tmp_path):
    machine = tmp_path / 'machine'
    monkeypatch.setattr(discovery, 'MACHINE_DIR', machine)
    monkeypatch.setattr(discovery, 'DIRECT_QUERIES', ('one', 'two', 'three'))
    monkeypatch.setattr(discovery, 'carry_forward_supply', lambda: [])
    snapshot = discovery.operator_rulings.load_snapshot(tmp_path / 'rules')
    monkeypatch.setattr(discovery.operator_rulings, 'load_snapshot', lambda: snapshot)
    calls = []
    def search(query, limit):
        calls.append(query)
        return []
    checkpoint = tmp_path / 'checkpoint.json'
    for run_id, call_cap in [('first', 1), ('second', 10)]:
        summary = discovery.run(
            search=search, scrape=lambda url: {}, run_id=run_id,
            out_dir=tmp_path / run_id, known=discovery.KnownSupply(),
            seed_from_sheet=False, do_five_routes=False, do_marketplaces=False,
            time_budget_s=1, max_source_calls=call_cap, checkpoint_path=checkpoint)
        if run_id == 'first':
            assert summary['status'] == 'PARTIAL_RESUMABLE'
            assert len(summary['source_coverage']['pending']) == 2
    assert calls == ['one', 'two', 'three']
    assert summary['status'] == 'DISCOVERED'
    assert Path(summary['apply_inventory']).is_file()
    assert Path(summary['sheet_candidate_plan']).is_file()
    assert summary['effects'] == {'applications_submitted': 0, 'sheet_writes': 0,
                                  'tracking_links_clicked': 0, 'outreach_sent': 0}


@pytest.mark.parametrize('text,region,status', [
    ('We deliver GLP-1 telehealth medication in the United States.\nJoin our affiliate program.', 'US', 'active'),
    ('GLP-1 telehealth is not available in the United States.\nJoin our affiliate program.', '', 'active'),
    ('GLP-1 telehealth costs in the United States.\nJoin our affiliate program.', '', 'active'),
    ('We deliver GLP-1 medication in the United States.\nOur affiliate program is closed.\nJoin our affiliate program.', 'US', 'paused'),
    ('We deliver GLP-1 medication in the United States.\nAffiliate program information.', 'US', ''),
])
def test_direct_inventory_requires_affirmative_page_service_and_recruitment(discovery, text, region, status):
    candidate = {
        'provider': 'Example Clinic', 'official_url': 'https://example.invalid/',
        'program_url': 'https://example.invalid/affiliates', 'network': 'direct',
        'evidence_excerpt': text, 'glp_evidence': {'tier': 'explicit_glp1'},
        'discovered_by': 'fixture', 'evidence_source': 'program_page',
        'enrichment': {'status': 'ok'}, 'evidence_sha256': 'a' * 64,
        'commercial_page_evidence': discovery.commercial_page_evidence(text),
    }
    program = discovery.build_apply_inventory([candidate])['programs'][0]
    assert program['program_id'] == 'domain:example.invalid'
    assert program['region'] == region
    assert program['offer_status'] == status
    assert program['payout'] is None and program['terms'] is None
    candidate['evidence_source'] = 'search_result'
    snippet = discovery.build_apply_inventory([candidate])['programs'][0]
    assert snippet['region'] == '' and snippet['offer_status'] == ''


def test_incomplete_admission_propagates_to_cli_and_saved_summary(discovery, monkeypatch, tmp_path):
    import types
    summary = {'status': 'DISCOVERED', 'apply_inventory': str(tmp_path / 'direct-supply.json')}
    monkeypatch.setattr(discovery, 'run', lambda **kwargs: summary)
    monkeypatch.setattr(discovery, 'firecrawl_search', lambda: None)
    monkeypatch.setattr(discovery, 'firecrawl_scrape', lambda: None)
    monkeypatch.setattr(discovery, 'live_platform_discovery_adapters', lambda: {})
    feeder = types.SimpleNamespace(
        freshest_inventories=lambda **kwargs: [],
        admit_discovery=lambda *args, **kwargs: {'status': 'INCOMPLETE'})
    monkeypatch.setitem(sys.modules, 'affiliate_auto_apply', feeder)
    status = discovery.main(['--admit-applications', '--time-budget-seconds', '360',
                             '--output-dir', str(tmp_path), '--json'])
    assert status == 2
    receipt = json.loads((tmp_path / 'summary.json').read_text())
    assert receipt['status'] == 'ADMISSION_INCOMPLETE'
    assert receipt['discovery_status'] == 'DISCOVERED'


def test_cli_passes_direct_and_fresh_marketplace_inventories_to_admission(discovery, monkeypatch, tmp_path):
    import types
    direct = tmp_path / 'direct-supply.json'
    market = tmp_path / 'awin.json'
    monkeypatch.setattr(discovery, 'run', lambda **kwargs: {'status': 'DISCOVERED', 'apply_inventory': str(direct)})
    monkeypatch.setattr(discovery, 'firecrawl_search', lambda: None)
    monkeypatch.setattr(discovery, 'firecrawl_scrape', lambda: None)
    monkeypatch.setattr(discovery, 'live_platform_discovery_adapters', lambda: {})
    captured = []
    def freshest(**kwargs):
        assert kwargs == {'include_direct': False}
        return [market]
    def admit(paths, **kwargs):
        captured.append((paths, kwargs))
        return {'status': 'COMPLETE'}
    monkeypatch.setitem(sys.modules, 'affiliate_auto_apply', types.SimpleNamespace(
        freshest_inventories=freshest, admit_discovery=admit))
    assert discovery.main(['--admit-applications', '--output-dir', str(tmp_path), '--json']) == 0
    assert captured == [([direct, market], {'apply': True, 'deadline_seconds': 120})]


def test_admission_cannot_consume_all_scheduler_headroom(discovery):
    with pytest.raises(SystemExit) as refusal:
        discovery.main(['--admit-applications', '--time-budget-seconds', '480'])
    assert refusal.value.code == 2


def test_card_reports_queue_outcomes(tmp_path):
    module = load('affiliate_monday_discovery_card')
    (tmp_path / 'summary.json').write_text(json.dumps({
        'status': 'ADMISSION_INCOMPLETE', 'application_admission': {'status': 'INCOMPLETE',
        'outcomes': [{'status': 'admitted'}, {'status': 'deferred'}, {'status': 'reconcile_required'}]}}))
    card = module.compose(tmp_path, {})['card']
    assert '1 admitted' in card and '1 deferred' in card and '1 need reconciliation' in card
    assert 'Review only.' not in card
