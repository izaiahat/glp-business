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


def windows(as_of, lookback_days=90):
    if as_of.tzinfo is None or not 7 <= lookback_days <= 366:
        raise ValueError('aware_clock_and_7_to_366_day_lookback_required')
    now = as_of.astimezone(timezone.utc)
    end = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=now.weekday())
    previous_month_end = end.replace(day=1)
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


def run(output, roster, identities, *, as_of=None, lookback_days=90, collectors=None, budget_seconds=240, max_requests=32):
    as_of = as_of or datetime.now(timezone.utc)
    window = windows(as_of, lookback_days)
    marketplaces = roster.get('marketplaces')
    if not isinstance(marketplaces, list) or roster.get('marketplace_count') != len(marketplaces):
        raise ValueError('roster_count_mismatch')
    names = [token(m['marketplace']) for m in marketplaces]
    if len(set(names)) != len(names):
        raise ValueError('duplicate_marketplace')
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
    save(output/'STARTED.json', {'status': 'RUNNING', 'window': window})
    deadline = time.monotonic()+budget_seconds
    for name in names:
        collector = (collectors or {}).get(name)
        if collector is None:
            result['gaps'].append({'network': name, 'state': 'UNSUPPORTED_PARTITION',
                'needed': 'typed_account_program_tenant_registry_and_reviewed_reporting_adapter'})
        elif time.monotonic() >= deadline:
            result['gaps'].append({'network': name, 'state': 'DEADLINE_UNREAD'})
        else:
            try:
                page_index = [0]
                def sink(page):
                    page_index[0] += 1
                    save(output/(name+'-page-'+str(page_index[0]).zfill(4)+'.json'), page)
                observations = collector(window, deadline, max_requests, sink=sink)
                result['observations'].extend(observations)
                if not observations or any(not x.get('report_complete_period') for x in observations):
                    result['gaps'].append({'network': name, 'state': 'PARTIAL_ACCOUNT_REPORT'})
                # An account-wide report is never proof of enumerating all held accounts/programs.
                result['gaps'].append({'network': name, 'state': 'IDENTITY_AND_CLASSIFICATION_COVERAGE_UNVERIFIED'})
            except Exception as exc:
                result['gaps'].append({'network': name, 'state': 'SAFE_COLLECTOR_ERROR', 'exception_class': type(exc).__name__})
        save(output/'checkpoint.json', result)
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
    p.add_argument('--live-cj-readonly',action='store_true')
    p.add_argument('--live-network-readonly',action='store_true')
    args=p.parse_args(argv)
    here=Path(__file__).resolve().parent
    business=Path('/home/ubuntu/business')
    required=[Path(__file__).resolve(),here/'glp_network_revenue_v2.py']
    if args.live_cj_readonly:
        required += [business/'scripts/marketplace_adapters'/f for f in ('__init__.py','cj.py','_base.py')]
    if args.live_network_readonly:
        if args.live_cj_readonly:
            raise ValueError('choose_one_live_mode')
        required += [here/'affiliate_network_access.py', here/'impact_api_credentials.py']
        required += [here/('affiliate_reporting_'+name+'.py') for name in (
            'partnerstack','admitad','clickbank','refersion','healthtrader','sovrn')]
        required += [here/'marketplace_adapters'/f for f in (
            '__init__.py','_base.py','awin.py','impact.py','cj.py','affiliati.py','katalys.py','partnerize.py')]
    pin_hash=verify_pins(args.source_manifest,required)
    roster=json.loads(args.roster.read_text())
    registry=json.loads(args.identity_registry.read_text())
    if registry.get('schema')!='reporting-identity-registry-v1':
        raise ValueError('identity_registry_schema_invalid')
    collectors={}
    if args.live_cj_readonly:
        sys.path.insert(0,str(business/'scripts'))
        from marketplace_adapters import cj
        from marketplace_adapters._base import RequestsTransport
        secret, account, _=cj._secret()
        accounts=[a for a in registry.get('accounts',[]) if a.get('network')=='cj' and a.get('account_id')==account and a.get('status')=='verified' and a.get('tenant_id')=='not_applicable']
        if not secret or len(accounts)!=1 or not accounts[0].get('evidence_ref'):
            raise ValueError('configured_cj_account_not_bound')
        transport=RequestsTransport(timeout=30)
        transport._sess().max_redirects=0
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
        from affiliate_network_access import weekly_collectors
        from marketplace_adapters._base import RequestsTransport
        collectors=weekly_collectors([token(m['marketplace']) for m in roster['marketplaces']],
                                     RequestsTransport(timeout=30))
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
                collectors=collectors,budget_seconds=args.budget_seconds,max_requests=args.max_requests)
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
