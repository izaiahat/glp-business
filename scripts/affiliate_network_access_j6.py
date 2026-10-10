"""Additive Digistore24 read contract; frozen predecessors remain untouched."""
from dataclasses import dataclass
import re
from urllib.parse import urlparse
import affiliate_network_access_j5 as j5
import affiliate_network_access as legacy

@dataclass(frozen=True)
class AccessResult(j5.AccessResult):
    resource_counts: dict | None = None
    source_identity: dict | None = None

class ReadTransport(j5.ReadTransport):
    def request(self,method,url,**kwargs):
        parsed=urlparse(url)
        if parsed.hostname != 'www.digistore24.com':
            return super().request(method,url,**kwargs)
        if (method!='GET' or parsed.scheme!='https' or parsed.netloc!='www.digistore24.com'
                or not re.fullmatch(r'/api/call/(getUserInfo|listCommissions|listPayouts)',parsed.path)):
            raise ValueError('route_not_read_allowlisted')
        # Delegate envelope/budget/hash handling after the route is validated.
        import hashlib,time
        from datetime import datetime,timezone
        if len(self.receipts)>=self.max_requests or time.monotonic()>=self.deadline:
            raise ValueError('budget_exhausted')
        kwargs['timeout']=max(.1,min(30,self.deadline-time.monotonic()))
        response=self.transport.request(method,url,**kwargs)
        self.receipts.append({'method':method,'endpoint':url,'http_status':response.status,
            'response_sha256':hashlib.sha256(response.body).hexdigest(),'fetched_at':datetime.now(timezone.utc).isoformat()})
        if response.status!=200:
            raise legacy.ProviderResponseError('http_status_unproven')
        doc=response.json()
        if not isinstance(doc,dict) or doc.get('result')!='success':
            raise legacy.ProviderResponseError('provider_response_unproven')
        self.raw_documents.append(doc)
        return response


def conversions_earnings(network,transport,**kwargs):
    if network!='digistore24':
        return j5.conversions_earnings(network,transport,**kwargs)
    from affiliate_reporting_digistore24 import collect
    return collect(transport,**kwargs)


def weekly_collectors(names,transport):
    collectors=j5.weekly_collectors(names,transport)
    if 'digistore24' not in names:
        return collectors
    def collect(window,deadline,max_requests,*,sink):
        observations,remaining=[],max_requests
        for label,start,end in [('closed_week',window['week_start'],window['end_exclusive']),('lookback',window['lookback_start'],window['end_exclusive']),('closed_month',window['last_closed_month_start'],window['last_closed_month_end'])]:
            result=conversions_earnings('digistore24',transport,start=start,end=end,deadline=deadline,max_requests=remaining)
            remaining-=len(result.receipts)
            page=result.to_dict()
            page.update(schema='coverage_observation',period_purpose=label,start_inclusive=start,end_exclusive=end,
                payload_complete=False,account_scope_complete=False,financial_classification='UNKNOWN',reporting_timezone='UNKNOWN_QUERY_CALENDAR_CEST_IS_CURRENT_RESPONSE_NOT_PERIOD_PROOF')
            sink(page)
            observations.append(page)
            if any(r['http_status'] in (401,403,429) for r in result.receipts):
                break
        return observations
    collectors['digistore24']=collect
    return collectors
