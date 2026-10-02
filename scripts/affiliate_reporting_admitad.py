"""Admitad publisher actions: cached bearer or one client-credentials exchange.

Official reports: https://developers.mitgo.com/hc/en-us/articles/34481279018130-Publisher-reports
Client authorization: https://developers.mitgo.com/hc/en-us/articles/34481345606546-Client-authorization
Only the read-only statistics scope is requested. Tokens remain in memory;
no credential-file writes, refresh/retry, or token bodies in receipts.
Date-only filters and timezone-free timestamps do not prove UTC coverage.
"""
from datetime import datetime, timedelta
from affiliate_network_access import AccessResult, ReadTransport, ProviderResponseError, _window
from affiliate_reporting_partnerstack import _identifier, _amount
from marketplace_adapters import _base
import re

API='https://api.admitad.com/statistics/actions/'


def _headers(t):
    import time
    try:
        token=_base.secret_text('affiliate-marketplaces','admitad','access_token').strip()
    except FileNotFoundError:
        token=''
    if not token:
        client=_base.secret_text('affiliate-marketplaces','admitad','client_id').strip()
        secret=_base.secret_text('affiliate-marketplaces','admitad','client_secret').strip()
        if not client or not secret:
            raise FileNotFoundError('cached_token_missing')
        if t.max_requests < 1 or time.monotonic() >= t.deadline:
            raise ValueError('read_budget_exhausted')
        # Authentication is separate from the reporting read budget. Fixed HTTPS
        # origin and no redirects; its secret response is never a page receipt.
        raw=t.transport
        if isinstance(raw,_base.RequestsTransport):
            raw._sess().max_redirects=0
        request=getattr(raw,'auth_request',raw.request)
        response=request('POST','https://api.admitad.com/token/',
            auth=(client,secret),headers={'Accept':'application/json'},
            data={'grant_type':'client_credentials','client_id':client,'scope':'statistics'},
            timeout=max(.1,min(30,t.deadline-time.monotonic())))
        if response.status != 200:
            raise ProviderResponseError('token_exchange_failed_no_retry')
        doc=response.json()
        if (not isinstance(doc,dict) or doc.get('error') or
                not isinstance(doc.get('access_token'),str) or not doc['access_token'].strip() or
                str(doc.get('token_type','')).casefold() != 'bearer' or
                not isinstance(doc.get('scope'),str) or 'statistics' not in doc['scope'].split() or
                type(doc.get('expires_in')) is not int or doc['expires_in'] <= 0):
            raise ProviderResponseError('token_schema_or_scope_unproven')
        token=doc['access_token'].strip()
    return {'Authorization':'Bearer '+token}


def _project(row):
    if 'fella' in str(row.get('advcampaign_name','')).casefold():
        return None
    status=row.get('status')
    return {'provider_event_id':str(row['action_id']),
            'program_id':_identifier(row.get('advcampaign_id')),
            'source_status':status if status in {'pending','approved','declined','approved_but_stalled'} else 'unknown',
            'reported_amount':_amount(row.get('payment')),
            'amount_semantics':'reported_action_payment_not_verified_cash',
            'currency':row.get('currency') if isinstance(row.get('currency'),str) and re.fullmatch(r'[A-Z]{3}',row['currency']) else None,
            'source_processed':row.get('processed') if type(row.get('processed')) is int and row['processed'] in (0,1) else None,
            'source_paid':row.get('paid') if type(row.get('paid')) is int and row['paid'] in (0,1) else None,
            'source_action_date':row['action_date'], 'source_timezone':'UNKNOWN',
            'classification':'UNKNOWN','attribution':'UNKNOWN'}


def collect(transport, *, start, end, deadline=None, max_requests=32):
    t=ReadTransport(transport,deadline=deadline,max_requests=max_requests)
    records=[]
    binding='CONFIGURED_CACHED_BEARER_NOT_RESPONSE_OWNED_IDENTITY'
    reason='READ_FAILED_OR_COUNT_PAGINATION_PERIOD_UNPROVEN_NO_RETRY'
    try:
        a,b=_window(start,end)
        if a.hour or a.minute or a.second or b.hour or b.minute or b.second:
            raise ValueError('calendar_boundaries_required')
        try:
            headers=_headers(t)
        except FileNotFoundError:
            return AccessResult('admitad','conversions_earnings','SUPPORTED',API,
                account_binding=binding,reason='CACHED_ACCESS_TOKEN_MISSING_NO_TOKEN_MINT')
        offset,expected,seen=0,None,set()
        while True:
            params={'limit':500,'offset':offset,'total':0,'order_by':'datetime',
                    'date_start':a.strftime('%d.%m.%Y'),
                    'date_end':(b-timedelta(days=1)).strftime('%d.%m.%Y')}
            doc=t.request('GET',API,headers=headers,params=params).json()
            if not isinstance(doc,dict):
                raise ValueError('schema')
            if doc.get('success') is False or doc.get('status') not in (None,200,'200','success'):
                raise ProviderResponseError('provider_error')
            rows,meta=doc.get('results'),doc.get('_meta')
            if not isinstance(rows,list) or not isinstance(meta,dict) or len(rows)>500:
                raise ValueError('pagination_schema')
            total=meta.get('count')
            if type(total) is not int or total<0 or type(meta.get('offset')) is not int or meta['offset']!=offset or type(meta.get('limit')) is not int or meta['limit']!=500:
                raise ValueError('count_pagination_mismatch')
            if expected is not None and total!=expected:
                raise ValueError('count_changed')
            expected=total
            for row in rows:
                if not isinstance(row,dict) or not _identifier(row.get('action_id')) or str(row['action_id']) in seen:
                    raise ValueError('identity')
                stamp=row.get('action_date')
                if not isinstance(stamp,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}',stamp):
                    raise ValueError('vendor_action_date_schema')
                vendor_date=datetime.strptime(stamp,'%Y-%m-%d %H:%M:%S').date()
                if not a.date()<=vendor_date<b.date():
                    raise ValueError('vendor_date_population_mismatch')
                seen.add(str(row['action_id']))
                record=_project(row)
                if record:
                    records.append(record)
            offset+=len(rows)
            if offset>total:
                raise ValueError('count_mismatch')
            if offset==total:
                reason='ROWS_RECONCILED_VENDOR_DATE_TIMEZONE_UNPROVEN_TEST_REAL_CASH_UNKNOWN'
                break
            if not rows:
                raise ValueError('missing_page')
        return AccessResult('admitad','conversions_earnings','SUPPORTED',API,
            coverage='PARTIAL',records=tuple(records),receipts=tuple(t.receipts),
            account_binding=binding,reason=reason)
    except Exception as error:
        failed=not t.receipts or isinstance(error,ProviderResponseError) or any(not 200<=r['http_status']<300 for r in t.receipts)
        return AccessResult('admitad','conversions_earnings','SUPPORTED',API,
            coverage='UNKNOWN' if failed else 'PARTIAL',records=tuple(records) if not failed else (),
            receipts=tuple(t.receipts),account_binding=binding,reason=reason)
