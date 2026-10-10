"""Pinned source-owned closed invoice intake; no revenue or lineage oracle.

The source invoice table supplies billed/paid/balance amounts. Invoice.balance
is only outstanding balance, never paid cash. Missing relationship arrays are
UNKNOWN, not empty. A retained paid statement remains historical evidence;
this reader does not claim an automatic portal refresh or fleet income.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re

ORIGIN='https://orderlywellnesscorporation.everflowclient.io'
PREFIX=ORIGIN+'/api/affiliates/'


def read(path, expected_sha256, *, as_of):
    raw=Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest()!=expected_sha256:
        raise ValueError('statement_intake_pin_mismatch')
    data=json.loads(raw)
    if data.get('schema')!='everflow-closed-statement-intake-j9-v1' or data.get('origin')!=ORIGIN:
        raise ValueError('statement_origin_schema_unproven')
    captured=datetime.fromisoformat(data['captured_at'])
    if captured.tzinfo is None or not captured<=as_of:
        raise ValueError('statement_capture_clock_unproven')
    receipts=data['receipts']
    def source(endpoint,method):
        rows=[r for r in receipts if r.get('endpoint')==PREFIX+endpoint and r.get('method')==method]
        if len(rows)!=1 or rows[0].get('http_status')!=200 or not re.fullmatch('[a-f0-9]{64}',rows[0].get('response_sha256','')):
            raise ValueError('statement_source_receipt_unproven')
        return rows[0]
    account=source('affiliate','GET')
    table=source('billings/affiliates/invoicestable','POST')
    doc=table['document']; rows=doc['invoices']; paging=doc['paging']
    if not rows or paging.get('page')!=1 or type(paging.get('total_count')) is not int or paging['total_count']!=len(rows):
        raise ValueError('statement_population_unproven')
    if len({r['network_affiliate_invoice_id'] for r in rows})!=len(rows) or any(r.get('affiliate_invoice_status')!='paid' for r in rows):
        raise ValueError('statement_paid_population_unproven')
    row=max(rows,key=lambda r:r['end_time'])
    identifier=row['network_affiliate_invoice_id']
    if type(identifier) is not int or identifier<=0:
        raise ValueError('statement_id_unproven')
    detail=source('billings/affiliates/invoices/'+str(identifier),'GET')
    invoice=detail['document']; acct=account['document']
    if (invoice.get('status')!='paid' or invoice.get('network_affiliate_id')!=acct.get('network_affiliate_id')
            or invoice.get('network_id')!=acct.get('network_id') or row.get('network_id')!=acct.get('network_id')
            or type(acct.get('network_affiliate_id')) is not int or acct['network_affiliate_id']<=0):
        raise ValueError('statement_account_binding_unproven')
    for field in ('network_affiliate_invoice_id','start_time','end_time','timezone_id','currency_id','balance'):
        if row.get(field)!=invoice.get(field):
            raise ValueError('statement_table_detail_conflict')
    if row.get('currency_id')!='USD':
        raise ValueError('statement_currency_unproven')
    calendar=data['calendar_evidence']
    if calendar.get('invoice_id')!=identifier or calendar.get('source')!='authenticated invoice detail DOM':
        raise ValueError('statement_calendar_unproven')
    def boundary(field, stamp):
        match=re.fullmatch(r'(\d{2}/\d{2}/\d{4}) \(UTC([+-]\d{2}:\d{2})\)',calendar.get(field,''))
        if not match:
            raise ValueError('statement_offset_unproven')
        date,offset=match.groups()
        value=datetime.fromisoformat(stamp+offset)
        if value.strftime('%m/%d/%Y')!=date:
            raise ValueError('statement_calendar_date_conflict')
        return value
    start=boundary('start',row['start_time']); end=boundary('end',row['end_time'])+timedelta(seconds=1)
    if not start<end<=as_of:
        raise ValueError('statement_period_not_closed')
    def minor(field):
        value=row.get(field)
        if isinstance(value,bool) or not isinstance(value,(int,float,str)):
            raise ValueError('statement_amount_unproven')
        number=Decimal(str(value))*100
        if not number.is_finite() or number!=number.to_integral_value():
            raise ValueError('statement_amount_precision_unproven')
        return int(number)
    result={'network':'everflow','tenant_id':ORIGIN.removeprefix('https://'),'network_id':str(acct['network_id']),
        'account_id':str(acct['network_affiliate_id']),'statement_id':str(identifier),'source_status':'paid',
        'start_inclusive':start.isoformat(),'end_exclusive':end.isoformat(),'source_timezone_id':row['timezone_id'],
        'currency':row['currency_id'],'billed_minor':minor('billed'),'paid_minor':minor('paid'),'balance_minor':minor('balance'),
        'amount_scope':'SOURCE_WHOLE_INVOICE_NOT_GLP_ATTRIBUTED_REVENUE','eligible_REAL_lineage':False,
        'lineage_state':'UNKNOWN_MISSING_NATIVE_DETAIL_PAYMENT_MEMBERSHIP',
        'source_relationship_keys':sorted(invoice.get('relationship',{})),
        'owner':'GLP-334 Orderly account owner','provider_holds':'UNCHANGED',
        'evidence':{'intake_path':str(Path(path).resolve()),'intake_sha256':expected_sha256,
            'table_response_sha256':table['response_sha256'],'table_pointer':'/invoices/'+str(rows.index(row)),
            'invoice_response_sha256':detail['response_sha256'],'account_response_sha256':account['response_sha256']}}
    return {'status':'SOURCE_CLOSED_STATEMENT_READ_LINEAGE_UNKNOWN','captured_at':data['captured_at'],
        'freshness':'HISTORICAL_PINNED_STATEMENT_NOT_AUTOMATIC_PORTAL_REFRESH',
        'selection':'MOST_RECENT_PAID_STATEMENT_IN_COMPLETE_TABLE_AT_CAPTURE','statements':[result]}
