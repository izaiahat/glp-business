"""Official account, commission and credit-note reads; no GLP cash inference.

Contracts: https://www.digistore24.com/api/docs/paths/getUserInfo.yaml,
listCommissions.yaml and listPayouts.yaml. No purchase calls, credit-note URLs,
tracking visits or mutations. Vendor/affiliate roles may coexist. Query-calendar
semantics and REAL/TEST/provider crosswalks remain unproven. Credit-note list
length is NOT an independently counted settled payout population.
"""
from decimal import Decimal,InvalidOperation
import re
from affiliate_network_access_j6 import AccessResult,ReadTransport
from affiliate_network_access import _window,ProviderResponseError
from marketplace_adapters import _base as credentials
API='https://www.digistore24.com/api/call'


def _id(value):
    text=str(value)
    if isinstance(value,bool) or not isinstance(value,(str,int)) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',text):
        raise ValueError('identity_unproven')
    return text


def _amount(value):
    if isinstance(value,bool):
        raise ValueError('amount_unproven')
    try:
        amount=Decimal(str(value))
        if not amount.is_finite():
            raise ValueError('amount_unproven')
        return str(amount)
    except InvalidOperation:
        raise ValueError('amount_unproven') from None


def _base(row,kind,account):
    identifier=_id(row.get('id'))
    currency=row.get('currency')
    if not isinstance(currency,str) or not re.fullmatch(r'[A-Z]{3}',currency):
        raise ValueError('currency_unproven')
    return {'record_kind':kind,'provider_event_id':identifier,'account_id':account,
        'currency':currency,'classification':'UNKNOWN','attribution':'UNKNOWN',
        'program_id':None,'commission_minor':None,'cash_minor':None}


def collect(transport,*,start,end,deadline=None,max_requests=32):
    t=ReadTransport(transport,deadline=deadline,max_requests=max_requests)
    records=[]
    binding='UNKNOWN'
    identity=None
    counts={}
    try:
        lo,hi=_window(start,end)
        token=credentials.secret_text('glpclinicscout','digistore24_api_key').strip()
        if not token:
            raise FileNotFoundError('cached_api_key_missing')
        headers={'X-DS-API-KEY':token}
        def request(fn,params=None):
            response=t.request('GET',API+'/'+fn,headers=headers,params=params or {})
            doc=response.json()
            if not isinstance(doc.get('data'),dict):
                raise ValueError('data_schema')
            return doc['data']
        user=request('getUserInfo')
        account=_id(user.get('user_id'))
        roles=user.get('granted_roles')
        if not isinstance(roles,str) or 'affiliate' not in roles.split(','):
            raise ValueError('affiliate_role_unproven')
        identity={'account_id':account,'native_id_type':'api_key_holder_user_id','roles':[r for r in roles.split(',') if r in {'affiliate','merchant','user'}],
            'evidence':{'response_sha256':t.receipts[-1]['response_sha256'],'pointer':'/data/user_id'},'scope':'ONE_AUTHENTICATED_USER_NOT_ALL_ACCOUNTS_OR_PROVIDER_APPROVAL'}
        binding='RESPONSE_OWNED_USER_ID:'+account
        expected=None
        seen=set()
        page=1
        while True:
            data=request('listCommissions',{'from':lo.strftime('%Y-%m-%d %H:%M:%S'),'to':hi.strftime('%Y-%m-%d %H:%M:%S'),
                'page_no':page,'page_size':100,'transaction_type':'payment,refund,refund_request,chargeback','commission_type':'all'})
            count=data.get('item_count')
            pages=data.get('page_count')
            items=data.get('items')
            if (type(count) is not int or count<0 or type(pages) is not int or pages<0 or type(data.get('page_no')) is not int or data['page_no']!=page
                    or not isinstance(items,list) or len(items)>100 or str(data.get('page_size'))!='100'
                    or (expected is not None and expected!=count) or pages!=(count+99)//100):
                raise ValueError('pagination_unproven')
            expected=count
            for index,row in enumerate(items):
                if not isinstance(row,dict):
                    raise ValueError('row_schema')
                record=_base(row,'commission',account)
                if record['provider_event_id'] in seen:
                    raise ValueError('duplicate_commission')
                seen.add(record['provider_event_id'])
                record.update(commission_id=record['provider_event_id'],transaction_id=_id(row.get('transaction_id')),
                    purchase_id=_id(row.get('purchase_id')),reported_amount=_amount(row.get('amount')),
                    amount_semantics='NATIVE_COMMISSION_NOT_CLASSIFIED_GLP_REVENUE',
                    evidence={'response_sha256':t.receipts[-1]['response_sha256'],'pointer':f'/data/items/{index}'})
                records.append(record)
            if page>=pages:
                if len(seen)!=expected:
                    raise ValueError('count_unproven')
                break
            if not items:
                raise ValueError('pagination_stalled')
            page+=1
        counts['commissions']={'returned':len(seen),'declared':expected,'population_proven':True,'period_proven':False}
        payouts=request('listPayouts').get('payout_list')
        if not isinstance(payouts,list):
            raise ValueError('payout_schema')
        payout_ids=set()
        for index,row in enumerate(payouts):
            if not isinstance(row,dict):
                raise ValueError('payout_schema')
            record=_base(row,'credit_note',account)
            if record['provider_event_id'] in payout_ids:
                raise ValueError('duplicate_credit_note')
            payout_ids.add(record['provider_event_id'])
            record.update(payout_id=record['provider_event_id'],reported_amount=_amount(row.get('affiliate_net_amount')),
                amount_semantics='NATIVE_AFFILIATE_CREDIT_NOTE_NOT_VERIFIED_CASH_OR_PERIOD',
                evidence={'response_sha256':t.receipts[-1]['response_sha256'],'pointer':f'/data/payout_list/{index}'})
            records.append(record)
        counts['payouts']={'returned':len(payouts),'declared':None,'population_proven':False}
        totals={'commission_item_count':expected,'credit_note_returned_count':len(payouts),
            'reported_commission_amount':'0' if expected==0 else None,
            'amount_scope':'UNCLASSIFIED_ACCOUNT_REQUESTED_VENDOR_CALENDAR_NOT_GLP_CASH'}
        return AccessResult(network='digistore24',capability='conversions_earnings',status='SUPPORTED',source='DIGISTORE24_OFFICIAL_FINANCIAL_API',coverage='PARTIAL',
            records=tuple(records),reason='ACCOUNT_AND_COMMISSION_COUNT_BOUND_QUERY_CALENDAR_REAL_TEST_PROVIDER_CROSSWALK_PAYOUT_MEMBERSHIP_UNPROVEN',
            receipts=tuple(t.receipts),account_binding=binding,reported_totals=totals,resource_counts=counts,source_identity=identity,
            declared_row_count=expected,returned_row_count=len(seen))
    except (OSError,ValueError,KeyError,TypeError) as error:
        return AccessResult(network='digistore24',capability='conversions_earnings',status='SUPPORTED',source='DIGISTORE24_OFFICIAL_FINANCIAL_API',
            coverage='UNKNOWN' if isinstance(error,ProviderResponseError) or not t.receipts else 'PARTIAL',records=tuple(records),
            reason='DIGISTORE24_SCHEMA_OR_POPULATION_UNPROVEN_NO_RETRY',receipts=tuple(t.receipts),account_binding=binding,
            resource_counts=counts,source_identity=identity)
