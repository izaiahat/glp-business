#!/usr/bin/env python3
"""Guarded GLP commission notice. Manual entry point; never infers income from gaps.

A per-event journal holds the lock across post/readback. Ambiguous POST failures stay
queued/uncertain and require exact channel reconciliation, never a blind retry.
"""
from __future__ import annotations
import argparse
import fcntl
import json
import os
import re
from pathlib import Path

PAYMENTS = '1525219463866089566'


def eligible(event):
    return (event.get('authenticated') is True and event.get('classification') == 'REAL'
            and event.get('attribution') == 'GLP' and event.get('event_type') == 'commission'
            and event.get('mapped_status') in ('approved', 'payable', 'paid')
            and type(event.get('commission_minor')) is int and event['commission_minor'] > 0
            and event.get('currency') in ('USD', 'EUR', 'GBP', 'CAD', 'AUD')
            and isinstance(event.get('receipt_sha256'), str)
            and re.fullmatch('[a-f0-9]{64}', event['receipt_sha256']) is not None)


def card(event, *, test=False):
    if not test and not eligible(event):
        raise ValueError('not_verified_positive_real_commission')
    if test and event.get('classification') != 'TEST':
        raise ValueError('test_classification_required')
    identity = [event.get(k) for k in ('network', 'account_id', 'tenant_id', 'program_id', 'provider_event_id')]
    if not all(isinstance(v, str) and re.fullmatch('[A-Za-z0-9._:/-]{1,100}', v) for v in identity):
        raise ValueError('incomplete_event_identity')
    key = event.get('revision_key')
    if not isinstance(key, str) or not re.fullmatch('[a-f0-9]{64}', key):
        raise ValueError('revision_key_required')
    amount = event.get('commission_minor')
    currency = event.get('currency')
    if type(amount) is int and amount > 0 and currency in ('USD', 'EUR', 'GBP', 'CAD', 'AUD'):
        display = f'{amount // 100}.{amount % 100:02d} {currency}'
    elif test:
        display = 'unknown'
    else:
        raise ValueError('unknown_or_zero_real_commission')
    status = event.get('mapped_status')
    if not isinstance(status, str) or not re.fullmatch('[a-z_]{1,24}', status):
        raise ValueError('status_required')
    return ('GLP Clinic Scout earnings ' + ('TEST — synthetic, no real earning' if test else 'REAL — source-reported commission')
            + '\nIdentity: ' + ' / '.join(identity) + '\nStatus: ' + status
            + '\nCommission: ' + display + '\nEvent revision: ' + key
            + '\nCash settlement: not established by this notice')


def _save(path, doc):
    tmp = path.with_name(path.name + '.tmp.' + str(os.getpid()))
    try:
        with tmp.open('x') as f:
            json.dump(doc, f, sort_keys=True, indent=2)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def deliver(event, journal, transport, *, test=False, dry_run=False):
    """transport(method, path, payload=None) -> {'ok', 'json'}; only #payments."""
    if not test and not eligible(event):
        return {'state': 'SUPPRESSED_UNVERIFIED_OR_NONPOSITIVE', 'channel': None}
    content = card(event, test=test)
    key = ('test:' if test else 'real:') + event['revision_key']
    if dry_run:
        return {'state': 'DRY_RUN', 'channel': PAYMENTS, 'key': key, 'content': content}
    journal = Path(journal)
    journal.parent.mkdir(parents=True, exist_ok=True)
    with journal.open('a+') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        raw = f.read()
        doc = json.loads(raw) if raw else {}
        item = doc.get(key)
        if item and item['state'] == 'sent':
            return {'state': 'ALREADY_SENT', 'channel': PAYMENTS, 'message_id': item['message_id']}
        if item and item['state'] == 'uncertain':
            # Prefer exact message readback when POST returned an ID. Otherwise
            # recent search can confirm delivery, but a miss never proves absence.
            try:
                if item.get('message_id'):
                    response = transport('GET', f'/channels/{PAYMENTS}/messages/{item["message_id"]}')
                    match = [response.get('json')] if response.get('ok') else []
                else:
                    response = transport('GET', f'/channels/{PAYMENTS}/messages?limit=100')
                    match = response.get('json') if response.get('ok') and isinstance(response.get('json'), list) else []
                match = [m for m in match if isinstance(m, dict) and m.get('content') == content]
                if len(match) == 1 and str(match[0].get('id', '')).isdigit():
                    item.update(state='sent', message_id=str(match[0]['id']))
                    doc[key] = item
                    _rewrite_locked(f, doc)
                    return {'state': 'RECONCILED_SENT', 'channel': PAYMENTS, 'message_id': item['message_id']}
            except Exception:
                pass
            return {'state': 'QUEUED_UNCERTAIN', 'channel': PAYMENTS}
        doc[key] = {'state': 'uncertain', 'content': content}
        _rewrite_locked(f, doc)
        try:
            response = transport('POST', f'/channels/{PAYMENTS}/messages',
                                 {'content': content, 'allowed_mentions': {'parse': []}})
            message_id = str((response.get('json') or {}).get('id') or '')
            if not response.get('ok') or not message_id.isdigit():
                return {'state': 'QUEUED_UNCERTAIN', 'channel': PAYMENTS}
            doc[key]['message_id'] = message_id
            _rewrite_locked(f, doc)
            readback = transport('GET', f'/channels/{PAYMENTS}/messages/{message_id}')
            if not readback.get('ok') or (readback.get('json') or {}).get('content') != content:
                return {'state': 'QUEUED_UNCERTAIN', 'channel': PAYMENTS}
            doc[key]['state'] = 'sent'
            _rewrite_locked(f, doc)
            return {'state': 'SENT', 'channel': PAYMENTS, 'message_id': message_id}
        except Exception:
            return {'state': 'QUEUED_UNCERTAIN', 'channel': PAYMENTS}


def _rewrite_locked(f, doc):
    f.seek(0)
    f.truncate()
    json.dump(doc, f, sort_keys=True)
    f.flush()
    os.fsync(f.fileno())


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--event', type=Path, required=True)
    p.add_argument('--journal', type=Path, required=True)
    p.add_argument('--test', action='store_true', help='explicit synthetic TEST event')
    p.add_argument('--ledger', type=Path, help='required reporting-v2 ledger for REAL events')
    p.add_argument('--dry-run', action='store_true')
    args = p.parse_args()
    event = json.loads(args.event.read_text())
    if not args.test:
        if args.ledger is None or not args.ledger.is_file():
            p.error('REAL event requires existing reporting-v2 ledger')
        from glp_network_revenue_v2 import Store
        with Store(args.ledger) as store:
            current = [row for row in store.current() if row['revision_key'] == event.get('revision_key')]
        if len(current) != 1 or current[0] != event:
            p.error('event does not match the current immutable reporting ledger revision')
    if args.dry_run:
        result = deliver(event, args.journal, None, test=args.test, dry_run=True)
    else:
        # The installed sender owns credentials, routing and request policy.
        from payments_money_in_router import discord_request
        result = deliver(event, args.journal, discord_request, test=args.test)
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
