"""Three real weekly adapter defects, exercised offline with provider I/O denied."""
import ast
import datetime as dt
import hashlib
import json
from pathlib import Path
import runpy
import sys
import types

import pytest
from test_glp44_weekly_gsc_aggregate import test_registered_collector_to_report as ranking_fixture

ROOT = Path(__file__).resolve().parents[1]
HOST = 'glpclinicscout.com'
STREAM = '15060837778'
GA_PATHS = ['/', '/about/', '/privacy/', '/privacy/cookies/', '/terms/']
PH_PATHS = GA_PATHS + ['/providers/', '/compare/']


def load_without_host_imports(rel, **bindings):
    tree = ast.parse((ROOT / rel).read_text())
    tree.body = [n for n in tree.body if not (
        isinstance(n, ast.ImportFrom) and (n.module or '').startswith('ops.')
        or isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
        and ast.unparse(n.value.func) == 'sys.path.insert'
        or isinstance(n, ast.If) and '__name__' in ast.unparse(n.test))]
    ns = {'__name__': 'offline_weekly', '__file__':str(ROOT/rel), **bindings}
    exec(compile(tree, str(ROOT / rel), 'exec'), ns)
    return ns


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    import urllib.request
    monkeypatch.setattr(urllib.request, 'urlopen', lambda *a, **kw: pytest.fail('network forbidden'))


def test_weekly_source_stage_requests_bounded_posthog(tmp_path, monkeypatch):
    rails = runpy.run_path(str(ROOT / 'ops/glp/seo_data_rails.py'))
    ph = object.__new__(rails['PostHog'])
    queries = []
    def hogql(query):
        queries.append(query)
        assert "properties.$host = 'glpclinicscout.com'" in query
        assert 'properties.$pathname IN' in query
        assert all(repr(p) in query for p in PH_PATHS)
        assert "ifNull(toString(properties.is_test), '') NOT IN ('true', '1')" in query
        assert not any(s in query for s in ('$current_url', 'person', 'distinct_id', 'affiliate_click', 'campaign'))
        assert '2026-09-27 23:41:00' in query
        assert '2026-10-07 00:00:00' in query
        return {'ok': True, 'columns': ['path', 'views'], 'rows': [['/', 7]]}
    ph.hogql = hogql
    ns = load_without_host_imports('scripts/glp_weekly_seo_collect.py', CognyClient=object,
                                  DataForSEO=object, GoogleRail=lambda: object(),
                                  PostHog=lambda: ph)
    base = tmp_path / 'business'
    release = base / 'release'
    (release / 'data').mkdir(parents=True)
    (release / 'src').mkdir()
    def put(path, doc):
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = (json.dumps(doc) + '\n').encode()
        path.write_bytes(raw)
        return {'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest()}
    authority = put(base / 'authority.json', {})
    seo = base / 'reports/glp-clinic-scout/seo-machine'
    put(seo / 'current-content-base.json', {'release_root': str(release), 'deployment_id': 'fixture',
                                           'manifest': authority, 'final_receipt': authority})
    for name in ('program-registry', 'program-content-ledger', 'provider-pricing-matrix', 'provider-page-contracts', 'page-seo-copy'):
        put(release / 'data' / (name + '.json'), {'programs': []})
    put(seo / 'keyword-gap/current-topic-supply.json', {})
    class Cogny:
        def __init__(self, store): pass
        def initialize(self): pass
    def collect(client, store, date):
        snap = store.save('cogny.json', {'result': {'rows': [], 'complete': True}})
        return {'domain_property_available': True, 'binding': snap, 'queries': [],
                'snapshots': {k: snap for k in ('ga4', 'gsc-28', 'gsc-90', 'gsc-prior-28')}}
    stub = types.ModuleType('ops.glp.seo_weekly_cogny')
    stub.collect = collect
    stub.read_binding = lambda b: json.loads(Path(b['path']).read_text())
    monkeypatch.setitem(sys.modules, stub.__name__, stub)
    estate=types.ModuleType('ops.glp.weekly_strategy_inputs')
    estate.estate_snapshot=runpy.run_path(str(ROOT/'ops/glp/weekly_strategy_inputs.py'))['estate_snapshot']
    monkeypatch.setitem(sys.modules,estate.__name__,estate)
    ns.update(B=base, RecordedCogny=Cogny, capture_page=lambda route, store: (route, {}, None))
    result = ns['run'](tmp_path / 'collected', report_date=dt.date(2026, 10, 7))
    inputs = json.loads(Path(result['manifest']['path']).read_text())['inputs']
    binding = inputs['posthog']
    raw = Path(binding['path']).read_bytes()
    snapshot = json.loads(raw)
    assert snapshot['result']['ok'] is True, snapshot
    assert hashlib.sha256(raw).hexdigest() == binding['sha256']
    assert len(queries) == 1
    assert snapshot['result']['complete'] is True
    assert snapshot['result']['rows'] == [['/', 7]]
    assert snapshot['result']['total_views'] == 7
    assert snapshot['context']['article_measurement'] == 'UNMEASURED_BY_DESIGN'
    assert snapshot['result']['affiliate_clicks'] is None
    assert snapshot['context']['requested'] is True
    assert snapshot['request_started_at_utc'] <= snapshot['captured_at_utc']
    assert json.loads(Path(inputs['estate']['path']).read_text())['result']['ok'] is False
    assert result['actual_cost_usd'] == 0
    # Empty complete neutral evidence can be zero. Missing schema, unknown paths,
    # malformed counts or duplicate groups remain unknown, never article zeros.
    for response, expected in (
        ({'ok': True, 'columns': ['path', 'views'], 'rows': []}, 0),
        ({'ok': False, 'rows': []}, None),
        ({'ok': True, 'rows': []}, None),
        ({'ok': True, 'columns': ['path', 'views'], 'rows': [['/reviews/example/', 2]]}, None),
        ({'ok': True, 'columns': ['path', 'views'], 'rows': [[None, 2]]}, None),
        ({'ok': True, 'columns': ['path', 'views'], 'rows': [['/', None]]}, None),
        ({'ok': True, 'columns': ['path', 'views'], 'rows': [['/', 1], ['/', 2]]}, None),
    ):
        ph.hogql = lambda query: response
        observed = ph.neutral_pageviews(start_date='2026-09-23', end_date='2026-10-07')
        assert observed['total_views'] == expected
        assert observed['complete'] is (expected is not None)
        assert observed['affiliate_clicks'] is None


@pytest.mark.parametrize('response,expected', [
    # Exact independent-review counterexamples plus schema-complete controls.
    ({'columns': ['path', 'views'], 'results': []}, 0),
    ({'columns': ['path', 'views']}, None),
    ({'columns': ['path', 'views'], 'results': None}, None),
    ({'columns': ['path', 'views'], 'results': False}, None),
    ({'columns': ['path', 'views'], 'results': 0}, None),
    ({'columns': ['path', 'views'], 'results': ''}, None),
    ({'columns': ['path', 'views'], 'results': {}}, None),
    ({'columns': ['path', 'views'], 'results': 'malformed'}, None),
    (None, None),
    ([], None),
    ({'results': []}, None),
    ({'columns': None, 'results': []}, None),
    ({'columns': ['path', 'views'], 'results': [['/', 7]]}, 7),
])
def test_neutral_posthog_raw_http_schema(response, expected, monkeypatch):
    """Real hogql -> neutral_pageviews; only raw HTTP is stubbed, no credentials."""
    tree = ast.parse((ROOT / 'ops/glp/seo_data_rails.py').read_bytes())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'PostHog')
    calls = []
    def http(url, **kwargs):
        calls.append((url, kwargs))
        return response
    ns = {'dt': dt, 'Any': object, '_http': http,
          'POSTHOG_PROJECT': 'offline-fixture',
          'POSTHOG_QUERY_URL': 'https://invalid.example/never-requested'}
    exec(compile(ast.Module(body=[cls], type_ignores=[]), '<exact PostHog>', 'exec'), ns)
    ph = object.__new__(ns['PostHog'])
    ph._key = 'inert-not-a-credential'
    observed = ph.neutral_pageviews(start_date='2026-10-01', end_date='2026-10-07')
    assert len(calls) == 1
    assert calls[0][1]['body']['query']['kind'] == 'HogQLQuery'
    assert observed['total_views'] == expected
    assert observed['ok'] is (expected is not None)
    assert observed['complete'] is (expected is not None)
    assert observed['state'] == ('SOURCE_REPORTED' if expected is not None else 'UNKNOWN')
    assert observed['affiliate_clicks'] is None
    assert observed['context']['article_measurement'] == 'UNMEASURED_BY_DESIGN'
    assert observed['rows'] == ([['/', 7]] if expected == 7 else [])


def test_weekly_ga4_constrains_stream_and_neutral_routes(tmp_path):
    rails = runpy.run_path(str(ROOT / 'ops/glp/seo_data_rails.py'))
    tools = {k: k for k in ('gsc_list_properties', 'gsc_search_analytics', 'ga4_run_report', 'bing_query_stats', 'bing_page_stats')}
    ns = load_without_host_imports('ops/glp/seo_weekly_cogny.py', TOOL=tools, GoogleRail=rails['GoogleRail'])
    class Store:
        def __init__(self): self.n = 0
        def save(self, name, doc):
            path = tmp_path / f'{self.n}.json'; self.n += 1
            raw = json.dumps(doc).encode(); path.write_bytes(raw)
            return {'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest()}
    calls = []
    class Client:
        bad = None
        def call(self, tool, arguments):
            if tool == 'gsc_list_properties': return [{'siteUrl': ns['PREFIX']}]
            if tool == 'gsc_search_analytics': return {'rows': []}
            if tool != 'ga4_run_report': return []
            calls.append(arguments)
            row = {'streamId': STREAM, 'hostName': HOST, 'pagePath': '/', 'screenPageViews': 7, 'sessions': 3}
            if self.bad: row.update(self.bad)
            return {'rows': [row], 'rowCount': 1}
    client = Client()
    for mutation in (None, {'streamId': '15224515823'}, {'pagePath': '/reviews/example/'},
                     {'hostName': '127.0.0.1'}, {'screenPageViews': None}):
        client.bad = mutation
        result = ns['collect'](client, Store(), dt.date(2026, 10, 7))
        snap = ns['read_binding'](result['snapshots']['ga4'])
        args = calls[-1]
        assert args['dimensions'] == ['streamId', 'hostName', 'pagePath']
        filters = args['dimension_filter']['andGroup']['expressions']
        assert {f['filter']['fieldName'] for f in filters} == {'streamId', 'hostName', 'pagePath'}
        fields = {f['filter']['fieldName']: f['filter'] for f in filters}
        assert fields['streamId']['stringFilter']['value'] == STREAM
        assert fields['hostName']['stringFilter']['value'] == HOST
        assert fields['pagePath']['inListFilter']['values'] == GA_PATHS
        assert all(next(v for k, v in f.items() if k != 'fieldName')['caseSensitive'] for f in fields.values())
        assert snap['context']['consumer_stream_id'] == STREAM
        assert snap['result']['complete'] is (mutation is None)
        if mutation:
            assert snap['result']['rows'] == []
        else:
            assert snap['result']['rows'] == [{'dimensions': [HOST, '/'], 'metrics': [7.0, 3.0]}]
        assert result['domain_property_available'] is False


def test_article_and_click_measurements_remain_null(tmp_path, monkeypatch, capsys):
    ranking_fixture(tmp_path, monkeypatch, capsys, 'complete')
    report = json.loads((tmp_path / 'reports/2026-09-27.json').read_text())
    for name, value in report['downstream_corroboration'].items():
        if name.endswith(('sessions', 'pageviews', 'views', 'clicks')):
            assert value['value'] is None, (name, value)
            assert value['classification'] == 'UNMEASURED_BY_DESIGN'
    assert report['neutral_analytics']['ga4']['complete'] is True
    assert report['neutral_analytics']['ga4']['rows'] == []
    families = report['page_family_analysis']['families']
    for family in families.values():
        for source in ('ga4', 'posthog'):
            assert family[source]['classification'] == 'UNMEASURED_BY_DESIGN'
            for name in ('sessions', 'views', 'affiliate_clicks', 'intent_events', 'page_count', 'affiliate_click_rate'):
                if name in family[source]: assert family[source][name] is None
    hypothesis = report['page_family_analysis']['hypothesis_test']
    assert hypothesis['provider_review_conversion_proven'] is None
