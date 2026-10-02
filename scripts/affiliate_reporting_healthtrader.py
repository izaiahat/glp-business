"""Official HealthTrader conversion search; vendor dates are not proven UTC.

Apiary reference verified 2026-10-02. Cached bearer only, never token mint.
No customer/product fields, tracking links, TEST/REAL or cash inference.
"""
from datetime import datetime, timedelta
import re
from affiliate_network_access import AccessResult, ReadTransport, ProviderResponseError, _window
from affiliate_reporting_partnerstack import _identifier, _amount
from marketplace_adapters import _base

SOURCE = 'https://api.healthtrader.com/affiliates/v1/conversions/search'
DOCS = ('https://htaffiliate.docs.apiary.io/reference/conversions/conversions-search/search-all-conversions',)
REPORTING_TIMEZONE = 'UNKNOWN'
TIME_AXIS = 'vendor_conversion_created_date'


def collect(transport, *, start, end, deadline=None, max_requests=32):
    a,b=_window(start,end)
    t=ReadTransport(transport,deadline=deadline,max_requests=max_requests)
    records=[]
    reason='HTTP_SCHEMA_OR_VENDOR_PERIOD_UNPROVEN_NO_RETRY'
    try:
        if any(x.hour or x.minute or x.second for x in (a,b)):
            raise ValueError('calendar_boundaries_required')
        try:
            token=_base.secret_json('affiliate-marketplaces','healthtrader','access_token.json').get('access_token')
        except FileNotFoundError:
            token=None
        if not isinstance(token,str) or not token.strip():
            return AccessResult('healthtrader','conversions_earnings','SUPPORTED',SOURCE,
                reason='CACHED_ACCESS_TOKEN_MISSING_NO_TOKEN_MINT')
        page,seen,expected=1,set(),None
        while True:
            doc=t.request('GET',SOURCE,headers={'Authorization':'Bearer '+token,'Accept':'application/json'},
                params={'date_from':a.date().isoformat(),'date_to':(b-timedelta(days=1)).date().isoformat(),
                        'page':page,'per_page':50,'sort':'id'}).json()
            if (not isinstance(doc,dict) or doc.get('error') or doc.get('errors') or doc.get('success') is False):
                raise ProviderResponseError('provider_or_schema_error')
            rows,info=doc.get('conversions'),doc.get('info')
            if not isinstance(rows,list) or not isinstance(info,dict) or len(rows)>50:
                raise ProviderResponseError('schema_unrecognized')
            total=info.get('total_records')
            if type(total) is not int or total<0 or (expected is not None and total!=expected):
                raise ValueError('count_unproven')
            expected=total
            for row in rows:
                if not isinstance(row,dict) or not _identifier(row.get('id')) or str(row['id']) in seen:
                    raise ValueError('identity')
                stamp=row.get('created')
                if not isinstance(stamp,str):
                    raise ValueError('created_missing')
                vendor_date=datetime.fromisoformat(stamp.replace('Z','+00:00')).date()
                if not a.date()<=vendor_date<b.date():
                    raise ValueError('vendor_date_population_mismatch')
                seen.add(str(row['id']))
                if 'fella' in str(row.get('offer','')).casefold():
                    continue
                currency=row.get('currency_base')
                status=row.get('status')
                records.append({'provider_event_id':str(row['id']),
                    'source_status':status if status in ('Approved','Declined','Pending','Paid') else 'unknown',
                    'source_created':stamp,'source_timezone':'UNKNOWN',
                    'reported_commission':_amount(row.get('commission_amount')),
                    'amount_semantics':'reported_commission_not_verified_cash',
                    'currency':currency if isinstance(currency,str) and re.fullmatch(r'[A-Z]{3}',currency) else None,
                    'classification':'UNKNOWN','attribution':'UNKNOWN'})
            if len(seen)>total:
                raise ValueError('count_mismatch')
            if len(seen)==total:
                reason='ROWS_RECONCILED_VENDOR_DATE_TIMEZONE_INCLUSIVITY_UNPROVEN_TEST_REAL_CASH_UNKNOWN'
                break
            if not rows:
                raise ValueError('missing_page')
            page+=1
        return AccessResult('healthtrader','conversions_earnings','SUPPORTED',SOURCE,
            coverage='PARTIAL',records=tuple(records),receipts=tuple(t.receipts),reason=reason,
            account_binding='CONFIGURED_CACHED_BEARER_NOT_RESPONSE_OWNED_IDENTITY')
    except Exception as error:
        failed=not t.receipts or isinstance(error,ProviderResponseError) or any(not 200<=r['http_status']<300 for r in t.receipts)
        return AccessResult('healthtrader','conversions_earnings','SUPPORTED',SOURCE,
            coverage='UNKNOWN' if failed else 'PARTIAL',records=tuple(records) if not failed else (),
            receipts=tuple(t.receipts),reason=reason)
