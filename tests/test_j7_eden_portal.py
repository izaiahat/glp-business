import hashlib,json,sys,time
from pathlib import Path
from datetime import datetime,timezone
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import affiliate_reporting_eden_portal_j7 as eden
from glp_affiliate_weekly_reporting_j6 import windows


def test_eden_fresh_zero_financial_intake_has_own_account_not_xentraffic(tmp_path):
    def fake(expression):
        if 'protected_account_required' in expression:return {'partner':'62','calendar':True,'currency':True}
        if expression.startswith('window.__j7FinancialRead('):
            return {'http_status':200,'response_sha256':'d'*64,'keys':['conversions','paging'],
                    'paging':{'page':1,'page_size':100,'total_count':0},'row_count':0,
                    'captured_at':datetime.now(timezone.utc).isoformat()}
        return True
    w=windows(datetime.now(timezone.utc))
    doc=eden.capture(fake,owned_session='TEST_FIXTURE_NOT_REAL',window=w,goto_url=lambda x:None,wait_for_load=lambda:None)
    assert doc['schema']=='eden-fresh-financial-v1'
    assert all(r['account_id']=='62' and r['account_binding']=='RESPONSE_OWNED_PARTNER_ID:62'
               and r['tenant_id']=='eden.everflowclient.io' and r['cash_minor'] is None for r in doc['observations'])
    path=tmp_path/'fixture.json';path.write_text(json.dumps(doc))
    sink=[]
    eden.collector(path,hashlib.sha256(path.read_bytes()).hexdigest())(w,time.monotonic()+5,32,sink=sink.append)
    assert len(sink)==3
    doc['observations'][0]['account_binding']='RESPONSE_OWNED_PARTNER_ID:4384'
    path.write_text(json.dumps(doc))
    with pytest.raises(ValueError,match='identity_or_empty_population'):
        eden.collector(path,hashlib.sha256(path.read_bytes()).hexdigest())(w,time.monotonic()+5,32,sink=lambda r:None)
