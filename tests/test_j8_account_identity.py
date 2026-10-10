"""Account-only resolution must not alter financial/classification acceptance."""
from datetime import datetime, timezone
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import glp_affiliate_weekly_reporting_j7 as engine


def test_response_owned_identity_reaches_persisted_report_without_cash_promotion(tmp_path):
    receipt={'endpoint':'https://www.digistore24.com/api/call/getUserInfo','http_status':200,'response_sha256':'a'*64}
    row={'network':'digistore24','status':'SUPPORTED','account_binding':'RESPONSE_OWNED_USER_ID:5725668',
         'source_identity':{'account_id':'5725668','native_id_type':'api_key_holder_user_id','roles':['affiliate'],
                            'evidence':{'response_sha256':'a'*64,'pointer':'/data/user_id'}},
         'receipts':[receipt],'records':[],'report_complete_period':False}
    def collector(window,deadline,max_requests,*,sink):
        sink(row)
        return [row]
    result=engine.run(tmp_path/'report',{'marketplace_count':1,'marketplaces':[{'marketplace':'digistore24'}]},[],
                      as_of=datetime(2026,10,10,tzinfo=timezone.utc),collectors={'digistore24':collector})
    diagnostic=result.get('account_identity_resolution',{})
    assert diagnostic.get('matched_network_count')==1, 'source-owned account identity dropped before report persistence'
    assert diagnostic['rows'][0]['account_id']=='5725668'
    assert diagnostic['rows'][0]['tenant_id']=='not_applicable'
    assert diagnostic['rows'][0]['program_identity']=='UNKNOWN'
    assert result['fleet_cash_minor'] is None and result['fleet_commission_minor'] is None
    assert {'network':'digistore24','state':'IDENTITY_AND_CLASSIFICATION_COVERAGE_UNVERIFIED'} in result['gaps']


def test_maxbounty_reader_retains_typed_publisher_receipt(monkeypatch,tmp_path):
    import json
    import affiliate_reporting_maxbounty as old
    import affiliate_reporting_maxbounty_j7 as reader
    from marketplace_adapters import _base as base
    credential=tmp_path/'fixture.json'
    credential.write_text(json.dumps({'email':'ops@glpclinicscout.com','password':'SYNTHETIC_OFFLINE_ONLY'}))
    monkeypatch.setattr(old,'credentials_path',lambda:credential)
    class Transport:
        def request(self,method,url,**kwargs):
            if url.endswith('/authentication'):
                doc={'success':True,'mb-api-token':'SYNTHETIC_TOKEN','mb-aid':803003}
            elif url.endswith('/affiliate'):
                doc={'success':True,'publisher_id':803003,'affiliate_personal_information':{'email':'ops@glpclinicscout.com'}}
            elif url.endswith('/reports/earnings'):
                doc={'success':True,'report':[],'totals':dict(earnings=0,clicks=0,leads=0,sales=0)}
            elif '/accountBalance/' in url:
                doc={'success':False,'errors':{'message':reader.NO_BALANCES}}
            else:
                doc={'success':True,'report':[],'total_pages':0,'page':1}
            return base.json_response(doc)
    result=reader.collect(Transport(),start='2026-09-01T00:00:00+00:00',end='2026-10-01T00:00:00+00:00').to_dict()
    identity=result.get('source_identity')
    assert identity is not None, 'publisher role/receipt dropped despite response-owned binding'
    assert identity['account_id']=='803003' and identity['native_id_type']=='publisher_id'
    from affiliate_account_identity_j8 import resolve_accounts
    assert resolve_accounts(['maxbounty'],[result])['matched_network_count']==1
    assert 'SYNTHETIC_TOKEN' not in json.dumps(result)


def test_unproven_or_conflicting_accounts_never_select_default():
    from copy import deepcopy
    from affiliate_account_identity_j8 import resolve_accounts
    receipt={'endpoint':'https://www.digistore24.com/api/call/getUserInfo','http_status':200,'response_sha256':'a'*64}
    row={'network':'digistore24','status':'SUPPORTED','account_binding':'RESPONSE_OWNED_USER_ID:1','source_identity':{'account_id':'1','native_id_type':'api_key_holder_user_id',
         'roles':['affiliate'],'evidence':{'response_sha256':'a'*64,'pointer':'/data/user_id'}},'receipts':[receipt]}
    other=deepcopy(row);other['source_identity']['account_id']='2';other['account_binding']='RESPONSE_OWNED_USER_ID:2'
    conflict=resolve_accounts(['digistore24'],[row,other])
    assert conflict['matched_network_count']==0
    assert conflict['rows'][0]['reason']=='CONFLICTING_OR_UNAUTHENTICATED_SOURCE_IDENTITY'
    for field in ('receipts','source_identity'):
        invalid=deepcopy(row);invalid.pop(field)
        assert resolve_accounts(['digistore24'],[invalid])['matched_network_count']==0
    wrong_role=deepcopy(row);wrong_role['source_identity']['roles']=['vendor']
    assert resolve_accounts(['digistore24'],[wrong_role])['matched_network_count']==0
    tenant=deepcopy(row);tenant['network']='everflow'
    assert resolve_accounts(['everflow'],[tenant])['matched_network_count']==0


