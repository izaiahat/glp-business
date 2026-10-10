"""Synthetic fixtures; these are not production conversions."""
import json
import sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import affiliate_network_access_j6 as access
from marketplace_adapters import _base

class Transport:
    def __init__(self, mismatch=False):
        self.mismatch=mismatch
    def request(self,method,url,**kwargs):
        assert method=='GET'
        if url.endswith('/Reports/Conversions'):
            return _base.json_response({'success':True,'row_count':1,'data':[{'conversion_id':'1234','event_id':5144,'offer_id':42896,'conversion_date':'2026-09-15T18:37:33.017','price':0,'disposition':None,'offer_name':'Offline candidate','subid_1':'PRIVATE_SUBID','udid':'PRIVATE_DEVICE'}]})
        if url.endswith('/Billing/Bills'):
            return _base.json_response({'success':True,'row_count':1 if self.mismatch else 0,'data':[]})
        raise AssertionError('not allowlisted')

def test_native_ids_and_zero_price_are_not_lost_or_promoted(monkeypatch):
    monkeypatch.setattr(_base,'secret_json',lambda *args:{'affiliate_id':'99','api_key':'SYNTHETIC_KEY'})
    result=access.conversions_earnings('affiliati',Transport(),start='2026-09-01T00:00:00+00:00',end='2026-10-01T00:00:00+00:00').to_dict()
    row=result['records'][0]
    assert row['provider_event_id']=='1234' and row['native_conversion_id']=='1234'
    assert row['native_event_id']=='5144' and row['native_offer_id']=='42896'
    assert row['reported_amount']=='0' and row['classification']=='UNKNOWN'
    assert row['commission_id'] is None and row['payout_id'] is None
    assert result['resource_counts']['bills']['declared']==0
    assert result['resource_counts']['bills']['population_proven'] is True
    assert result['cash_minor'] is result['commission_minor'] is None
    assert result['report_complete_period'] is False
    for value in ['PRIVATE_SUBID','PRIVATE_DEVICE','SYNTHETIC_KEY']:
        assert value not in json.dumps(result)

def test_bill_count_conflict_refuses(monkeypatch):
    monkeypatch.setattr(_base,'secret_json',lambda *args:{'affiliate_id':'99','api_key':'SYNTHETIC_KEY'})
    result=access.conversions_earnings('affiliati',Transport(True),start='2026-09-01T00:00:00+00:00',end='2026-10-01T00:00:00+00:00')
    assert 'bills_count_mismatch' in result.reason
    assert result.cash_minor is None

def test_bill_route_rejects_mutations():
    with pytest.raises(ValueError):
        access.ReadTransport(Transport()).request('POST','https://login.affiliatinetwork.com/affiliates/api/Billing/Bills')
