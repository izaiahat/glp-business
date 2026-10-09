from test_firecrawl_integration import ROOT, fc
import importlib.util
import json
from pathlib import Path

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
