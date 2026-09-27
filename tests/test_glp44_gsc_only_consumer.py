"""Four offline acceptance checks for the explicit registered-controller GSC branch."""
import datetime as dt
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_glp44_weekly_gsc_aggregate import load_report

ROOT = Path(__file__).resolve().parents[1]
CONTROLLER = ROOT / 'scripts/glp_weekly_seo_control.py'
REPORT = ROOT / 'scripts/glp_weekly_seo_ranking_report.py'
RAIL = ROOT / 'ops/glp/seo_data_rails.py'
ANALYSIS = ROOT / 'scripts/glp_weekly_seo_analysis.py'
PIN_SOURCE = ROOT / 'ops/glp/seo-weekly-installed-pins.json'
INSTALLED_ROOT = Path('/home/ubuntu/business')
PREDECESSOR_CONTROLLER_SHA256 = 'd35dfd19b273feea449ad187cb1adcfb6161a87f3cde7a2ea2421a6306c499d2'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fixture(tmp_path, monkeypatch, *, mode='normal'):
    import urllib.request
    monkeypatch.setattr(urllib.request, 'urlopen', lambda *a, **kw: pytest.fail('network forbidden'))
    # Exercise the unchanged full verifier over all 20 identities. Curated
    # sources retain their real candidate bytes; host-only dependencies are
    # fixture stand-ins on CI (not claims about installed source identity).
    original = json.loads(PIN_SOURCE.read_bytes())
    assert len(original['paths']) == 20
    curated = json.loads((ROOT / 'ops/rebuild-source-manifest.json').read_text())['files']
    relocated = {}
    for absolute, expected in original['paths'].items():
        relative = Path(absolute).relative_to(INSTALLED_ROOT)
        source = ROOT / relative
        dest = tmp_path / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        if source.is_file():
            dest.write_bytes(source.read_bytes())
            assert sha(dest) == expected, relative
        else:
            assert str(relative) not in curated
            dest.write_bytes(b'fixture-only host dependency: ' + str(relative).encode())
        relocated[str(dest)] = sha(dest)
    paths = {'controller': tmp_path / CONTROLLER.relative_to(ROOT),
             'report': tmp_path / REPORT.relative_to(ROOT),
             'rail': tmp_path / RAIL.relative_to(ROOT)}
    pins = tmp_path / 'pins.json'
    pins.write_text(json.dumps({**original, 'paths': relocated}))
    guard_spec = importlib.util.spec_from_file_location('gsc_only_test_analysis', ANALYSIS)
    guard = importlib.util.module_from_spec(guard_spec)
    guard_spec.loader.exec_module(guard)
    guard.PIN_PATH = pins
    guard.PIN_SHA256 = sha(pins)
    spec = importlib.util.spec_from_file_location('gsc_only_test_controller', CONTROLLER)
    control = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(control)
    control.BUSINESS = tmp_path
    control.REPORT_SCRIPT = paths['report']
    control.REPORT_ROOT = tmp_path / 'reports/weekly-ranking-report'
    control.CONTROL_ROOT = tmp_path / 'reports/weekly-control'
    control.LOCK_PATH = tmp_path / 'locks/weekly-control.lock'
    reporter_ns = load_report()
    requests = []

    class Google:
        def gsc_search_analytics(self, **kw):
            requests.append(kw)
            start = dt.date.fromisoformat(kw['start_date'])
            metrics = dict(clicks=0, impressions=0, ctr=0, position=0)
            if kw['dimensions'] == ['date']:
                rows = [{'keys': [(start + dt.timedelta(days=n)).isoformat()], **metrics} for n in range(7)]
                if mode == 'unknown' and kw['property_uri'] == 'https://glpclinicscout.com/':
                    rows.pop()
            else:
                rows = [{'keys': [], **metrics}]
            return {'ok': True, 'complete': True, 'rows': rows}

    reporter_ns['GoogleRail'] = Google
    reporter = SimpleNamespace(**reporter_ns)
    def load(name, path):
        if name == 'weekly_gsc_pin_guard':
            return guard
        if name == 'glp_weekly_gsc_collector':
            assert path == paths['report']
            return reporter
        pytest.fail('other rail/weekly loader entered: ' + name)
    monkeypatch.setattr(control, 'load_module', load)
    return control, requests, paths, pins


def run(control, capsys, date=None):
    date = date or dt.datetime.now(control.PT).date()
    code = control.main(['--gsc-only', '--report-date', date.isoformat()])
    return code, json.loads(capsys.readouterr().out)


@pytest.mark.parametrize('replay', [False, True])
def test_full_pin_admission_refuses_predecessor_controller_before_reporter(
        tmp_path, monkeypatch, capsys, replay):
    control, requests, paths, pins = fixture(tmp_path, monkeypatch)
    if replay:
        code, first = run(control, capsys)
        assert code == 0 and len(requests) == 4
        aggregate = Path(first['aggregate']['path'])
        receipt = Path(first['receipt'])
        before = (aggregate.read_bytes(), receipt.read_bytes())
    # Keep all 20 keys and all 19 other hashes; pair the predecessor controller
    # leaf with its matching in-memory literal. This is candidate-only, not an
    # installed pin write. The actual copied controller remains the new source.
    predecessor = json.loads(pins.read_bytes())
    assert len(predecessor['paths']) == 20
    predecessor['paths'][str(paths['controller'])] = PREDECESSOR_CONTROLLER_SHA256
    pins.write_text(json.dumps(predecessor))
    guard = control.load_module('weekly_gsc_pin_guard', ANALYSIS)
    guard.PIN_SHA256 = sha(pins)
    with pytest.raises(ValueError, match='weekly_installed_source_drift'):
        guard.verify_pins()  # same unmodified 20-source verifier, independent oracle
    loaded = []
    real_load = control.load_module
    def track_loader(name, path):
        if name == 'glp_weekly_gsc_collector':
            loaded.append(name)
        return real_load(name, path)
    monkeypatch.setattr(control, 'load_module', track_loader)
    monkeypatch.setattr(control, 'write_json_once',
                        lambda *a, **kw: pytest.fail('output write before pin admission'))
    code, blocked = run(control, capsys)
    assert code == 2 and blocked['status'] == 'GSC_ONLY_BLOCKED'
    assert 'weekly_installed_source_drift' in blocked['error']
    assert not loaded and len(requests) == (4 if replay else 0)
    if replay:
        assert (aggregate.read_bytes(), receipt.read_bytes()) == before
    else:
        assert not control.REPORT_ROOT.exists() and not control.CONTROL_ROOT.exists()


def test_explicit_branch_uses_registered_lock_pins_and_only_gsc(tmp_path, monkeypatch, capsys):
    control, requests, paths, pins = fixture(tmp_path, monkeypatch)
    code, result = run(control, capsys)
    assert code == 0 and result['status'] == 'GSC_ONLY_PARTIAL_REVIEW'
    assert result['full_weekly_pass'] is False and result['natural_weekly_run_observed'] is False
    assert result['trigger_identity'] == 'manual_explicit_gsc_only'
    assert control.LOCK_PATH.is_file()
    assert [(r['property_uri'], r['dimensions']) for r in requests] == [
        ('https://glpclinicscout.com/', ['date']), ('https://glpclinicscout.com/', []),
        ('sc-domain:glpclinicscout.com', ['date']), ('sc-domain:glpclinicscout.com', [])]
    assert not (control.REPORT_ROOT / f"{result['report_date']}.json").exists()
    assert not (control.CONTROL_ROOT / result['report_date'] / 'receipt.json').exists()
    paths['rail'].write_text('drift')
    code, blocked = run(control, capsys, dt.date.fromisoformat(result['report_date']))
    assert code == 2 and 'weekly_installed_source_drift' in blocked['error'] and len(requests) == 4


def test_persisted_snapshot_review_scope_window_and_unknown_zero(tmp_path, monkeypatch, capsys):
    for mode in ('normal', 'unknown'):
        control, requests, paths, pins = fixture(tmp_path / mode, monkeypatch, mode=mode)
        original = control.write_json_once
        def persist_then_poison(path, value):
            original(path, value)
            if path.name.endswith('-gsc-sitewide-aggregate.json'):
                value['scopes']['consumer_https_apex']['totals']['clicks'] = 999
        monkeypatch.setattr(control, 'write_json_once', persist_then_poison)
        code, result = run(control, capsys)
        assert code == 0, result
        binding = result['aggregate']
        raw = Path(binding['path']).read_bytes()
        assert sha(Path(binding['path'])) == binding['sha256'] and len(raw) == binding['bytes']
        assert result['review']['scopes']['consumer_https_apex']['totals']['clicks'] == (0 if mode == 'normal' else None)
        assert result['review']['scopes']['consumer_https_apex']['state'] == (
            'SOURCE_REPORTED' if mode == 'normal' else 'UNKNOWN_DATE_COVERAGE')
        assert result['review']['window'].endswith((dt.date.fromisoformat(result['report_date'])
                                                    - dt.timedelta(days=3)).isoformat())
        assert len(requests) == 4 and all(r['data_state'] == 'final' for r in requests)
        assert result['sources']['producer']['sha256'] == sha(paths['report'])
        assert result['sources']['consumer']['sha256'] == sha(paths['controller'])
        persisted = json.loads(raw)
        review_date = dt.date.fromisoformat(result['report_date'])
        reporter = SimpleNamespace(report_windows=load_report()['report_windows'])
        persisted['scopes']['consumer_https_apex']['context']['property'] = 'sc-domain:glpclinicscout.com'
        with pytest.raises(ValueError, match='gsc_aggregate_context_invalid'):
            control.gsc_review(persisted, review_date, reporter, now=dt.datetime.now(dt.timezone.utc))
        persisted['scopes']['consumer_https_apex']['context']['property'] = 'https://glpclinicscout.com/'
        persisted['scopes']['consumer_https_apex']['date_availability'][0]['date'] = '2020-01-01'
        with pytest.raises(ValueError, match='gsc_date_coverage_invalid'):
            control.gsc_review(persisted, review_date, reporter, now=dt.datetime.now(dt.timezone.utc))
        persisted['scopes']['consumer_https_apex']['date_availability'][0]['date'] = (
            review_date - dt.timedelta(days=9)).isoformat()
        with pytest.raises(ValueError, match='gsc_capture_clock_invalid'):
            control.gsc_review(persisted, review_date, reporter,
                               now=dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=4))


def test_success_and_failure_never_enter_paid_stage_policy_or_send(tmp_path, monkeypatch, capsys):
    control, requests, paths, pins = fixture(tmp_path, monkeypatch)
    forbidden = lambda *a, **kw: pytest.fail('full-weekly/effect boundary entered')
    for name in ('execute', 'completed_receipt', 'resume_modules', 'subprocess'):
        monkeypatch.setattr(control, name, forbidden)
    code, result = run(control, capsys)
    assert code == 0 and len(requests) == 4
    (control.CONTROL_ROOT / result['report_date'] / 'gsc-only-receipt.json').write_text('{bad')
    code, blocked = run(control, capsys)
    assert code == 2 and blocked['status'] == 'GSC_ONLY_BLOCKED'
    assert len(requests) == 4 and blocked['full_weekly_pass'] is False


def test_replay_exact_snapshot_and_refuse_collision_incomplete_historical(tmp_path, monkeypatch, capsys):
    control, requests, paths, pins = fixture(tmp_path, monkeypatch)
    code, first = run(control, capsys)
    assert code == 0
    code, replay = run(control, capsys)
    assert code == 0 and replay['idempotent_replay'] and replay['reviewed_at_utc'] == first['reviewed_at_utc']
    assert replay['review'] == first['review'] and len(requests) == 4
    aggregate = Path(first['aggregate']['path'])
    saved = aggregate.read_bytes()
    aggregate.write_bytes(saved + b' ')
    code, blocked = run(control, capsys)
    assert code == 2 and 'gsc_only_source_drift' in blocked['error'] and len(requests) == 4
    aggregate.write_bytes(saved)
    receipt = Path(first['receipt'])
    receipt.unlink()
    code, blocked = run(control, capsys)
    assert code == 2 and 'gsc_only_incomplete_or_colliding_state' in blocked['error']
    assert len(requests) == 4
    aggregate.unlink()
    receipt.parent.rmdir()
    historical = dt.datetime.now(control.PT).date() - dt.timedelta(days=3)
    code, blocked = run(control, capsys, historical)
    assert code == 2 and 'gsc_only_historical_collection_refused' in blocked['error']
    assert len(requests) == 4
