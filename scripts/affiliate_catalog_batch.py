#!/usr/bin/env python3
"""Bounded, resumable API-first catalog producer for scheduled affiliate jobs.

The installed legacy adapters remain the documented provider contracts. Their
portal fallback is disabled: browser work belongs to an authorized vault run,
not an unattended Playwright/password profile. Checkpoints contain normalized
inventories only, never authentication responses or transport credentials.
"""
from __future__ import annotations
import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import sys
import time

BUSINESS = Path('/home/ubuntu/business')
MACHINE = BUSINESS / 'reports/glp-clinic-scout/affiliate-machine'
STATE = MACHINE / 'catalog-batch/checkpoint.json'
LEGACY_SHA = '58e3073c0ae9cf957acfdae9ffd9904c4f490090ce7c1a08225a6b80e180e7da'


def save(path, doc):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.new')
    with tmp.open('w') as stream:
        json.dump(doc, stream, indent=2, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    tmp.chmod(0o600)
    tmp.replace(path)


class ReadDeadline(BaseException):
    pass


@contextlib.contextmanager
def bounded_read(seconds):
    previous = signal.getsignal(signal.SIGALRM)
    if signal.getitimer(signal.ITIMER_REAL)[0]:
        raise RuntimeError('catalog_timer_already_owned')
    def expire(*_args):
        raise ReadDeadline()
    signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def portal_refusal(network, display, api_attempt=None):
    out = dict(api_attempt or {})
    out.update(network=display, schema='affiliate-marketplace-supply-v1')
    out.setdefault('programs', [])
    out.setdefault('reported_total', None)
    out['enumerated'] = len(out['programs'])
    out['blockers'] = list(out.get('blockers') or []) + [{
        'error_class': 'authorized_vault_browser_required',
        'detail': 'No unattended portal login; API tried first where documented. '
                  'Use a fresh vault browser session only with the heavy lock free.'}]
    out.setdefault('auth_state', 'BLOCKED')
    return out


def live_legacy():
    source = BUSINESS / 'scripts/marketplace_supply_sweep.py'
    if hashlib.sha256(source.read_bytes()).hexdigest() != LEGACY_SHA:
        raise RuntimeError('legacy_catalog_contract_pin_mismatch')
    sys.path.insert(0, str(BUSINESS / 'scripts'))
    spec = importlib.util.spec_from_file_location('f01_catalog_contract', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._portal_catalog_sweep = portal_refusal
    return module


def run(legacy, outdir, *, state=STATE, only=None, budget=420, source_timeout=75):
    """Rotate unfinished networks; preserve complete denominators for 24h."""
    outdir, state = Path(outdir), Path(state)
    state.parent.mkdir(parents=True, exist_ok=True)
    with state.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        now = dt.datetime.now(dt.timezone.utc)
        document = json.loads(state.read_text()) if state.exists() else {}
        if document and document.get('schema') != 'affiliate-catalog-batch-v1':
            raise ValueError('unsupported_catalog_checkpoint')
        rows = document.setdefault('networks', {})
        document['schema'] = 'affiliate-catalog-batch-v1'
        names = list(only or legacy.SWEEPS)
        unknown = set(names) - set(legacy.SWEEPS)
        if unknown:
            raise ValueError('unknown_catalog_network')
        # Older attempts first; even an early blocked or slow provider cannot
        # monopolize successive runs. Held XenTraffic is never invoked.
        names.sort(key=lambda name: rows.get(name, {}).get('attempted_at', ''))
        deadline = time.monotonic() + budget
        attempted = []
        for name in names:
            if name == 'xentraffic':
                continue
            prior = rows.get(name, {})
            if prior.get('complete') and (now - dt.datetime.fromisoformat(
                    prior['attempted_at'])).total_seconds() < 86400:
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 1:
                break
            attempted.append(name)
            try:
                with bounded_read(min(source_timeout, remaining)):
                    inventory = legacy.SWEEPS[name](legacy._base.RequestsTransport(timeout=20))
            except (ReadDeadline, Exception) as exc:
                inventory = {'network': name, 'schema': legacy.SCHEMA,
                             'auth_state': 'BLOCKED', 'programs': [], 'enumerated': 0,
                             'reported_total': None, 'blockers': [{
                                 'error_class': 'bounded_read_timeout' if isinstance(exc, ReadDeadline)
                                                else type(exc).__name__}]}
            verdict = legacy.completeness(inventory)
            rows[name] = {'attempted_at': now.isoformat(), 'inventory': inventory,
                          'complete': verdict['complete'], 'verdict': verdict}
            save(state, document)  # every network survives an interrupted parent
        summary = {'schema': legacy.SCHEMA, 'captured_at_utc': now.isoformat(),
                   'outdir': str(outdir), 'networks': {}, 'gaps': [],
                   'batch': {'attempted': attempted, 'checkpoint': str(state),
                             'budget_seconds': budget},
                   'effects': {'provider_mutations': 0, 'tracking_clicks': 0,
                               'sheet_writes': 0}, 'secret_printed': False}
        for name in names:
            row = rows.get(name)
            if name == 'xentraffic':
                inventory = {'network': name, 'auth_state': 'BLOCKED', 'programs': [],
                             'enumerated': 0, 'reported_total': None,
                             'blockers': [{'error_class': 'operator_hold'}]}
            elif row:
                inventory = dict(row['inventory'])
                inventory['catalog_observed_at_utc'] = row['attempted_at']
            else:
                inventory = {'network': name, 'auth_state': 'DEFERRED', 'programs': [],
                             'enumerated': 0, 'reported_total': None,
                             'blockers': [{'error_class': 'batch_pending'}]}
            verdict = legacy.completeness(inventory)
            path = outdir / (name + '-inventory.json')
            save(path, inventory)
            summary['networks'][name] = {**verdict, 'auth_state': inventory['auth_state'],
                                        'inventory': str(path),
                                        'catalog_observed_at_utc': inventory.get('catalog_observed_at_utc')}
            if not verdict['complete']:
                summary['gaps'].append({'network': name, 'reason': verdict['detail']})
        if any(r.get('inventory', {}).get('auth_state') == 'DEFERRED' for r in rows.values()):
            raise ValueError('deferred_catalog_must_not_be_checkpointed')
        summary['total_enumerated'] = sum(r['enumerated'] for r in summary['networks'].values())
        summary['all_complete'] = not summary['gaps']
        summary['batch']['pending'] = [n for n in names if n not in rows and n != 'xentraffic']
        save(outdir / '_sweep-summary.json', summary)
        return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--outdir', required=True, type=Path)
    parser.add_argument('--only', nargs='*')
    parser.add_argument('--time-budget-seconds', type=float, default=420)
    args = parser.parse_args(argv)
    if not 0 < args.time_budget_seconds <= 420:
        parser.error('catalog budget must be in (0,420]')
    print(json.dumps(run(live_legacy(), args.outdir, only=args.only,
                         budget=args.time_budget_seconds), sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
