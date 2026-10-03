"""Isolated host-receipt regression; never publishes or sends a real alert."""
from __future__ import annotations

import copy
import importlib.util
import json
import sqlite3
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import cron_bridge_action as runner

DAY = '2026-10-02'
SLOT = DAY + 'T11:00:America/Los_Angeles'


def load_sensor(monkeypatch):
    # Only uncurated host dependencies are replaced, never terminal admission.
    alerts = []
    monkeypatch.setitem(sys.modules, 'red_alert_hook', types.SimpleNamespace(
        raise_alert=lambda payload, live: alerts.append((payload, live))))
    monkeypatch.setitem(sys.modules, 'daily_outcome_cards', types.SimpleNamespace())
    spec = importlib.util.spec_from_file_location('sensor_under_test', ROOT / 'ops/hermes-cron-scripts/glp_seo_publish_failure_alert.py')
    sensor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sensor)
    return sensor, alerts


@pytest.fixture
def host_failure(tmp_path, monkeypatch):
    sensor, alerts = load_sensor(monkeypatch)
    # The actual registered producer emits this receipt/state into scratch with
    # fake bridge transport, notification sink and fixture-only release predicate.
    monkeypatch.setattr(runner, 'article_release_live', lambda: True)
    monkeypatch.setattr(runner, 'utc_now', lambda: DAY + 'T18:05:00+00:00')
    job = {'job_id': 'fixture-failed-job', 'status': 'done', 'rc': 2,
           'timed_out': False, 'interrupted': False}
    runs = tmp_path / 'runs'
    result = runner.run_action('article_carrier_consume', {'mode': 'consume'},
        state_dir=runs, actions={'article_carrier_consume': {'timeout_s': 1}},
        call=lambda *args: (200, job), notify=lambda *args: {'exact_readback': True},
        invocation='scheduled', due_slot=SLOT)
    assert result['status'] == 'FAIL' and result['rc'] == 2
    sensor.HOST_RUNS = runs
    sensor.EXEC_DB = tmp_path / 'legacy.db'
    with sqlite3.connect(sensor.EXEC_DB) as db:
        db.execute('CREATE TABLE executions (job_id TEXT, status TEXT, started_at TEXT)')
        db.execute('INSERT INTO executions VALUES (?, ?, ?)',
                   (sensor.CARRIER_JOB, 'completed', '2026-08-17T12:09:01-07:00'))
    sensor.STATE = tmp_path / 'alert-state.json'
    monkeypatch.setattr(sensor, 'pt_today', lambda: DAY)
    summary = {'new_route': {'scheduled': True, 'published_count': 0,
               'cadence': 'M/W/F', 'accepted_failures': {
                   'accepted_inherited_count': None, 'new_failure_count': None}},
               'r2_refresh': {'scheduled': True, 'ok': True, 'status': 'PASS_DAILY_QUOTA',
               'published_count': 2, 'completed': 2, 'required': 2, 'deficit': 0}}
    monkeypatch.setattr(sensor, 'publication_summary', lambda day: copy.deepcopy(summary))
    return sensor, alerts, result, summary


def test_registered_host_failure_alerts_once_without_legacy_execution(host_failure, capsys):
    sensor, alerts, result, summary = host_failure
    assert sensor.main() == 0
    assert len(alerts) == 1, 'active host failed, but disabled legacy execution hid it'
    assert alerts[0][0]['impact'].endswith('NEW_ROUTE')
    assert "ended 'failed'" in alerts[0][0]['what']
    assert str(sensor.HOST_RUNS) in alerts[0][0]['where']
    assert sensor.main() == 0
    assert len(alerts) == 1
    summary['new_route']['published_count'] = 1
    assert sensor.main() == 0
    assert len(alerts) == 2 and alerts[-1][0]['recovery']
    assert sensor.main() == 0
    assert len(alerts) == 2  # unchanged healthy cycle is silent
    assert json.loads(sensor.STATE.read_text()) == {'alerted_day': DAY, 'recovered': True}


@pytest.mark.parametrize('invalid', [
    'pending', 'common_pending', 'held', 'wrong_action', 'wrong_payload',
    'wrong_path', 'wrong_slot', 'wrong_invocation', 'wrong_rc',
    'stale_day', 'naive_date', 'unsettled_bridge', 'interrupted', 'timed_out',
    'missing_job_id', 'broken_json', 'missing_receipt', 'producer_busy',
])
def test_unsettled_or_unbound_host_truth_never_falls_back_to_legacy(host_failure, invalid):
    sensor, alerts, result, _ = host_failure
    state_path = sensor.HOST_RUNS / f'{sensor.HOST_ACTION}-{sensor.HOST_DIGEST}.state.json'
    receipt_path = Path(result['receipt_path'])
    state = json.loads(state_path.read_text())
    receipt = json.loads(receipt_path.read_text())
    # Same-day legacy success must not override migrated unknown/pending truth.
    with sqlite3.connect(sensor.EXEC_DB) as db:
        db.execute('UPDATE executions SET started_at=?', (DAY + 'T12:00:00-07:00',))
    if invalid == 'pending':
        state['pending'] = {'job_id': 'ambiguous-do-not-replay'}
    elif invalid == 'common_pending':
        (sensor.HOST_RUNS / f'{sensor.HOST_ACTION}.state.json').write_text(
            json.dumps({'pending': {'job_id': 'ambiguous-do-not-replay'}}))
    elif invalid == 'held':
        receipt.update(status='DECLINED_RELEASE_HOLD', rc=0)
        state['slots'][SLOT]['rc'] = 0
    elif invalid == 'wrong_action':
        receipt['action'] = 'seo_update_run'
    elif invalid == 'wrong_payload':
        receipt['payload_sha256'] = '0' * 64
    elif invalid == 'wrong_path':
        receipt['receipt_path'] = '/unbound/receipt.json'
    elif invalid == 'wrong_slot':
        receipt['due_slot'] = '2026-09-30T11:00:America/Los_Angeles'
    elif invalid == 'wrong_invocation':
        receipt['invocation'] = 'manual'
    elif invalid == 'wrong_rc':
        receipt['rc'] = 0
    elif invalid == 'stale_day':
        receipt['started_utc'] = '2026-09-30T18:00:00+00:00'
    elif invalid == 'naive_date':
        receipt['started_utc'] = DAY + 'T18:00:00'
    elif invalid == 'unsettled_bridge':
        receipt['bridge']['status'] = 'running'
    elif invalid in ('interrupted', 'timed_out'):
        receipt['bridge'][invalid] = True
    elif invalid == 'missing_job_id':
        del receipt['bridge']['job_id']
    state_path.write_text(json.dumps(state))
    receipt_path.write_text(json.dumps(receipt))
    if invalid == 'broken_json':
        receipt_path.write_text('{')
    elif invalid == 'missing_receipt':
        receipt_path.unlink()
    if invalid == 'producer_busy':
        import fcntl
        with (sensor.HOST_RUNS / f'{sensor.HOST_ACTION}.lock').open('rb') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            assert sensor.carrier_terminal(DAY) is None
    else:
        assert sensor.carrier_terminal(DAY) is None
        assert sensor.main() == 0 and alerts == []
        assert not sensor.STATE.exists()


def test_legacy_consumer_without_host_state_and_missing_database(host_failure):
    sensor, _, _, _ = host_failure
    sensor.HOST_RUNS = sensor.HOST_RUNS / 'unmigrated'
    with sqlite3.connect(sensor.EXEC_DB) as db:
        db.execute('UPDATE executions SET started_at=?', (DAY + 'T12:00:00-07:00',))
    assert sensor.carrier_terminal(DAY) == 'completed'
    assert sensor.carrier_terminal('2026-10-03') is None
    sensor.EXEC_DB.unlink()
    assert sensor.carrier_terminal(DAY) is None
    assert not sensor.EXEC_DB.exists()


@pytest.mark.parametrize('invocation', ['scheduled', 'manual_registered'])
def test_host_success_is_terminal_but_healthy_lanes_do_not_alert(host_failure, invocation):
    sensor, alerts, result, summary = host_failure
    path = Path(result['receipt_path'])
    receipt = json.loads(path.read_text())
    receipt.update(status='PASS', rc=0, invocation=invocation)
    receipt['bridge']['rc'] = 0
    path.write_text(json.dumps(receipt))
    state_path = sensor.HOST_RUNS / f'{sensor.HOST_ACTION}-{sensor.HOST_DIGEST}.state.json'
    state = json.loads(state_path.read_text())
    state['slots'][SLOT]['rc'] = 0
    state_path.write_text(json.dumps(state))
    summary['new_route']['published_count'] = 1
    assert sensor.carrier_terminal(DAY) == 'completed'
    assert sensor.main() == 0 and sensor.main() == 0
    assert alerts == [] and not sensor.STATE.exists()


def test_native_wrapper_and_payload_inventory_close_on_reviewed_bytes():
    import hashlib
    inventory = json.loads((ROOT / 'ops/scheduled-source-drift-manifest.json').read_text())
    row = next(row for row in inventory['jobs'] if row['id'] == '76de44046a9b')
    wrapper = ROOT / 'ops/hermes-cron-scripts/glp_seo_publish_failure_alert_cron.sh'
    payload = ROOT / 'ops/hermes-cron-scripts/glp_seo_publish_failure_alert.py'
    assert row['reviewed_sha256'] == hashlib.sha256(wrapper.read_bytes()).hexdigest()
    assert row['payloads'] == [{
        'path': '/home/ubuntu/.hermes/scripts/glp_seo_publish_failure_alert.py',
        'sha256': hashlib.sha256(payload.read_bytes()).hexdigest(),
        'owner': '/home/ubuntu/.hermes/scripts/glp_seo_publish_failure_alert_cron.sh'}]
    assert 'python3 /home/ubuntu/.hermes/scripts/glp_seo_publish_failure_alert.py' in wrapper.read_text()

