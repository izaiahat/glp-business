import importlib.util
import json
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import firecrawl_client as fc


def test_topic_supply_retains_current_minter_40_query_plan_with_six_empty_results(monkeypatch, tmp_path, capsys):
    import copy
    import socket
    import datetime as dt
    script_dir = ROOT / 'systems/seo-blog-machine/scripts'
    monkeypatch.syspath_prepend(str(script_dir))
    spec = importlib.util.spec_from_file_location('topic_supply_query_plan_fixture', script_dir / 'refresh_topic_supply_firecrawl.py')
    assert spec is not None and spec.loader is not None
    supply = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(supply)
    monkeypatch.setattr(socket.socket, 'connect', lambda *a, **kw: pytest.fail('offline regression attempted transport'))
    feed = {'research_backlog': [
        {'keyword': f'fixture-{i:03}', 'target_route': f'/guides/fixture-{i:03}/',
         'reason': 'HOLD_INSUFFICIENT_MEASURED_EVIDENCE'} for i in range(41)
    ] + [
        {'keyword': 'Fella Health', 'target_route': '/guides/fella/', 'reason': 'excluded_provider'},
        {'keyword': 'blocked-template', 'target_route': '/guides/blocked/', 'reason': 'HOLD_TEMPLATE_UNAVAILABLE'},
        {'target_query': 'zz-legacy', 'target_route': '/guides/legacy/', 'reason': 'insufficient_measured_competitor_evidence'},
    ]}
    before = copy.deepcopy(feed)
    intended = {f'fixture-{i:03}': f'/guides/fixture-{i:03}/' for i in range(40)}
    assert supply.target_map(feed, None) == intended
    assert supply.target_map({'research_backlog': feed['research_backlog'][-1:]}, None) == {'zz-legacy': '/guides/legacy/'}
    feed_path, output, page_two = (tmp_path / name for name in ('feed.json', 'output.json', 'page-two.json'))
    feed_path.write_text(json.dumps(feed))
    monkeypatch.setattr(supply, 'api_key', lambda: 'fixture-only')
    calls = []
    def offline_urlopen(request, **kwargs):
        import io
        calls.append(json.loads(request.data))
        rows = [] if len(calls) <= 6 else [{'url': 'https://example.invalid/source', 'title': 'Fixture', 'markdown': 'Reported comparison information.'}]
        return io.BytesIO(json.dumps({'success': True, 'id': f'fixture-only-{len(calls)}', 'data': {'web': rows}}).encode())
    monkeypatch.setattr(fc, 'firecrawl_urlopen', offline_urlopen)
    monkeypatch.setattr(sys, 'argv', ['refresh_topic_supply_firecrawl.py', '--feed', str(feed_path), '--output', str(output), '--page-two-report', str(page_two)])
    assert supply.main() == 0
    first = json.loads(capsys.readouterr().out)
    pack = json.loads(output.read_text())
    assert first['provider_calls'] == 40
    assert pack['query_plan'] == [{'target_query': q, 'target_route': r} for q, r in intended.items()]
    assert len(pack['targets']) == 34
    assert len(pack['failures']) == 6
    assert all(row['reason'] == 'no_web_results' for row in pack['failures'])
    assert len(json.loads(page_two.read_text())['targets']) == 34
    assert len(calls) == 40
    assert all(body['country'] == 'US' and body['location'] == 'United States' and body['tbs'] == 'qdr:w'
               and body['limit'] == 3 and body['scrapeOptions'] == {'formats': ['markdown'], 'onlyMainContent': True, 'proxy': 'basic'} for body in calls)
    assert feed == before  # Research selection never promotes a HOLD.
    assert supply.target_map({}, pack) == intended
    assert supply.prior_is_fresh(pack, intended)
    # Real older packs retained empty responses separately, without query_plan.
    legacy = copy.deepcopy(pack)
    legacy.pop('query_plan')
    assert supply.prior_is_fresh(legacy, intended)
    unknown = copy.deepcopy(legacy)
    unknown['failures'][0]['reason'] = 'RuntimeError'
    assert not supply.prior_is_fresh(unknown, intended)
    stale = copy.deepcopy(pack)
    stale['captured_at_utc'] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=8)).strftime('%Y-%m-%dT%H:%M:%SZ')
    assert not supply.prior_is_fresh(stale, intended)
    assert not supply.prior_is_fresh(pack, {**intended, 'changed-query': '/guides/changed/'})
    saved = output.read_bytes(), page_two.read_bytes()
    assert supply.main() == 0
    second = json.loads(capsys.readouterr().out)
    assert second['status'] == 'PASS_FRESH_REUSE' and second['targets'] == 40 and second['provider_calls'] == 0
    assert len(calls) == 40
    assert (output.read_bytes(), page_two.read_bytes()) == saved


def test_grouped_v2_search_fixture():
    payload = {'success': True, 'id': 'search-id', 'data': {'web': [{'url': 'https://a', 'title': 'A'}, {'title': 'no URL'}], 'news': [{'url': 'https://b'}], 'images': [{'url': 'https://image'}]}}
    assert [r['url'] for r in fc.search_rows(payload)] == ['https://a', 'https://b']
    with pytest.raises(ValueError):
        fc.search_rows({'data': [{'url': 'v1'}]})
    with pytest.raises(ValueError):
        fc.search_rows({'success': False, 'data': payload['data']})


def test_concurrent_secret_free_ledger(tmp_path):
    path = tmp_path / 'ops.jsonl'
    body = {'url': 'https://example.com/?token=secret', 'query': 'person@email.test', 'headers': {'Authorization': 'Bearer secret'}, 'formats': [{'type': 'json', 'prompt': 'sensitive'}], 'alexandria': [{'options': {'email': 'person@email.test'}}]}
    def write(i):
        return fc.record_operation('fixture', '/v2/scrape', body, payload={'data': {'metadata': {'scrapeId': 'safe-id', 'creditsUsed': 5}}}, outcome='success', ledger_path=path, run_id='run', operation_id=str(i))
    with ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(write, range(40)))
    text = path.read_text()
    rows = [json.loads(line) for line in text.splitlines()]
    assert len(rows) == 40 and len({r['operation_id'] for r in rows}) == 40
    assert all(r['creditsUsed'] == 5 and r['scrape_search_id'] == 'sha256:' + fc._hash('safe-id') for r in rows)
    assert 'secret' not in text and 'person@' not in text and 'sensitive' not in text
    assert path.stat().st_mode & 0o777 == 0o600
    assert fc.record_operation('test', '/v2/search', outcome='success', ledger_path=path)['creditsUsed'] is None


def test_no_v1_maintained_callers():
    for rel in ['scripts/affiliate_supply_discovery.py', 'scripts/glp_reddit_daily_scout.py', 'scripts/glp_price_verification.py', 'scripts/research_glp_batch2_fda_warning_provenance.py', 'scripts/g6_r1a_stage5_research_fetch.py', 'systems/seo-blog-machine/scripts/competitor_gap.py', 'tools/glp/evidence_refresh.py']:
        assert 'api.firecrawl.dev/v1' not in (ROOT / rel).read_text(), rel


def test_no_hardcoded_enhanced_in_maintained_producers():
    import ast
    manifest = json.loads((ROOT / 'ops/rebuild-source-manifest.json').read_text())
    for rel in manifest['files']:
        if not rel.endswith('.py') or not rel.startswith(('scripts/', 'systems/', 'tools/', 'glp-clinic-partner-outreach-machine/')):
            continue
        tree = ast.parse((ROOT / rel).read_text())
        forced_proxy = any(isinstance(node, ast.Dict) and any(
            isinstance(key, ast.Constant) and key.value == 'proxy' and
            isinstance(value, ast.Constant) and value.value == 'enhanced'
            for key, value in zip(node.keys, node.values)) for node in ast.walk(tree))
        forced_fetch_arg = any(isinstance(node, ast.Call) and any(
            isinstance(arg, ast.Constant) and arg.value == 'enhanced' for arg in node.args)
            and isinstance(node.func, ast.Name) and node.func.id == 'firecrawl_fetch'
            for node in ast.walk(tree))
        assert not (forced_proxy or forced_fetch_arg), rel


def test_cleanup_maintained_price_and_sponsor():
    price = (ROOT / 'scripts/glp_price_verification.py').read_text()
    sponsor = (ROOT / 'glp-clinic-partner-outreach-machine/refill_email_ready_targets.py').read_text()
    assert '/v1/' not in price and 'proxy: str = \'basic\'' in price
    assert '"proxy": "enhanced"' not in price
    assert 'LedgerSDKClient(Firecrawl(api_key=api_key)' in sponsor


def test_client_refuses_v1_and_uncapped_agent(tmp_path):
    client = fc.FirecrawlClient('test', api_key='not-used', ledger_path=tmp_path/'ledger')
    with pytest.raises(ValueError):
        client.request('https://api.firecrawl.dev/v1/search', {})
    with pytest.raises(ValueError):
        client.request('agent', {'prompt': 'test'})
    with pytest.raises(ValueError):
        client.request('https://api.firecrawl.dev/v2/agent', {'prompt': 'test'})
    assert not (tmp_path/'ledger').exists()


def test_sdk_snake_case_credit_and_scrape_id():
    assert fc._receipt({'data':{'metadata':{'scrape_id':'sdk-id','credits_used':1}}})==('sha256:' + fc._hash('sdk-id'),1)


def test_onboarding_json_schema_is_source_only():
    source = (ROOT / 'scripts/g6_r1a_stage5_research_fetch.py').read_text()
    assert '"required": ["price", "eligibility", "states"]' in source
    assert '"ledger_operation_id": getattr(response, "firecrawl_operation_id", None)' in source
    assert 'EXTRACTED_REQUIRES_SOURCE_REVIEW_NOT_REGISTRY_WRITE' in source


def test_sdk_proxy_preserves_typed_v2_result(monkeypatch,tmp_path):
    from types import SimpleNamespace
    monkeypatch.setattr(fc,'LEDGER',tmp_path/'sdk.jsonl')
    typed=SimpleNamespace(web=[],model_dump=lambda **kw: {'success':True,'creditsUsed':2,'data':{'web':[]}})
    client=fc.LedgerSDKClient(SimpleNamespace(search=lambda query,**kw:typed),'sdk-fixture')
    assert client.search('private query',limit=1) is typed
    rows=[json.loads(s) for s in fc.LEDGER.read_text().splitlines()]
    assert rows[-1]['creditsUsed']==2
    assert [r['outcome'] for r in rows]==['started','success']
    assert 'private query' not in fc.LEDGER.read_text()


@pytest.mark.parametrize('basic_ok,expected',[(True,['basic']),(False,['basic','auto'])])
def test_price_proxy_sequence(monkeypatch,basic_ok,expected):
    import glp_price_verification as price
    from types import SimpleNamespace
    calls=[]
    def post(url,**kwargs):
        proxy=kwargs['json']['proxy'];calls.append(proxy)
        text='First party offer $100. '*50 if basic_ok or proxy=='auto' else ''
        return SimpleNamespace(status_code=200,content=text.encode(),json=lambda:{'success':True,'data':{'markdown':text,'metadata':{'statusCode':200}}})
    monkeypatch.setattr(fc,'firecrawl_post',post)
    def requests_unavailable(url):
        raise OSError('fixture transport unavailable')
    monkeypatch.setattr(price,'requests_fetch',requests_unavailable)
    result=price.fetch_source(None,'https://example.invalid/pricing','fixture','fixture-only')
    assert result['text']
    assert calls==expected


def test_opaque_receipt_ids_are_hashed(tmp_path):
    opaque = 'fc-' + 'fixture' * 8
    row = fc.record_operation('test', '/v2/scrape', payload={'id': opaque},
                              outcome='success', ledger_path=tmp_path/'ops')
    assert opaque not in (tmp_path/'ops').read_text()
    assert row['scrape_search_id'] == 'sha256:' + fc._hash(opaque)


def test_price_ambiguous_basic_effect_stops_fallback(monkeypatch, tmp_path):
    import requests
    import glp_price_verification as price
    calls = []
    monkeypatch.setattr(fc, 'LEDGER', tmp_path/'ops')
    def post(*args, **kwargs):
        calls.append(kwargs['json']['proxy'])
        raise requests.Timeout('offline ambiguous effect')
    monkeypatch.setattr(requests, 'post', post)
    monkeypatch.setattr(price, 'requests_fetch', lambda u: (_ for _ in ()).throw(OSError('offline')))
    result = price.fetch_source(None, 'https://example.invalid/pricing', 'fixture', 'fixture-only')
    assert calls == ['basic']
    assert result['text'] == ''
    assert result['http_status'] is None


@pytest.mark.parametrize('data', [None, 'missing', [], {}, {'web': None}, {'web': {}}, {'news': []}])
def test_actual_affiliate_caller_refuses_invalid_grouped_search(monkeypatch, tmp_path, data):
    import io
    import affiliate_supply_discovery as supply
    import glp_price_verification as price
    payload = {'success': True}
    if data != 'missing':
        payload['data'] = data
    monkeypatch.setattr(price, 'env_value', lambda name: 'fixture-only')
    monkeypatch.setattr(fc, 'LEDGER', tmp_path/'ops')
    monkeypatch.setattr(fc.urllib.request, 'urlopen', lambda *a, **kw: io.BytesIO(json.dumps(payload).encode()))
    with pytest.raises((ValueError, RuntimeError)):
        supply.firecrawl_search()('fixture query')
    assert fc.search_rows({'success': True, 'data': {'web': []}}, sources=('web',)) == []


@pytest.mark.parametrize('status', [404, 403, 500])
def test_price_v2_source_status_not_provider_http_status(monkeypatch, status):
    import glp_price_verification as price
    from types import SimpleNamespace
    payload = {'success': True, 'data': {'markdown': 'Error page is not price evidence. ' * 20,
               'metadata': {'statusCode': status, 'title': 'Page Not Found'}}}
    monkeypatch.setattr(fc, 'firecrawl_post', lambda *a, **kw: SimpleNamespace(
        status_code=200, content=json.dumps(payload).encode(), json=lambda: payload))
    result = price.firecrawl_fetch('https://example.invalid/pricing', 'fixture-only')
    assert result['http_status'] == status
    assert result['text'] == ''


def test_stage5_v2_404_body_cannot_be_onboarding_evidence(monkeypatch):
    path = ROOT / 'scripts/g6_r1a_stage5_research_fetch.py'
    spec = importlib.util.spec_from_file_location('stage5_v2_source_status_fixture', path)
    stage = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(stage)
    from types import SimpleNamespace
    payload = {'success': True, 'data': {'markdown': 'The page you are looking for is not available. ' * 30,
               'metadata': {'statusCode': 404}, 'json': {}}}
    monkeypatch.setattr(fc, 'firecrawl_post', lambda *a, **kw: SimpleNamespace(
        status_code=200, content=json.dumps(payload).encode(), text=json.dumps(payload), headers={}, json=lambda: payload))
    result = stage.scrape('https://example.invalid/pricing', 'fixture-only', attempts=1)
    assert result['status'] == 'not_found'
    assert result['source_http_status'] == 404
    assert 'onboarding_fields' not in result


def test_alexandria_reported_cost_and_sdk_scrape_id_are_accounted():
    ident, credits = fc._receipt({'scrape_id': 'provider-id', 'data': {
        'alexandria': [{'creditsCost': 5}, {'credits_cost': 2}]}})
    assert credits == 7
    assert ident == 'sha256:' + fc._hash('provider-id')
    assert fc._receipt({'creditsUsed': 3, 'data': {'alexandria': [{'creditsCost': 9}]}})[1] == 3
    for cost in (None, True, -1, '5'):
        assert fc._receipt({'data': {'alexandria': [{'creditsCost': cost}]}})[1] is None
    assert fc._receipt({'data': {'alexandria': []}})[1] is None


@pytest.mark.parametrize('raw,expected', [
    (b'{"success":false,"creditsUsed":1,"id":"private-receipt","error":"secret error"}', 1),
    (b'{"success":false}', None),
    (b'not JSON', None),
])
def test_http_error_retains_billing_without_consuming_caller_body(monkeypatch, tmp_path, raw, expected):
    import io
    import urllib.error
    error = urllib.error.HTTPError('https://api.firecrawl.dev/v2/scrape', 500,
                                  'engines', {}, io.BytesIO(raw))
    def refuse(*a, **kw):
        raise error
    monkeypatch.setattr(fc.urllib.request, 'urlopen', refuse)
    ledger = tmp_path / 'ops'
    client = fc.FirecrawlClient('fixture', api_key='unused', ledger_path=ledger)
    with pytest.raises(urllib.error.HTTPError) as caught:
        client.scrape({'url': 'https://clinic.example/pricing'})
    assert caught.value.code == 500
    assert caught.value.read() == raw
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert [row['outcome'] for row in rows] == ['started', 'http_error']
    assert rows[-1]['http_status'] == 500
    assert rows[-1]['creditsUsed'] == expected
    assert caught.value.firecrawl_operation == rows[-1]
    assert 'private-receipt' not in ledger.read_text()
    assert 'secret error' not in ledger.read_text()
