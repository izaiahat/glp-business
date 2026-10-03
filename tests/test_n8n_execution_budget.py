import importlib.util
from pathlib import Path
from datetime import datetime, timezone

SOURCE = Path(__file__).resolve().parents[1] / 'ops/n8n_execution_budget.py'

def module():
    assert SOURCE.is_file(), 'monthly execution budget guard missing'
    spec = importlib.util.spec_from_file_location('n8n_execution_budget', SOURCE)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m

def test_projected_budget_alerts_at_70_percent_not_only_current_usage():
    m = module()
    now = datetime(2026, 10, 11, tzinfo=timezone.utc)
    executions = [{'id': str(i), 'workflowId': 'w1', 'startedAt': '2026-10-10T12:00:00Z'} for i in range(330)]
    r = m.summarize(executions, now)
    assert r['month_executions_observed'] == 330
    assert r['projected_month_end'] >= 7000
    assert r['status'] == 'WARN_70_PERCENT'
    assert r['cap'] == 10000


def test_collection_paginates_and_never_runs_a_workflow():
    m = module()
    calls = []
    def get(method, path):
        calls.append((method, path))
        if 'cursor=' not in path:
            return {'data': [{'id': '2', 'workflowId': 'w', 'startedAt': '2026-10-10T10:00:00Z'}], 'nextCursor': 'more'}
        return {'data': [{'id': '1', 'workflowId': 'w', 'startedAt': '2026-10-01T00:00:00Z'}], 'nextCursor': None}
    rows, proof = m.collect(get, datetime(2026, 10, 11, tzinfo=timezone.utc))
    assert len(rows) == 2
    assert proof['complete'] is True
    assert len(calls) == 2
    assert all(method == 'GET' and '/executions?' in path for method, path in calls)


def test_month_reset_and_retention_high_water():
    m = module()
    now = datetime(2026, 10, 20, tzinfo=timezone.utc)
    r = m.summarize([], now, previous_count=7001)
    assert r['month_execution_high_water'] == 7001
    assert r['status'] != 'PASS'
    assert m.summarize([], now)['month_execution_high_water'] == 0


def test_health_check_is_daily_persistent_and_failure_is_not_zero(tmp_path):
    m = module()
    now = datetime(2026, 10, 11, tzinfo=timezone.utc)
    calls = []
    def get(method, path):
        calls.append(path)
        return {'data': [], 'nextCursor': None}
    first = m.health_check(tmp_path / 'budget.json', now=now, get=get, audit=lambda get: {'status': 'PASS'})
    assert first['daily_line'].startswith('n8n execution budget:')
    assert first['snapshot']['projected_month_end'] == 0
    assert m.health_check(tmp_path / 'budget.json', now=now, get=get, audit=lambda get: {'status': 'PASS'})['daily_line'] is None
    assert len(calls) == 1
    def fail(method, path):
        raise RuntimeError('sensitive remote exception never published')
    failed = m.health_check(tmp_path / 'other.json', now=now, get=fail)
    assert failed['snapshot']['status'] == 'UNKNOWN'
    assert failed['snapshot']['month_executions_observed'] is None
    assert failed['issue']['classification'] == 'n8n_execution_budget_unknown'
    assert 'sensitive' not in str(failed)


def test_health_budget_alert_is_not_an_automatic_workflow_shutdown(tmp_path):
    m = module()
    now = datetime(2026, 10, 11, tzinfo=timezone.utc)
    rows = [{'id': str(i), 'workflowId': 'w', 'startedAt': '2026-10-10T12:00:00Z'} for i in range(330)]
    def get(method, path):
        assert method == 'GET' and path.startswith('/executions?')
        return {'data': rows, 'nextCursor': None}
    r = m.health_check(tmp_path / 'budget.json', now=now, get=get, audit=lambda get: {'status': 'PASS'})
    assert r['issue']['classification'] == 'n8n_execution_budget_70_percent'
    assert r['issue']['severity'] == 'yellow'


def test_budget_connected_to_existing_health_incident_path():
    import ast
    path = SOURCE.parent / 'hermes-cron-scripts/scheduled_job_health_manager.py'
    source = path.read_text()
    tree = ast.parse(source)
    run = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'run')
    calls = [n for n in ast.walk(run) if isinstance(n, ast.Call)]
    assert any(isinstance(n.func, ast.Attribute) and n.func.attr == 'health_check' for n in calls), 'budget not in actual health run'
    assert 'n8n-execution-budget' in source
    assert 'last_n8n_execution_budget' in source


def test_published_invariant_audit_leaves_events_untouched():
    m = module()
    def get(method, path):
        assert method == 'GET'
        wid = path.split('/')[-1]
        if wid == m.ARTICLE:
            return {'id': wid, 'active': False}
        return {'id': wid, 'active': True, 'activeVersionId': 'v',
                'settings': {}, 'activeVersion': {'versionId': 'v', 'nodes': [
                    {'name': 'poll', 'type': 'n8n-nodes-base.scheduleTrigger', 'disabled': wid != m.TOP_FIVE[0]},
                    {'name': 'event', 'type': 'n8n-nodes-base.webhook'}]}}
    r = m.audit_invariants(get)
    assert r['status'] == 'DRIFT'
    assert r['enabled_schedule_nodes'] == [{'workflow_id': m.TOP_FIVE[0], 'node': 'poll'}]
    assert r['article_inactive'] is True


def test_count_carries_distinct_ids_across_deleted_history_and_month_rollover(tmp_path):
    m = module()
    p = tmp_path / 'budget.json'
    from datetime import timedelta
    now = datetime(2026, 10, 11, tzinfo=timezone.utc)
    def get_for(identity):
        return lambda method, path: {'data': [{'id': identity, 'workflowId': 'w', 'startedAt': '2026-10-10T12:00:00Z'}], 'nextCursor': None}
    audit = lambda get: {'status': 'PASS'}
    assert m.health_check(p, now=now, get=get_for('1'), audit=audit)['snapshot']['month_execution_high_water'] == 1
    assert m.health_check(p, now=now + timedelta(hours=2), get=get_for('2'), audit=audit)['snapshot']['month_execution_high_water'] == 2
    reset = m.health_check(p, now=datetime(2026, 11, 1, tzinfo=timezone.utc), get=lambda *args: {'data': [], 'nextCursor': None}, audit=audit)
    assert reset['snapshot']['month_execution_high_water'] == 0


def test_pagination_refusals_are_not_success():
    import pytest
    m = module()
    now = datetime(2026, 10, 11, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match='incomplete'):
        m.collect(lambda *args: {'data': [{'id':'1', 'startedAt':'2026-10-10T12:00:00Z'}], 'nextCursor': 'again'}, now, max_pages=1)
    with pytest.raises(ValueError, match='invalid'):
        m.collect(lambda *args: {'data': None}, now)


def test_corrupt_state_is_unknown_and_not_silently_reset(tmp_path):
    m = module()
    path = tmp_path / 'budget.json'
    path.write_text('corrupt')
    r = m.health_check(path, get=lambda *a: {'data': []})
    assert r['snapshot']['status'] == 'UNKNOWN'
    assert path.read_text() == 'corrupt'

def test_glp338_daily_digest_shows_actual_forecast_and_headroom(tmp_path):
    m = module()
    now = datetime(2026, 10, 11, tzinfo=timezone.utc)
    rows = [{'id': str(i), 'workflowId': 'w', 'startedAt': '2026-10-10T12:00:00Z'} for i in range(100)]
    get = lambda method, path: {'data': rows, 'nextCursor': None}
    r = m.health_check(tmp_path / 'budget.json', now=now, get=get, audit=lambda get: {'status': 'PASS'})
    s = r['snapshot']
    assert s['headroom_to_cap'] == m.CAP - max(s['month_execution_high_water'], s['projected_month_end'])
    line = r['daily_line']
    assert '100/10000 actual month-to-date' in line
    assert f"forecast month-end {s['projected_month_end']}/10000" in line
    assert f"headroom {s['headroom_to_cap']} under 10000" in line
    # Headroom never reads healthier than the forecast: over-cap forecasts go negative.
    hot = m.summarize([{'id': str(i), 'workflowId': 'w', 'startedAt': '2026-10-10T12:00:00Z'} for i in range(600)], now)
    assert hot['status'] == 'CAP_RISK' and hot['headroom_to_cap'] < 0


def test_glp338_digest_tracks_batched_polling_set_and_heaviest_workflows():
    m = module()
    now = datetime(2026, 10, 11, tzinfo=timezone.utc)
    rows = [{'id': f'{wid}-{i}', 'workflowId': wid, 'startedAt': '2026-10-10T12:00:00Z'}
            for n, wid in ((3, m.TOP_FIVE[0]), (2, m.TOP_FIVE[4]), (7, 'postback'), (1, m.ARTICLE))
            for i in range(n)]
    s = m.summarize(rows, now)
    assert s['top_five_polling_executions'] == 5
    assert s['heaviest_workflows'][0] == {'workflow_id': 'postback', 'executions': 7}
    line = m.daily_line(s, '2026-10')
    assert 'batched polling set 5;' in line and f'heaviest postback=7,{m.TOP_FIVE[0]}=3' in line
    assert 'heaviest none;' in m.daily_line(m.summarize([], now), '2026-10')


def test_glp338_same_day_upgrade_emits_once_without_resetting_daily_marker(tmp_path):
    import json
    m = module()
    now = datetime(2026, 10, 3, 12, tzinfo=timezone.utc)
    path = tmp_path / 'budget.json'
    reporting_path = path.with_name(path.name + '.health.json')
    snapshot = m.summarize([], now)
    snapshot['invariants'] = {'status': 'PASS', 'article_inactive': True}
    path.write_text(json.dumps({'snapshot': snapshot, 'month_utc': '2026-10',
                               'month_high_water': 0, 'daily_line_day': '2026-10-03'}))
    reporting_path.write_text(json.dumps({'daily_line_day': '2026-10-03'}))
    before = (path.read_bytes(), reporting_path.read_bytes())
    def no_refresh(*args):
        raise AssertionError('cached observation must remain cached')
    preview = m.health_check(path, now=now, get=no_refresh, dry_run=True)
    assert preview['daily_line'] is not None, 'same-day old marker suppressed upgraded digest'
    assert '0/10000 actual month-to-date' in preview['daily_line']
    assert 'forecast month-end 0/10000; headroom 10000 under 10000' in preview['daily_line']
    assert (path.read_bytes(), reporting_path.read_bytes()) == before
    emitted = m.health_check(path, now=now, get=no_refresh)
    assert emitted['daily_line'] == preview['daily_line']
    assert json.loads(path.read_text())['daily_line_day'] == '2026-10-03'
    assert json.loads(reporting_path.read_text())['daily_line_day'] == '2026-10-03'
    assert m.health_check(path, now=now, get=no_refresh)['daily_line'] is None


def test_glp338_regression_snapshot_cached_before_upgrade_still_reports_headroom():
    m = module()
    old = {'status': 'PASS', 'month_execution_high_water': 1200, 'projected_month_end': 4000}
    line = m.daily_line(old, '2026-10')
    assert 'headroom 6000 under 10000' in line and 'heaviest UNKNOWN' in line
    unknown = m.daily_line({'status': 'UNKNOWN', 'month_execution_high_water': None, 'projected_month_end': None}, '2026-10')
    assert 'UNKNOWN/10000 actual' in unknown and 'headroom UNKNOWN' in unknown
