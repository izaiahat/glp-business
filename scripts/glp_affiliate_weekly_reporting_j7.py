#!/usr/bin/env python3
"""Bounded, local-only weekly accounting coverage runner. No scheduler or sender.

Uses UTC accounting weeks; a PT Monday timer does not change provider reporting
boundaries. Scope gaps remain explicit and never become measured zero.
"""
from __future__ import annotations
import argparse
import fcntl
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from glp_network_revenue_v2 import canonical, digest, token
from marketplace_adapters._base import RequestsTransport


class RunReadTransport(RequestsTransport):
    """One J7 attempt counter shared by reporting, auth and caller retries.

    Reuse the maintained transport and its calls counter. Disable hidden HTTP
    retries/redirects so each boundary crossing is one outbound attempt;
    failed attempts consume capacity. Never retain headers/auth bodies.
    """
    def __init__(self, *, max_requests, deadline, timeout=30):
        if type(max_requests) is not int or not 1 <= max_requests <= 128:
            raise ValueError('run_bounds_invalid')
        super().__init__(timeout=timeout)
        self.max_requests=max_requests
        self.deadline=deadline
        self._configured=False

    def _sess(self):
        session=super()._sess()
        if not self._configured:
            from requests.adapters import HTTPAdapter
            session.max_redirects=0
            session.mount('https://',HTTPAdapter(max_retries=0))
            session.mount('http://',HTTPAdapter(max_retries=0))
            self._configured=True
        return session

    def request(self, method, url, **kwargs):
        if self.calls >= self.max_requests:
            raise ValueError('run_request_budget_exhausted')
        # Session setup has no I/O; prepare it before the inherited attempt count.
        self._sess()
        remaining=self.deadline-time.monotonic()
        if remaining <= 0:
            raise ValueError('run_request_deadline_exhausted')
        kwargs['timeout']=min(kwargs.get('timeout') or self.timeout,remaining)
        return super().request(method,url,**kwargs)

    def auth_request(self, method, url, **kwargs):
        return self.request(method,url,**kwargs)

    def budget_snapshot(self):
        return {'scope':'RUN_WIDE_OUTBOUND_ATTEMPTS','limit':self.max_requests,
            'attempted':self.calls,'remaining':self.max_requests-self.calls,
            'exhausted':self.calls >= self.max_requests,
            'includes':'REPORTING_AUTHENTICATION_FAILED_ATTEMPTS_AND_CALLER_RETRIES',
            'automatic_http_retries':0,'automatic_redirects':0}


def windows(as_of, lookback_days=90):
    if as_of.tzinfo is None or not 7 <= lookback_days <= 366:
        raise ValueError('aware_clock_and_7_to_366_day_lookback_required')
    now = as_of.astimezone(timezone.utc)
    end = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=now.weekday())
    # The latest closed month follows the execution clock, not the previous
    # Monday. During the first days of a month the Monday can be in last month.
    previous_month_end = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    previous_month_start = (previous_month_end - timedelta(days=1)).replace(day=1)
    return {'week_start': (end-timedelta(days=7)).isoformat(), 'end_exclusive': end.isoformat(),
            'lookback_start': (end-timedelta(days=lookback_days)).isoformat(),
            'last_closed_month_start': previous_month_start.isoformat(),
            'last_closed_month_end': previous_month_end.isoformat(), 'reporting_timezone': 'UTC'}


def save(path, data):
    """Atomic private report; callers use a unique run directory."""
    path = Path(path)
    tmp = path.with_name(path.name+'.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w') as handle:
            handle.write(json.dumps(data, indent=2, sort_keys=True, allow_nan=False)+'\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def collect_cj(transport, secret, account_id, start, end, *, deadline, max_requests=32, sink=None):
    """Reuse S2 CJ read query; bounded time bisection handles payloadComplete=false.

    Splitting documented posting-date half-open intervals avoids guessing a
    cursor parameter. At one-second granularity or bounds, leave partial.
    """
    import re
    from glp_network_revenue_v2 import money, timestamp
    if not re.fullmatch(r'\d+', account_id):
        raise ValueError('cj_account_id_invalid')
    a, b = datetime.fromisoformat(start), datetime.fromisoformat(end)
    if a.tzinfo is None or b.tzinfo is None or not a < b:
        raise ValueError('cj_window_invalid')
    queue = []
    cursor = a
    while cursor < b:
        next_end = min(cursor+timedelta(days=7), b)
        queue.append((cursor, next_end))
        cursor = next_end
    receipts = []
    incomplete_hashes = set()
    accepted_count = 0
    accepted_partitions = 0
    all_complete = True
    authentication_blocked = False
    calls = 0
    while queue and calls < max_requests and time.monotonic() < deadline:
        left, right = queue.pop(0)
        query = '{ publisherCommissions(forPublishers: ['+json.dumps(account_id)+'], sincePostingDate: '+json.dumps(left.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))+', beforePostingDate: '+json.dumps(right.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))+') { count payloadComplete maxCommissionId records { commissionId advertiserId advertiserName publisherId postingDate eventDate actionStatus validationStatus original pubCommissionAmountUsd } } }'
        rec = {'schema':'coverage_observation', 'network':'cj', 'account_id':account_id,
            'tenant_id':'not_applicable', 'program_scope':'ALL_ACCOUNT_PROGRAMS', 'program_id':None,
            'time_axis':'posting', 'reporting_timezone':'UTC', 'boundary_verified':True,
            'start_inclusive':left.isoformat(), 'end_exclusive':right.isoformat(),
            'method':'POST', 'endpoint':'https://commissions.api.cj.com/query',
            'read_only_query':True, 'query_sha256':hashlib.sha256(query.encode()).hexdigest(),
            'period_closed':right <= datetime.now(timezone.utc), 'payload_complete':False,
            'account_scope_complete':False, 'report_complete_period':False,
            'record_count':None, 'currency':'USD', 'commission_minor':None, 'cash_minor':None,
            'status':'PARTIAL', 'captured_at':datetime.now(timezone.utc).isoformat()}
        calls += 1
        try:
            response = transport.request('POST', rec['endpoint'],
                headers={'Authorization':'Bearer '+secret, 'Content-Type':'application/json'},
                json_body={'query':query}, timeout=max(0.1,min(30,deadline-time.monotonic())))
            rec.update(http_status=response.status, response_sha256=hashlib.sha256(response.body).hexdigest())
            if response.status != 200:
                rec['reason']='HTTP_ERROR_NO_AUTOMATIC_AUTH_RETRY'
                all_complete=False
                authentication_blocked=response.status in (401,403)
            else:
                doc=response.json()
                box=doc.get('data',{}).get('publisherCommissions') if isinstance(doc,dict) and isinstance(doc.get('data'),dict) else None
                if not isinstance(box,dict) or doc.get('errors') or not isinstance(box.get('records'),list):
                    raise ValueError('cj_schema_invalid')
                rows=box['records']
                if type(box.get('count')) is not int or box['count']!=len(rows):
                    raise ValueError('cj_count_mismatch')
                safe=[]
                excluded=0
                seen_events=set()
                for x in rows:
                    if not isinstance(x,dict) or str(x.get('publisherId'))!=account_id:
                        raise ValueError('cj_response_account_mismatch')
                    event_key=(str(x.get('advertiserId')),str(x.get('commissionId')))
                    if event_key in seen_events:
                        raise ValueError('cj_duplicate_event')
                    seen_events.add(event_key)
                    if 'fella' in str(x.get('advertiserName','')).casefold():
                        excluded+=1
                        continue
                    if not all(re.fullmatch(r'\d+',str(x.get(k,''))) for k in ('commissionId','advertiserId')):
                        raise ValueError('cj_report_identity_invalid')
                    posted=timestamp(x.get('postingDate'))
                    if not left <= datetime.fromisoformat(posted) < right:
                        raise ValueError('cj_row_outside_requested_window')
                    source_status=x.get('actionStatus')
                    safe.append({'provider_event_id':str(x['commissionId']), 'program_id':str(x['advertiserId']),
                        'posting_date':posted, 'commission_minor':money(x.get('pubCommissionAmountUsd'),'USD'),
                        'source_status':source_status if source_status in {'new','extended','locked','closed'} else 'unknown',
                        'classification':'UNKNOWN', 'attribution':'UNKNOWN', 'receipt_pointer':'/accounting_projection/'+str(len(safe))})
                rec.update(record_count=len(rows), fella_excluded_count=excluded, accounting_projection=safe,
                    payload_complete=box.get('payloadComplete') is True,
                    status='READ_COMPLETE' if box.get('payloadComplete') is True else 'PARTIAL')
                if rec['payload_complete']:
                    accepted_count += len(rows)
                    accepted_partitions += 1
                    rec['report_complete_period']=rec['period_closed']
                elif rec['response_sha256'] in incomplete_hashes or (right-left).total_seconds()<=1:
                    rec['reason']='REPEATED_INCOMPLETE_PAGE_OR_MINIMUM_WINDOW'
                    all_complete=False
                else:
                    incomplete_hashes.add(rec['response_sha256'])
                    seconds=int((right-left).total_seconds())//2
                    middle=left+timedelta(seconds=seconds)
                    queue[0:0]=[(left,middle),(middle,right)]
                    rec['reason']='SUPERSEDED_BY_BOUNDED_TIME_PARTITIONS'
        except Exception as exc:
            rec.update(reason='SAFE_REPORT_ERROR', exception_class=type(exc).__name__)
            all_complete=False
        receipts.append(rec)
        if sink:
            sink(rec)
        if authentication_blocked:
            break
    complete=all_complete and not queue and accepted_partitions>0
    summary={'schema':'coverage_observation', 'network':'cj','account_id':account_id,
        'tenant_id':'not_applicable','program_id':None,'program_scope':'ALL_ACCOUNT_PROGRAMS',
        'time_axis':'posting','reporting_timezone':'UTC','start_inclusive':start,'end_exclusive':end,
        'currency':'USD','record_count':accepted_count if complete else None,
        'observed_complete_partition_rows':accepted_count,'request_count':calls,
        'authentication_blocked':authentication_blocked,
        'unread_partition_count':len(queue),'payload_complete':complete,
        'period_closed':b <= datetime.now(timezone.utc),'boundary_verified':True,
        'account_scope_complete':False,'report_complete_period':complete and b <= datetime.now(timezone.utc),
        'commission_minor':None,'cash_minor':None,'financial_classification':'UNKNOWN',
        'status':'READ_COMPLETE_ACCOUNT_SCOPE_ONLY' if complete else 'PARTIAL',
        'page_receipt_hashes':[digest(r) for r in receipts]}
    receipts.append(summary)
    if sink:
        sink(summary)
    return receipts


def _noncommercial(record):
    """Explicit stored markers only; no guessed selftest prefixes or time joins."""
    from glp_network_revenue_v2 import TEST_ID
    if record.get('network') == '__receiver_selftest__':
        return 'SELFTEST'
    marked = lambda value: value is True or (type(value) is int and value == 1) or value in ('true', '1')
    if (marked(record.get('is_test')) or record.get('classification') == 'TEST'
            or str(record.get('event_status', '')).casefold() == 'test' or record.get('provider_event_id') == TEST_ID):
        return 'TEST'
    if marked(record.get('is_probe')) or record.get('probe_kind') == 'program_liveness':
        return 'PROBE'
    return None


def _financial_provenance(record):
    import re
    try:
        token(record.get('provider_event_id'))
    except ValueError:
        return False
    return (record.get('authenticated') is True and record.get('classification') == 'REAL'
            and record.get('attribution') == 'GLP' and not _noncommercial(record)
            and isinstance(record.get('receipt_sha256'), str)
            and re.fullmatch('[a-f0-9]{64}', record['receipt_sha256']) is not None
            and isinstance(record.get('receipt_pointer'), str)
            and re.fullmatch(r'/[A-Za-z0-9_/-]{0,255}', record['receipt_pointer']) is not None)


def _mapping_reference(value):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 512


def reconcile_conversions(snapshot, observations, identities):
    """Reconcile supplied local evidence, without creating identity/member edges.

    Native conversion_id equality requires a source mapping reference; it is
    never aliased to provider_event_id. A payout_id requires its own membership
    reference. Current weekly projections lack these edges and remain UNKNOWN.
    Supplied classification/authentication are input claims, not live validation.
    """
    from collections import Counter
    from glp_network_revenue_v2 import EXPONENT, IDENTITY
    result = {'schema': 'glp-f12-conversion-reconciliation-v1', 'status': 'UNKNOWN',
              'provenance': 'SUPPLIED_LOCAL_EVIDENCE_NOT_VERIFIED_PRODUCTION_DATA',
              'input_sha256': None, 'weekly_observations_sha256': digest(observations),
              'input_event_count': None, 'input_completeness': 'UNKNOWN', 'coverage_complete': False,
              'commission_minor': None, 'cash_minor': None, 'rows': [], 'gaps': [],
              'excluded_counts': {'SELFTEST': 0, 'TEST': 0, 'PROBE': 0}}
    if snapshot is None:
        result['gaps'].append('CONVERSION_SNAPSHOT_MISSING')
        return result
    if (not isinstance(snapshot, dict) or snapshot.get('schema') != 'glp-f12-reconciliation-input-v1'
            or not isinstance(snapshot.get('evidence_ref'), str) or not snapshot['evidence_ref']
            or not isinstance(snapshot.get('conversions'), list)
            or any(not isinstance(r, dict) for r in snapshot['conversions'])
            or any(not isinstance(snapshot.get(k, []), list) or
                   any(not isinstance(r, dict) for r in snapshot.get(k, []))
                   for k in ('clicks', 'financial_records'))):
        raise ValueError('reconciliation_input_schema_invalid')
    result.update(input_sha256=digest(snapshot), input_event_count=len(snapshot['conversions']),
                  input_completeness='SUPPLIED_COMPLETE' if snapshot.get('complete') is True else 'UNKNOWN')
    # Completeness of a supplied export does not prove financial or live coverage.
    result['gaps'].append('PRODUCTION_AND_FINANCIAL_POPULATION_UNVERIFIED')
    financial = list(snapshot.get('financial_records', []))
    for observation in observations:
        for field in ('records', 'accounting_projection'):
            financial.extend(r for r in observation.get(field, []) if isinstance(r, dict))
    # Overlapping weekly/lookback/month pages may repeat identical evidence.
    # Different revisions or values remain competing candidates, never last-win.
    financial = list({digest(r): r for r in financial}.values())
    # Native commission identity excludes its conversion mapping and currency.
    # Detect competing mappings/revisions across all evidence before filtering
    # by conversion, including records whose mapped conversion is absent.
    commission_counts = Counter(digest([*[r.get(k) for k in IDENTITY], r.get('provider_event_id')])
                                for r in financial if r.get('event_type') == 'commission')
    counts = Counter(digest(r.get('revenue_event_id')) for r in snapshot['conversions'])
    native_counts = Counter(digest([*[r.get(k) for k in IDENTITY], r.get('conversion_id')])
                            for r in snapshot['conversions'])
    for event in snapshot['conversions']:
        attributes = event.get('attributes_json', '{}')
        try:
            attributes = json.loads(attributes) if isinstance(attributes, str) else attributes
        except ValueError:
            attributes = None
        clicks = [c for c in snapshot.get('clicks', []) if event.get('cs_click_id')
                  and c.get('click_token') == event['cs_click_id'] and event.get('network')
                  and c.get('network') == event['network'] and event.get('program_id')
                  and c.get('program_id') == event['program_id']]
        kind = _noncommercial(event) or (_noncommercial(attributes) if isinstance(attributes, dict) else None)
        kind = kind or next((_noncommercial(c) for c in clicks if _noncommercial(c)), None)
        if kind:
            result['excluded_counts'][kind] += 1
            continue
        try:
            event_id = token(event.get('revenue_event_id'))
            native_id = token(event.get('conversion_id'))
        except ValueError:
            event_id = native_id = None
        row = {'conversion_event_id': event_id, 'classification': 'REAL' if event.get('classification') == 'REAL' else 'UNKNOWN',
               'earnings_state': 'UNKNOWN', 'payout_state': 'UNKNOWN', 'commission_minor': None,
               'payout_total_minor': None, 'cash_minor': None, 'currency': None,
               'payout_amount_scope': None, 'evidence': {'conversion_snapshot': snapshot['evidence_ref']}, 'gaps': []}
        result['rows'].append(row)
        if not isinstance(attributes, dict):
            row['gaps'].append('CONVERSION_ATTRIBUTES_INVALID')
        if event.get('event_type') != 'accepted_conversion':
            row['gaps'].append('CONVERSION_EVENT_TYPE_UNSUPPORTED')
        if event_id is None or counts[digest(event_id)] != 1:
            row['gaps'].append('CONVERSION_RECEIPT_MISSING_OR_AMBIGUOUS')
        if native_id is None:
            row['gaps'].append('NATIVE_CONVERSION_ID_MISSING')
        elif native_counts[digest([*[event.get(k) for k in IDENTITY], native_id])] != 1:
            row['gaps'].append('NATIVE_CONVERSION_ID_AMBIGUOUS')
        if len(clicks) > 1:
            row['gaps'].append('CLICK_IDENTITY_AMBIGUOUS')
        if event.get('classification') != 'REAL':
            row['gaps'].append('CONVERSION_CLASSIFICATION_UNKNOWN')
        typed = tuple(event.get(k) for k in IDENTITY)
        bound = [i for i in identities if tuple(i.get(k) for k in IDENTITY) == typed and i.get('status') == 'verified']
        if (any(not isinstance(v, str) or not v or v == 'UNKNOWN' for v in typed) or len(bound) != 1
                or not _mapping_reference(bound[0].get('evidence_ref'))
                or not isinstance(bound[0].get('provider_slug'), str) or not bound[0]['provider_slug']
                or bound[0].get('tenant_kind') not in {'host', 'not_applicable'}
                or (bound[0]['tenant_kind'] == 'not_applicable') != (typed[2] == 'not_applicable')):
            row['gaps'].append('TYPED_IDENTITY_UNRESOLVED_OR_AMBIGUOUS')
        elif 'fella' in bound[0]['provider_slug'].casefold():
            row['gaps'].append('PROVIDER_EXCLUDED')
        else:
            row['evidence']['typed_identity'] = bound[0]['evidence_ref']
        if row['gaps']:
            continue
        matches = [r for r in financial if r.get('event_type') == 'commission'
                   and tuple(r.get(k) for k in IDENTITY) == typed
                   and r.get('conversion_id') == event['conversion_id']]
        if len(matches) != 1:
            row['gaps'].append('EARNINGS_EDGE_MISSING' if not matches else 'EARNINGS_EDGE_AMBIGUOUS')
            continue
        earning = matches[0]
        kind = _noncommercial(earning)
        if kind:
            result['rows'].pop()
            result['excluded_counts'][kind] += 1
            continue
        if commission_counts[digest([*typed, earning.get('provider_event_id')])] != 1:
            row['gaps'].append('EARNINGS_EDGE_AMBIGUOUS')
            continue
        if (not _mapping_reference(earning.get('conversion_join_evidence_ref'))
                or not _financial_provenance(earning)):
            row['gaps'].append('EARNINGS_MAPPING_OR_PROVENANCE_UNVERIFIED')
            continue
        if any(event.get(k) and earning.get(k) and event[k] != earning[k]
               for k in ('cs_click_id', 'network_click_id')):
            row['gaps'].append('EARNINGS_CLICK_IDENTITY_CONFLICT')
            continue
        row.update(earnings_state='JOINED', currency=earning.get('currency'))
        row['evidence']['earnings'] = {k: earning[k] for k in
                                      ('provider_event_id', 'receipt_sha256', 'receipt_pointer', 'conversion_join_evidence_ref')}
        amount = earning.get('commission_minor')
        if type(amount) is int and abs(amount) <= 2**63-1 and earning.get('currency') in EXPONENT:
            row['commission_minor'] = amount
        else:
            row['gaps'].append('EARNINGS_AMOUNT_UNKNOWN')
        if not isinstance(earning.get('payout_id'), str) or not earning['payout_id'] or not _mapping_reference(earning.get('payout_join_evidence_ref')):
            row['gaps'].append('SETTLEMENT_MEMBERSHIP_MISSING')
            continue
        payouts = [r for r in financial if r.get('event_type') == 'settlement'
                   and tuple(r.get(k) for k in IDENTITY) == typed
                   and r.get('provider_event_id') == earning['payout_id']]
        if len(payouts) != 1:
            row['gaps'].append('PAYOUT_EDGE_MISSING' if not payouts else 'PAYOUT_EDGE_AMBIGUOUS')
            continue
        payout = payouts[0]
        if not _financial_provenance(payout) or payout.get('mapped_status') != 'paid':
            row['gaps'].append('PAYOUT_PROVENANCE_OR_PAID_STATE_UNVERIFIED')
            continue
        if earning.get('currency') not in EXPONENT or payout.get('currency') != earning['currency']:
            row['gaps'].append('PAYOUT_CURRENCY_MISMATCH')
            continue
        row.update(payout_state='JOINED', payout_amount_scope='WHOLE_SETTLEMENT_NOT_CONVERSION_CASH')
        row['evidence']['payout'] = {k: payout[k] for k in ('provider_event_id', 'receipt_sha256', 'receipt_pointer')}
        row['evidence']['payout']['membership'] = earning['payout_join_evidence_ref']
        amount = payout.get('net_paid_minor')
        if type(amount) is int and abs(amount) <= 2**63-1:
            row['payout_total_minor'] = amount
        else:
            row['gaps'].append('PAYOUT_AMOUNT_UNKNOWN')
        row['gaps'].append('PER_CONVERSION_CASH_ALLOCATION_UNAVAILABLE')
    if any(r['earnings_state'] == 'JOINED' for r in result['rows']):
        result['status'] = 'PARTIAL'
    return result


def marketplace_order(roster, start_marketplace=None):
    """Validate an explicit rotation, never filter or mutate the supplied roster."""
    marketplaces = roster.get('marketplaces')
    if not isinstance(marketplaces, list) or roster.get('marketplace_count') != len(marketplaces):
        raise ValueError('roster_count_mismatch')
    names = [token(m['marketplace']) for m in marketplaces]
    if len(set(names)) != len(names):
        raise ValueError('duplicate_marketplace')
    if start_marketplace is None:
        return names
    selectors = start_marketplace if isinstance(start_marketplace, list) else [start_marketplace]
    if len(selectors) != 1:
        raise ValueError('duplicate_start_marketplace')
    start = selectors[0]
    if not isinstance(start, str) or start not in names:
        raise ValueError('start_marketplace_invalid')
    offset = names.index(start)
    return names[offset:] + names[:offset]


def run(output, roster, identities, *, as_of=None, lookback_days=90, collectors=None, budget_seconds=240, max_requests=32,
        reconciliation_input=None, statement_intake=None, statement_sha256=None, request_transport=None,
        start_marketplace=None):
    as_of = as_of or datetime.now(timezone.utc)
    window = windows(as_of, lookback_days)
    names = marketplace_order(roster, start_marketplace)
    if not 1 <= budget_seconds <= 1800 or not 1 <= max_requests <= 128:
        raise ValueError('run_bounds_invalid')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    result = {'schema': 'glp-affiliate-weekly-report-v1', 'status': 'PARTIAL',
        'invocation': 'manual_or_scheduler_unverified', 'captured_at': as_of.isoformat(),
        'window': window, 'marketplace_count': len(names), 'roster_sha256': digest(roster),
        'identity_registry_sha256': digest(identities), 'coverage_complete': False,
        'roster_freshness': 'SUPPLIED_SNAPSHOT_NOT_LIVE_SHEET_READ',
        'fleet_commission_minor': None, 'fleet_cash_minor': None, 'measured_epc': None,
        'payment_candidates': [], 'observations': [], 'gaps': [],
        'unresolved_pending_beyond_lookback': 'UNKNOWN_NO_OPERATIONAL_LEDGER_READ',
        'skills_loaded': ['affiliate-network-reporting'], 'skills_should_have_loaded': ['affiliate-network-reporting']}
    if start_marketplace is not None:
        result['selection_start_marketplace'] = names[0]
    save(output/'STARTED.json', {'status': 'RUNNING', 'window': window})
    deadline = time.monotonic()+budget_seconds
    if request_transport is not None:
        if request_transport.max_requests != max_requests:
            raise ValueError('run_transport_budget_mismatch')
        request_transport.deadline=min(request_transport.deadline,deadline)
        deadline=request_transport.deadline
        result['http_budget']=request_transport.budget_snapshot()
    for name in names:
        collector = (collectors or {}).get(name)
        if collector is None:
            result['gaps'].append({'network': name, 'state': 'UNSUPPORTED_PARTITION',
                'needed': 'typed_account_program_tenant_registry_and_reviewed_reporting_adapter'})
        elif time.monotonic() >= deadline:
            result['gaps'].append({'network': name, 'state': 'DEADLINE_UNREAD'})
        elif request_transport is not None and request_transport.calls >= max_requests:
            result['gaps'].append({'network': name, 'state': 'REQUEST_BUDGET_UNREAD',
                'unread_or_partial_periods':['closed_week','lookback','closed_month']})
        else:
            try:
                page_index = [0]
                def sink(page):
                    page_index[0] += 1
                    save(output/(name+'-page-'+str(page_index[0]).zfill(4)+'.json'), page)
                remaining=max_requests-request_transport.calls if request_transport is not None else max_requests
                observations = collector(window, deadline, remaining, sink=sink)
                result['observations'].extend(observations)
                if not observations or any(not x.get('report_complete_period') for x in observations):
                    result['gaps'].append({'network': name, 'state': 'PARTIAL_ACCOUNT_REPORT'})
                # An account-wide report is never proof of enumerating all held accounts/programs.
                result['gaps'].append({'network': name, 'state': 'IDENTITY_AND_CLASSIFICATION_COVERAGE_UNVERIFIED'})
            except Exception as exc:
                result['gaps'].append({'network': name, 'state': 'SAFE_COLLECTOR_ERROR', 'exception_class': type(exc).__name__})
            if request_transport is not None and request_transport.calls >= max_requests:
                complete={x.get('period_purpose') for x in result['observations']
                    if x.get('network')==name and x.get('report_complete_period')}
                unread=[p for p in ('closed_week','lookback','closed_month') if p not in complete]
                if unread:
                    result['gaps'].append({'network': name, 'state': 'REQUEST_BUDGET_EXHAUSTED_PARTITION',
                        'unread_or_partial_periods':unread})
        if request_transport is not None:
            result['http_budget']=request_transport.budget_snapshot()
        save(output/'checkpoint.json', result)
    if statement_intake:
        from affiliate_closed_statement_j9 import read
        result['closed_payout_statements'] = read(statement_intake, statement_sha256, as_of=as_of)
    from affiliate_account_identity_j8 import resolve_accounts
    result['account_identity_resolution'] = resolve_accounts(names, result['observations'])
    result['conversion_reconciliation'] = reconcile_conversions(reconciliation_input, result['observations'], identities)
    result['completed_at'] = datetime.now(timezone.utc).isoformat()
    save(output/'RESULT.json', result)
    return result


def verify_pins(manifest, required):
    pins=json.loads(Path(manifest).read_text())['files']
    if not all(str(Path(p).resolve()) in pins for p in required):
        raise ValueError('source_pin_missing')
    for path, expected in pins.items():
        if hashlib.sha256(Path(path).read_bytes()).hexdigest()!=expected:
            raise ValueError('source_pin_mismatch')
    return digest(pins)


def main(argv=None):
    import signal
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source-manifest',type=Path,required=True)
    p.add_argument('--roster',type=Path,required=True)
    p.add_argument('--identity-registry',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--lookback-days',type=int,default=90)
    p.add_argument('--budget-seconds',type=int,default=240)
    p.add_argument('--max-requests',type=int,default=32)
    p.add_argument('--start-marketplace',action='append',help='Rotate the full roster to this exact marketplace; specify once')
    p.add_argument('--reconciliation-input',type=Path,help='Optional local F12 evidence JSON; never fetches conversions')
    p.add_argument('--statement-intake',type=Path)
    p.add_argument('--statement-sha256')
    p.add_argument('--eden-intake',type=Path)
    p.add_argument('--eden-intake-sha256')
    p.add_argument('--xentraffic-intake',type=Path)
    p.add_argument('--xentraffic-sha256')
    p.add_argument('--live-cj-readonly',action='store_true')
    p.add_argument('--live-network-readonly',action='store_true')
    args=p.parse_args(argv)
    here=Path(__file__).resolve().parent
    business=Path('/home/ubuntu/business')
    required=[Path(__file__).resolve(),here/'glp_network_revenue_v2.py',here/'affiliate_reporting_xentraffic_j7.py',here/'affiliate_account_identity_j8.py']
    if args.live_cj_readonly:
        required += [business/'scripts/marketplace_adapters'/f for f in ('__init__.py','cj.py','_base.py')]
    if args.live_network_readonly:
        if args.live_cj_readonly:
            raise ValueError('choose_one_live_mode')
        required += [here/'affiliate_network_access.py', here/'impact_api_credentials.py']
        required += [here/('affiliate_reporting_'+name+'.py') for name in (
            'partnerstack','admitad','clickbank','refersion','healthtrader')]
        required += [here/'affiliate_network_access_j7.py', here/'affiliate_reporting_maxbounty_j7.py', here/'marketplace_adapters/katalys_j7.py', here/'affiliate_reporting_everflow_roster_j7.py', here/'affiliate_reporting_eden_portal_j7.py']
        required += [here/'affiliate_reporting_everflow.py', here/'affiliate_network_access_j6.py', here/'affiliate_reporting_maxbounty.py', here/'maxbounty_publisher_api.py']
        required += [here/'marketplace_adapters'/f for f in (
            '__init__.py','_base.py','awin.py','impact.py','cj.py','affiliati.py','katalys.py','partnerize.py')]
    if args.statement_intake:
        required.append(here/'affiliate_closed_statement_j9.py')
    pin_hash=verify_pins(args.source_manifest,required)
    reconciliation_input = json.loads(args.reconciliation_input.read_text()) if args.reconciliation_input else None
    roster=json.loads(args.roster.read_text())
    registry=json.loads(args.identity_registry.read_text())
    if registry.get('schema')!='reporting-identity-registry-v1':
        raise ValueError('identity_registry_schema_invalid')
    names=marketplace_order(roster,args.start_marketplace)
    collectors={}
    transport=None
    if args.live_cj_readonly:
        sys.path.insert(0,str(business/'scripts'))
        from marketplace_adapters import cj
        secret, account, _=cj._secret()
        accounts=[a for a in registry.get('accounts',[]) if a.get('network')=='cj' and a.get('account_id')==account and a.get('status')=='verified' and a.get('tenant_id')=='not_applicable']
        if not secret or len(accounts)!=1 or not accounts[0].get('evidence_ref'):
            raise ValueError('configured_cj_account_not_bound')
        transport=RunReadTransport(max_requests=args.max_requests,deadline=time.monotonic()+args.budget_seconds)
        def cj_collector(w,deadline,max_requests,*,sink):
            receipts=[]
            remaining=max_requests
            periods=[('closed_week',w['week_start'],w['end_exclusive']),
                ('lookback',w['lookback_start'],w['end_exclusive']),
                ('closed_month',w['last_closed_month_start'],w['last_closed_month_end'])]
            for label,start,end in periods:
                def labelled_sink(page):
                    page['period_purpose']=label
                    sink(page)
                pages=collect_cj(transport,secret,account,start,end,deadline=deadline,max_requests=remaining,sink=labelled_sink)
                remaining-=pages[-1]['request_count']
                if pages[-1]['authentication_blocked']:
                    remaining=0
                receipts.extend(pages)
            return receipts
        collectors['cj']=cj_collector
    if args.live_network_readonly:
        sys.path.insert(0,str(here.parent))
        from affiliate_network_access_j7 import weekly_collectors
        transport=RunReadTransport(max_requests=args.max_requests,deadline=time.monotonic()+args.budget_seconds)
        collectors=weekly_collectors(names,
                                     transport, eden_intake=args.eden_intake, eden_sha256=args.eden_intake_sha256)
    if args.live_network_readonly and args.xentraffic_intake:
        from affiliate_reporting_xentraffic_j7 import collector
        collectors['xentraffic']=collector(args.xentraffic_intake,args.xentraffic_sha256)
    live_readonly=args.live_cj_readonly or args.live_network_readonly
    args.output.parent.mkdir(parents=True,exist_ok=True)
    # No lock deletion: flock releases on process exit, preserving other owners.
    lockpath=args.output.parent/'.weekly-reporting.lock'
    fd=os.open(lockpath,os.O_RDWR|os.O_CREAT,0o600)
    with os.fdopen(fd,'w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        oldhandler=None
        if live_readonly:
            def expired(signum,frame):
                raise SystemExit('bounded_reporting_deadline_checkpoint_retained')
            oldhandler=signal.signal(signal.SIGALRM,expired)
            signal.alarm(args.budget_seconds+5)
        try:
            result=run(args.output,roster,registry.get('identities',[]),lookback_days=args.lookback_days,
                collectors=collectors,budget_seconds=args.budget_seconds,max_requests=args.max_requests,
                reconciliation_input=reconciliation_input,
                statement_intake=args.statement_intake, statement_sha256=args.statement_sha256,
                request_transport=transport, start_marketplace=args.start_marketplace)
            result['source_pins_sha256']=pin_hash
            result['invocation']='MANUAL_READ_ONLY' if live_readonly else 'OFFLINE_DRY_RUN_NO_PROVIDER_IO'
            save(args.output/'RESULT.json',result)
        finally:
            if live_readonly:
                signal.alarm(0)
                signal.signal(signal.SIGALRM,oldhandler)
    print(json.dumps({'status':result['status'],'marketplace_count':result['marketplace_count'],
        'coverage_complete':False,'result':str(args.output/'RESULT.json')}))
    return 0


if __name__=='__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, KeyError) as exc:
        print(json.dumps({'status':'BLOCKED','error_class':type(exc).__name__}))
        raise SystemExit(2)
