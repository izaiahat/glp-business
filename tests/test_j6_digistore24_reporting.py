"""Synthetic offline fixtures only; no production financial claims."""
import hashlib
import json
import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from marketplace_adapters import _base
import affiliate_network_access_j6 as access

class Transport:
    def __init__(self, commissions=None, count=None, payouts=None):
        self.calls=[]
        self.commissions=[] if commissions is None else commissions
        self.count=len(self.commissions) if count is None else count
        self.payouts=[] if payouts is None else payouts
    def request(self, method, url, **kwargs):
        self.calls.append((method,url,kwargs))
        assert method=='GET'
        fn=url.rsplit('/',1)[-1]
        data={'getUserInfo':{'user_id':'123','user_name':'offline-account','granted_roles':'affiliate,merchant','thankyou_page_key':'SYNTHETIC_SECRET','contact':{'email':'SYNTHETIC_PII'}},
              'listCommissions':{'page_no':1,'page_size':'100','item_count':self.count,'page_count':1 if self.commissions else 0,'items':self.commissions},
              'listPayouts':{'payout_list':self.payouts}}[fn]
        return _base.json_response({'result':'success','timezone':'CEST','utc_offset':'7200','data':data})


def collect(monkeypatch, transport):
    monkeypatch.setattr(_base,'secret_text',lambda *a:'SYNTHETIC_KEY')
    return access.conversions_earnings('digistore24',transport,start='2026-09-01T00:00:00+00:00',end='2026-10-01T00:00:00+00:00').to_dict()


def test_digistore24_missing_official_financial_route_is_wired(monkeypatch):
    t=Transport([{'id':55,'transaction_id':77,'purchase_id':'PURCHASE_A','amount':12.34,'currency':'EUR','reason':'affiliate_share','created_at':'2026-09-10 12:00:00','schedule_payout_at':'2026-09-21','secret':'SYNTHETIC_PRIVATE'}],payouts=[{'id':99,'currency':'EUR','affiliate_net_amount':12.34,'processed_at':'2026-09-21 12:00:00','commission_list_url':'https://unsafe.example/SYNTHETIC_PRIVATE','credit_note_url':'SYNTHETIC_PRIVATE'}])
    result=collect(monkeypatch,t)
    assert result['status']=='SUPPORTED' and result['coverage']=='PARTIAL'
    assert result['account_binding']=='RESPONSE_OWNED_USER_ID:123'
    row=next(r for r in result['records'] if r['record_kind']=='commission')
    assert row['commission_id']=='55' and row['transaction_id']=='77' and row['purchase_id']=='PURCHASE_A'
    assert row['reported_amount']=='12.34' and row['currency']=='EUR'
    assert 'payout_id' not in row  # schedule date is NOT membership
    assert result['commission_minor'] is result['cash_minor'] is result['record_count'] is None
    assert result['report_complete_period'] is False
    assert result['resource_counts']['commissions']['declared']==1
    assert result['resource_counts']['payouts']['declared'] is None
    assert all(r['classification']=='UNKNOWN' for r in result['records'])
    serialized=json.dumps(result)
    assert all(v not in serialized for v in ('SYNTHETIC_KEY','SYNTHETIC_SECRET','SYNTHETIC_PII','SYNTHETIC_PRIVATE'))
    assert [u.rsplit('/',1)[-1] for _,u,_ in t.calls]==['getUserInfo','listCommissions','listPayouts']


def test_empty_commissions_are_real_declared_zero_not_fleet_cash(monkeypatch):
    result=collect(monkeypatch,Transport())
    assert result['reported_totals']['commission_item_count']==0
    assert result['reported_totals']['reported_commission_amount']=='0'
    assert result['reported_totals']['amount_scope']=='UNCLASSIFIED_ACCOUNT_REQUESTED_VENDOR_CALENDAR_NOT_GLP_CASH'
    assert result['resource_counts']['payouts']=={'returned':0,'declared':None,'population_proven':False}
    assert result['commission_minor'] is result['cash_minor'] is None
    assert result['records']==()


def test_count_conflict_never_becomes_zero(monkeypatch):
    result=collect(monkeypatch,Transport(count=1))
    assert result['reason']=='DIGISTORE24_SCHEMA_OR_POPULATION_UNPROVEN_NO_RETRY'
    assert result['reported_totals'] is None


def test_read_guard_refuses_mutations_and_unbound_paths():
    wrapped=access.ReadTransport(object())
    for method,url in [('POST','https://www.digistore24.com/api/call/listPayouts'),('GET','https://www.digistore24.com/api/call/createPayout'),('GET','https://www.digistore24.com/api/call/listPayouts/other'),('GET','https://tracking.digistore24.com/api/call/listPayouts')]:
        with pytest.raises(ValueError,match='route_not_read_allowlisted'):
            wrapped.request(method,url)


def test_versioned_successor_preserves_j5_and_pins_all_imports():
    root=Path(__file__).resolve().parents[1]
    old=(root/'ops/hermes-cron-scripts/glp_affiliate_weekly_reporting_j5.sh').read_bytes()
    new=(root/'ops/hermes-cron-scripts/glp_affiliate_weekly_reporting_j6.sh').read_bytes()
    manifest=(root/'ops/glp/reporting-v2/source-manifest-j6.json').read_bytes()
    old_manifest=(root/'ops/glp/reporting-v2/source-manifest-j5.json').read_bytes()
    old_job=(root/'scripts/glp_affiliate_weekly_reporting_j5_job.py').read_bytes()
    new_job=(root/'scripts/glp_affiliate_weekly_reporting_j6_job.py').read_bytes()
    expected=old.replace(b'_j5',b'_j6').replace(b'manifest-j5',b'manifest-j6').replace(hashlib.sha256(old_manifest).hexdigest().encode(),hashlib.sha256(manifest).hexdigest().encode()).replace(hashlib.sha256(old_job).hexdigest().encode(),hashlib.sha256(new_job).hexdigest().encode())
    assert expected==new
    pins=json.loads(manifest)['files']
    for name in ('affiliate_network_access_j6.py','affiliate_reporting_digistore24.py','glp_affiliate_weekly_reporting_j6.py','glp_affiliate_weekly_reporting_j6_job.py'):
        assert pins['/home/ubuntu/business/scripts/'+name]==hashlib.sha256((root/'scripts'/name).read_bytes()).hexdigest()
