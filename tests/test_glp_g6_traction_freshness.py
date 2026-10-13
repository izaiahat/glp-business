import hashlib
import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 12, 16, 0, tzinfo=timezone.utc)


def load():
    sys.path.insert(0, str(ROOT / 'ops'))
    spec = importlib.util.spec_from_file_location('g6_traction', ROOT / 'scripts/glp_g6_weekly_traction_report.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def bind(path, doc):
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(doc) + '\n').encode()
    path.write_bytes(raw)
    return {'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest()}


def weekly_run(root, name, captured, *, clicks, completed=True):
    run = root / name
    stamp = captured.isoformat()
    snap = lambda rows: {'captured_at_utc': stamp, 'context': {'start_date': '2026-09-12', 'end_date': '2026-10-09'},
                         'result': {'ok': True, 'complete': True, 'rows': rows}}
    snapshots = {key: bind(run / f'collection/cogny/snapshots/{key}.json', snap([{'Query': 'glp'}]))
                 for key in ('bing-page', 'bing-query')}
    evidence = {'schema': 'glp-weekly-cogny-evidence-v1', 'report_date': captured.date().isoformat(),
                'snapshots': snapshots, 'queries': [{'state': 'RETURNED'}, {'state': 'UNKNOWN_TRANSPORT_OR_PROVIDER'}]}
    inputs = {
        'gsc-28': bind(run / 'collection/inputs/gsc-28.json',
                       snap([{'keys': ['glp clinic', 'https://glpclinicscout.com/'], 'clicks': clicks, 'impressions': 40}])),
        'ga4': bind(run / 'collection/inputs/ga4.json', snap([{'dimensions': ['glpclinicscout.com', '/'], 'metrics': [7.0, 5.0]}])),
        'cogny-weekly-evidence': bind(run / 'collection/cogny/EVIDENCE.json', evidence),
    }
    bind(run / 'collection/MODULE-INPUTS.json', {'schema': 'glp-weekly-module-inputs-v1', 'built_at_utc': stamp, 'inputs': inputs})
    if completed:
        bind(run / 'REPORT.json', {'schema': 'glp-weekly-strategy-report-v1', 'generated_at_utc': stamp})
    return run


def fixture(module, monkeypatch, tmp_path, *, matrix_at):
    weekly = tmp_path / 'weekly-strategy'
    matrix = tmp_path / 'analytics/current/route-instrumentation-matrix.json'
    bind(matrix, {'schema': 'route-instrumentation-matrix-v1', 'generated_at_utc': matrix_at.isoformat(),
                  'sitemap_route_count': 3, 'routes': [{'path': p, 'in_sitemap': True, 'http_status': s}
                                                       for p, s in (('/', 200), ('/providers/', 200), ('/old/', 404))]})
    flywheel = tmp_path / 'flywheel'
    bind(flywheel / 'approved-programs-manifest.json', {'programs': [{'live_link_allowed': False}]})
    bind(flywheel / 'affiliate-application-ledger.json', {'applications': [{'program_id': 'gobymeds', 'status': 'approved'}]})
    monkeypatch.setattr(module, 'WEEKLY_ROOT', weekly)
    monkeypatch.setattr(module, 'ROUTE_MATRIX', matrix)
    monkeypatch.setattr(module, 'MANIFEST', flywheel / 'approved-programs-manifest.json')
    monkeypatch.setattr(module, 'LEDGER', flywheel / 'affiliate-application-ledger.json')
    monkeypatch.setattr(module, 'OUTPUT_DIR', flywheel / 'weekly-traction-reports')
    return weekly


def test_report_reads_newest_completed_weekly_run_and_records_input_dates(monkeypatch, tmp_path):
    m = load()
    weekly = fixture(m, monkeypatch, tmp_path, matrix_at=NOW - timedelta(days=1))
    weekly_run(weekly, 'run-20260705T090000Z-old', datetime(2026, 7, 5, 9, tzinfo=timezone.utc), clicks=1)
    newest = weekly_run(weekly, 'run-20261011T090000Z-new', NOW - timedelta(days=1, hours=7), clicks=3)
    weekly_run(weekly, 'run-20261012T150000Z-partial', NOW - timedelta(hours=1), clicks=99, completed=False)

    report = m.build('2026-10-12', now=NOW)

    fresh = report['source_freshness']
    assert set(fresh) == {'weekly-review', 'gsc-28', 'ga4', 'cogny-weekly-evidence', 'bing-page', 'bing-query', 'route-matrix'}
    assert fresh['weekly-review'] == {'date': '2026-10-11', 'observed_at_utc': '2026-10-11T09:00:00+00:00',
                                      'age_hours': 31.0, 'path': str(newest)}
    assert all(row['date'] in ('2026-10-11', '2026-10-12') for row in fresh.values())
    assert fresh['gsc-28']['path'].startswith(str(newest))
    metrics = report['metrics']
    assert metrics['gsc_clicks_28d'] == 3 and metrics['gsc_impressions_28d'] == 40
    assert metrics['ga4_neutral_route_page_views_28d'] == 7.0
    assert metrics['sitemap_routes_live_200'] == 2 and metrics['sitemap_routes'] == 3
    assert metrics['bing_page_rows'] == 1 and metrics['cogny_queries_returned'] == 1
    assert 'indexed_url_inspection_samples' not in metrics


def test_input_older_than_eight_days_is_refused_without_writing_a_report(monkeypatch, tmp_path, capsys):
    m = load()
    weekly = fixture(m, monkeypatch, tmp_path, matrix_at=datetime(2026, 7, 9, 12, tzinfo=timezone.utc))
    weekly_run(weekly, 'run-20261011T090000Z-new', NOW - timedelta(days=1), clicks=3)
    monkeypatch.setattr(m, 'datetime', type('FrozenDatetime', (datetime,), {'now': staticmethod(lambda tz=None: NOW)}))
    monkeypatch.setattr(sys, 'argv', ['glp_g6_weekly_traction_report.py', '--as-of', '2026-10-12'])

    assert m.main() == 2

    err = capsys.readouterr().err
    assert err.startswith('G6_INPUT_REFUSED: route-matrix: input dated 2026-07-09 is 95 days old (limit 8)')
    assert not (tmp_path / 'flywheel/weekly-traction-reports').exists()
