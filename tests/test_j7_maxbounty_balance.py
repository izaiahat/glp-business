import json,os,sys,time
from pathlib import Path
from datetime import datetime,timezone
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import affiliate_reporting_maxbounty_j7 as m
from marketplace_adapters import _base as b

class Transport:
    def request(self,method,url,**kwargs):
        if url.endswith('/authentication'):d={'success':True,'mb-api-token':'TEST_TOKEN','mb-aid':803003}
        elif url.endswith('/affiliate'):d={'success':True,'publisher_id':803003,'affiliate_personal_information':{'email':'ops@glpclinicscout.com'}}
        elif url.endswith('/reports/earnings'):d={'success':True,'report':[],'totals':{'earnings':0,'clicks':0,'leads':0,'sales':0}}
        elif '/accountBalance/' in url:d={'success':False,'errors':{'message':m.NO_BALANCES}}
        else:d={'success':True,'report':[],'total_pages':0,'page':1}
        return b.json_response(d)


def test_explicit_source_no_balance_history_not_authentication_failure(monkeypatch,tmp_path):
    import affiliate_reporting_maxbounty as old
    fake=tmp_path/'fixture.json';fake.write_text(json.dumps({'email':'ops@glpclinicscout.com','password':'FAKE_PASSWORD'}))
    monkeypatch.setattr(old,'credentials_path',lambda:fake)
    reader=old if os.getenv('J7_REGRESSION_LEGACY') else m
    result=reader.collect(Transport(),start='2026-09-01T00:00:00+00:00',end='2026-10-01T00:00:00+00:00')
    assert 'BALANCE_HISTORY_READ' in result.reason
    assert result.resource_counts=={'conversions':0,'reversals':0,'balance_transactions':0}
    assert result.receipts[-1]['provider_success'] is False
    assert result.receipts[-1]['resource_state']=='SOURCE_DECLARED_NO_BALANCE_HISTORY'
    assert result.coverage=='PARTIAL' and result.reported_totals['currency'] is None
    assert 'FAKE_PASSWORD' not in json.dumps(result.to_dict()) and 'TEST_TOKEN' not in json.dumps(result.to_dict())

@pytest.mark.parametrize('doc',[
    {'success':False,'errors':{'message':'Invalid API token.'}},
    {'success':False,'errors':{'message':m.NO_BALANCES,'other':'anything'}},
    {'success':False,'message':m.NO_BALANCES},
    {'success':False,'errors':{'message':m.NO_BALANCES},'account_balances':[]},
    {'success':True,'errors':{'message':m.NO_BALANCES}},
])
def test_only_exact_source_absence_is_recognized(doc):assert not m.explicit_absence(doc)
