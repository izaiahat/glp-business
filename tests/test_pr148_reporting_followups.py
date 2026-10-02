"""Provider-free regressions; null fixture mirrors the bounded live shape."""
import pytest
import affiliate_network_access as access
from marketplace_adapters import _base
from glp_api_collectors_smoke import _row_count

START = '2026-09-21T00:00:00+00:00'
END = '2026-09-28T00:00:00+00:00'

class Sequence:
    def __init__(self, *documents):
        self.documents = list(documents)
        self.calls = []
    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return _base.json_response(self.documents.pop(0))

def test_clickbank_null_is_explicit_unknown_not_zero(monkeypatch):
    monkeypatch.setattr(_base, 'secret_text', lambda *p: 'SYNTHETIC')
    t = Sequence(None)
    result = access.conversions_earnings('clickbank', t, start=START, end=END)
    assert result.coverage == 'UNKNOWN'
    assert result.reason == 'HTTP200_NULL_BODY_NO_POPULATION_PROOF'
    assert result.record_count is result.commission_minor is result.cash_minor is None
    assert len(t.calls) == 1
    assert _row_count('clickbank', _base.json_response(None)) is None

def test_admitad_missing_cache_uses_existing_clients_once(monkeypatch):
    def secrets(*parts):
        if parts[-1] == 'access_token':
            raise FileNotFoundError('synthetic absent cache')
        return {'client_id':'SYNTHETIC_ID', 'client_secret':'SYNTHETIC_SECRET'}[parts[-1]]
    monkeypatch.setattr(_base, 'secret_text', secrets)
    t = Sequence({'access_token':'SYNTHETIC_BEARER', 'token_type':'bearer',
                  'scope':'statistics', 'expires_in':604800},
                 {'results':[], '_meta':{'count':0, 'offset':0, 'limit':500}})
    result = access.conversions_earnings('admitad', t, start=START, end=END, max_requests=1)
    assert result.coverage == 'PARTIAL'
    assert len(t.calls) == 2
    assert t.calls[0][0:2] == ('POST', 'https://api.admitad.com/token/')
    assert t.calls[0][2]['data'] == {'grant_type':'client_credentials',
        'client_id':'SYNTHETIC_ID', 'scope':'statistics'}
    assert t.calls[0][2]['auth'] == ('SYNTHETIC_ID','SYNTHETIC_SECRET')
    assert t.calls[1][0] == 'GET'
    assert len(result.receipts) == 1
    assert result.record_count is result.cash_minor is None
    assert 'SYNTHETIC' not in str(result.to_dict())

def test_healthtrader_verified_search_keeps_vendor_period_unknown(monkeypatch):
    monkeypatch.setattr(_base,'secret_json',lambda *p:{'access_token':'SYNTHETIC'})
    t=Sequence({'info':{'total_records':1},'conversions':[{
        'id':1,'offer':'fixture','created':'2026-09-21 08:00:00',
        'commission_amount':'£4.87','currency_base':'GBP','status':'Paid',
        'product_name':'DO_NOT_RETAIN','aref':'DO_NOT_RETAIN'}]})
    result=access.conversions_earnings('healthtrader',t,start=START,end=END,max_requests=1)
    assert result.coverage == 'PARTIAL'
    assert len(t.calls) == 1
    assert t.calls[0][0:2] == ('GET','https://api.healthtrader.com/affiliates/v1/conversions/search')
    assert t.calls[0][2]['params'] == {'date_from':'2026-09-21','date_to':'2026-09-27',
        'page':1,'per_page':50,'sort':'id'}
    assert result.records[0]['classification'] == 'UNKNOWN'
    assert result.records[0]['reported_commission'] is None
    assert result.record_count is result.commission_minor is result.cash_minor is None
    assert 'DO_NOT_RETAIN' not in str(result.to_dict())

@pytest.mark.parametrize('status,payload', [
    (401,{'error':'SYNTHETIC_SECRET'}),
    (200,{'access_token':'SYNTHETIC','token_type':'bearer','expires_in':60,'scope':'manage_advcampaigns'}),
    (200,None),
])
def test_admitad_bad_auth_never_reads_or_retries(monkeypatch,status,payload):
    monkeypatch.setattr(_base,'secret_text',lambda *p:'' if p[-1]=='access_token' else 'SYNTHETIC_SECRET')
    class FailedAuth:
        calls=[]
        def request(self,method,url,**kwargs):
            self.calls.append((method,url))
            return _base.json_response(payload,status)
    t=FailedAuth()
    result=access.conversions_earnings('admitad',t,start=START,end=END,max_requests=1)
    assert result.coverage == 'UNKNOWN'
    assert t.calls == [('POST','https://api.admitad.com/token/')]
    assert not result.receipts
    assert 'SYNTHETIC' not in str(result.to_dict())

@pytest.mark.parametrize('status,payload',[(401,{}),(200,None),(200,{'conversions':[]}),
    (200,{'error':'SYNTHETIC','conversions':[],'info':{'total_records':0}})])
def test_healthtrader_http_or_unknown_schema_is_not_zero(monkeypatch,status,payload):
    monkeypatch.setattr(_base,'secret_json',lambda *p:{'access_token':'SYNTHETIC'})
    class BadResponse:
        calls=0
        def request(self,*args,**kwargs):
            self.calls+=1
            return _base.json_response(payload,status)
    t=BadResponse()
    result=access.conversions_earnings('healthtrader',t,start=START,end=END,max_requests=1)
    assert result.coverage == 'UNKNOWN'
    assert result.record_count is result.commission_minor is result.cash_minor is None
    assert t.calls == 1
    assert 'SYNTHETIC' not in str(result.to_dict())

def test_smoke_auth_and_reporting_budgets_are_separate():
    from glp_api_collectors_smoke import OneCallTransport
    t=OneCallTransport('admitad',Sequence({'access_token':'SYNTHETIC'}, {'results':[]}))
    t.auth_request('POST','https://api.admitad.com/token/')
    t.request('GET','https://api.admitad.com/statistics/actions/')
    assert t.calls == t.auth_calls == 1
    with pytest.raises(ValueError):
        t.auth_request('POST','https://api.admitad.com/token/')
    with pytest.raises(ValueError):
        t.request('GET','https://api.admitad.com/statistics/actions/')
    with pytest.raises(ValueError):
        OneCallTransport('healthtrader',Sequence()).auth_request('POST','https://api.healthtrader.com/oauth/token')

def test_healthtrader_reporting_guard_rejects_non_search_routes():
    t=access.ReadTransport(Sequence())
    for method,url in [('POST','https://api.healthtrader.com/affiliates/v1/conversions/search'),
                       ('GET','http://api.healthtrader.com/affiliates/v1/conversions/search'),
                       ('GET','https://api.healthtrader.com/affiliates/v1/conversions/search/extra'),
                       ('GET','https://api.healthtrader.com/affiliates/v1/link/generate')]:
        with pytest.raises(ValueError):
            t.request(method,url)
    assert not t.receipts
