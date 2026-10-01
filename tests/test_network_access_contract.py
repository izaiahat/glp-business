"""Synthetic provider fixtures. Never use host credentials or network."""
import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))


def test_unknown_is_not_zero():
    c = importlib.import_module('affiliate_network_access')
    result = c.conversions_earnings('unbound-network', None,
        start='2026-09-01T00:00:00+00:00', end='2026-09-08T00:00:00+00:00')
    assert result.status == 'UNKNOWN'
    assert result.coverage == 'UNKNOWN'
    assert result.record_count is None
    assert result.commission_minor is None
    assert result.cash_minor is None
    assert result.source and result.fetched_at
    assert result.to_dict()['record_count'] is None


def test_awin_partitions_and_no_customer_data(monkeypatch):
    c = importlib.import_module('affiliate_network_access')
    from marketplace_adapters import awin
    from marketplace_adapters._base import json_response
    monkeypatch.setattr(awin, '_headers', lambda: {'Authorization': 'synthetic'})
    calls = []
    class Transport:
        def request(self, method, url, **kw):
            calls.append((method, url, kw))
            return json_response([])
    r = c.conversions_earnings('awin', Transport(),
        start='2026-07-01T00:00:00+00:00', end='2026-09-08T00:00:00+00:00')
    assert r.status == 'SUPPORTED'
    assert r.coverage == 'COMPLETE' and r.record_count == 0
    assert len(calls) == 3
    assert r.commission_minor is None and r.cash_minor is None
    assert all(x[0] == 'GET' for x in calls)
    assert 'Authorization' not in str(r.to_dict())


import pytest


@pytest.fixture(autouse=True)
def forbid_external_effects(monkeypatch):
    import socket
    from marketplace_adapters import _base
    def forbidden(*args, **kwargs):
        raise BaseException('offline_fixture_attempted_external_effect')
    monkeypatch.setattr(socket,'create_connection',forbidden)
    monkeypatch.setattr(_base.RequestsTransport,'request',forbidden)
    monkeypatch.setattr(_base,'secret_json',forbidden)
    monkeypatch.setattr(_base,'secret_text',forbidden)


@pytest.mark.parametrize('url,method,body', [
    ('https://login.affiliatinetwork.com/affiliates/api/Offers/Campaign/Create','POST',{}),
    ('https://partners.clickdealer.com/affiliates/api/1/offers.asmx/ApplyForOffer','GET',{}),
    ('https://www.awin1.com/cread.php','GET',{}),
    ('https://api.healthtrader.com/oauth/token','POST',{}),
    ('https://commissions.api.cj.com/query','POST',{'query':'mutation { anything }'}),
])
def test_read_transport_blocks_all_effect_routes(url,method,body):
    c=importlib.import_module('affiliate_network_access')
    with pytest.raises(ValueError):
        c.ReadTransport(None).request(method,url,json_body=body)


def test_unknown_result_rejects_numeric_claim():
    c=importlib.import_module('affiliate_network_access')
    with pytest.raises(ValueError):
        c.AccessResult('other','conversions_earnings',record_count=0)


@pytest.mark.parametrize('network,payload,expected', [
    ('impact', {'Actions': [], '@numpages': '1', '@page': '1', '@total': '0'}, 'COMPLETE'),
    ('impact', {'Actions': [], '@numpages': '0', '@page': '1', '@total': '0'}, 'COMPLETE'),
    ('affiliati', {'success': True, 'data': [], 'row_count': 0}, 'COMPLETE'),
    ('partnerize', {'conversions': [], 'count': 0, 'hypermedia': {'pagination': {}}}, 'COMPLETE'),
    ('katalys', {'rows': [], 'rowCount': 0}, 'COMPLETE'),
    ('katalys', {'rows': [], 'rowCount': 1}, 'PARTIAL'),
    ('katalys', {'rows': []}, 'PARTIAL'),
    ('cj', {'data': {'publisherCommissions': {'records': [], 'count': 0, 'payloadComplete': True}}}, 'COMPLETE'),
])
def test_financial_adapter_fixtures(monkeypatch, network, payload, expected):
    c = importlib.import_module('affiliate_network_access')
    from marketplace_adapters import impact, affiliati, partnerize, cj, _base
    monkeypatch.setattr(impact, 'credentials', lambda: ('fixture-account', 'synthetic'))
    monkeypatch.setattr(affiliati, '_credentials', lambda: ('123', 'synthetic'))
    monkeypatch.setattr(partnerize, '_auth', lambda: ('fixture', 'synthetic'))
    monkeypatch.setattr(cj, '_secret', lambda: ('synthetic', '8010186', '101822575'))
    monkeypatch.setattr(_base, 'secret_text', lambda *a: 'synthetic')
    class Transport:
        def request(self, method, url, **kw):
            assert 'Apply' not in url
            return _base.json_response(payload)
    class Limiter:
        def request(self, transport, method, url, **kw):
            return transport.request(method, url, **kw)
    r = c.conversions_earnings(network, Transport(),
        start='2026-09-01T00:00:00+00:00', end='2026-09-08T00:00:00+00:00',
        profile_id='098e5455-ca72-4099-9ff9-a9dd014148e7', limiter=Limiter())
    assert r.status == 'SUPPORTED'
    assert r.coverage == expected
    assert r.record_count == (0 if expected == 'COMPLETE' else None)
    assert r.commission_minor is None and r.cash_minor is None
    assert 'synthetic' not in str(r.to_dict())


@pytest.mark.parametrize('capability', ['program_status','terms_commission','issued_tracking_link'])
def test_impact_exact_campaign_four_function_contract(monkeypatch, capability):
    c = importlib.import_module('affiliate_network_access')
    from marketplace_adapters import impact, _base
    from types import SimpleNamespace
    monkeypatch.setattr(impact, 'load_impact_api_credentials', lambda: SimpleNamespace(
        basic_auth=('fixture','synthetic'), api_base=impact.API))
    t = _base.RecordingTransport([('GET','/Campaigns/123.json',_base.json_response({
        'CampaignId':'123','ContractStatus':'Active','PayoutTerms':'10 USD per sale',
        'TrackingLink':'https://example.invalid/issued-tracker'}))])
    r = getattr(c,capability)('impact', t, program_id='123')
    assert r.status == 'SUPPORTED' and r.coverage == 'COMPLETE'
    assert r.record_count == 1
    assert len(t.calls) == 1  # never visits returned link


def test_weekly_contract_keeps_every_roster_gap_visible(tmp_path, monkeypatch):
    c = importlib.import_module('affiliate_network_access')
    w = importlib.import_module('glp_affiliate_weekly_reporting')
    called = []
    def read(network, transport, **kwargs):
        called.append(network)
        return c.AccessResult(network,'conversions_earnings')
    monkeypatch.setattr(c,'conversions_earnings',read)
    names = ['awin','impact','cj','affiliati','katalys','maxbounty','shareasale','other']
    collectors = c.weekly_collectors(names, None)
    roster = {'marketplace_count':len(names),'marketplaces':[{'marketplace':n} for n in names]}
    result = w.run(tmp_path/'report',roster,[],collectors=collectors)
    persisted = __import__('json').loads((tmp_path/'report'/'RESULT.json').read_text())
    assert set(called) == set(names)-{'shareasale'}
    assert {r['network'] for r in persisted['observations']} == set(names)
    assert all(r['record_count'] is None for r in persisted['observations'])
    assert result['fleet_commission_minor'] is None


@pytest.mark.parametrize('network', ['maxbounty','katalys','everflow','xentraffic'])
def test_portal_metadata_never_logs_in(network):
    c = importlib.import_module('affiliate_network_access')
    for capability in ('program_status','terms_commission','issued_tracking_link'):
        r = getattr(c,capability)(network, None, program_id='123')
        assert r.status == 'PORTAL_ONLY'
        assert r.record_count is None


def test_observed_awin_commission_is_separate_from_cash(monkeypatch):
    c = importlib.import_module('affiliate_network_access')
    from marketplace_adapters import awin,_base
    monkeypatch.setattr(awin,'_headers',lambda: {})
    payload=[{'id':1,'advertiserId':123,'commissionStatus':'approved',
              'commissionAmount':{'amount':'15.50','currency':'USD'},'customerEmail':'DO_NOT_RETAIN'}]
    t=_base.RecordingTransport([('GET','/transactions/',_base.json_response(payload))])
    r=c.conversions_earnings('awin',t,start='2026-09-01T00:00:00+00:00',end='2026-09-08T00:00:00+00:00')
    assert r.records[0]['reported_amount']=='15.50'
    assert r.records[0]['currency']=='USD'
    assert r.cash_minor is None and r.commission_minor is None
    assert 'DO_NOT_RETAIN' not in str(r.to_dict())


def test_matrix_covers_all_56_route_buckets_without_promoting_unknowns():
    import json
    path=ROOT/'reports/ops/glp333-network-access-contract-20261001/RESULT.json'
    doc=json.loads(path.read_text())
    rows=doc['networks']
    assert len(rows)==len({r['network'] for r in rows})==56
    caps={'program_status','terms_commission','issued_tracking_link','conversions_earnings'}
    assert sum(len(r['functions']) for r in rows)==224
    for row in rows:
        assert set(row['functions'])==caps
        for cell in row['functions'].values():
            assert cell['status'] in {'SUPPORTED','UNSUPPORTED','PORTAL_ONLY','UNKNOWN'}
            if cell['status']=='SUPPORTED':
                assert cell['endpoint'] and cell['method'] in {'GET','POST'}
            if cell['coverage']!='COMPLETE':
                assert cell.get('record_count') is None
    assert next(r for r in rows if r['network']=='shareasale')['canonical_access_owner']=='awin'


def test_actual_weekly_job_forwards_all_network_contract(tmp_path, monkeypatch):
    import json, hashlib
    c=importlib.import_module('affiliate_network_access')
    job=importlib.import_module('glp_affiliate_weekly_reporting_job')
    calls=[]
    def read(network, transport, **kwargs):
        calls.append(network)
        return c.AccessResult(network,'conversions_earnings')
    monkeypatch.setattr(c,'conversions_earnings',read)
    source=json.loads((ROOT/'ops/glp/reporting-v2/source-manifest.json').read_text())
    files={str(ROOT/Path(p).relative_to('/home/ubuntu/business')):v for p,v in source['files'].items()}
    manifest=tmp_path/'pins.json'
    manifest.write_text(json.dumps({'files':files}))
    roster=tmp_path/'roster.json'
    roster.write_text(json.dumps({'marketplace_count':3,'marketplaces':[{'marketplace':n} for n in ('awin','impact','maxbounty')]}))
    result=job.main(['--source-manifest',str(manifest),'--manifest-sha256',hashlib.sha256(manifest.read_bytes()).hexdigest(),
        '--roster',str(roster),'--identity-registry',str(ROOT/'ops/glp/reporting-v2/identity-registry.json'),
        '--report-root',str(tmp_path/'out'),'--live-network-readonly'])
    assert result==0 and set(calls)=={'awin','impact','maxbounty'}
    output=list((tmp_path/'out').glob('*/RESULT.json'))
    assert len(output)==1
    doc=json.loads(output[0].read_text())
    assert doc['invocation']=='MANUAL_READ_ONLY'
    assert {x['network'] for x in doc['observations']}==set(calls)
    assert all(x['record_count'] is None for x in doc['observations'])


def test_partnerize_alphanumeric_campaign_identity(monkeypatch):
    c = importlib.import_module('affiliate_network_access')
    from marketplace_adapters import partnerize,_base
    monkeypatch.setattr(partnerize,'_auth',lambda: ('fixture','synthetic'))
    class T:
        def request(self, method, url, **kw):
            return _base.json_response({'campaigns':[{'campaign':{'campaign_id':'1101l123'}}]
                                       if url.endswith('/approved') else []})
    r=c.program_status('partnerize',T(),program_id='1101l123')
    assert r.coverage == 'COMPLETE'


@pytest.mark.parametrize('network,payload', [
    ('awin', {'programmeInfo': {'id':123}, 'commissionRange':[{'min':10,'max':10}]}),
    ('affiliati', {'success':True,'row_count':1,'data':[{'offer_id':123,'offer_contract_id':4,'campaign_id':5,
        'offer_name':'Fixture','offer_status':{'offer_status_name':'Active'},'price':10}]}),
    ('healthtrader', [{'id':123,'status':'active','payout':10,'tracking_url':'https://example.invalid/tracker'}]),
    ('partnerize', {'campaigns':[{'campaign':{'campaign_id':'123','title':'Fixture'}}]}),
    ('cj', '<cj-api><advertisers total-matched="1"><advertiser><advertiser-id>123</advertiser-id><relationship-status>joined</relationship-status><actions><action><name>Sale</name><commission><default>10</default></commission></action></actions></advertiser></advertisers></cj-api>'),
])
def test_metadata_adapter_status_fixture(monkeypatch, network, payload):
    c = importlib.import_module('affiliate_network_access')
    from marketplace_adapters import awin,affiliati,healthtrader,partnerize,cj,_base
    monkeypatch.setattr(awin,'_headers',lambda: {})
    monkeypatch.setattr(affiliati,'_credentials',lambda: ('123','synthetic'))
    monkeypatch.setattr(healthtrader,'access_token',lambda *a: 'synthetic')
    monkeypatch.setattr(partnerize,'_auth',lambda: ('fixture','synthetic'))
    monkeypatch.setattr(cj,'_secret',lambda: ('synthetic','8010186','101822575'))
    class T:
        def request(self, method, url, **kw):
            if network == 'awin' and url.endswith('/programmes'):
                return _base.json_response([{'id':123}] if kw['params']['relationship']=='joined' else [])
            if network == 'partnerize' and not url.endswith('/approved'):
                return _base.json_response({'campaigns':[]})
            return _base.Response(200,payload) if isinstance(payload,str) else _base.json_response(payload)
    r = c.program_status(network,T(),program_id='123')
    assert r.status == 'SUPPORTED' and r.coverage == 'COMPLETE'
    assert r.record_count == 1
