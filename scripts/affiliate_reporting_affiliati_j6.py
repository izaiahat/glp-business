"""Read-only CAKE conversion/bill identity; no TEST-to-REAL promotion.

Source contract: Affiliati Swagger /Reports/Conversions and /Billing/Bills.
The bill schema has no conversion membership or ISO currency, so no joins or
cash are invented. One bounded page is coverage evidence, not all accounts.
"""
from datetime import timedelta
from affiliate_network_access_j6 import AccessResult,ReadTransport
from affiliate_network_access import _window,ProviderResponseError
from affiliate_reporting_digistore24 import _id,_amount
import re


def _stamp(value):
    return value if isinstance(value,str) and re.fullmatch(r'[0-9TZ:+. -]{10,40}',value) else None
from marketplace_adapters import affiliati


def collect(transport,*,start,end,deadline=None,max_requests=32):
    t=ReadTransport(transport,deadline=deadline,max_requests=max_requests)
    records=[]
    counts={}
    try:
        lo,hi=_window(start,end)
        account,key=affiliati._credentials()
        def request(path,params):
            doc=t.request('GET',affiliati.API+path,params={'affiliate_id':account,'api_key':key,**params}).json()
            if not isinstance(doc,dict) or doc.get('success') is not True:
                raise ProviderResponseError('provider_response_unproven')
            rows=doc.get('data')
            count=doc.get('row_count')
            if not isinstance(rows,list) or type(count) is not int or count<0 or not all(isinstance(r,dict) for r in rows):
                raise ValueError('resource_schema_unproven')
            return rows,count
        rows,count=request('/Reports/Conversions',{'start_date':lo.isoformat(),'end_date':(hi-timedelta(seconds=1)).isoformat(),'start_at_row':1,'row_limit':100,'conversion_type':'all'})
        if count!=len(rows) or len(rows)>100:
            raise ValueError('conversions_count_mismatch')
        seen=set()
        counts['conversions']={'returned':len(rows),'declared':count,'population_proven':len(rows)<100,'period_proven':False}
        for i,row in enumerate(rows):
            # Never export known Fella records, even as unclassified evidence.
            if 'fella' in str(row.get('offer_name','')).casefold():
                continue
            cid=_id(row.get('conversion_id'))
            if cid is None or cid in seen:
                raise ValueError('conversion_identity_unproven')
            seen.add(cid)
            records.append({'resource':'conversions','provider_event_id':cid,'native_conversion_id':cid,
                'native_event_id':_id(row.get('event_id')),'native_offer_id':_id(row.get('offer_id')),
                'program_id':None,'commission_id':None,'payout_id':None,'currency':None,
                'source_conversion_date':_stamp(row.get('conversion_date')),
                'reported_amount':_amount(row.get('price')),'amount_semantics':'reported_conversion_price_not_cash',
                'classification':'UNKNOWN','provider_scope':'UNMAPPED_NATIVE_OFFER_NOT_PROVIDER_APPROVAL',
                'receipt_sha256':t.receipts[-1]['response_sha256'],'receipt_pointer':f'/data/{i}'})
        bills,count=request('/Billing/Bills',{})
        if count!=len(bills):
            raise ValueError('bills_count_mismatch')
        counts['bills']={'returned':len(bills),'declared':count,'population_proven':True,'period_proven':False}
        seen=set()
        for i,row in enumerate(bills):
            bid=_id(row.get('bill_id'))
            if bid is None or bid in seen:
                raise ValueError('bill_identity_unproven')
            seen.add(bid)
            records.append({'resource':'bills','provider_event_id':bid,'native_bill_id':bid,
                'commission_id':None,'conversion_id':None,'program_id':None,'currency':None,
                'reported_amount':_amount(row.get('bill_amount')),'source_start_date':_stamp(row.get('start_date')),
                'source_end_date':_stamp(row.get('end_date')),'source_date_paid':_stamp(row.get('date_paid')),
                'classification':'UNKNOWN','amount_semantics':'reported_bill_not_verified_settlement',
                'receipt_sha256':t.receipts[-1]['response_sha256'],'receipt_pointer':f'/data/{i}'})
        reason='NATIVE_CONVERSION_AND_BILL_IDS_RETAINED_ACCOUNT_CALENDAR_CURRENCY_REAL_TEST_MEMBERSHIP_UNPROVEN'
    except (OSError,ValueError,KeyError,TypeError) as exc:
        reason='AFFILIATI_READ_UNPROVEN:'+str(exc) if type(exc) is ValueError else 'AFFILIATI_READ_UNPROVEN:'+type(exc).__name__
    return AccessResult(network='affiliati',capability='conversions_earnings',status='SUPPORTED',
        source='AFFILIATI_OFFICIAL_CONVERSION_AND_BILL_API',coverage='PARTIAL' if t.receipts else 'UNKNOWN',
        records=tuple(records),receipts=tuple(t.receipts),reason=reason,resource_counts=counts,
        account_binding='REQUEST_CREDENTIAL_NOT_RESPONSE_OWNED_IDENTITY')
