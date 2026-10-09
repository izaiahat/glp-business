import importlib.util
import json
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import firecrawl_client as fc


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


def test_affiliate_map_batch_true_request_cap(monkeypatch):
    import ast, io, re, threading, urllib.request
    from types import SimpleNamespace
    tree = ast.parse((ROOT / 'scripts/affiliate_site_evidence.py').read_text())
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'firecrawl_fetcher')
    namespace = dict(threading=threading, urllib=urllib, json=json, re=re,
                     time=SimpleNamespace(sleep=lambda n: None), normalize_target=lambda u: u)
    exec(compile(ast.Module(body=[function], type_ignores=[]), '<fetcher>', 'exec'), namespace)
    requests = []
    def response(req, **kw):
        requests.append(req.full_url)
        body = {'links': [{'url': 'https://example.org/weight-loss'}]} if req.full_url.endswith('/map') else {'id': 'batch'} if req.full_url.endswith('/scrape') else {'status': 'completed', 'data': [{'markdown': 'Source terms'}]}
        return io.BytesIO(json.dumps(body).encode())
    monkeypatch.setattr(fc, 'firecrawl_urlopen', response)
    fetch = namespace['firecrawl_fetcher'](api_key='fixture', max_requests=3)
    assert fetch('Example', 'https://example.org')[0] == 'Source terms'
    assert fetch('Example', 'https://example.org')[2].endswith(':request_cap')
    assert fetch.request_counts == {'requests': 3, 'cap': 3}
    assert [u.rsplit('/v2/', 1)[1] for u in requests] == ['map', 'batch/scrape', 'batch/scrape/batch']


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


@pytest.mark.parametrize('basic_ok,expected',[(True,['basic']),(False,['basic','enhanced'])])
def test_price_proxy_sequence(monkeypatch,basic_ok,expected):
    import glp_price_verification as price
    from types import SimpleNamespace
    calls=[]
    def post(url,**kwargs):
        proxy=kwargs['json']['proxy'];calls.append(proxy)
        text='First party offer $100. '*50 if basic_ok or proxy=='enhanced' else ''
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
