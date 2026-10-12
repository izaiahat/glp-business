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
