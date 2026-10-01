"""Offline reproductions through the real job-health runner; all effects denied."""
import importlib.util
import json
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, path, monkeypatch):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, mod)
    spec.loader.exec_module(mod)
    return mod


def runner(tmp_path, monkeypatch, get, audit=None):
    monkeypatch.setattr(sys, 'path', sys.path.copy())
    budget = load('n8n_execution_budget', ROOT / 'ops/n8n_execution_budget.py', monkeypatch)
    fmt = types.ModuleType('discord_fmt')
    for name in ('title', 'kv', 'kvline', 'section', 'meaning', 'subtext', 'render'):
        setattr(fmt, name, lambda *a, **kw: '')
    monkeypatch.setitem(sys.modules, 'discord_fmt', fmt)
    for name in ('pushover_client', 'red_alert_hook', 'monitor_heartbeats', 'scheduled_source_drift'):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    manager = load('budget_review_manager', ROOT / 'ops/hermes-cron-scripts/scheduled_job_health_manager.py', monkeypatch)
    state_path = tmp_path / 'incidents.json'
    budget_path = tmp_path / 'budget.json'
    clock = [datetime(2026, 10, 11, tzinfo=timezone.utc)]
    reports, reds = [], []
    original = budget.health_check
    monkeypatch.setattr(budget, 'health_check', lambda **kw: original(budget_path, get=get, audit=audit, **kw))
    monkeypatch.setattr(manager, 'utc_now', lambda: clock[0])
    monkeypatch.setattr(manager, 'STATE_PATH', state_path)
    monkeypatch.setattr(manager, 'load_yaml', lambda *a: {})
    monkeypatch.setattr(manager, 'build_registry_map', lambda: {})
    monkeypatch.setattr(manager, 'job_list', lambda: [])
    monkeypatch.setattr(manager, 'collect_daily_source_drift', lambda *a: None, raising=False)
    monkeypatch.setattr(manager.monitor_heartbeats, 'audit', lambda **kw: {'status': 'PASS', 'issues': []}, raising=False)
    monkeypatch.setattr(manager, 'append_ledger', lambda *a: None)
    monkeypatch.setattr(manager, 'write_full_report', lambda issues, **kw: reports.append(issues) or tmp_path / 'report.md')
    monkeypatch.setattr(manager, 'render_report', lambda issues, **kw: ','.join(i.classification for i in issues))
    monkeypatch.setattr(manager, 'raise_red_alert_for_issue', lambda issue, **kw: reds.append(issue) or {})
    def run():
        assert manager.run(dry_run=False, force_report=False, verbose=False) == 0
        return json.loads(state_path.read_text())
    return budget, manager, clock, budget_path, reports, reds, run


def rows(count, prefix='a'):
    return [{'id': f'{prefix}{i}', 'workflowId': 'w', 'startedAt': '2026-10-10T12:00:00Z'} for i in range(count)]


def test_f1_threshold_alerts_once_per_crossing_and_unknown_does_not_rearm(tmp_path, monkeypatch):
    current = [rows(330)]
    failing = [False]
    def get(method, path):
        assert method == 'GET' and path.startswith('/executions?')
        if failing[0]:
            raise RuntimeError('outage')
        return {'data': current[0], 'nextCursor': None}
    b, m, clock, path, reports, reds, run = runner(tmp_path, monkeypatch, get, lambda get: {'status': 'PASS'})
    counts = []
    for hour, count in ((0, 330), (1, 340), (2, 350), (7, 350)):
        clock[0] = datetime(2026, 10, 11, tzinfo=timezone.utc) + timedelta(hours=hour)
        current[0] = rows(count)
        counts.append(run()['last_alert_count'])
    assert counts == [1, 0, 0, 0]
    clock[0] += timedelta(hours=1)
    failing[0] = True
    assert run()['last_n8n_execution_budget']['status'] == 'UNKNOWN'
    clock[0] += timedelta(hours=1)
    failing[0] = False
    assert run()['last_alert_count'] == 0  # still above, not a new crossing
    clock[0] += timedelta(hours=1)
    current[0] = []
    assert run()['last_n8n_execution_budget']['status'] == 'PASS'
    clock[0] += timedelta(hours=1)
    current[0] = rows(400, 'b')
    assert run()['last_alert_count'] == 1
    assert len([i for report in reports for i in report if i.classification == 'n8n_execution_budget_70_percent']) == 2


@pytest.mark.parametrize('count,status', [(330, 'WARN_70_PERCENT'), (700, 'CAP_RISK')])
def test_f2_article_activation_alert_survives_warning_and_cap_risk(tmp_path, monkeypatch, count, status):
    current = [rows(count)]
    active = [False]
    def get(method, path):
        assert method == 'GET'
        if path.startswith('/executions?'):
            return {'data': current[0], 'nextCursor': None}
        wid = path.split('/')[-1]
        if wid == 'uabEqV8bzEYanLVJ':
            return {'active': active[0]}
        return {'active': True, 'activeVersionId': 'v', 'settings': {},
                'activeVersion': {'versionId': 'v', 'nodes': [{'name': 'poll', 'type': 'n8n-nodes-base.scheduleTrigger', 'disabled': True}]}}
    b, m, clock, path, reports, reds, run = runner(tmp_path, monkeypatch, get)
    assert run()['last_n8n_execution_budget']['status'] == status
    active[0] = True
    current[0] = rows(count + 10)
    clock[0] += timedelta(hours=1)
    state = run()
    assert state['last_n8n_execution_budget']['status'] == status
    assert state['last_alert_count'] == 1
    assert len(reports) == 2
    assert [i.classification for i in reports[-1]] == ['n8n_article_carrier_active']
    assert any(i.classification == 'n8n_article_carrier_active' for i in reds)
    assert all(not i.can_auto_fix and not i.safe_to_rerun for i in reports[-1])


@pytest.mark.parametrize('failure_stage', ['executions', 'invariants'])
def test_f3_failed_reads_are_hourly_cached_unknown_then_retry(tmp_path, monkeypatch, failure_stage):
    attempts = []
    fail = [True]
    def get(method, path):
        assert method == 'GET'
        attempts.append(path)
        if fail[0] and (failure_stage == 'executions' or path.startswith('/workflows/')):
            raise RuntimeError('sensitive remote exception')
        if path.startswith('/executions?'):
            return {'data': [], 'nextCursor': None}
        if path.endswith('uabEqV8bzEYanLVJ'):
            return {'active': False}
        return {'active': True, 'activeVersionId': 'v', 'activeVersion': {'versionId': 'v', 'nodes': [{'type': 'event'}]}}
    b = load('n8n_execution_budget', ROOT / 'ops/n8n_execution_budget.py', monkeypatch)
    start = datetime(2026, 10, 11, tzinfo=timezone.utc)
    path = tmp_path / 'budget.json'
    for minute in range(5):
        result = b.health_check(path, now=start + timedelta(minutes=minute), get=get)
        assert result['snapshot']['status'] == 'UNKNOWN'
        assert result['snapshot']['month_executions_observed'] is None
        assert 'sensitive' not in str(result)
    expected = 1 if failure_stage == 'executions' else 2
    assert len(attempts) == expected
    fail[0] = False
    assert b.health_check(path, now=start + timedelta(seconds=3599), get=get)['snapshot']['status'] == 'UNKNOWN'
    assert len(attempts) == expected
    assert b.health_check(path, now=start + timedelta(hours=1), get=get)['snapshot']['status'] == 'PASS'
    assert len(attempts) == expected + 7


def test_f4_corrupt_ledger_daily_line_and_incident_are_deduped_and_quarantined(tmp_path, monkeypatch, capsys):
    def forbidden(*a):
        raise AssertionError('must not query or reset corrupt accounting')
    b, m, clock, path, reports, reds, run = runner(tmp_path, monkeypatch, forbidden)
    path.write_bytes(b'corrupt')
    for hour in (0, 1, 7):
        clock[0] = datetime(2026, 10, 11, 8, tzinfo=timezone.utc) + timedelta(hours=hour)
        state = run()
        assert state['last_n8n_execution_budget']['status'] == 'UNKNOWN'
    assert capsys.readouterr().out.count('n8n execution budget:') == 1
    assert len(reports) == 1
    assert path.read_bytes() == b'corrupt'
    quarantine = list(tmp_path.glob('budget.json.quarantine.*'))
    assert len(quarantine) == 1
    assert quarantine[0].read_bytes() == b'corrupt'
    # Dry-run previews without writing a second quarantine/dedupe record.
    preview = tmp_path / 'preview.json'
    preview.write_bytes(b'corrupt')
    b = load('budget_preview', ROOT / 'ops/n8n_execution_budget.py', monkeypatch)
    b.health_check(preview, now=clock[0], get=forbidden, dry_run=True)
    assert sorted(p.name for p in tmp_path.glob('preview*')) == ['preview.json', 'preview.lock']


def test_daily_reporting_dedupe_survives_healthy_to_corrupt_and_repair(tmp_path, monkeypatch):
    b = load('budget_reporting_transition', ROOT / 'ops/n8n_execution_budget.py', monkeypatch)
    now = datetime(2026, 10, 11, 8, tzinfo=timezone.utc)
    path = tmp_path / 'budget.json'
    healthy = dict(get=lambda *a: {'data': [], 'nextCursor': None}, audit=lambda get: {'status': 'PASS'})
    assert b.health_check(path, now=now, **healthy)['daily_line']
    original = path.read_bytes()
    path.write_bytes(b'corrupt')
    assert b.health_check(path, now=now + timedelta(hours=1), **healthy)['daily_line'] is None
    assert path.read_bytes() == b'corrupt'
    path.write_bytes(original)  # simulated authorized repair in an offline fixture
    assert b.health_check(path, now=now + timedelta(hours=2), **healthy)['daily_line'] is None
    path.write_bytes(b'corrupt')
    next_day = b.health_check(path, now=now + timedelta(days=1), **healthy)
    assert next_day['daily_line']
    assert b.health_check(path, now=now + timedelta(days=1, hours=1), **healthy)['daily_line'] is None


def test_failure_cache_is_not_bypassed_by_utc_month_boundary(tmp_path, monkeypatch):
    b = load('budget_failure_rollover', ROOT / 'ops/n8n_execution_budget.py', monkeypatch)
    calls = []
    def fail(method, path):
        calls.append(path)
        raise RuntimeError('outage')
    now = datetime(2026, 10, 31, 23, 59, tzinfo=timezone.utc)
    path = tmp_path / 'budget.json'
    assert b.health_check(path, now=now, get=fail)['snapshot']['status'] == 'UNKNOWN'
    assert b.health_check(path, now=now + timedelta(minutes=2), get=fail)['snapshot']['status'] == 'UNKNOWN'
    assert len(calls) == 1
    assert b.health_check(path, now=now + timedelta(hours=1), get=fail)['snapshot']['month_utc'] == '2026-11'
    assert len(calls) == 2


@pytest.mark.parametrize('count', [330, 700])
@pytest.mark.parametrize('recovery', ['inactive', 'unknown'])
def test_f5_article_activation_rearms_only_after_verified_recovery(tmp_path, monkeypatch, count, recovery):
    active, failed = [False], [False]
    def get(method, path):
        assert method == 'GET'
        if failed[0]:
            raise RuntimeError('offline unknown article state')
        if path.startswith('/executions?'):
            return {'data': rows(count), 'nextCursor': None}
        if path.endswith('uabEqV8bzEYanLVJ'):
            return {'active': active[0]}
        return {'active': True, 'activeVersionId': 'v', 'settings': {},
                'activeVersion': {'versionId': 'v', 'nodes': [
                    {'name': 'poll', 'type': 'n8n-nodes-base.scheduleTrigger', 'disabled': True}]}}
    b, m, clock, path, reports, reds, run = runner(tmp_path, monkeypatch, get)
    article_counts = []
    keys = []
    for hour, flag in enumerate((False, True, False, True, False, True)):
        clock[0] = datetime(2026, 10, 11, tzinfo=timezone.utc) + timedelta(hours=hour)
        active[0] = flag
        failed[0] = recovery == 'unknown' and hour == 2
        before = len(reds)
        state = run()
        emitted = [i for i in reds[before:] if i.classification == 'n8n_article_carrier_active']
        article_counts.append(len(emitted))
        keys.extend(i.incident_key for i in emitted)
        assert all(not i.can_auto_fix and not i.safe_to_rerun for i in emitted)
        if failed[0]:
            assert state['last_n8n_execution_budget']['status'] == 'UNKNOWN'
    assert article_counts == ([0, 1, 0, 1, 0, 1] if recovery == 'inactive' else [0, 1, 0, 0, 0, 1])
    assert len(keys) == len(set(keys))


@pytest.mark.parametrize('initial_count', [330, 700])
@pytest.mark.parametrize('unknown_between', [False, True])
def test_f6_cap_risk_alerts_once_independently_of_warning(tmp_path, monkeypatch, initial_count, unknown_between):
    current, failed = [rows(initial_count)], [False]
    def get(method, path):
        assert method == 'GET' and path.startswith('/executions?')
        if failed[0]:
            raise RuntimeError('offline unknown budget')
        return {'data': current[0], 'nextCursor': None}
    b, m, clock, path, reports, reds, run = runner(tmp_path, monkeypatch, get, lambda get: {'status': 'PASS'})
    first = run()
    assert first['last_alert_count'] == 1
    if unknown_between:
        clock[0] += timedelta(hours=1)
        failed[0] = True
        assert run()['last_n8n_execution_budget']['status'] == 'UNKNOWN'
        failed[0] = False
    clock[0] += timedelta(hours=1)
    current[0] = rows(700)
    escalated = run()
    assert escalated['last_n8n_execution_budget']['status'] == 'CAP_RISK'
    assert escalated['last_alert_count'] == (1 if initial_count == 330 else 0)
    assert len(reds) == 1
    # Changed evidence and cooldown expiry cannot repeat RED within this episode.
    for hour, count in ((3, 710), (7, 330), (8, 720)):
        clock[0] = datetime(2026, 10, 11, tzinfo=timezone.utc) + timedelta(hours=hour)
        current[0] = rows(count)
        assert run()['last_alert_count'] == 0
    assert len(reds) == 1
    # A known below-threshold read starts a genuinely new warning episode.
    clock[0] += timedelta(hours=1)
    current[0] = []
    assert run()['last_n8n_execution_budget']['status'] == 'PASS'
    clock[0] += timedelta(hours=1)
    current[0] = rows(800)
    assert run()['last_alert_count'] == 1
    assert len(reds) == 2
    assert len({i.incident_key for i in reds}) == 2
    assert all(not i.can_auto_fix and not i.safe_to_rerun for i in reds)
