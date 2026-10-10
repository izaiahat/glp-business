"""Synthetic fixtures exercise statement intake; never provider revenue."""
import hashlib
import json
from datetime import datetime, timezone
from glp_affiliate_weekly_reporting_j7 import run


def intake(tmp_path):
    origin='https://orderlywellnesscorporation.everflowclient.io'
    invoice={'network_affiliate_invoice_id':2809,'network_id':3708,'network_affiliate_id':2608,
        'status':'paid','start_time':'2026-08-01 00:00:00','end_time':'2026-08-31 23:59:59',
        'timezone_id':80,'currency_id':'USD','balance':0,'relationship':{}}
    table={k:v for k,v in invoice.items() if k not in ('network_affiliate_id','relationship','status')}
    table.update(affiliate_invoice_status='paid',billed=0,paid=0)
    def receipt(path,method,doc):
        return {'endpoint':origin+'/api/affiliates/'+path,'method':method,'http_status':200,
                'response_sha256':'a'*64,'document':doc}
    data={'schema':'everflow-closed-statement-intake-j9-v1','origin':origin,
        'captured_at':'2026-10-10T05:30:29+00:00','receipts':[
        receipt('billings/affiliates/invoicestable','POST',{'invoices':[table],
            'paging':{'page':1,'page_size':100,'total_count':1}}),
        receipt('billings/affiliates/invoices/2809','GET',invoice),
        receipt('affiliate','GET',{'network_id':3708,'network_affiliate_id':2608})],
        'calendar_evidence':{'invoice_id':2809,'start':'08/01/2026 (UTC-04:00)',
            'end':'08/31/2026 (UTC-04:00)','source':'authenticated invoice detail DOM'}}
    path=tmp_path/'fixture.json';path.write_text(json.dumps(data))
    return path,hashlib.sha256(path.read_bytes()).hexdigest()


def test_weekly_consumes_source_zero_statement_without_promoting_lineage(tmp_path):
    path,sha=intake(tmp_path)
    result=run(tmp_path/'run',{'marketplaces':[],'marketplace_count':0},[],
        as_of=datetime(2026,10,10,6,tzinfo=timezone.utc),statement_intake=path,statement_sha256=sha)
    row=result['closed_payout_statements']['statements'][0]
    assert row['statement_id']=='2809'
    assert row['account_id']=='2608' and row['currency']=='USD'
    assert row['billed_minor']==row['paid_minor']==row['balance_minor']==0
    assert row['start_inclusive']=='2026-08-01T00:00:00-04:00'
    assert row['end_exclusive']=='2026-09-01T00:00:00-04:00'
    assert row['lineage_state']=='UNKNOWN_MISSING_NATIVE_DETAIL_PAYMENT_MEMBERSHIP'
    assert row['eligible_REAL_lineage'] is False
    assert result['fleet_commission_minor'] is None and result['fleet_cash_minor'] is None
    assert json.loads((tmp_path/'run/RESULT.json').read_text())['closed_payout_statements']==result['closed_payout_statements']


def test_statement_rejects_conflict_unknown_currency_and_incomplete_population(tmp_path):
    import pytest
    from affiliate_closed_statement_j9 import read
    for mutation in ('currency','account','count','status','pin'):
        path,sha=intake(tmp_path)
        data=json.loads(path.read_text())
        if mutation=='currency':
            data['receipts'][0]['document']['invoices'][0]['currency_id']=None
        elif mutation=='account':
            data['receipts'][1]['document']['network_affiliate_id']=999
        elif mutation=='count':
            data['receipts'][0]['document']['paging']['total_count']=2
        elif mutation=='status':
            data['receipts'][0]['document']['invoices'][0]['affiliate_invoice_status']='unpaid'
        path.write_text(json.dumps(data))
        sha='0'*64 if mutation=='pin' else hashlib.sha256(path.read_bytes()).hexdigest()
        with pytest.raises(ValueError):
            read(path,sha,as_of=datetime(2026,10,10,6,tzinfo=timezone.utc))

