"""The official data population is rows; undocumented rowCount is retained."""
import json,os,sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from marketplace_adapters import katalys,katalys_j7 as k,_base as b
PROFILE='098e5455-ca72-4099-9ff9-a9dd014148e7'
class Limiter:
    def request(self,transport,method,url,**kwargs):return transport.request(method,url,**kwargs)
class Transport:
    def __init__(self,conversion=None,statistics=None):
        self.calls=[];self.conv=conversion or {'success':True,'rowCount':1,'rows':[]};self.stats=statistics if statistics is not None else {'success':True,'rowCount':0,'rows':[]}
    def request(self,method,url,**kwargs):
        self.calls.append((url,kwargs['json_body']))
        return b.json_response(self.conv if url.endswith('/conversions') else self.stats)

def test_documented_empty_rows_not_one_fake_conversion(monkeypatch):
    monkeypatch.setattr(b,'secret_text',lambda *a:'FAKE_TEST_KEY')
    reader=katalys if os.getenv('J7_REGRESSION_LEGACY') else k
    t=Transport();r=reader.conversion_report('2026-09-01','2026-09-30',t,profile_id=PROFILE,timezone='UTC',limiter=Limiter())
    assert r['status']=='PASS' and r['completeness']=='COMPLETE' and r['row_count']==0
    assert r['vendor_rowCount']==1 and r['rows']==[] and r['classification']=='TEST_REAL_UNKNOWN'
    assert r['corroboration']['row_count']==0 and r['corroboration']['profile_id']==PROFILE
    assert r['paid_cash'] is None and r['currency'] is None
    assert 'FAKE_TEST_KEY' not in json.dumps(r)
    assert len(t.calls)==2 and t.calls[0][1]['filters']==t.calls[1][1]['filters'][:2]

@pytest.mark.parametrize('stats',[
    {'success':True,'rows':[{'aff_id':13031,'conversions':1,'payout':1}]},
    {'success':False,'rows':[]},
    {'success':True,'rows':[],'has_more':True},
])
def test_empty_count_conflict_requires_matching_complete_empty_stats(monkeypatch,stats):
    monkeypatch.setattr(b,'secret_text',lambda *a:'FAKE_TEST_KEY')
    r=k.conversion_report('2026-09-01','2026-09-30',Transport(statistics=stats),profile_id=PROFILE,timezone='UTC',limiter=Limiter())
    assert r['status']=='BLOCKED' and r['completeness']=='UNKNOWN'

@pytest.mark.parametrize('conv',[
    {'success':True,'rowCount':2,'rows':[]},
    {'success':True,'rowCount':1,'rows':[],'has_more':True},
    {'success':True,'rowCount':1,'total_count':1,'rows':[]},
    {'success':False,'rowCount':1,'rows':[]},
])
def test_pagination_failure_and_other_count_mismatch_oracles_unchanged(monkeypatch,conv):
    monkeypatch.setattr(b,'secret_text',lambda *a:'FAKE_TEST_KEY')
    r=k.conversion_report('2026-09-01','2026-09-30',Transport(conversion=conv),profile_id=PROFILE,timezone='UTC',limiter=Limiter())
    assert r['status']=='PARTIAL' and r['completeness']=='INCOMPLETE' and r['row_count'] is None
