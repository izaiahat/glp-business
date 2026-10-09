"""Offline registered ranking collector -> report fixture; no live rail imports.

Execute unchanged source AST bodies, excluding only host import/bootstrap nodes.
The curated mirror deliberately does not carry Cogny or private runtime inputs.
"""
import ast
import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any
import runpy
import types

import pytest

ROOT = Path(__file__).resolve().parents[1]
RAILS = ROOT / 'ops/glp/seo_data_rails.py'
REPORT = ROOT / 'scripts/glp_weekly_seo_ranking_report.py'


def load_report():
    tree = ast.parse(REPORT.read_text())
    tree.body = [node for node in tree.body if not (
        isinstance(node, ast.ImportFrom) and (node.module or '').startswith('ops.')
        or isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
        and ast.unparse(node.value.func) == 'sys.path.insert'
        or isinstance(node, ast.If) and '__name__' in ast.unparse(node.test))]
    ns: dict[str, Any] = {'__name__': 'weekly_fixture'}
    exec(compile(tree, str(REPORT), 'exec'), ns)
    return ns


def test_empty_dimensions_not_none(monkeypatch):
    rail_ns = runpy.run_path(str(RAILS))
    cls = rail_ns['GoogleRail']
    rail = object.__new__(cls)
    monkeypatch.setattr(rail, '_headers', lambda: {})
    requests = []

    def http(url, **kw):
        requests.append((url, kw['body']))
        return {'rows': []}

    monkeypatch.setitem(cls.gsc_search_analytics.__globals__, '_http', http)
    common = dict(start_date='2026-09-01', end_date='2026-09-07')
    for dims in (None, [], ['page']):
        assert rail.gsc_search_analytics(**common, dimensions=dims)['ok']
    assert [body['dimensions'] for _, body in requests] == [['query'], [], ['page']]
    assert not {'type', 'dataState', 'aggregationType'} & requests[0][1].keys()
    assert rail.gsc_search_analytics(**common, dimensions=[], property_uri='https://glpclinicscout.com/',
                                     search_type='web', data_state='final', aggregation_type='byProperty')['ok']
    url, body = requests[-1]
    assert 'https%3A%2F%2Fglpclinicscout.com%2F' in url
    assert (body['dimensions'], body['type'], body['dataState'], body['aggregationType']) == ([], 'web', 'final', 'byProperty')


@pytest.mark.parametrize('mode', ['complete', 'missing_day', 'missing_row', 'missing_metric', 'transport', 'unexpected_query', 'zero'])
def test_registered_collector_to_report(tmp_path, monkeypatch, capsys, mode, rail_case=None):
    # Fail if an unmocked network boundary is reached, including sends.
    import urllib.request
    monkeypatch.setattr(urllib.request, 'urlopen', lambda *a, **k: pytest.fail('network forbidden'))
    ns = load_report()
    ledger = tmp_path / 'ledger.json'
    ledger.write_text(json.dumps({'routes': {'/guides/example/': {'seed': True}}}))
    ns.update(BUSINESS=ROOT, LEDGER=ledger, R2_ROOT=tmp_path / 'no-r2', REPORT_ROOT=tmp_path / 'reports')
    requests = []
    values = dict(clicks=2, impressions=20, ctr=.1, position=12)

    rail_class = runpy.run_path(str(RAILS))['GoogleRail']
    class Google:
        ga4_neutral_filter = rail_class.ga4_neutral_filter
        GA4_CONSUMER_STREAM_ID = rail_class.GA4_CONSUMER_STREAM_ID
        GA4_NEUTRAL_PATHS = rail_class.GA4_NEUTRAL_PATHS
        def _access_token(self):
            return None

        def gsc_url_inspection(self, url):
            return {'ok': True, 'url': url, 'verdict': 'PASS'}

        def ga4_run_report(self, **kw):
            assert kw['dimensions'] == ['streamId', 'hostName', 'pagePath']
            assert kw['dimension_filter'] == rail_class.ga4_neutral_filter()
            return {'ok': True, 'complete': True, 'rows': [],
                    'dimension_names': kw['dimensions'], 'metric_names': kw['metrics']}

        def gsc_search_analytics(self, **kw):
            requests.append(kw)
            dims = kw['dimensions']
            if dims in (['page'], ['query', 'page']):
                keys = ['https://glpclinicscout.com/guides/example/']
                if dims == ['query', 'page']:
                    keys.insert(0, 'PRIVATE_QUERY_SENTINEL')
                return {'ok': True, 'complete': True, 'rows': [{'keys': keys, **values}]}
            assert kw['search_type'] == 'web' and kw['data_state'] == 'final'
            assert kw['aggregation_type'] == 'byProperty'
            assert (kw['start_date'], kw['end_date']) == ('2026-09-18', '2026-09-24')
            consumer = kw['property_uri'] == 'https://glpclinicscout.com/'
            if consumer and mode == 'transport':
                raise RuntimeError('PRIVATE_QUERY_SENTINEL')
            if dims == ['date']:
                rows = [{'keys': [str(dt.date(2026, 9, 18) + dt.timedelta(days=n))], **values} for n in range(7)]
                if consumer and mode == 'missing_day':
                    rows.pop()
            else:
                assert dims == []
                rows = [{'keys': [], **values, 'impressions': 140 if consumer else 210}]
                if consumer and mode == 'missing_row':
                    rows = []
                elif consumer and mode == 'missing_metric':
                    rows[0].pop('clicks')
                elif consumer and mode == 'unexpected_query':
                    rows[0]['keys'] = ['PRIVATE_QUERY_SENTINEL']
                elif consumer and mode == 'zero':
                    rows[0].update(clicks=0, impressions=0, ctr=0, position=0)
            if rail_case is not None:
                if consumer and dims == []:
                    present, keys = rail_case
                    if present:
                        rows[0]['keys'] = keys
                    else:
                        rows[0].pop('keys')
                cls = runpy.run_path(str(RAILS))['GoogleRail']
                rail = object.__new__(cls)
                monkeypatch.setattr(rail, '_headers', lambda: {})

                def http(url, **request):
                    body = request['body']
                    assert body['dimensions'] == dims
                    assert (body['startDate'], body['endDate']) == ('2026-09-18', '2026-09-24')
                    assert (body['type'], body['dataState'], body['aggregationType']) == ('web', 'final', 'byProperty')
                    return {'rows': rows}

                monkeypatch.setitem(cls.gsc_search_analytics.__globals__, '_http', http)
                return rail.gsc_search_analytics(**kw)
            return {'ok': True, 'complete': True, 'rows': rows}

    ns.update(GoogleRail=Google, PostHog=lambda: types.SimpleNamespace(neutral_pageviews=lambda **kw: {'rows': []}),
              connect=lambda **kw: types.SimpleNamespace(call=lambda *a: []),
              TOOL={'bing_query_stats': 'query', 'bing_page_stats': 'page'})
    # Existing shared input path, actually hashed and consumed by the reporter.
    footprint = tmp_path / 'footprint.json'
    footprint.write_text(json.dumps({'captured_at_utc': '2026-09-27T12:00:00Z',
                                     'result': {'ok': False, 'state': 'UNKNOWN_METERED_COLLECTION_HELD'}}))
    module_input = tmp_path / 'MODULE-INPUTS.json'
    module_input.write_text(json.dumps({'inputs': {'footprint': {'path': str(footprint),
                                     'sha256': hashlib.sha256(footprint.read_bytes()).hexdigest()}}}))
    assert ns['main'](['--report-date', '2026-09-27', '--module-input', str(module_input)]) == 0
    summary = json.loads(capsys.readouterr().out)
    report_path = Path(summary['report'])
    report = json.loads(report_path.read_text())
    assert summary['report_sha256'] == hashlib.sha256(report_path.read_bytes()).hexdigest()
    assert summary['discord_status'] == 'not_requested'
    aggregate = report['google_sitewide_aggregate']
    binding = aggregate.pop('receipt')
    raw = Path(binding['path']).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == binding['sha256']
    assert json.loads(raw) == aggregate
    assert binding in report['evidence']
    assert b'PRIVATE_QUERY_SENTINEL' not in raw
    consumer = aggregate['scopes']['consumer_https_apex']
    domain = aggregate['scopes']['domain_including_partners']
    assert consumer['context']['property'] == 'https://glpclinicscout.com/'
    assert domain['context']['property'] == 'sc-domain:glpclinicscout.com'
    assert domain['totals']['impressions'] == 210
    assert domain['state'] == 'SOURCE_REPORTED'
    for scope in (consumer, domain):
        assert scope['captured_at_utc'] and scope['request_started_at_utc']
        assert scope['context']['dimensions'] == []
    if mode in ('complete', 'zero'):
        assert consumer['state'] == 'SOURCE_REPORTED'
        assert consumer['totals']['impressions'] == (140 if mode == 'complete' else 0)
    else:
        assert consumer['state'].startswith('UNKNOWN')
        assert all(v is None for v in consumer['totals'].values())
    if mode == 'missing_day':
        assert consumer['missing_dates'] == ['2026-09-24']
        assert consumer['date_availability'][-1]['state'] == 'UNKNOWN'
        assert consumer['date_availability'][-1]['metrics']['clicks'] is None
    # Original article subset remains independent of both property denominators.
    assert report['headline']['google_clicks_latest_complete_week_indexed_pages']['value'] == 2
    assert report['google_ranking']['all_ledger_articles_current']['impressions'] == 20
    assert report['google_ranking']['indexed_pages_current']['impressions'] == 20
    assert len([r for r in requests if r['dimensions'] == []]) == (1 if mode == 'transport' else 2)
    if rail_case is not None:
        assert b'"keys"' not in raw and b'"error"' not in raw
        assert b'invalid_gsc_keys_shape' not in raw


@pytest.mark.parametrize('present,keys,mode', [
    pytest.param(True, False, 'malformed_keys', id='false'),
    pytest.param(True, 0, 'malformed_keys', id='zero-key'),
    pytest.param(True, '', 'malformed_keys', id='empty-string'),
    pytest.param(True, {}, 'malformed_keys', id='empty-object'),
    pytest.param(False, None, 'complete', id='omitted'),
    pytest.param(True, [], 'complete', id='empty-list'),
    pytest.param(True, [], 'zero', id='numeric-zero'),
])
def test_aggregate_keys_rail_to_persisted_receipt(tmp_path, monkeypatch, capsys, present, keys, mode):
    # Reuse the unchanged collector/main persistence and article-metric assertions,
    # but route aggregate and date requests through the actual rail with stubbed HTTP.
    test_registered_collector_to_report(tmp_path, monkeypatch, capsys, mode,
                                        rail_case=(present, keys))
