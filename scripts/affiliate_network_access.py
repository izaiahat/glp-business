#!/usr/bin/env python3
"""Read-only affiliate access contract. Unknown money never becomes zero.

Access support and observation coverage are separate. No applications, link
creation, browser fallback, classification, ledger ingestion or settlement.
"""
from __future__ import annotations
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Literal

Support = Literal['SUPPORTED', 'PORTAL_ONLY', 'UNSUPPORTED', 'UNKNOWN']
Coverage = Literal['COMPLETE', 'PARTIAL', 'UNKNOWN']

# The scheduler roster selects accounts; this tuple identifies reviewed readers.
ADDITIONAL_REPORTING_NETWORKS = ('partnerstack', 'admitad', 'clickbank', 'refersion', 'healthtrader')
REPORTING_TIMEZONES = {'partnerstack':'UTC', 'refersion':'UTC', 'admitad':'UNKNOWN',
                      'clickbank':'UNKNOWN_VENDOR_CALENDAR', 'healthtrader':'UNKNOWN'}


@dataclass(frozen=True)
class AccessResult:
    network: str
    capability: str
    status: Support = 'UNKNOWN'
    source: str = 'NO_AUTHENTICATED_ROUTE_BOUND'
    fetched_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    coverage: Coverage = 'UNKNOWN'
    records: tuple[dict, ...] = ()
    record_count: int | None = None
    commission_minor: int | None = None
    cash_minor: int | None = None
    reason: str = 'NO_REVIEWED_READ_ADAPTER'
    receipts: tuple[dict, ...] = ()
    account_binding: str = 'UNKNOWN'
    report_complete_period: bool = False

    def __post_init__(self):
        if self.status not in {'SUPPORTED','PORTAL_ONLY','UNSUPPORTED','UNKNOWN'}:
            raise ValueError('access_status_invalid')
        if self.coverage not in {'COMPLETE','PARTIAL','UNKNOWN'}:
            raise ValueError('coverage_invalid')
        if (self.status != 'SUPPORTED' or self.coverage != 'COMPLETE') and any(
                v is not None for v in (self.record_count,self.commission_minor,self.cash_minor)):
            raise ValueError('unknown_cannot_be_numeric')
        if self.record_count is not None and (type(self.record_count) is not int or self.record_count < 0):
            raise ValueError('record_count_invalid')

    def to_dict(self):
        return asdict(self)


class ProviderResponseError(ValueError):
    """An explicit provider error proves no observation coverage."""


def _failure_coverage(transport, error):
    return ('UNKNOWN' if isinstance(error, ProviderResponseError) or not transport.receipts
            else 'PARTIAL')


def _meaningful(value):
    """Empty nested containers and parser placeholders are not field evidence."""
    if isinstance(value, str):
        return bool(value.strip()) and value.strip().casefold() != 'unknown'
    if isinstance(value, dict):
        return any(_meaningful(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return bool(value) and all(_meaningful(v) for v in value)
    return value is not None


def _projection(network, rows):
    """Allowlisted reported money; no customer/click/order data or cash inference."""
    import re
    from decimal import Decimal, InvalidOperation
    fields = {'awin':('id','advertiserId','commissionStatus','commissionAmount','currency'),
              'impact':('Id','CampaignId','State','Payout','Currency'),
              'affiliati':('conversion_id','offer_id','disposition','price','currency'),
              'partnerize':('conversion_id','campaign_id','conversion_status','publisher_commission','currency'),
              'katalys':('seq','offer_id','status','payout','currency')}
    event, program, status, amount, currency = fields[network]
    out=[]
    for item in rows:
        row=item.get('conversion',item)
        if any('fella' in str(row.get(k,'')).casefold() for k in ('advertiserName','AdvertiserName','offer_name','CampaignName')):
            continue
        identity=str(row.get(program,''))
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',identity):
            continue
        value=row.get(amount)
        cur=row.get(currency)
        if isinstance(value,dict):
            cur=value.get('currency')
            value=value.get('amount')
        try:
            number=Decimal(str(value))
            money=str(number) if number.is_finite() else None
        except (InvalidOperation,ValueError):
            money=None
        raw_status=str(row.get(status,'')).lower()
        out.append({'program_id':identity,
            'source_status':raw_status if raw_status in {'pending','approved','declined','reversed','locked','new','extended','closed'} else 'unknown',
            'reported_amount':money,'amount_semantics':'price_currency_unverified' if network=='affiliati' else 'reported_commission_not_cash',
            'currency':cur if isinstance(cur,str) and re.fullmatch(r'[A-Z]{3}',cur) else None,
            'classification':'UNKNOWN','attribution':'UNKNOWN'})
    return out


def _window(start, end):
    a, b = datetime.fromisoformat(start), datetime.fromisoformat(end)
    if a.tzinfo is None or b.tzinfo is None or not a < b:
        raise ValueError('aware_ordered_window_required')
    a, b = a.astimezone(timezone.utc), b.astimezone(timezone.utc)
    if a.microsecond or b.microsecond:
        raise ValueError('whole_second_window_required')
    return a, b


def conversions_earnings(network, transport, *, start, end, deadline=None,
                         max_requests=32, **kwargs) -> AccessResult:
    import hashlib
    import time
    from datetime import timedelta
    a, b = _window(start, end)
    if network in ADDITIONAL_REPORTING_NETWORKS:
        from importlib import import_module
        return import_module('affiliate_reporting_' + network).collect(
            transport, start=start, end=end, deadline=deadline, max_requests=max_requests)
    if network != 'awin':
        return _other_earnings(network, transport, start=start, end=end,
                               deadline=deadline, max_requests=max_requests, **kwargs)
    from marketplace_adapters import awin
    transport = ReadTransport(transport, deadline=deadline, max_requests=max_requests)
    source = awin.API + '/publishers/' + awin.PUBLISHER_ID + '/transactions/'
    receipts, count, projected, seen = [], 0, [], set()
    cursor = a
    deadline = deadline if deadline is not None else time.monotonic() + 120
    try:
        while cursor < b:
            if len(receipts) >= max_requests or time.monotonic() >= deadline:
                raise ValueError('budget_exhausted')
            right = min(cursor + timedelta(days=30), b)
            response = transport.request('GET', source, headers=awin._headers(), params={
                'startDate': cursor.strftime('%Y-%m-%dT%H:%M:%S'),
                'endDate': (right-timedelta(seconds=1)).strftime('%Y-%m-%dT%H:%M:%S'),
                'timezone': 'UTC', 'dateType': 'transaction', 'showBasketProducts': 'false'},
                timeout=max(.1, min(30, deadline-time.monotonic())))
            receipts.append({'method':'GET', 'endpoint':source, 'http_status':response.status,
                'response_sha256':hashlib.sha256(response.body).hexdigest(),
                'start_inclusive':cursor.isoformat(), 'end_exclusive':right.isoformat()})
            if not response.ok:
                raise ValueError('http_error')
            rows = response.json()
            if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
                raise ValueError('schema_invalid')
            for row in rows:
                identity=str(row.get('id',''))
                if not identity or identity in seen:
                    raise ValueError('missing_or_duplicate_transaction')
                seen.add(identity)
            projected.extend(_projection(network,rows))
            count += len(rows)
            cursor = right
        return AccessResult(network, 'conversions_earnings', 'SUPPORTED', source,
            coverage='COMPLETE', record_count=count, receipts=tuple(receipts), records=tuple(projected),
            reason='ACCOUNT_ROWS_ONLY_TEST_REAL_ATTRIBUTION_AND_CURRENCY_UNKNOWN',
            account_binding='request:publisher:' + awin.PUBLISHER_ID,
            report_complete_period=b <= datetime.now(timezone.utc))
    except Exception as error:
        return AccessResult(network, 'conversions_earnings', 'SUPPORTED', source,
            coverage=_failure_coverage(transport, error), receipts=tuple(transport.receipts),
            reason='READ_FAILED_OR_INCOMPLETE_NO_AUTOMATIC_RETRY')


def program_status(network, transport, *, program_id, **kwargs) -> AccessResult:
    return _metadata(network, 'program_status', transport, program_id, **kwargs)


def terms_commission(network, transport, *, program_id, **kwargs) -> AccessResult:
    return _metadata(network, 'terms_commission', transport, program_id, **kwargs)


def issued_tracking_link(network, transport, *, program_id, **kwargs) -> AccessResult:
    return _metadata(network, 'issued_tracking_link', transport, program_id, **kwargs)


def _metadata(network, capability, transport, program_id, **kwargs):
    if network in {'maxbounty', 'katalys', 'everflow', 'xentraffic'}:
        return AccessResult(network, capability, 'PORTAL_ONLY',
            reason='NO_AUTHORIZED_API_ROUTE_NO_BROWSER_IN_THIS_CONTRACT')
    if network != 'impact':
        return _catalogue_metadata(network, capability, transport, program_id, **kwargs)
    source = 'https://api.impact.com/Mediapartners/<sid>/Campaigns/{program_id}.json'
    t = ReadTransport(transport, **kwargs)
    try:
        if not str(program_id).isdigit():
            raise ValueError('exact_program_id_required')
        from marketplace_adapters import impact
        row = impact.campaign(program_id, t)['record']
        if str(row.get('CampaignId')) != str(program_id):
            raise ValueError('program_identity_mismatch')
        keys = {'program_status': ('ContractStatus',),
                'terms_commission': ('PublicTermsAndConditions','TermsAndConditions','PayoutTerms','Payouts','ContractCurrency','PromotionalPolicy'),
                'issued_tracking_link': ('TrackingLink',)}[capability]
        record = {k:row[k] for k in keys if row.get(k) not in (None,'',[])}
        if not record:
            raise ValueError('capability_fields_not_returned')
        record['program_id'] = str(program_id)
        return AccessResult(network, capability, 'SUPPORTED', source,
            coverage='COMPLETE', records=(record,), record_count=1, receipts=tuple(t.receipts),
            reason='EXACT_RECORD_ONLY_NOT_FULL_LEGAL_TERMS_OR_ELIGIBILITY_CLEARANCE',
            account_binding='CONFIGURED_PARTNER_API_CREDENTIAL')
    except Exception as error:
        return AccessResult(network, capability, 'SUPPORTED', source,
            coverage=_failure_coverage(t, error), receipts=tuple(t.receipts),
            reason='EXACT_RECORD_OR_CAPABILITY_FIELD_UNPROVEN')


CATALOGUE_ROUTES = {
    'awin': 'https://api.awin.com/publishers/2979387/programmedetails',
    'cj': 'https://advertiser-lookup.api.cj.com/v2/advertiser-lookup',
    'affiliati': 'https://login.affiliatinetwork.com/affiliates/api/Offers/Feed',
    'partnerize': 'https://api.partnerize.com/user/publisher/1100l432092/campaign/<status>',
    'healthtrader': 'https://api.healthtrader.com/affiliates/v1/offers/list',
}


def _catalogue_metadata(network, capability, transport, program_id, **kwargs):
    import re
    import xml.etree.ElementTree as ET
    if network not in CATALOGUE_ROUTES:
        return AccessResult(network, capability)
    if capability == 'issued_tracking_link' and network in {'awin','partnerize'}:
        return AccessResult(network, capability, 'UNKNOWN', CATALOGUE_ROUTES[network],
            reason='DETERMINISTIC_GRAMMAR_IS_NOT_PROVIDER_ISSUED_LINK_READ_NO_GENERATION')
    source = CATALOGUE_ROUTES[network]
    t = ReadTransport(transport, **kwargs)
    try:
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',str(program_id)):
            raise ValueError('exact_program_id_required')
        if network == 'awin':
            from marketplace_adapters import awin
            if capability == 'program_status':
                source = awin.API+'/publishers/'+awin.PUBLISHER_ID+'/programmes'
                got = awin.readback(t)
                matches = [r for r in got['programs'] if r['program_id'] == str(program_id)]
                if got.get('blockers') or len(matches) != 1:
                    raise ValueError('relationship_not_unique')
                record = {'relationship':matches[0]['relationship']}
            else:
                doc = t.request('GET',source,headers=awin._headers(),params={'advertiserId':str(program_id)}).json()
                row = doc.get('programmeInfo',{})
                if str(row.get('id')) != str(program_id):
                    raise ValueError('program_identity_mismatch')
                record = {k:doc.get(k) for k in ('commissionRange','kpi')}
        elif network == 'cj':
            from marketplace_adapters import cj
            if capability == 'issued_tracking_link':
                source = cj.LINK_SEARCH
                got = cj.link_search(program_id,t,max_pages=1)
                record = {'links':[{'link_id':r['link_id'],'url':r['clickUrl']} for r in got.get('links',[]) if r.get('clickUrl')]}
            else:
                rows = cj._rows(ET.fromstring(cj._lookup(t,{'advertiser-ids':str(program_id)}).body))
                matches = [r for r in rows if r['program_id'] == str(program_id)]
                if len(matches) != 1:
                    raise ValueError('exact_program_not_unique')
                row = matches[0]
                record = ({'relationship':row['relationship']} if capability == 'program_status' else
                          {'actions':row['actions'],'currency':row['currency']})
        elif network == 'affiliati':
            from marketplace_adapters import affiliati
            got = affiliati.readback(t, max_pages=kwargs.get('max_requests',32))
            if got.get('complete') is not True:
                raise ValueError('offer_inventory_incomplete')
            matches = [r for r in got['programs'] if r['program_id'] == str(program_id)]
            if len(matches) != 1:
                raise ValueError('offer_contract_not_unique')
            row = matches[0]
            if capability == 'issued_tracking_link':
                source = affiliati.API+'/Offers/Campaign'
                campaign = affiliati._campaign_configuration(row['campaign_id'],str(program_id),t)
                creative = affiliati._default_creative(campaign)
                record = {'tracking_url':creative.get('unique_link'), 'campaign_id':row['campaign_id'],
                          'creative_id':creative.get('creative_id')}
                if not record['tracking_url']:
                    raise ValueError('issued_link_not_returned')
            else:
                keys = ('relationship','campaign_id','offer_contract_id') if capability == 'program_status' else (
                    'payout_display','paid_event','restrictions','advertiser_extended_terms','allowed_media_types')
                record = {k:row.get(k) for k in keys}
        elif network == 'partnerize':
            from marketplace_adapters import partnerize
            if capability == 'terms_commission':
                source = partnerize.API+'/v2/publishers/'+partnerize.PUBLISHER_ID+'/campaigns/terms'
                doc = t.request('GET',source,auth=partnerize._auth(),params={'campaigns':str(program_id)}).json()
                record = {'terms':[{'title':x.get('title'),'content':partnerize._strip_html(x.get('content'))}
                                  for x in doc.get('terms',[])]}
            else:
                got = partnerize.readback(t)
                matches = [r for r in got['programs'] if r['program_id'] == str(program_id)]
                if got.get('blockers') or len(matches) != 1:
                    raise ValueError('relationship_not_unique')
                record = {'relationship':matches[0]['relationship']}
        else:
            from marketplace_adapters import healthtrader
            # Stored bearer only: do not invoke readback's mint-and-store fallback.
            doc = healthtrader._offers(t,healthtrader.access_token(t)).json()
            matches = [r for r in healthtrader._rows(doc) if str(r.get('unique_id') or r.get('id')) == str(program_id)]
            if len(matches) != 1:
                raise ValueError('offer_not_unique')
            row = matches[0]
            keys = {'program_status':('status',),'terms_commission':('payout','commission','restrictions','currency','description'),
                    'issued_tracking_link':('tracking_url',)}[capability]
            record = {k:row.get(k) for k in keys}
        record = {k:v for k,v in record.items() if _meaningful(v)}
        if (not record or (capability == 'program_status' and
                not _meaningful(record.get('relationship', record.get('status')))) or
                (network == 'partnerize' and capability == 'terms_commission' and
                 not all(_meaningful(term.get('content')) for term in record.get('terms', [])))):
            raise ValueError('capability_fields_missing')
        record['program_id'] = str(program_id)
        return AccessResult(network, capability, 'SUPPORTED', source, coverage='COMPLETE',
            records=(record,), record_count=1, receipts=tuple(t.receipts),
            reason='EXACT_RECORD_ONLY_NOT_FULL_TERMS_CATALOGUE_OR_HOLD_CLEARANCE',
            account_binding='CONFIGURED_PUBLISHER_CREDENTIAL_NOT_RESPONSE_OWNED_IDENTITY')
    except Exception as error:
        return AccessResult(network, capability, 'SUPPORTED', source,
            coverage=_failure_coverage(t, error), receipts=tuple(t.receipts),
            reason='EXACT_RECORD_OR_CAPABILITY_FIELD_UNPROVEN')


def weekly_collectors(names, transport):
    """One observable partition per roster rail, including unknown and aliases."""
    def collector_for(network):
        def collect(window, deadline, max_requests, *, sink):
            observations, remaining = [], max_requests
            periods = [('closed_week',window['week_start'],window['end_exclusive']),
                       ('lookback',window['lookback_start'],window['end_exclusive']),
                       ('closed_month',window['last_closed_month_start'],window['last_closed_month_end'])]
            for label, start, end in periods:
                if network == 'shareasale':
                    result = AccessResult(network,'conversions_earnings',source='canonical:awin',
                        reason='LEGACY_IDENTITY_RETAINED_NO_DUPLICATE_AWIN_READ_OR_APPLICATION')
                else:
                    result = conversions_earnings(network,transport,start=start,end=end,deadline=deadline,
                        max_requests=remaining,
                        profile_id='098e5455-ca72-4099-9ff9-a9dd014148e7' if network == 'katalys' else None)
                remaining -= len(result.receipts)
                page = result.to_dict()
                page.update(schema='coverage_observation',period_purpose=label,
                    start_inclusive=start,end_exclusive=end,payload_complete=result.coverage == 'COMPLETE',
                    account_scope_complete=False,financial_classification='UNKNOWN',
                    profile_binding_source='retained S2 Katalys account read 2026-09-08' if network == 'katalys' else None)
                if network in ADDITIONAL_REPORTING_NETWORKS:
                    page['reporting_timezone'] = REPORTING_TIMEZONES[network]
                sink(page)
                observations.append(page)
                if any(r['http_status'] in (401,403,429) for r in result.receipts):
                    break
            return observations
        return collect
    return {name:collector_for(name) for name in names}


REPORT_ROUTES = {
    'awin': 'https://api.awin.com/publishers/2979387/transactions/',
    'impact': 'https://api.impact.com/Mediapartners/<sid>/Actions',
    'cj': 'https://commissions.api.cj.com/query',
    'affiliati': 'https://login.affiliatinetwork.com/affiliates/api/Reports/Conversions',
    'partnerize': 'https://api.partnerize.com/reporting/report_publisher/publisher/1100l432092/conversion.json',
    'katalys': 'https://api.katalys.com/v1/report/conversions',
}


class ReadTransport:
    """Fixed read routes only; no redirects, mutations, credentials or raw errors in receipts."""
    def __init__(self, transport, *, deadline=None, max_requests=32):
        import time
        from marketplace_adapters._base import RequestsTransport
        if isinstance(transport, RequestsTransport):
            transport._sess().max_redirects = 0
        self.transport = transport
        self.deadline = deadline if deadline is not None else time.monotonic()+120
        self.max_requests = max_requests
        self.receipts = []
        self.raw_documents = []  # in-memory only, never serialized

    def request(self, method, url, **kwargs):
        import hashlib
        import re
        import time
        from urllib.parse import urlsplit
        from marketplace_adapters._base import Response
        parsed = urlsplit(url)
        routes = {
            'api.awin.com': r'/publishers/2979387/(programmes|programmedetails|transactions/)',
            'api.impact.com': r'/Mediapartners/[^/]+/(Campaigns(?:/\d+)?\.json|Actions)',
            'advertiser-lookup.api.cj.com': r'/v2/advertiser-lookup',
            'link-search.api.cj.com': r'/v2/link-search',
            'login.affiliatinetwork.com': r'/affiliates/api/(Offers/(Feed|Campaign|Event)|Creatives/GetCode|Reports/Conversions)',
            'api.partnerize.com': r'/(user/publisher/1100l432092/campaign/(approved|pending|rejected|t)|v2/publishers/1100l432092/campaigns/terms|reference/publisher/camref/1100l432092|reporting/report_publisher/publisher/1100l432092/conversion.json)',
            'api.healthtrader.com': r'/affiliates/v1/offers/list',
            'api.partnerstack.com': r'/api/v2/(rewards|transactions|payouts)',
            'api.admitad.com': r'/statistics/actions/',
            'api.clickbank.com': r'/rest/1\.3/quickstats/list',
        }
        post = {'https://commissions.api.cj.com/query', 'https://api.katalys.com/v1/report/conversions',
                'https://graphql.refersion.com'}
        valid = (method == 'GET' and parsed.hostname in routes and
                 re.fullmatch(routes[parsed.hostname], parsed.path))
        if method == 'POST' and url in post:
            body = kwargs.get('json_body', {})
            valid = url.startswith('https://api.katalys.com/') or (
                isinstance(body.get('query'), str) and
                not re.search(r'\b(mutation|subscription)\b', body['query'], re.I))
        if (not valid or parsed.scheme != 'https' or parsed.query or parsed.fragment or
                parsed.username is not None or parsed.password is not None or parsed.port):
            raise ValueError('route_not_read_allowlisted')
        if len(self.receipts) >= self.max_requests or time.monotonic() >= self.deadline:
            raise ValueError('read_budget_exhausted')
        kwargs['timeout'] = max(.1, min(30, self.deadline-time.monotonic()))
        response = self.transport.request(method, url, **kwargs)
        safe_url = re.sub(r'/Mediapartners/[^/]+/', '/Mediapartners/<sid>/', url)
        self.receipts.append({'method':method, 'endpoint':safe_url, 'http_status':response.status,
            'response_sha256':hashlib.sha256(response.body).hexdigest(),
            'fetched_at':datetime.now(timezone.utc).isoformat()})
        if not response.ok:
            if response.status == 429 and url == REPORT_ROUTES['katalys']:
                # The shared limiter must observe this response while holding its
                # lock. The report parser rejects it without retrying or decoding.
                return Response(response.status, response.body, response.headers)
            raise ValueError('http_error_no_retry')
        try:
            document = response.json()
        except ValueError:
            document = None
        if isinstance(document, dict) and any(
                value for key, value in document.items() if key.casefold() in {'error','errors'}):
            raise ProviderResponseError('provider_error_envelope')
        if document is not None:
            self.raw_documents.append(document)
        return Response(response.status, response.body, response.headers)  # drop credentialed response.url


IMPACT_MAX_WINDOW_DAYS = 45


def _impact_windows(a, b):
    """Contiguous half-open sub-windows of at most 45 days covering [a, b)."""
    from datetime import timedelta
    out, cursor = [], a
    while cursor < b:
        right = min(cursor + timedelta(days=IMPACT_MAX_WINDOW_DAYS), b)
        out.append((cursor, right))
        cursor = right
    return out


def _impact_actions(t, a, b):
    """Read Actions per sub-window; None unless every sub-window reconciles.

    StartDate/EndDate keep the provider's own filter axis (not EventDate), so an
    action updated inside two sub-windows can repeat; its latest read is kept once.
    Page and count reconciliation restart for each sub-window.
    """
    from datetime import timedelta
    from marketplace_adapters import impact
    sid, key = impact.credentials()
    merged = {}
    for left, right in _impact_windows(a, b):
        page, expected, count, seen = 1, None, 0, set()
        while True:
            before = len(t.receipts)
            try:
                r = t.request('GET', impact.API+'/Mediapartners/'+sid+'/Actions', auth=(sid,key),
                    headers={'Accept':'application/json'}, params={'StartDate':left.strftime('%Y-%m-%dT%H:%M:%SZ'),
                    'EndDate':(right-timedelta(seconds=1)).strftime('%Y-%m-%dT%H:%M:%SZ'), 'PageSize':100, 'Page':page})
            finally:
                for receipt in t.receipts[before:]:
                    receipt.update(start_inclusive=left.isoformat(), end_exclusive=right.isoformat(), page=page)
            doc = r.json()
            rows, total = doc.get('Actions'), int(doc['@total'])
            empty_terminal = total == 0 and rows == [] and page == 1 and int(doc['@numpages']) == 0
            terminal = empty_terminal or page == int(doc['@numpages'])
            if int(doc['@page']) != page or (page > int(doc['@numpages']) and not empty_terminal):
                raise ValueError('page_mismatch')
            if total < 0 or not isinstance(rows,list):
                raise ValueError('count_schema')
            if expected is not None and total != expected:
                raise ValueError('count_changed')
            expected = total
            for row in rows:
                raw = row if isinstance(row,dict) else {}
                identity = raw.get('Id')
                if not isinstance(identity,(str,int)) or isinstance(identity,bool) or str(identity) in seen:
                    raise ValueError('missing_or_duplicate_event')
                seen.add(str(identity))
                merged.pop(str(identity), None)
                merged[str(identity)] = _projection('impact',[raw])
            count += len(rows)
            if terminal:
                if count != total:
                    return None
                break
            if not rows or count >= total:
                return None
            page += 1
    return len(merged), [p for kept in merged.values() for p in kept]


def _other_earnings(network, transport, *, start, end, deadline=None, max_requests=32,
                    profile_id=None, limiter=None):
    from datetime import timedelta
    if network not in REPORT_ROUTES:
        state = 'PORTAL_ONLY' if network in {'maxbounty', 'xentraffic', 'everflow'} else 'UNKNOWN'
        return AccessResult(network, 'conversions_earnings', state,
            reason='NO_AUTHORIZED_API_REPORT_BOUND_NO_BROWSER_AUTOMATION')
    source = REPORT_ROUTES[network]
    t = ReadTransport(transport, deadline=deadline, max_requests=max_requests)
    a, b = _window(start, end)
    count, complete, period = 0, False, False
    projected = []
    binding = 'CONFIGURED_CREDENTIAL_NOT_RESPONSE_OWNED_IDENTITY'
    try:
        if network == 'cj':
            from marketplace_adapters import cj
            from glp_affiliate_weekly_reporting import collect_cj
            secret, account, _ = cj._secret()
            pages = collect_cj(t, secret, account, start, end, deadline=t.deadline, max_requests=max_requests)
            summary = pages[-1]
            complete = summary['payload_complete']
            count = summary['record_count']
            period = summary['report_complete_period']
            binding = 'request:publisher:' + account
            projected = [row for page in pages if page.get('payload_complete')
                         for row in page.get('accounting_projection',[])]
        elif network == 'katalys':
            from marketplace_adapters import katalys
            if not profile_id or a.time().isoformat() != '00:00:00' or b.time().isoformat() != '00:00:00':
                raise ValueError('bound_profile_and_calendar_window_required')
            got = katalys.conversion_report(a.date().isoformat(), (b-timedelta(days=1)).date().isoformat(),
                t, profile_id=profile_id, timezone='UTC', limiter=limiter)
            # The legacy parser accepts rows without count; this contract does not.
            doc = t.raw_documents[-1] if t.raw_documents else {}
            declared = [doc[k] for k in ('rowCount','total','total_count','totalCount') if k in doc]
            complete = (got['completeness'] == 'COMPLETE' and bool(declared) and
                        all(type(n) is int and n == got['row_count'] for n in declared))
            count = got['row_count']
            projected = _projection(network,got['rows'])
            period = complete and got['period_state'] == 'CLOSED'
            binding = 'request:profile:' + profile_id + ';aff_id:13031;retained_profile_binding'
        elif network == 'impact':
            merged = _impact_actions(t, a, b)
            complete = merged is not None
            count, projected = merged if complete else (0, [])
        else:
            page, expected, seen = 1, None, set()
            while True:
                if network == 'affiliati':
                    from marketplace_adapters import affiliati
                    r = affiliati._request(t, 'GET', '/Reports/Conversions', params={
                        'start_date':a.strftime('%Y-%m-%dT%H:%M:%S'), 'end_date':b.strftime('%Y-%m-%dT%H:%M:%S'),
                        'conversion_type':'all', 'exclude_bot_traffic':False, 'unbilled_only':False,
                        'start_at_row':(page-1)*100+1, 'row_limit':100,
                        'sort_field':'conversion_date', 'sort_descending':False,
                        'fields':['conversion_id','conversion_date','offer_id','price','disposition']})
                    doc = r.json()
                    if doc.get('success') is not True:
                        raise ValueError('provider_error')
                    rows, total = doc.get('data'), doc.get('row_count')
                    terminal = isinstance(rows,list) and count+len(rows) == total
                    id_key = 'conversion_id'
                else:
                    from marketplace_adapters import partnerize
                    r = t.request('GET', source, auth=partnerize._auth(), params={
                        'start_date':a.strftime('%Y-%m-%dT%H:%M:%SZ'),
                        'end_date':(b-timedelta(seconds=1)).strftime('%Y-%m-%dT%H:%M:%SZ'),
                        'timezone':'UTC','include_payment_info':'true','limit':300})
                    doc = r.json()
                    rows, total = doc.get('conversions'), doc.get('count')
                    terminal = not doc.get('hypermedia',{}).get('pagination',{}).get('next_page')
                    id_key = 'conversion_id'
                if type(total) is not int or total < 0 or not isinstance(rows,list):
                    raise ValueError('count_schema')
                if expected is not None and total != expected:
                    raise ValueError('count_changed')
                expected = total
                for row in rows:
                    raw = row.get('conversion',row) if isinstance(row,dict) else {}
                    identity = raw.get(id_key)
                    if not isinstance(identity,(str,int)) or isinstance(identity,bool) or str(identity) in seen:
                        raise ValueError('missing_or_duplicate_event')
                    seen.add(str(identity))
                projected.extend(_projection(network,rows))
                count += len(rows)
                if terminal:
                    complete = count == total
                    break
                if not rows or count >= total or network == 'partnerize':
                    break  # do not guess Partnerize cursor or trust arbitrary next URL
                page += 1
        return AccessResult(network, 'conversions_earnings', 'SUPPORTED', source,
            coverage='COMPLETE' if complete else 'UNKNOWN' if network == 'impact' else 'PARTIAL',
            record_count=count if complete else None,
            receipts=tuple(t.receipts), records=tuple(projected) if complete else (),
            account_binding=binding, report_complete_period=period,
            reason='ACCOUNT_ROW_COVERAGE_ONLY_CURRENCY_CLASSIFICATION_CASH_UNKNOWN'
                if complete else 'COUNT_PAGINATION_OR_PERIOD_PROOF_INCOMPLETE')
    except Exception as error:
        # A failed Impact sub-window leaves the requested window unproven as a whole.
        return AccessResult(network, 'conversions_earnings', 'SUPPORTED', source,
            coverage='UNKNOWN' if network == 'impact' else _failure_coverage(t, error),
            receipts=tuple(t.receipts),
            reason='READ_FAILED_OR_INCOMPLETE_NO_AUTOMATIC_RETRY', account_binding=binding)
