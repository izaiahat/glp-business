#!/usr/bin/env python3
"""One-shot sponsor cost booking under the Oct-15 operator decision.

Only authenticated usage GETs. No scrape/search/replay, refund, terminal-outcome
change or reservation settlement. Default is a no-ledger-write plan. Historical
raw null receipts remain immutable; latest logical paid sponsor costs become
non-null only after separately reviewed adoption and explicit --apply.
"""
from __future__ import annotations
import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path

from firecrawl_client import FirecrawlClient, LEDGER
from glp_sponsor_firecrawl_supply import Producer, SPONSOR_CALLERS
from glp_firecrawl_jobs import private_json

DECISION_SHA256 = '63b94cd28422b5bb4a5399afbe9854a5f80dbe0d5aa7418f0c7793f6eb1f5b87'
CUTOFF = dt.datetime(2026, 10, 15, 1, 4, tzinfo=dt.timezone.utc)
REASON = ('Operator Oct15 API-only conservative accounting: each unknown is charged '
          'the WHOLE covering authenticated account usage-window delta, minimum one. '
          'Not an exact billed receipt; no allocation, zero-cost inference, replay or settlement.')


def encoded(value):
    return (json.dumps(value, sort_keys=True) + '\n').encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def timestamp(value):
    result = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('UTC timestamp required')
    return result


def charges_for(rows, historical, observed):
    if historical.get('success') is not True:
        raise RuntimeError('authenticated_usage_missing')
    latest = {row['operation_id']: row for row in rows if row.get('caller') in SPONSOR_CALLERS}
    charges = []
    for ident, row in sorted(latest.items()):
        if row.get('endpoint', '').startswith('/v2/team/'):
            continue  # Read-only account metadata has no paid cost to book.
        cost = row.get('creditsUsed')
        if cost is not None:
            if type(cost) not in (int, float) or not 0 <= cost < float('inf'):
                raise RuntimeError('invalid_existing_cost')
            continue
        at = timestamp(row['at_utc'])
        if at >= CUTOFF:
            raise RuntimeError('null_receipt_outside_decision_scope')
        matches = []
        for period in historical.get('periods', []):
            start = timestamp(period['startDate'])
            end = timestamp(period['endDate']) if period.get('endDate') else observed
            delta = period.get('creditsUsed')
            if start <= at < end and type(delta) is int and delta >= 0:
                matches.append((start, end, delta))
        if len(matches) != 1:
            raise RuntimeError('authenticated_usage_window_missing_or_ambiguous')
        start, end, delta = matches[0]
        charges.append({'operation_id': ident, 'credits': max(1, delta),
                        'window_start': start.isoformat(), 'window_end': end.isoformat(),
                        'account_usage_delta': delta, 'original': row})
    return charges


def book(state, ledger, client, receipt, *, apply=False, expected_sha=None):
    state, ledger, receipt = Path(state), Path(ledger), Path(receipt)
    if receipt.exists() or receipt.resolve() == ledger.resolve():
        raise RuntimeError('new_receipt_path_required')
    producer = Producer(state_dir=state, import_only=True)
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Refuse active daily producer or any shared-budget holder before API access.
    with (state / 'run.lock').open('a') as run_lock:
        fcntl.flock(run_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with producer.budget_locked():
            usage = client.request('team/credit-usage', method='GET')
            history = client.request('team/credit-usage/historical', method='GET')
            observed = dt.datetime.now(dt.timezone.utc)
            data = usage.get('data') or {}
            if (usage.get('success') is not True or type(data.get('remainingCredits')) is not int
                    or type(data.get('planCredits')) is not int
                    or not 0 <= data['remainingCredits'] <= data['planCredits']
                    or not timestamp(data['billingPeriodStart']) <= observed < timestamp(data['billingPeriodEnd'])):
                raise RuntimeError('authenticated_usage_missing')
            # Explicit safe fields only; provider schema may include API key IDs.
            safe_history = {'success': history.get('success'), 'periods': [
                {k: p[k] for k in ('startDate', 'endDate', 'creditsUsed') if k in p}
                for p in history.get('periods', [])]}
            evidence = {'observed_utc': observed.isoformat(),
                        'current': {k: data[k] for k in ('remainingCredits', 'planCredits', 'billingPeriodStart', 'billingPeriodEnd')},
                        'historical': safe_history}
            evidence_sha = sha(encoded(evidence))
            # Same advisory ledger flock as the shared append-only client.
            fd = os.open(ledger, os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW)
            with os.fdopen(fd, 'r+b') as stream:
                fcntl.flock(stream, fcntl.LOCK_EX)
                before = stream.read()
                pre = sha(before)
                if apply and pre != expected_sha:
                    raise RuntimeError('ledger_changed_replan_required')
                if before and not before.endswith(b'\n'):
                    raise RuntimeError('incomplete_ledger_row')
                rows = [json.loads(line) for line in before.splitlines() if line.strip()]
                charges = charges_for(rows, safe_history, observed)
                additions = []
                for charge in charges:
                    row = dict(charge['original'])
                    row['creditsUsed'] = charge['credits']
                    row['cost_booking'] = {'reason': REASON, 'decision_sha256': DECISION_SHA256,
                        'booked_utc': observed.isoformat(), 'pre_ledger_sha256': pre,
                        'usage_evidence_sha256': evidence_sha, 'receipt': str(receipt.resolve()),
                        'original_row_sha256': sha(encoded(charge['original'])),
                        **{k: charge[k] for k in ('window_start', 'window_end', 'account_usage_delta')}}
                    additions.append(row)
                extra = b''.join(encoded(row) for row in additions)
                result = {'schema': 'sponsor-conservative-cost-booking-v1', 'status': 'prepared' if apply else 'plan',
                          'reason': REASON, 'decision_sha256': DECISION_SHA256,
                          'pre_ledger_sha256': pre, 'expected_post_ledger_sha256': sha(before + extra),
                          'booked_operations': len(charges), 'charges': [{k: v for k, v in c.items() if k != 'original'} for c in charges],
                          'usage_evidence': evidence, 'usage_evidence_sha256': evidence_sha,
                          'paid_operations_executed': 0, 'reservations_changed': False}
                # Prepare durable pre/post evidence BEFORE appending. A crash is
                # reconciled by these hashes, never by repeating provider work.
                receipt.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                out = os.open(receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(out, 'wb') as f:
                    f.write(encoded(result)); f.flush(); os.fsync(f.fileno())
                if apply:
                    stream.write(extra); stream.flush(); os.fsync(stream.fileno())
                    stream.seek(0); after = stream.read()
                    if sha(after) != result['expected_post_ledger_sha256']:
                        raise RuntimeError('booking_readback_hash_mismatch')
                    result.update(status='applied', post_ledger_sha256=sha(after),
                                  unknown_remaining=len(charges_for([json.loads(l) for l in after.splitlines() if l], safe_history, observed)))
                    private_json(receipt, result)
                return result


def main():
    from glp_sponsor_campaign import STATE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--receipt', required=True, type=Path)
    parser.add_argument('--decision', required=True, type=Path)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--expected-ledger-sha256')
    args = parser.parse_args()
    if sha(args.decision.read_bytes()) != DECISION_SHA256:
        parser.error('exact approved Oct15 decision required')
    if args.apply and not args.expected_ledger_sha256:
        parser.error('--apply requires the planned ledger hash')
    result = book(STATE, LEDGER, FirecrawlClient('sponsor_full_reconciliation'), args.receipt,
                  apply=args.apply, expected_sha=args.expected_ledger_sha256)
    print(json.dumps({k: result[k] for k in ('status', 'booked_operations', 'pre_ledger_sha256')}))


if __name__ == '__main__':
    main()
