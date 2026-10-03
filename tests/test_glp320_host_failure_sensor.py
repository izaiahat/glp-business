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


def test_isolated_test_path_uses_real_receipt_and_captures_once(host_failure, monkeypatch, capsys):
    sensor, alerts, result, _ = host_failure
    root = sensor.HOST_RUNS.parent
    (root / 'TEST.json').write_text(json.dumps({
        'schema': 'glp320-isolated-failure-sensor-test-v1', 'pt_date': DAY}))
    monkeypatch.setattr(sensor, 'TEST_ROOT', root, raising=False)
    monkeypatch.setenv('GLP320_FAILURE_SENSOR_TEST', 'failure')
    assert sensor.main() == 0
    assert 'TEST — GLP320' in capsys.readouterr().out
    assert alerts == [], 'TEST must not call production red-alert hook'
    assert sensor.main() == 0
    assert capsys.readouterr().out == ''
    monkeypatch.setenv('GLP320_FAILURE_SENSOR_TEST', 'recovery')
    assert sensor.main() == 0 and sensor.main() == 0
    assert capsys.readouterr().out == ''
    events = [json.loads(line) for line in (root / 'captured-alerts.jsonl').read_text().splitlines()]
    assert len(events) == 2
    assert all(event['test_only'] is True for event in events)
    assert [event['payload']['recovery'] for event in events] == [False, True]
    assert not sensor.STATE.exists()


@pytest.mark.parametrize('damage', ['pending', 'lost_state', 'bad_state', 'symlink', 'hardlink', 'production_receipt', 'marker', 'mode'])
def test_test_path_refuses_unknown_or_production_alias(host_failure, monkeypatch, damage):
    sensor, alerts, result, _ = host_failure
    root = sensor.HOST_RUNS.parent
    (root / 'TEST.json').write_text(json.dumps({'schema': sensor.TEST_SCHEMA, 'pt_date': DAY}))
    monkeypatch.setattr(sensor, 'TEST_ROOT', root)
    monkeypatch.setenv('GLP320_FAILURE_SENSOR_TEST', 'failure')
    if damage == 'pending':
        (root / 'test-state.json').write_text('{"pending":true}')
    elif damage == 'lost_state':
        (root / 'captured-alerts.jsonl').write_text('{}\n')
    elif damage == 'bad_state':
        (root / 'test-state.json').write_text('{"unknown":true}')
    elif damage in ('symlink', 'hardlink'):
        target = root.parent / (root.name + '-production-state')
        target.write_text('protected')
        if damage == 'symlink':
            (root / 'test-state.json').symlink_to(target)
        else:
            import os
            os.link(target, root / 'test-state.json')
    elif damage == 'production_receipt':
        branch = root / 'runs' / f'{sensor.HOST_ACTION}-{sensor.HOST_DIGEST}.state.json'
        state = json.loads(branch.read_text())
        state['last_receipt'] = '/home/ubuntu/business/reports/ops/n8n-to-cron-20260924/runs/article_carrier_consume-' + 'a'*32 + '.json'
        branch.write_text(json.dumps(state))
    elif damage == 'marker':
        (root / 'TEST.json').write_text('{}')
    elif damage == 'mode':
        monkeypatch.setenv('GLP320_FAILURE_SENSOR_TEST', '/arbitrary/path')
    with pytest.raises(RuntimeError, match='TEST'):
        sensor.main()
    assert alerts == []
    if damage in ('symlink', 'hardlink'):
        assert target.read_text() == 'protected'


def test_exact_wrapper_test_transport_and_silence(host_failure, monkeypatch):
    import os
    import subprocess
    sensor, alerts, _, _ = host_failure
    root = sensor.HOST_RUNS.parent
    (root / 'TEST.json').write_text(json.dumps({'schema': sensor.TEST_SCHEMA, 'pt_date': DAY}))
    # The unchanged registered argv is intercepted ONLY by this offline fixture.
    # The real candidate payload is loaded, with fixed TEST_ROOT mapped to scratch.
    bindir = root / 'bin'
    bindir.mkdir()
    shim = bindir / 'python3'
    shim.write_text(f'''#!{sys.executable} -B
import importlib.util, os, sys
from pathlib import Path
assert sys.argv[1:] == ['/home/ubuntu/.hermes/scripts/glp_seo_publish_failure_alert.py']
root = Path({str(root)!r})
source = Path({str(ROOT / 'ops/hermes-cron-scripts/glp_seo_publish_failure_alert.py')!r})
def audit(event, args):
    if event.startswith(('socket.', 'subprocess.')) or event == 'os.system':
        raise AssertionError('effect denied: ' + event)
    if event == 'open' and isinstance(args[0], (str, bytes)):
        path = Path(os.fsdecode(args[0])).absolute()
        if str(path).startswith(('/home/ubuntu/business/', '/home/ubuntu/.hermes/')) and not (path.is_relative_to(root) or path == source or path.is_relative_to(Path(sys.base_prefix))):
            raise AssertionError('production read denied: ' + str(path))
        if args[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC) and not path.is_relative_to(root):
            raise AssertionError('write denied: ' + str(path))
sys.addaudithook(audit)
spec = importlib.util.spec_from_file_location('sensor', source)
sensor = importlib.util.module_from_spec(spec)
exec(compile(source.read_bytes(), str(source), 'exec'), sensor.__dict__)
sensor.TEST_ROOT = root
raise SystemExit(sensor.main())
''')
    shim.chmod(0o700)
    outputs, counts = [], []
    for mode in ('failure', 'failure', 'recovery', 'healthy'):
        result = subprocess.run(['bash', str(ROOT / 'ops/hermes-cron-scripts/glp_seo_publish_failure_alert_cron.sh')],
            env={**os.environ, 'PATH': str(bindir) + ':' + os.environ['PATH'],
                 'PYTHONDONTWRITEBYTECODE': '1', 'GLP320_FAILURE_SENSOR_TEST': mode},
            capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, result.stdout + result.stderr
        outputs.append(result.stdout)
        counts.append(len((root / 'captured-alerts.jsonl').read_text().splitlines()))
    assert outputs[0].startswith('TEST — GLP320')
    assert outputs[1:] == ['', '', '']
    assert counts == [1, 1, 2, 2]
    assert alerts == []
    (root / 'TEST-TRACE.json').write_text(json.dumps({
        'schema': 'glp320-offline-wrapper-trace-v1', 'registered_run': False,
        'modes': ['failure', 'failure', 'recovery', 'healthy'],
        'capture_counts': counts, 'stdout': outputs,
        'production_effects': False}, indent=2))


def test_test_capture_uncertainty_never_replays(host_failure, monkeypatch, capsys):
    sensor, alerts, _, _ = host_failure
    root = sensor.HOST_RUNS.parent
    (root / 'TEST.json').write_text(json.dumps({'schema': sensor.TEST_SCHEMA, 'pt_date': DAY}))
    monkeypatch.setattr(sensor, 'TEST_ROOT', root)
    monkeypatch.setenv('GLP320_FAILURE_SENSOR_TEST', 'failure')
    original = Path.open
    def fail_capture(path, *args, **kwargs):
        if path == root / 'captured-alerts.jsonl':
            raise OSError('fixture capture failed after intent')
        return original(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'open', fail_capture)
        with pytest.raises(OSError, match='fixture capture'):
            sensor.main()
    with pytest.raises(RuntimeError, match='uncertain capture'):
        sensor.main()
    assert alerts == [] and capsys.readouterr().out == ''
    assert json.loads((root / 'test-state.json').read_text()) == {'pending': True}


def test_test_cold_healthy_is_silent(host_failure, monkeypatch, capsys):
    sensor, alerts, _, _ = host_failure
    root = sensor.HOST_RUNS.parent
    (root / 'TEST.json').write_text(json.dumps({'schema': sensor.TEST_SCHEMA, 'pt_date': DAY}))
    monkeypatch.setattr(sensor, 'TEST_ROOT', root)
    monkeypatch.setenv('GLP320_FAILURE_SENSOR_TEST', 'healthy')
    assert sensor.main() == 0
    assert alerts == [] and capsys.readouterr().out == ''
    assert not (root / 'test-state.json').exists()
    assert not (root / 'captured-alerts.jsonl').exists()


def test_exact_native_copin_proposal_uses_existing_producer(tmp_path, monkeypatch):
    import hashlib
    import fleet_pin_release as release
    import pin_drift_report as drift
    from test_fleet_pin_release import _review
    native = tmp_path / 'native'
    native.mkdir()
    wrapper = native / 'glp_seo_publish_failure_alert_cron.sh'
    wrapper.write_bytes((ROOT / 'ops/hermes-cron-scripts' / wrapper.name).read_bytes())
    registry = tmp_path / 'jobs.json'
    # Deliberate offline fixture, not a current complete-record runtime proposal.
    registry.write_text(json.dumps({'jobs': [{'id': '76de44046a9b', 'enabled': True,
        'script': wrapper.name, 'script_sha256': 'bc11df966517025e109519d2840ea24ca4eadfdb9f7c5c339ccd455afe6751a2'}]}))
    monkeypatch.setattr(drift, 'BUSINESS', tmp_path)
    monkeypatch.setattr(drift, 'HERMES_SCRIPTS', native)
    monkeypatch.setattr(drift, 'PIN_ARCHIVE', tmp_path / 'archive')
    monkeypatch.setattr(drift, 'PYTHON_PIN_ROOTS', ())
    monkeypatch.setattr(drift, 'PYTHON_SCALAR_PIN_ROOTS', (native,))
    monkeypatch.setattr(drift, 'PYTHON_PIN_SOURCES', ())
    monkeypatch.setattr(drift, 'HISTORICAL_RECORD_SOURCES', frozenset())
    review = tmp_path / 'FIXTURE-ONLY-review.json'
    _review(review, [wrapper])
    before = registry.read_bytes()
    code, proposal = release.release(review, release.JsonRegistry(registry),
        registry_path=registry, apply=False)
    assert code == 0 and proposal['dry_run'] and not proposal['applied']
    assert proposal['proposed_registry_updates'] == [{
        'job_id': '76de44046a9b', 'script': str(wrapper),
        'script_sha256': hashlib.sha256(wrapper.read_bytes()).hexdigest()}]
    assert registry.read_bytes() == before and proposal['pin_updates'] == []
    (tmp_path / 'FIXTURE-ONLY-pin-proposal.json').write_text(json.dumps(proposal, indent=2))


@pytest.mark.parametrize('test_mode,payload,expected,rc', [
    (None, 'ALERTED: carrier=failed, failures=NEW_ROUTE, fixture', '🔴 **SEO Publish Alert**', 0),
    (None, 'already alerted for fixture', '', 0),
    (None, 'TEST — GLP320 failure-sensor acceptance: fixture', '', 0),
    ('failure', 'ALERTED: carrier=failed, failures=NEW_ROUTE, fixture', '', 1),
])
def test_wrapper_cannot_relabel_test_as_production(tmp_path, test_mode, payload, expected, rc):
    import os
    import subprocess
    shim = tmp_path / 'python3'
    shim.write_text(f'#!{sys.executable} -B\nprint({payload!r})\n')
    shim.chmod(0o700)
    env = {**os.environ, 'PATH': str(tmp_path) + ':' + os.environ['PATH']}
    env.pop('GLP320_FAILURE_SENSOR_TEST', None)
    if test_mode is not None:
        env['GLP320_FAILURE_SENSOR_TEST'] = test_mode
    result = subprocess.run(['bash', str(ROOT / 'ops/hermes-cron-scripts/glp_seo_publish_failure_alert_cron.sh')],
        env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == rc
    assert result.stdout.startswith(expected) if expected else result.stdout == ''
    if rc:
        assert result.stderr.startswith('TEST — GLP320 REFUSED')


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

