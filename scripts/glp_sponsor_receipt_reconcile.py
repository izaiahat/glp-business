#!/usr/bin/env python3
"""Reconcile the exact Oct16 interrupted sponsor scrape from provider GET receipts.

Default plan only. This is NOT the historical conservative booking decision and
never uses account balance deltas. No paid request, retry, capture import,
reservation settlement, execution rewrite, or send is possible here. UNKNOWN
retains null cost and the existing admission/no-replay hold.
"""
from __future__ import annotations
import argparse
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
import stat
from pathlib import Path
from urllib.parse import urlencode, quote

from firecrawl_client import FirecrawlClient, LEDGER
from glp_sponsor_firecrawl_supply import Producer

# Scope is literal and reviewable; extending it requires a separate source change.
OPERATION = {
    'operation_id':'ad3ae711-d49a-42fa-b332-a6ce60ee376c',
    'caller':'sponsor_firecrawl_supply', 'endpoint':'/v2/scrape',
    'run':'sponsor-supply-20261016T2350',
    'at_utc':'2026-10-16T23:54:58.594497+00:00',
    'outcome':'started', 'http_status':None,
    'url_hash':['0ae5d38e52758e3b58d75f65c90c6f6673bed269cc5f1c6bb727c848ab434bc1'],
}
PAYLOAD_SHA = '0bbca81cc5eb401e28f9a041d2a46554eb1b86a32de2aeba82befdd0c2af136a'
SCHEMA = 'sponsor-exact-provider-receipt-v1'


def encoded(value):
    return (json.dumps(value,sort_keys=True,allow_nan=False)+'\n').encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def private_create(path, raw):
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as stream:
        stream.write(raw);stream.flush();os.fsync(stream.fileno())


def exact_row(rows):
    found=[r for r in rows if r.get('operation_id')==OPERATION['operation_id']]
    if not found:
        raise RuntimeError('exact_operation_missing')
    for r in found:
        if (any(r.get(k)!=v for k,v in OPERATION.items()) or
                r.get('options',{}).get('payload_sha256')!=PAYLOAD_SHA):
            raise RuntimeError('exact_operation_identity_changed')
    return found[-1]


def provider_evidence(client, rows, original, receipt):
    """Only GET activity + GET existing scrape. Persist decoded private captures.

    Activity timestamps currently arrive timezone-naive despite OpenAPI date-time.
    Do not infer a timezone or book by time proximity: require one target job in
    the COMPLETE activity listing and one local logical scrape in its 24h window.
    The final receipt must independently match both provider ID and source URL.
    """
    activities=[];cursor=None;seen=set();pages=0
    for page in range(10):
        query={'endpoint':'scrape','limit':100}
        if cursor:query['cursor']=cursor
        payload=client.request('team/activity?'+urlencode(query),method='GET',timeout=60)
        private_create(Path(str(receipt)+f'.activity-{page+1:02}.json'),encoded(payload))
        pages+=1
        if payload.get('success') is not True or not isinstance(payload.get('data'),list):
            return {'status':'UNKNOWN','reason':'activity_response_invalid'}
        activities.extend(payload['data'])
        if payload.get('has_more') is False and payload.get('cursor') is None:
            break
        cursor=payload.get('cursor')
        if payload.get('has_more') is not True or not isinstance(cursor,str) or not cursor or cursor in seen:
            return {'status':'UNKNOWN','reason':'activity_pagination_invalid'}
        seen.add(cursor)
    else:
        return {'status':'UNKNOWN','reason':'activity_pagination_bound'}
    target=original['url_hash'][0]
    matches=[r for r in activities if isinstance(r,dict) and r.get('endpoint')=='scrape'
             and r.get('api_version')=='v2' and isinstance(r.get('target'),str)
             and sha(r['target'].encode())==target]
    # Count entries, not deduped IDs: duplicated pages are ambiguous too.
    if len(matches)!=1 or not isinstance(matches[0].get('id'),str) or not matches[0]['id']:
        return {'status':'UNKNOWN','reason':'provider_target_absent_or_ambiguous'}
    observed=dt.datetime.now(dt.timezone.utc)
    start=dt.datetime.fromisoformat(original['at_utc'])
    if not observed-dt.timedelta(hours=24) < start <= observed:
        return {'status':'UNKNOWN','reason':'outside_provider_activity_retention'}
    local=set()
    for r in rows:
        if r.get('endpoint')!='/v2/scrape' or target not in r.get('url_hash',[]):
            continue
        try:at=dt.datetime.fromisoformat(r['at_utc'].replace('Z','+00:00'))
        except (KeyError,ValueError,TypeError):
            return {'status':'UNKNOWN','reason':'local_target_time_invalid'}
        if at.tzinfo is None:
            return {'status':'UNKNOWN','reason':'local_target_time_invalid'}
        if observed-dt.timedelta(hours=24) < at <= observed:
            local.add(r.get('operation_id'))
    if local!={original['operation_id']}:
        return {'status':'UNKNOWN','reason':'local_target_ambiguous'}
    job=matches[0]['id']
    payload=client.request('scrape/'+quote(job,safe=''),method='GET',timeout=60)
    private_create(Path(str(receipt)+'.result.json'),encoded(payload))
    data=payload.get('data');meta=data.get('metadata') if isinstance(data,dict) else None
    if not isinstance(meta,dict) or payload.get('success') is not True:
        return {'status':'UNKNOWN','reason':'provider_result_incomplete'}
    ident='sha256:'+sha(job.encode())
    if (meta.get('scrapeId')!=job or not isinstance(meta.get('sourceURL'),str)
        or sha(meta['sourceURL'].encode())!=target
        or any(obj[key]!=job for obj in (payload,data,meta)
               for key in ('id','jobId','scrapeId','scrape_id') if key in obj)):
        return {'status':'UNKNOWN','reason':'provider_result_identity_mismatch'}
    # Ordinary scrape metadata is mandatory; envelope/SDK aliases may only
    # corroborate it. Generic receipt precedence or Alexandria sums cannot
    # authorize an exact-operation financial revision.
    cost=meta.get('creditsUsed')
    costs=[obj[key] for obj in (payload,data,meta)
           for key in ('creditsUsed','credits_used','creditsCost','credits_cost') if key in obj]
    if (type(cost) not in (int,float) or not math.isfinite(cost) or cost<0
        or any(type(c) not in (int,float) or not math.isfinite(c) or c<0 or c!=cost for c in costs)
        or 'alexandria' in data or 'alexandria' in payload):
        return {'status':'UNKNOWN','reason':'provider_receipt_credit_cost_unknown'}
    return {'status':'EXACT_RECEIPT','provider_id_sha256':ident,'url_hash':target,
            'creditsUsed':cost,'activity_pages':pages,
            'attribution':'unique complete 24h target + unique local operation + result ID/sourceURL',
            'result_sha256':sha(encoded(payload))}


def reconcile(state, ledger, client, receipt, *, apply=False, expected_sha=None,
              expected_evidence_sha=None):
    state,ledger,receipt=Path(state),Path(ledger),Path(receipt)
    if not isinstance(client,FirecrawlClient):
        raise TypeError('real_firecrawl_client_required')
    if apply and (not expected_sha or not expected_evidence_sha):
        raise RuntimeError('apply_requires_plan_bindings')
    # Refuse aliases and all already-used namespaces BEFORE any transport.
    if receipt.exists() or receipt.is_symlink() or receipt.resolve()==ledger.resolve():
        raise RuntimeError('new_receipt_path_required')
    receipt.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    producer=Producer(state_dir=state,import_only=True)
    with (state/'run.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with producer.budget_locked():
            with os.fdopen(os.open(ledger,os.O_RDONLY|os.O_NOFOLLOW),'rb') as stream:
                fcntl.flock(stream,fcntl.LOCK_SH);before=stream.read()
                ledger_identity=os.fstat(stream.fileno())
                if not stat.S_ISREG(ledger_identity.st_mode) or ledger_identity.st_nlink!=1:
                    raise RuntimeError('unsafe_ledger_file')
            if before and not before.endswith(b'\n'): raise RuntimeError('unterminated_ledger')
            rows=[json.loads(l) for l in before.splitlines() if l]
            row=exact_row(rows);pre=sha(before)
            result={'schema':SCHEMA,'pre_ledger_sha256':pre,'operation_id':OPERATION['operation_id'],
                    'paid_operations_executed':0,'reservations_changed':False,
                    'execution_changed':False,'original_outcome_preserved':True}
            if row.get('receipt_reconciliation'):
                audit=row['receipt_reconciliation']
                if audit.get('schema')!=SCHEMA or type(row.get('creditsUsed')) not in (int,float):
                    raise RuntimeError('existing_reconciliation_invalid')
                result.update(status='already_reconciled',creditsUsed=row['creditsUsed'],post_ledger_sha256=pre)
                private_create(receipt,encoded(result));return result
            if row.get('creditsUsed') is not None:
                raise RuntimeError('existing_cost_not_unknown')
            if apply and expected_sha!=pre:
                raise RuntimeError('ledger_changed_since_plan')
            audit_path=Path(str(receipt)+'.GET-audit.jsonl')
            private_create(audit_path,b'')
            read_client=FirecrawlClient(client.caller,run_id=client.run_id,
                                       api_key=client.key,ledger_path=audit_path)
            try:evidence=provider_evidence(read_client,rows,row,receipt)
            except Exception as exc:
                # Never expose provider errors/HTTP bodies or treat them as zero.
                evidence={'status':'UNKNOWN','reason':'read_api_failed','error_type':type(exc).__name__}
            evidence_sha=sha(encoded(evidence))
            result.update(status='UNKNOWN' if evidence['status']=='UNKNOWN' else 'plan',
                          evidence=evidence,evidence_sha256=evidence_sha,
                          GET_audit={'path':str(audit_path.resolve()),'sha256':sha(audit_path.read_bytes())})
            if evidence['status']=='UNKNOWN':
                private_create(receipt,encoded(result));return result
            result['creditsUsed']=evidence['creditsUsed']
            if apply and expected_evidence_sha!=evidence_sha:
                raise RuntimeError('provider_evidence_changed_since_plan')
            with os.fdopen(os.open(ledger,os.O_RDWR|os.O_NOFOLLOW),'r+b') as stream:
                fcntl.flock(stream,fcntl.LOCK_EX)
                current=os.fstat(stream.fileno())
                path_info=ledger.stat(follow_symlinks=False)
                if ((current.st_dev,current.st_ino)!=(ledger_identity.st_dev,ledger_identity.st_ino)
                    or (path_info.st_dev,path_info.st_ino)!=(current.st_dev,current.st_ino)
                    or current.st_nlink!=1):
                    raise RuntimeError('ledger_identity_changed_during_read_api')
                if stream.read()!=before:
                    raise RuntimeError('ledger_changed_during_read_api')
                added=dict(row,creditsUsed=evidence['creditsUsed'],receipt_reconciliation={
                    'schema':SCHEMA,'evidence_sha256':evidence_sha,'pre_ledger_sha256':pre,
                    'original_row_sha256':sha(encoded(row)),'provider_id_sha256':evidence['provider_id_sha256'],
                    'booked_utc':dt.datetime.now(dt.timezone.utc).isoformat(),'receipt':str(receipt.resolve())})
                extra=encoded(added)
                result['expected_post_ledger_sha256']=sha(before+extra)
                result['status']='prepared' if apply else 'plan'
                # Durable exact before/after intent first; after a crash inspect
                # these hashes and latest same-op row, never repeat paid work.
                private_create(receipt,encoded(result))
                if apply:
                    stream.seek(0,os.SEEK_END);stream.write(extra);stream.flush();os.fsync(stream.fileno())
                    stream.seek(0)
                    if sha(stream.read())!=result['expected_post_ledger_sha256']:
                        raise RuntimeError('append_readback_uncertain_inspect_no_retry')
                    result.update(status='applied',post_ledger_sha256=result['expected_post_ledger_sha256'])
                    # Preserve intent; separate exclusive terminal acknowledgement.
                    private_create(Path(str(receipt)+'.applied.json'),encoded(result))
                return result


def main():
    from glp_sponsor_campaign import STATE
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--receipt',type=Path,required=True)
    p.add_argument('--apply',action='store_true')
    p.add_argument('--expected-ledger-sha256')
    p.add_argument('--expected-evidence-sha256')
    a=p.parse_args()
    try:
        r=reconcile(STATE,LEDGER,FirecrawlClient('sponsor_exact_operation_reconciliation'),a.receipt,
                    apply=a.apply,expected_sha=a.expected_ledger_sha256,
                    expected_evidence_sha=a.expected_evidence_sha256)
    except (OSError,RuntimeError,ValueError,TypeError) as exc:
        print(json.dumps({'status':'REFUSED_OR_UNCERTAIN','error_type':type(exc).__name__,
                          'retry_paid_operation':False}));return 2
    print(json.dumps(r));return 2 if r['status']=='UNKNOWN' else 0


if __name__=='__main__':
    raise SystemExit(main())
