"""Read PartnerStack reports without combining rewards, sales and withdrawals.

Contracts: https://docs.partnerstack.com/reference/get_v2-rewards,
get_v2-transactions, get_v2-payouts and /docs/postbacks. Epoch-ms created
filters bound UTC creation coverage, not transaction/reward settlement time.
"""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import re

from affiliate_network_access import AccessResult, ReadTransport, ProviderResponseError, _window
from marketplace_adapters import _base

API = 'https://api.partnerstack.com/api/v2'
SEMANTICS = {'rewards':'reported_reward_not_cash',
             'transactions':'reported_customer_transaction_not_commission',
             'payouts':'reported_payout_not_verified_settlement'}


def _headers():
    token=_base.secret_text('glpclinicscout','partnerstack_api_key').strip()
    if not token:
        raise FileNotFoundError('cached_token_missing')
    return {'Authorization':'Bearer '+token}


def _identifier(value):
    return (str(value) if isinstance(value,(str,int)) and not isinstance(value,bool)
            and re.fullmatch(r'[A-Za-z0-9_-]{1,100}',str(value)) else None)


def _amount(value):
    if isinstance(value,bool):
        return None
    try:
        number = Decimal(str(value))
        return str(number) if number.is_finite() else None
    except (InvalidOperation,ValueError):
        return None


def _project(row, resource):
    company = row.get('company') if isinstance(row.get('company'),dict) else {}
    if 'fella' in str(company.get('name','')).casefold():
        return None
    status = row.get('status',row.get('reward_status'))
    return {'resource':resource, 'provider_event_id':str(row['key']),
            'program_id':_identifier(company.get('key')),
            'source_status':status if status in {'pending','hold','approved','declined','paid','reversed'} else 'unknown',
            'reported_amount':_amount(row.get('amount')),
            'amount_unit':'UNKNOWN' if resource=='payouts' else 'reported_minor_units',
            'amount_semantics':SEMANTICS[resource],
            'currency':row.get('currency') if isinstance(row.get('currency'),str) and re.fullmatch(r'[A-Z]{3}',row['currency']) else None,
            'classification':'UNKNOWN', 'attribution':'UNKNOWN'}


def collect(transport, *, start, end, deadline=None, max_requests=32):
    t=ReadTransport(transport,deadline=deadline,max_requests=max_requests)
    projected=[]
    count=0
    empty_counts_proven=True
    binding='CONFIGURED_PARTNER_BEARER_NOT_RESPONSE_OWNED_IDENTITY'
    try:
        a,b=_window(start,end)
        headers=_headers()
        lo,hi=int(a.timestamp()*1000),int(b.timestamp()*1000)-1
        for resource in SEMANTICS:
            cursor=None
            expected=None
            seen=set()
            resource_count=0
            while True:
                params={'limit':250,'min_created':lo,'max_created':hi,'order_by':'created_at'}
                if cursor:
                    params['starting_after']=cursor
                doc=t.request('GET',API+'/'+resource,headers=headers,params=params).json()
                if not isinstance(doc,dict):
                    raise ValueError('schema')
                if doc.get('status') not in (None,200,'200','success') or doc.get('success') is False:
                    raise ProviderResponseError('provider_error')
                data=doc.get('data')
                if not isinstance(data,dict) or not isinstance(data.get('items'),list) or type(data.get('has_more')) is not bool:
                    raise ValueError('pagination_schema')
                rows=data['items']
                if len(rows)>250:
                    raise ValueError('limit_exceeded')
                if 'total' in data:
                    total=data['total']
                    if type(total) is not int or total<0 or (expected is not None and expected!=total):
                        raise ValueError('count_mismatch')
                    expected=total
                for row in rows:
                    if not isinstance(row,dict) or not _identifier(row.get('key')) or row['key'] in seen:
                        raise ValueError('identity')
                    stamp=row.get('created_at')
                    if type(stamp) is not int or not lo<=stamp<=hi:
                        raise ValueError('creation_period_unproven')
                    seen.add(row['key'])
                    record=_project(row,resource)
                    if record:
                        projected.append(record)
                resource_count+=len(rows)
                count+=len(rows)
                if expected is not None and resource_count>expected:
                    raise ValueError('count_mismatch')
                if not data['has_more']:
                    if expected is not None and resource_count!=expected:
                        raise ValueError('count_mismatch')
                    if resource_count==0 and expected!=0:
                        empty_counts_proven=False
                    break
                if not rows:
                    raise ValueError('cursor_missing')
                cursor=rows[-1]['key']
        return AccessResult('partnerstack','conversions_earnings','SUPPORTED',API,
            coverage='COMPLETE' if empty_counts_proven else 'PARTIAL',records=tuple(projected),
            record_count=count if empty_counts_proven else None,
            report_complete_period=empty_counts_proven and b<=datetime.now(timezone.utc), receipts=tuple(t.receipts),
            account_binding=binding,reason='CREATION_ROWS_ONLY_RESOURCES_DISTINCT_TEST_REAL_AND_SETTLEMENT_UNKNOWN'
                if empty_counts_proven else 'EMPTY_RESOURCE_DECLARED_ZERO_UNPROVEN')
    except Exception as error:
        failed=not t.receipts or isinstance(error,ProviderResponseError) or any(not 200<=r['http_status']<300 for r in t.receipts)
        return AccessResult('partnerstack','conversions_earnings','SUPPORTED',API,
            coverage='UNKNOWN' if failed else 'PARTIAL',records=tuple(projected) if not failed else (),
            receipts=tuple(t.receipts),account_binding=binding,
            reason='READ_FAILED_OR_COUNT_PAGINATION_PERIOD_UNPROVEN_NO_RETRY')
