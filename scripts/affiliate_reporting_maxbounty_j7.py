"""J7 exact MaxBounty absence contract; all other failures stay fail-closed.

Official /accountBalance/{year} is a balance-history resource, not an
account-settings or report authorization resource. The observed source refuses
with its exact no-history statement. Preserve success=false and distinguish
ABSENT from an empty success page; never infer paid cash or a settled period.
"""
import hashlib,re,time
from datetime import datetime,timezone
from dataclasses import dataclass
from urllib.parse import urlparse
import affiliate_reporting_maxbounty as old
from affiliate_network_access_j5 import AccessResult as PriorResult
from affiliate_network_access import ProviderResponseError

NO_BALANCES='You currently have no account balances.'


def explicit_absence(doc):
    return (isinstance(doc,dict) and set(doc)=={'success','errors'} and doc['success'] is False
            and isinstance(doc['errors'],dict) and doc['errors']=={'message':NO_BALANCES})


@dataclass(frozen=True)
class AccessResult(PriorResult):
    resource_counts: dict | None = None
    source_identity: dict | None = None


class ReadTransport(old.ReadTransport):
    def request(self,method,url,**kwargs):
        p=urlparse(url)
        if not (p.hostname=='api.maxbounty.com' and p.path.startswith('/affiliates/api/accountBalance/')):
            return super().request(method,url,**kwargs)
        if (method!='GET' or p.scheme!='https' or p.netloc!='api.maxbounty.com' or p.query or p.fragment
            or not re.fullmatch('/affiliates/api/accountBalance/[0-9]{4}',p.path)):
            raise ValueError('route_not_read_allowlisted')
        if len(self.receipts)>=self.max_requests or time.monotonic()>=self.deadline:
            raise ValueError('budget_exhausted')
        kwargs['timeout']=max(.1,min(30,self.deadline-time.monotonic()))
        response=self.transport.request(method,url,**kwargs)
        doc=response.json() if response.status==200 else None
        absence=explicit_absence(doc)
        self.receipts.append({'method':method,'endpoint':url,'http_status':response.status,
            'response_sha256':hashlib.sha256(response.body).hexdigest(),'fetched_at':datetime.now(timezone.utc).isoformat(),
            'resource_state':'SOURCE_DECLARED_NO_BALANCE_HISTORY' if absence else 'RESPONSE_REQUIRES_VALIDATION',
            'provider_success':doc.get('success') if isinstance(doc,dict) else None})
        if response.status!=200 or not isinstance(doc,dict) or not (absence or doc.get('success') is True):
            raise ProviderResponseError('balance_response_unproven')
        self.raw_documents.append(doc)
        return response

# Reuse the authorized authenticator and projections without changing originals.
_window=old._window
_session=old._session
_identifier=old._identifier
_amount=old._amount
_row=old._row
SOURCE=old.SOURCE


def collect(transport, *, start, end, deadline=None, max_requests=32):
    from datetime import timedelta
    from urllib.parse import urlencode
    t=ReadTransport(transport,deadline=deadline,max_requests=max_requests)
    records,totals,binding,api=[],None,'UNKNOWN',None
    identity=None
    counts={'conversions':None,'reversals':None,'balance_transactions':None}
    reason='CREDENTIAL_HTTP_SCHEMA_OR_PERIOD_UNPROVEN_NO_RETRY'
    try:
        a,b=_window(start,end)
        if any((x.hour,x.minute,x.second,x.microsecond)!=(0,0,0,0) for x in (a,b)):
            raise ValueError('calendar_boundaries_required')
        api=_session(t);api.authenticate()
        publisher=_identifier(t.raw_documents[-1].get('publisher_id'))
        if publisher is None:raise ValueError('publisher_identity_missing')
        binding='RESPONSE_OWNED_PUBLISHER_ID:'+publisher
        identity={'account_id':publisher,'native_id_type':'publisher_id',
                  'evidence':{'pointer':'/publisher_id','response_sha256':t.receipts[-1]['response_sha256']},
                  'scope':'AUTHENTICATED_PUBLISHER_ONLY_NOT_PROGRAM_OR_FINANCIAL_CLASSIFICATION'}
        dates={'startDate':a.strftime('%Y-%m-%d'),'endDate':(b-timedelta(days=1)).strftime('%Y-%m-%d')}
        _,doc,_=api._request('/reports/earnings?'+urlencode(dates))
        if doc.get('success') is not True or not isinstance(doc.get('report'),list) or not isinstance(doc.get('totals'),dict):
            raise ValueError('earnings_schema')
        totals={k:_amount(doc['totals'].get(k)) for k in ('earnings','clicks','leads','sales')}
        totals.update(receipt_sha256=t.receipts[-1]['response_sha256'],receipt_pointer='/totals',
                      scope='WHOLE_ACCOUNT_UNCLASSIFIED_NOT_PROVIDER_ELIGIBLE_REVENUE',currency=None)
        for index,row in enumerate(doc['report']):
            rec=_row(row,kind='campaign_earnings',pointer='/report/'+str(index))
            if rec:records.append(dict(rec,receipt_sha256=t.receipts[-1]['response_sha256']))
        for kind in ('conversions','reversals'):
            page,expected,count=1,None,0
            while True:
                _,doc,_=api._request('/reports/'+kind+'?'+urlencode(dict(dates,page=page)))
                rows,pages=doc.get('report'),doc.get('total_pages')
                if (doc.get('success') is not True or not isinstance(rows,list) or len(rows)>50 or type(pages) is not int
                    or pages<0 or type(doc.get('page')) is not int or doc['page']!=page):raise ValueError('pagination_schema')
                if expected is not None and expected!=pages:raise ValueError('page_count_changed')
                expected=pages
                if pages==0 and (page!=1 or rows):raise ValueError('empty_page_count_mismatch')
                if page>pages and not (pages==0 and page==1):raise ValueError('page_overrun')
                count+=len(rows)
                for index,row in enumerate(rows):
                    rec=_row(row,kind=kind,pointer='/report/'+str(index))
                    if rec:records.append(dict(rec,receipt_sha256=t.receipts[-1]['response_sha256']))
                if pages==0 or page==pages:break
                if not rows:raise ValueError('missing_nonterminal_page')
                page+=1
            counts[kind]=count
        balance_count=0
        for year in range(a.year,(b-timedelta(days=1)).year+1):
            _,doc,_=api._request('/accountBalance/'+str(year))
            if explicit_absence(doc):continue
            if doc.get('success') is not True or not isinstance(doc.get('account_balances'),list):
                raise ValueError('balance_schema_no_empty_inference')
            balance_count+=len(doc['account_balances'])
            for index,row in enumerate(doc['account_balances']):
                rec=_row(row,kind='account_balance_transaction',pointer='/account_balances/'+str(index))
                if rec:records.append(dict(rec,receipt_sha256=t.receipts[-1]['response_sha256'],source_scope='WHOLE_YEAR_NOT_REQUESTED_PERIOD'))
        counts['balance_transactions']=balance_count
        reason='OFFICIAL_REPORTS_AND_BALANCE_HISTORY_READ_CALENDAR_CURRENCY_REAL_SETTLEMENT_UNKNOWN'
    except FileNotFoundError:reason='EXISTING_API_CREDENTIAL_PATH_MISSING'
    except Exception:pass
    finally:
        if api:api.close()
    return AccessResult('maxbounty','conversions_earnings','SUPPORTED',SOURCE,
        coverage='PARTIAL' if records or t.receipts else 'UNKNOWN',records=tuple(records),receipts=tuple(t.receipts),
        account_binding=binding,reported_totals=totals,resource_counts=counts,source_identity=identity,reason=reason)
