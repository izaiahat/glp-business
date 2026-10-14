"""GLP-44 weekly keyword/strategy collection: $25/week cap and non-null gap plan."""
import datetime as dt
import importlib.util
import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ops.glp import seo_weekly_budget as budget_mod  # noqa: E402
from ops.glp.seo_weekly_budget import BudgetRefused, WeeklyBudget, week_start  # noqa: E402
from ops.glp.weekly_keyword_plan import plan_keyword_gaps, update_plan  # noqa: E402

NOW = dt.datetime(2026, 10, 12, 3, 0, tzinfo=dt.timezone.utc)  # Sunday Oct 11 PT
LABS = '/dataforseo_labs/google/ranked_keywords/live'
VOLUME = '/keywords_data/google_ads/search_volume/live'


def make(tmp_path, alerts, journal=None, cap='25'):
    return WeeklyBudget(ledger_root=tmp_path / 'ledger', rail_journal=journal, cap_usd=cap,
                        alert=lambda m: alerts.append(m) or {'sent': True}, clock=lambda: NOW)


def labs(limit=1000):
    return [{'target': 'example.com', 'limit': limit, 'offset': 0, 'include_clickstream_data': False}]


def test_sunday_review_counts_toward_following_supply_week():
    assert week_start(NOW) == dt.date(2026, 10, 12)
    assert week_start(dt.datetime(2026, 10, 14, 18, tzinfo=dt.timezone.utc)) == dt.date(2026, 10, 12)


def test_reserve_settle_and_receipt_within_cap(tmp_path):
    alerts = []
    b = make(tmp_path, alerts)
    r = b.reserve('dataforseo', LABS, labs(), run_id='t')
    assert Decimal(r['estimate_usd']) == Decimal('0.132')
    b.settle(r, 0.0612)
    receipt = b.receipt()
    assert receipt['committed_usd'] == '0.0612' and receipt['within_cap'] is True
    assert receipt['actual_by_source_usd'] == {'dataforseo': '0.0612'}
    assert alerts == []


def test_over_cap_refuses_before_send_and_alerts_once(tmp_path):
    alerts = []
    b = make(tmp_path, alerts, cap='0.2')
    r = b.reserve('dataforseo', LABS, labs(), run_id='t')
    b.settle(r, 0.132)
    sent = []
    for _ in range(2):
        with pytest.raises(BudgetRefused, match='weekly_cap_exceeded'):
            b.guarded('dataforseo', LABS, labs(), lambda: sent.append(1), run_id='t', cost_of=lambda d: 0)
    assert sent == [] and len(alerts) == 1 and 'refused' in alerts[0]
    assert len(b.receipt()['refusals']) == 2


def test_unknown_cost_refuses_and_alerts(tmp_path):
    alerts = []
    b = make(tmp_path, alerts)
    with pytest.raises(BudgetRefused, match='unknown_cost'):
        b.reserve('dataforseo', '/backlinks/unpriced/live', [{'target': 'x'}], run_id='t')
    with pytest.raises(BudgetRefused, match='unknown_cost'):
        b.reserve('dataforseo', LABS, [{'target': 'x'}], run_id='t')  # no limit => unknown
    assert len(alerts) == 1


def test_unmeasured_actual_cost_blocks_further_metered_calls(tmp_path):
    alerts = []
    b = make(tmp_path, alerts)
    r = b.reserve('dataforseo', LABS, labs(), run_id='t')
    b.settle(r, None)  # ambiguous transport => unknown, not zero
    with pytest.raises(BudgetRefused, match='unknown_prior_spend_this_week'):
        b.reserve('dataforseo', VOLUME, [{'keywords': ['a']}], run_id='t')
    assert b.receipt()['within_cap'] is False
    # Zero-marginal rails remain usable (Cogny flat plan, GSC/GA4 free).
    assert b.reserve('cogny', 'get_search_analytics', {}, run_id='t')['estimate_usd'] == '0'


def test_shared_rail_journal_spend_counts_toward_cap(tmp_path):
    journal = tmp_path / 'rail.jsonl'
    at = NOW.timestamp()
    journal.write_text('\n'.join(json.dumps(r) for r in [
        {'at': at, 'event': 'ATTEMPT', 'attempt_id': 'a'},
        {'at': at, 'event': 'RESULT', 'attempt_id': 'a', 'cost': 24.95}]) + '\n')
    alerts = []
    b = make(tmp_path, alerts, journal=journal)
    with pytest.raises(BudgetRefused, match='weekly_cap_exceeded'):
        b.reserve('dataforseo', VOLUME, [{'keywords': ['a']}], run_id='t')
    journal.write_text(json.dumps({'at': at, 'event': 'ATTEMPT', 'attempt_id': 'z'}) + '\n')
    with pytest.raises(BudgetRefused, match='unknown_prior_spend'):
        make(tmp_path / 'b', alerts, journal=journal).reserve('dataforseo', VOLUME, [{'keywords': ['a']}], run_id='t')


def test_exact_append_only_rail_reconciliation(tmp_path, monkeypatch):
    """Real sticky-UNKNOWN regression and refusal controls; all evidence synthetic."""
    import hashlib
    from copy import deepcopy

    endpoint = '/serp/google/organic/live/advanced'
    payload = [{'keyword': 'offline fixture', 'depth': 10}]
    request_hash = hashlib.sha256(json.dumps([endpoint, payload], sort_keys=True,
                                             allow_nan=False).encode()).hexdigest()
    from ops.glp import seo_data_rails as rails
    task_id = '10121300-0000-0000-0000-000000000001'
    base = {'at': NOW.timestamp(), 'account': 'tryboringai', 'run_id': 'fixture-run',
            'attempt_id': 'fixture-attempt', 'request_hash': request_hash}
    attempt = dict(base, event='ATTEMPT', endpoint=endpoint, cost=None)
    ambiguous = dict(base, at=base['at'] + 2, event='AMBIGUOUS', cost=None,
                     provider_task_ids=[task_id])
    # Native account/authenticated transport is mocked ONLY in this offline test.
    # Local artifacts by themselves must never satisfy provider provenance.
    def offline_init(self, **kwargs):
        self._accounts = {'tryboringai': {'auth': 'OFFLINE-TRANSPORT-SENTINEL'}}
    monkeypatch.setattr(rails.DataForSEO, '__init__', offline_init)
    provider_record = {'id': task_id, 'url': 'v3' + endpoint, 'status': '20000',
                       'datetime_posted': NOW.strftime('%Y-%m-%d %H:%M:%S +00:00'),
                       'datetime_done': (NOW + dt.timedelta(seconds=1)).strftime('%Y-%m-%d %H:%M:%S +00:00'),
                       'cost': '0.002', 'metadata': dict(payload[0], api='serp', function='live',
                                                        se='google', se_type='organic')}
    provider_rows = [provider_record]
    provider_reads = []
    def offline_http(url, **kwargs):
        provider_reads.append((url, kwargs['body']))
        assert url == rails.DataForSEO.BASE + '/serp/id_list'
        assert kwargs['method'] == 'POST' and kwargs['retries'] == 1
        assert kwargs['body'][0]['include_metadata'] is True
        return {'status_code': 20000, 'cost': 0, 'tasks': [
            {'status_code': 20000, 'cost': 0, 'path': ['v3', 'serp', 'id_list'],
             'data': kwargs['body'][0], 'result': deepcopy(provider_rows)}]}
    monkeypatch.setattr(rails, '_http', offline_http)
    artifact = tmp_path / 'provider-response.json'
    evidence = {**{k: base[k] for k in ('account', 'run_id', 'attempt_id', 'request_hash')},
                'endpoint': endpoint, 'payload': payload,
                'response': {'status_code': 20000, 'cost': '0.002',
                             'tasks': [{'id': task_id, 'status_code': 20000,
                                        'cost': '0.002', 'data': deepcopy(provider_record['metadata'])}]}}
    artifact.write_text(json.dumps(evidence))
    proof = {'kind': 'dataforseo_response', 'path': str(artifact),
             'sha256': hashlib.sha256(artifact.read_bytes()).hexdigest()}
    result = dict(base, at=base['at'] + 8 * 86400, event='RESULT', state='MEASURED',
                  cost='0.002', provider_evidence=proof)
    journal = tmp_path / 'rail.jsonl'

    def check(rows, expected=('0.002', False), week=dt.date(2026, 10, 12)):
        journal.write_text('\n'.join(json.dumps(r) for r in rows) + '\n')
        before = journal.read_bytes()
        b = WeeklyBudget(ledger_root=tmp_path / 'budget', rail_journal=journal, alert=None,
                         clock=lambda: NOW)
        cost, unknown = b._rail_spend(week)
        assert (str(cost), unknown) == expected
        assert journal.read_bytes() == before  # reducer never rewrites history
        return b

    check([attempt, ambiguous, result, result])  # charge once, in original week
    check([attempt, ambiguous, result], ('0', False), dt.date(2026, 10, 19))
    # Ordinary maintained rail RESULT still needs no recovery artifact.
    ordinary = dict(base, event='RESULT', state='MEASURED', cost='0.002')
    check([attempt, ordinary, ordinary])
    check([attempt, ambiguous], ('0', True))
    check([attempt, ambiguous, ordinary], ('0', True))  # bare label is not recovery proof
    # A different raw hash without a competing payload cannot exclude ownership.
    check([attempt, ambiguous, result, dict(base, event='ATTEMPT', attempt_id='other', request_hash='other')],
          ('0', True))
    check([attempt, ambiguous, result, dict(base, event='ATTEMPT', attempt_id='overlapping')], ('0', True))
    check([result], ('0', True), dt.date(2026, 10, 19))  # orphan result
    for field in ('account', 'run_id', 'attempt_id', 'request_hash'):
        bad = dict(result, **{field: 'different'})
        check([attempt, ambiguous, bad], ('0', True))
    for cost in (None, True, -1, 'NaN', 'Infinity', 'estimate'):
        check([attempt, ambiguous, dict(result, cost=cost)], ('0', True))
    for state in ('UNKNOWN', 'BALANCE', 'ESTIMATED', 'EXHAUSTED', 'PROVIDER_ERROR'):
        check([attempt, ambiguous, dict(result, state=state)], ('0', True))
    for bad_proof in ({}, dict(proof, kind='balance'), dict(proof, path=str(tmp_path / 'missing')),
                      dict(proof, sha256='0' * 64)):
        check([attempt, ambiguous, dict(result, provider_evidence=bad_proof)], ('0', True))
    # Authenticated bytes with mismatched or incomplete contents still refuse.
    for field, value in [('account', 'other'), ('run_id', 'other'), ('attempt_id', 'other'),
                         ('request_hash', 'other'), ('endpoint', '/other'), ('payload', []),
                         ('response', {'cost': 0, 'tasks': []})]:
        bad_evidence = deepcopy(evidence)
        bad_evidence[field] = value
        artifact.write_text(json.dumps(bad_evidence))
        bad_proof = dict(proof, sha256=hashlib.sha256(artifact.read_bytes()).hexdigest())
        check([attempt, ambiguous, dict(result, provider_evidence=bad_proof)], ('0', True))
    for change in ('missing_task_id', 'duplicate_task', 'task_cost', 'envelope_cost', 'task_status'):
        bad_evidence = deepcopy(evidence)
        response = bad_evidence['response']
        if change == 'missing_task_id':
            response['tasks'][0].pop('id')
        elif change == 'duplicate_task':
            response['tasks'].append(deepcopy(response['tasks'][0]))
        elif change == 'task_cost':
            response['tasks'][0]['cost'] = '0.003'
        elif change == 'envelope_cost':
            response['cost'] = None
        else:
            response['tasks'][0]['status_code'] = 40200
        artifact.write_text(json.dumps(bad_evidence))
        bad_proof = dict(proof, sha256=hashlib.sha256(artifact.read_bytes()).hexdigest())
        check([attempt, ambiguous, dict(result, provider_evidence=bad_proof)], ('0', True))
    # Five independently reported failures, now also exercised with native identity.
    artifact.write_text(json.dumps(evidence))
    reads_before = len(provider_reads)
    check([attempt, dict(ambiguous, provider_task_ids=[]), result], ('0', True))
    assert len(provider_reads) == reads_before
    local_approval = dict(result, provider_evidence=dict(proof, approved=True))
    check([attempt, dict(ambiguous, provider_task_ids=[]), local_approval], ('0', True))
    assert len(provider_reads) == reads_before
    monkeypatch.setattr(rails, '_http', lambda *a, **k: (_ for _ in ()).throw(OSError('offline unavailable')))
    check([attempt, ambiguous, result], ('0', True))
    monkeypatch.setattr(rails, '_http', offline_http)
    wrong_query = deepcopy(evidence)
    wrong_query['response']['tasks'][0]['data']['keyword'] = 'unrelated'
    artifact.write_text(json.dumps(wrong_query))
    wrong_proof = dict(proof, sha256=hashlib.sha256(artifact.read_bytes()).hexdigest())
    check([attempt, ambiguous, dict(result, provider_evidence=wrong_proof)], ('0', True))
    artifact.write_text(json.dumps(evidence))
    conflicting = deepcopy(evidence)
    conflicting['response']['tasks'][0]['id'] = '10121300-0000-0000-0000-000000000002'
    second_artifact = tmp_path / 'conflicting.json'
    second_artifact.write_text(json.dumps(conflicting))
    second = dict(result, provider_evidence=dict(proof, path=str(second_artifact),
        sha256=hashlib.sha256(second_artifact.read_bytes()).hexdigest()))
    check([attempt, ambiguous, result, second], ('0', True))
    check([attempt, ambiguous, dict(result, endpoint='/UNRELATED/ENDPOINT')], ('0', True))
    check([attempt, ambiguous, dict(result, at=base['at'] + 1)], ('0', True))
    # Native account read, not the local response, must independently establish
    # exact cost, ID, request/endpoint and provider chronology. Missing != zero.
    for field, value in [('id', 'other'), ('url', 'v3/other'), ('cost', '0.003'),
                         ('status', '40400'), ('metadata', {'keyword': 'unrelated'}),
                         ('metadata', dict(provider_record['metadata'], target='UNRELATED')),
                         ('datetime_posted', '2026-10-01 03:00:00 +00:00'),
                         ('datetime_done', '2026-10-20 03:00:00 +00:00')]:
        provider_rows[0] = dict(provider_record, **{field: value})
        check([attempt, ambiguous, result], ('0', True))
    provider_rows.clear()
    check([attempt, ambiguous, result], ('0', True))
    provider_rows[:] = [provider_record, deepcopy(provider_record)]
    check([attempt, ambiguous, result], ('0', True))
    provider_rows[:] = [provider_record]
    artifact.write_text(json.dumps(evidence))
    zero_evidence = deepcopy(evidence)
    zero_evidence['response']['cost'] = zero_evidence['response']['tasks'][0]['cost'] = '0'
    artifact.write_text(json.dumps(zero_evidence))
    zero_proof = dict(proof, sha256=hashlib.sha256(artifact.read_bytes()).hexdigest())
    # Self-written zero still fails until authenticated task cost is also zero.
    check([attempt, ambiguous, dict(result, cost='0', provider_evidence=zero_proof)], ('0', True))
    provider_record['cost'] = '0'
    check([attempt, ambiguous, dict(result, cost='0', provider_evidence=zero_proof)], ('0', False))
    provider_record['cost'] = '0.002'
    artifact.write_text(json.dumps(evidence))
    check([attempt, ambiguous, result, dict(result, cost='0.003')], ('0', True))
    check([attempt, ambiguous, dict(result, at=base['at'] - 1)], ('0', True))
    check([result, attempt, ambiguous, result], ('0', True))
    check([attempt, ambiguous, result, ambiguous], ('0', True))  # later uncertainty wins
    check([attempt, dict(attempt, endpoint='/other'), ambiguous, result], ('0', True))
    b = check([attempt, ambiguous, dict(result, provider_evidence={})], ('0', True))
    sent = []
    with pytest.raises(BudgetRefused, match='unknown_prior_spend_this_week'):
        b.guarded('dataforseo', VOLUME, [{'keywords': ['a']}], lambda: sent.append(1),
                  run_id='fixture-run', cost_of=lambda _: 0)
    assert sent == []


@pytest.fixture
def competing_recovery(tmp_path, monkeypatch):
    """Synthetic native transport only; never a live billing receipt."""
    import hashlib
    from ops.glp import seo_data_rails as rails

    endpoint = '/serp/google/organic/live/advanced'
    payload = [{'keyword': 'offline fixture', 'depth': 10}]
    digest = hashlib.sha256(json.dumps([endpoint, payload], sort_keys=True).encode()).hexdigest()
    attempt = dict(event='ATTEMPT', at=NOW.timestamp() + .8, endpoint=endpoint,
                   account='tryboringai', run_id='offline-run', attempt_id='offline-attempt',
                   request_hash=digest)
    ambiguous = dict(attempt, event='AMBIGUOUS', at=NOW.timestamp() + 2)
    task_id = '10121300-0000-0000-0000-000000000001'
    echo = dict(payload[0], api='serp', function='live', se='google', se_type='organic')
    record = dict(id=task_id, url='v3' + endpoint, status='20000', cost='0.002',
                  metadata=dict(echo, os='windows'), datetime_posted=NOW.strftime('%Y-%m-%d %H:%M:%S %z'),
                  datetime_done=NOW.strftime('%Y-%m-%d %H:%M:%S %z'))
    def init(self, **kwargs):
        self._accounts = {'tryboringai': {'auth': 'OFFLINE-ONLY'}}
    def http(url, **kwargs):
        assert url == rails.DataForSEO.BASE + '/serp/id_list'
        assert kwargs['headers'] == {'Authorization': 'Basic OFFLINE-ONLY'}
        return dict(status_code=20000, cost=0, tasks=[dict(status_code=20000, cost=0,
                    path=['v3', 'serp', 'id_list'], data=kwargs['body'][0], result=[record])])
    monkeypatch.setattr(rails.DataForSEO, '__init__', init)
    monkeypatch.setattr(rails, '_http', http)
    artifact = tmp_path / 'offline-response.json'
    artifact.write_text(json.dumps(dict(attempt, payload=payload, response=dict(status_code=20000,
        cost='0.002', tasks=[dict(id=task_id, status_code=20000, cost='0.002', data=echo)]))))
    result = dict(attempt, event='RESULT', at=NOW.timestamp() + 3, state='MEASURED', cost='0.002',
                  provider_evidence=dict(kind='dataforseo_response', path=str(artifact),
                      sha256=hashlib.sha256(artifact.read_bytes()).hexdigest()))
    journal = tmp_path / 'offline-rail.jsonl'
    def reduce(competitor):
        rows = [*competitor, attempt, ambiguous, result]
        journal.write_text(''.join(json.dumps(r) + '\n' for r in rows))
        return make(tmp_path, [], journal).receipt()
    positive = reduce([])
    assert positive['rail_journal_usd'] == '0.002' and positive['within_cap'] is True
    return attempt, payload, reduce


def test_same_second_disjoint_native_attempts_refuse_recovery(competing_recovery):
    attempt, payload, reduce = competing_recovery
    other = dict(attempt, at=NOW.timestamp() + .1, attempt_id='offline-other')
    terminal = dict(other, at=NOW.timestamp() + .4, event='RESULT', cost='0.002', state='MEASURED')
    receipt = reduce([other, terminal])
    assert receipt['rail_journal_usd'] == '0.002'  # ordinary measured request only
    assert receipt['rail_journal_unknown'] is True and receipt['within_cap'] is False


def test_default_equivalent_native_requests_refuse_recovery(competing_recovery):
    import hashlib
    attempt, payload, reduce = competing_recovery
    explicit_default = [dict(payload[0], os='windows')]
    digest = hashlib.sha256(json.dumps([attempt['endpoint'], explicit_default], sort_keys=True).encode()).hexdigest()
    assert digest != attempt['request_hash']
    other = dict(attempt, attempt_id='offline-other', request_hash=digest)
    terminal = dict(other, at=NOW.timestamp() + 1, event='RESULT', cost='0.002', state='MEASURED')
    receipt = reduce([other, terminal])
    assert receipt['rail_journal_usd'] == '0.002'
    assert receipt['rail_journal_unknown'] is True and receipt['within_cap'] is False


def test_cap_cannot_exceed_operator_authority(tmp_path):
    with pytest.raises(ValueError, match='weekly_cap_outside_operator_authority'):
        WeeklyBudget(ledger_root=tmp_path, rail_journal=None, cap_usd='25.01', alert=None)


def load_collector():
    spec = importlib.util.spec_from_file_location('k44_collect', ROOT / 'scripts/glp_weekly_seo_collect.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_recorded_dataforseo_reserves_before_any_http(tmp_path, monkeypatch):
    collect = load_collector()
    store = collect.Store(tmp_path / 'store')
    alerts = []
    b = make(tmp_path, alerts, cap='0.05')
    posted = []
    monkeypatch.setattr(collect.DataForSEO, '_post', lambda self, *a, **k: posted.append(a) or {'cost': 0})
    dfs = collect.RecordedDFS(store, 'run-1', b)
    with pytest.raises(BudgetRefused):
        dfs._post(LABS, labs())
    assert posted == [] and not (store.path / 'raw').exists()
    dfs_ok = collect.RecordedDFS(store, 'run-2', make(tmp_path / 'ok', []))
    assert dfs_ok._post(VOLUME, [{'keywords': ['a']}]) == {'cost': 0}
    with pytest.raises(ValueError, match='weekly_budget_required'):
        collect.RecordedDFS(store, 'run-3')._post(VOLUME, [{'keywords': ['a']}])


def supply_row(keyword, route, cls='B', volume=900, state='READY_FOR_RESEARCH'):
    return {'keyword': keyword, 'target_route': route, 'opportunity_class': cls, 'queue_state': state,
            'source_opportunity_id': cls + '-1', 'strategy_binding': {'cluster_id': 'c', 'depth_stage': 'pillar'},
            'ws_s_quickwin': {'competitor': {'search_volume': volume, 'position': 7, 'url': 'https://c/x'},
                              'our_footprint': None, 'intent': 'commercial_review',
                              'gsc_query_observed_28d': None}}


def test_gap_plan_qualifies_current_demand_and_preserves_holds():
    supply = {'gaps': [
        supply_row('acme glp1 reviews', '/guides/acme-glp1-reviews/'),
        supply_row('fella health reviews', '/guides/fella-reviews/'),
        supply_row('held rx reviews', '/reviews/held-rx/'),
        supply_row('owned reviews', '/reviews/owned/'),
        supply_row('nodemand reviews', '/guides/nodemand/', volume=None),
        supply_row('volume only', '/guides/volume-only/', cls='C', volume=None),
    ]}
    volume = {'ok': True, 'rows': {'volume only': {'ok': True, 'volume': 320}, 'nodemand reviews': {'ok': True, 'volume': None}}}
    gaps, excluded = plan_keyword_gaps(supply, volume, held_routes=['/reviews/held-rx/'],
                                       live_routes=['/reviews/owned/', '/reviews/', '/reviews/acme/'])
    assert [g['keyword'] for g in gaps] == ['acme glp1 reviews', 'volume only']
    assert gaps[0]['priority'] == 1 and gaps[0]['action'] == 'new_article'
    assert gaps[0]['internal_link_targets'] == ['/reviews/acme/', '/reviews/']
    assert gaps[1]['demand_basis'] == ['dataforseo_google_ads_search_volume_current_week']
    reasons = {e['keyword']: e['reason'] for e in excluded}
    assert reasons == {'fella health reviews': 'fella_excluded', 'held rx reviews': 'provider_hold_preserved',
                       'owned reviews': 'existing_live_owner_refresh_not_new_article',
                       'nodemand reviews': 'unknown_or_no_current_positive_demand_not_zero'}
    assert all(g['publication_authorized'] is False for g in gaps)


def load_sync():
    spec = importlib.util.spec_from_file_location('k44_sync', ROOT / 'ops/glp/weekly_strategy_sync.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TOPICS = {'week_start': '2026-10-12', 'recommendations': [
    {'route': '/reviews/x/', 'query': 'x reviews', 'priority': 1, 'impressions': 120}]}


def test_failed_sources_keep_keyword_gaps_null_not_empty():
    m = load_sync()
    modules = {'modules': {'keyword_gap': {'status': 'UNKNOWN_INPUT', 'required_artifact': 'footprint failed'}}}
    doc = m.build_updates(TOPICS, modules, {}, source_observations={}, gap_volume={'ok': True, 'rows': {}})
    assert doc['keyword_gaps'] is None and doc['keyword_gap_status'] == 'UNKNOWN_INPUT'
    assert doc['update_plan'][0]['route'] == '/reviews/x/' and doc['update_plan'][0]['no_op_allowed'] is True


def test_passing_module_yields_non_null_gaps_consumed_by_refill_governance():
    m = load_sync()
    output = {'gaps': [supply_row('acme glp1 reviews', '/guides/acme-glp1-reviews/')], 'ws_s_extension': {'new_observations': []}}
    modules = {'modules': {'keyword_gap': {'status': 'PASS_ANALYSIS_ONLY', 'output': output}}}
    doc = m.build_updates(TOPICS, modules, {}, source_observations={}, gap_volume={'ok': False, 'rows': {}})
    assert doc['keyword_gaps'] and doc['keyword_gaps'][0]['keyword'] == 'acme glp1 reviews'
    sys.path.insert(0, str(ROOT / 'systems/seo-blog-machine/scripts'))
    from seo_weekly_governance import new_candidates
    rows = [{'target_query': 'acme glp1 reviews', 'target_route': '/guides/acme-glp1-reviews/'},
            {'target_query': 'other', 'target_route': '/guides/other/'}]
    assert new_candidates(rows, doc) == rows[:1]
    assert update_plan(doc['refresh_list'])[0]['materiality_required'] is True


def test_strategy_report_enables_budgeted_metered_collection():
    text = (ROOT / 'scripts/glp_weekly_strategy_report.py').read_text()
    assert 'allow_metered=True,budget=budget' in text and 'allow_metered=False' not in text
    assert budget_mod.WEEKLY_CAP_USD == Decimal('25')
