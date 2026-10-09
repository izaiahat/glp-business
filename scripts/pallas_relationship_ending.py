#!/usr/bin/env python3
"""Exact GLP-347 denial through the existing sole Sheet writer.

The current operator approves this termination disposition from retained mail.
This is NOT authenticated current portal status and does not relax network_cancelled.
"""
import hashlib
import json
import re
import argparse
import fcntl
import subprocess
import sys
import sqlite3
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr
from pathlib import Path

ROOT = Path('/home/ubuntu/business')
DECISION = 'ops/linear/approvals/OPERATOR-DECISION-20261007-grilling-round1.md'
DECISION_SHA = '6a4b9962169d0fd3aae8d1bdda6510e6649010045c4c0b481e3b5f1a214d47d6'
EVENT = 'evt_a53e6b7c535958b272d91cc7528e'
RAW_SHA = '1412b99a8dbdaca97786f7b1359f0797aa85b563ea85969c255e71039be1abd9'
NAME = 'Pallas Health – GLP-1 Weight Loss & NAD'
APPLICATION = 'https://app.katalys.com/pub/098e5455-ca72-4099-9ff9-a9dd014148e7/explore/1622'
UPDATE = 'operator-relationship-ended-pallas-katalys-1622-' + EVENT
DB = Path('/home/ubuntu/.hermes/state/email-monitor.sqlite3')
STATUS = 'ended_operator_contract_termination'


def cells(source, checked):
    if (not isinstance(source, list) or len(source) != 27 or source[2] != NAME
            or source[4] != 'Katalys' or source[5] != APPLICATION
            or source[10] not in {'live_wired_verified', 'approved_active_wired_live'}
            or not source[14] or source[19] is not True or source[20] is not True):
        raise ValueError('pallas_ending_exact_live_source_required')
    when = datetime.fromisoformat(checked.replace('Z', '+00:00'))
    if when.tzinfo is None or when < datetime(2026, 10, 7, tzinfo=timezone.utc):
        raise ValueError('pallas_ending_checked_time_invalid')
    note = (f'Affiliate relationship ended effective 2026-09-30, as reported by retained Katalys '
            f'contract-ended notice {EVENT}; raw_sha256={RAW_SHA}. Operator approved removal: '
            f'{DECISION} item 6, sha256={DECISION_SHA}. Exact Katalys offer 1622. '
            'Ready/live claims retracted; issued link and prior Notes retained as history. '
            'Editorial content retained; this is not a claim that Pallas care is unavailable. '
            'Current portal status not independently read; this operator disposition is not API proof. '
            'Reactivation requires separate operator approval.')
    return {'10': STATUS, '19': 'FALSE', '20': 'FALSE',
            '21': 'link_retired_operator_contract_ended',
            '23': str(source[23]) + ('\n' if source[23] else '') + note, '24': checked}


def matches(actual, source, projected):
    post = list(source)
    for key, value in projected.items(): post[int(key)] = False if key in ('19', '20') else value
    # Preserve typed boolean identity, not Python False == 0.
    return json.dumps(actual, ensure_ascii=False) in (
        json.dumps(source, ensure_ascii=False), json.dumps(post, ensure_ascii=False))


def validate(entry, values=None, *, root=ROOT, db_path=DB):
    evidence = entry['evidence']
    source = evidence['source_row']
    projected = cells(source, evidence['checked_at_utc'])
    if (entry.get('network') != 'Katalys' or str(entry.get('provider_program_id')) != '1622'
            or entry.get('program') != NAME or entry.get('queue_update_id') != UPDATE
            or entry.get('create') or entry.get('cells') != projected
            or evidence.get('event_id') != EVENT or evidence.get('raw_sha256') != RAW_SHA):
        raise ValueError('pallas_ending_exact_projection_required')
    if hashlib.sha256((root / DECISION).read_bytes()).hexdigest() != DECISION_SHA:
        raise ValueError('pallas_ending_operator_authority_drift')
    with sqlite3.connect(db_path.resolve().as_uri() + '?mode=ro', uri=True) as db:
        db.execute('PRAGMA query_only=ON')
        records = db.execute('SELECT raw_path,raw_sha256 FROM inbox_events WHERE event_id=?', (EVENT,)).fetchall()
    if len(records) != 1 or records[0][1] != RAW_SHA:
        raise ValueError('pallas_ending_event_custody_invalid')
    raw = Path(records[0][0]).read_bytes()
    if hashlib.sha256(raw).hexdigest() != RAW_SHA:
        raise ValueError('pallas_ending_mail_hash_drift')
    mail = BytesParser(policy=policy.default).parsebytes(raw)
    auth = [str(v).lower() for v in mail.get_all('Authentication-Results', [])
            if str(v).lower().strip().startswith('mx.google.com;')]
    if (parseaddr(str(mail['From']))[1].lower() != 'noreply@katalys.com'
            or str(mail['Subject']) != f'Your Contract for {NAME} has ended'
            or not any(re.search(r'(?:^|;)\s*dmarc=pass\b[^;]*\bheader\.from=katalys\.com(?:\s|;|$)', v) for v in auth)):
        raise ValueError('pallas_ending_authenticated_mail_invalid')
    message_body = mail.get_body(preferencelist=('html', 'plain'))
    if message_body is None:
        raise ValueError('pallas_ending_notice_body_missing')
    body = message_body.get_content()
    if '9/30/2026' not in body or 'Your links have been deactivated automatically.' not in body:
        raise ValueError('pallas_ending_notice_terms_invalid')
    if values is not None:
        rows = [n for n, row in enumerate(values[1:], 2)
                if len(row) > 5 and row[4] == 'Katalys' and row[5] == APPLICATION]
        if rows != [entry['row']] or not matches(values[entry['row'] - 1] + [''] * (27 - len(values[entry['row'] - 1])), source, projected):
            raise ValueError('pallas_ending_full_preimage_or_postimage_required')


def record_suppression(entry, values):
    """Use existing hygiene writer only after the Sheet's exact ended readback."""
    validate(entry, values)
    if values[entry['row']-1][10] != STATUS:
        raise ValueError('pallas_suppression_requires_ended_readback')
    sys.path.append(str(ROOT/'scripts'))
    import glp_relationship_hygiene as hygiene
    ledger_path = hygiene.LEDGER
    raw = ledger_path.read_bytes()
    ledger = json.loads(raw)
    key = 'katalys:1622'
    if key in ledger['entries']:
        existing = ledger['entries'][key]
        if existing.get('source_event_id') != EVENT or existing.get('ended_effective_at') != '2026-09-30':
            raise ValueError('pallas_suppression_existing_conflict')
        return False
    hygiene.suppress(ledger, 'katalys', '1622', {'advertiser_name':NAME,'campaign_title':NAME,
        'destination':'https://pallashealth.com'}, 'operator_contract_ended_2026-09-30', 'ended')
    ledger['entries'][key].update(source='pallas_relationship_ending.v1', source_event_id=EVENT,
        raw_sha256=RAW_SHA, operator_decision_sha256=DECISION_SHA, ended_effective_at='2026-09-30',
        editorial_retained=True, sheet_row=entry['row'])
    ledger['updated_at_utc'] = hygiene.utc()
    if ledger_path.read_bytes() != raw:
        raise ValueError('pallas_suppression_concurrent_change')
    import affiliate_terms_fanout as fanout
    fanout.atomic(ledger_path, fanout.encoded(ledger))
    return True


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--queue-file',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    queue=json.loads(args.queue_file.read_bytes())
    if not isinstance(queue,list) or len(queue)!=1 or queue[0].get('update_id')!=UPDATE:
        raise ValueError('pallas_exact_single_queue_required')
    import glp_sheet_sync_drain as drain
    import affiliate_sheet_batch_writer as writer
    from googleapiclient.discovery import build
    svc=build('sheets','v4',credentials=writer.creds(),cache_discovery=False)
    evidence=drain.parse_evidence(queue[0])
    entry={'row':queue[0]['sheet_row'],'program':queue[0]['program_name'],
           'network':queue[0]['network'],'provider_program_id':queue[0]['program_id'],
           'queue_update_id':UPDATE,'evidence':evidence,
           'cells':cells(evidence['source_row'],evidence['checked_at_utc'])}
    validate(entry,drain.read_sheet(svc))
    command=[sys.executable,str(Path(__file__).with_name('glp_sheet_sync_drain.py')),
             '--queue-file',str(args.queue_file),'--output',str(args.output),
             '--apply','true' if args.apply else 'false']
    outcome=subprocess.run(command,check=False)
    if outcome.returncode: return outcome.returncode
    summary=json.loads(args.output.read_bytes())
    if summary.get('refused') or summary.get('escalated') or summary.get('deferred'):
        raise ValueError('pallas_drain_not_successful')
    if args.apply:
        with writer.LOCK.open('a+b') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            added=record_suppression(entry,drain.read_sheet(svc))
        print(json.dumps({'status':'PASS_ENDED_AND_SUPPRESSED','suppression_added':added,
                          'sheet_row':entry['row'],'network':'Katalys','program_id':'1622'}))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
