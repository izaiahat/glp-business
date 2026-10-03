#!/usr/bin/env python3
"""Weekly discovery admission into the existing affiliate_application_queue.

Curated from the host-only auto-apply runner: retain inventory normalization and
the common candidate choke, replace retired daily caps/direct join execution
with journaled admission. This module cannot submit applications or write Sheets.
The existing application machine and portal executor own all submit-time gates.
Default is a provider-free dry run; --admit only writes the existing queue.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import signal
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
BUSINESS = Path('/home/ubuntu/business')
MACHINE_DIR = BUSINESS / 'reports/glp-clinic-scout/affiliate-machine'
JOURNAL_DIR = MACHINE_DIR / 'applications/discovery-admissions'
LOCK_PATH = MACHINE_DIR / 'applications/.application-machine.lock'
APPLICATION_QUEUE_TABLE = 'wuDGU2Xg1LvwXuk6'
ADMISSION_SCHEMA = 'glp.discovery-application-admission.v1'
AUTHORITY = 'operator-approved:2026-10-01:weekly-discovery-to-existing-queue'
FRESH_RELATIONSHIPS = {'available', 'notjoined', 'not_joined', 'not_related', 'apply_needed'}
# Exact display labels emitted by the retained marketplace inventory producers.
# Never punctuation-slug unknown labels or alter opaque program IDs.
INVENTORY_NETWORK_ALIASES = {
    'cj affiliate': 'cj',
    'rakuten advertising': 'rakuten',
    'katalys / revoffers': 'katalys',
    'impact.com': 'impact',
    'uppromote marketplace': 'uppromote',
}
HELD = re.compile(r'\b(fella(?:\s+health)?|telos|xen\s*traffic|health\s*rx)\b', re.I)
NEGATIVE_US = re.compile(
    r'\b(?:not\s+(?:available|offered|serving|accepting)|unavailable|do\s+not\s+(?:serve|accept))'
    r'[^.!?]{0,90}\b(?:united states|usa|us|u\.s\.)\b', re.I)


def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def write_json(path, value):
    """Atomic/fsynced local intent before any uncertain queue mutation."""
    write_bytes(path, (json.dumps(value, sort_keys=True, indent=2) + '\n').encode())


def write_bytes(path, raw):
    """Publish complete source/intent bytes, including the parent directory."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def inventory_network(value):
    label = str(value).strip().lower()
    return INVENTORY_NETWORK_ALIASES.get(label, label)


def stable_key(network, program_id):
    network, program_id = inventory_network(network), str(program_id).strip()
    if not re.fullmatch(r'[a-z0-9_-]+', network) or not program_id:
        raise ValueError('missing_exact_network_program_identity')
    return 'discovery-' + digest([network, program_id])


def normalize_program(prog, network, doc):
    """Keep network IDs, relationship and unknown economics; never infer US."""
    network = inventory_network(network)
    region = prog.get('region') or (prog.get('primaryRegion') or {}).get('countryCode') or ''
    candidate = {
        'network': network.lower(),
        'program_id': str(prog.get('program_id') or prog.get('id') or ''),
        'name': str(prog.get('name') or ''),
        'description': str(prog.get('description') or ''),
        'categories': prog.get('categories') or [],
        'region': str(region),
        'currency': prog.get('currency') or prog.get('currencyCode'),
        'payout': prog.get('payout'),
        'terms': prog.get('terms'),
        'relationship': str(prog.get('relationship') or '').lower(),
        'network_status': str(prog.get('network_status') or ''),
        'offer_status': str(prog.get('offer_status') or prog.get('status') or ''),
        'held': prog.get('held'),
        'suppressed': prog.get('suppressed'),
        'official_url': str(prog.get('official_url') or prog.get('displayUrl') or ''),
        'application_url': str(prog.get('application_url') or prog.get('url') or ''),
        'lane': 'glp1-weightloss',
    }
    if network == 'awin' and candidate['program_id'].isdigit():
        candidate['application_url'] = (
            'https://ui.awin.com/awin/affiliate/2979387/merchant-profile/' + candidate['program_id'])
    # Direct providers have no network-issued ID. Discovery emits a stable
    # first-party hostname identity; a mutable brand label cannot substitute.
    if network == 'direct':
        host = (urlsplit(candidate['official_url']).hostname or '').lower().removeprefix('www.')
        if not host or candidate['program_id'] != 'domain:' + host:
            candidate['identity_error'] = 'direct_identity_requires_first_party_domain'
    return candidate


def read_inventory(path):
    path = Path(path).resolve()
    raw = path.read_bytes()
    doc = json.loads(raw)
    network = str(doc.get('network') or path.stem.split('-')[0]).lower()
    rows = []
    if isinstance(doc.get('relationships'), dict):
        for buckets in doc['relationships'].values():
            for relationship, programs in buckets.items():
                for prog in programs:
                    rows.append(normalize_program({**prog, 'relationship': relationship}, network, doc))
    else:
        if not isinstance(doc.get('programs'), list):
            raise ValueError('inventory_programs_missing:' + path.name)
        rows = [normalize_program(p, network, doc) for p in doc['programs']]
    return rows, {'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest()}


def freshest_inventories(machine_dir=MACHINE_DIR, *, include_direct=True):
    """Only inventory documents, never arbitrary enumeration summary JSON."""
    best = {}
    for pattern in ('backrun-*/marketplace/*.json', 'enumeration-*/*.json',
                    'marketplace-adapters-*/*-readback.json'):
        for path in Path(machine_dir).glob(pattern):
            if path.name.startswith('_'):
                continue
            try:
                doc = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if not isinstance(doc, dict) or not (isinstance(doc.get('programs'), list)
                                                  or isinstance(doc.get('relationships'), dict)):
                continue
            net = str(doc.get('network') or path.stem.split('-')[0]).lower()
            if net == 'direct' and not include_direct:
                continue
            if net not in best or path.stat().st_mtime_ns > best[net].stat().st_mtime_ns:
                best[net] = path
    return list(best.values())


def candidate_gate(candidate, dedupe_index):
    """Narrow the existing filter with permanent/held provider and exact-US checks."""
    for p in (str(ROOT), str(ROOT / 'scripts')):
        if p not in sys.path:
            sys.path.insert(0, p)
    from scripts import affiliate_niche_geo_filter as nfilter
    name = re.sub(r'[^a-z0-9]+', ' ', ' '.join(str(candidate.get(k) or '')
                  for k in ('name', 'network', 'official_url')).lower())
    if HELD.search(name):
        return 'provider_excluded_or_held'
    if any(candidate.get(k) not in (None, False, '', 'false', 'FALSE') for k in ('held', 'suppressed')):
        return 'source_held_or_suppressed'
    if candidate.get('identity_error'):
        return candidate['identity_error']
    if candidate['network'] == 'maxbounty':
        return 'maxbounty_requires_staged_submission_packet'
    if candidate.get('region', '').upper() not in {'US', 'USA', 'UNITED STATES', 'UNITED STATES OF AMERICA'}:
        return 'explicit_us_service_required'
    if NEGATIVE_US.search(candidate.get('description') or ''):
        return 'contradictory_us_service_evidence'
    if candidate['relationship'] not in FRESH_RELATIONSHIPS:
        return 'relationship_not_fresh_available'
    gate = nfilter.choke_candidate(candidate, require_application_url=True,
                                  require_fit_evidence=True,
                                  operation='weekly_discovery_admission', persist=False)
    if not gate['operational']:
        return 'candidate_gate:' + ';'.join(gate.get('reason_codes') or [])
    # Historical dedupe legs retain display labels. Ask each exact spelling of
    # this rail; canonicalizing intake must not lose an existing suppression.
    labels = [candidate['network'], *(label for label, canonical in
        INVENTORY_NETWORK_ALIASES.items() if canonical == candidate['network'])]
    for label in labels:
        verdict = dedupe_index.verdict(candidate['name'], label, candidate['program_id'],
                                       urls=(candidate['official_url'], candidate['application_url']))
        if verdict is not None:
            return 'suppressed_or_engaged:' + str(verdict.reason)
    return ''


class DiscoveryAdmissionPending(ValueError):
    """Exact custody is valid, but feeder readback has not confirmed admission."""


def validate_discovery_row(row, *, journal_dir=JOURNAL_DIR):
    """Consumer admission proof, independent of queue-supplied authority strings.

    This grants entry to existing executors only. It never supplies consent,
    form fields, credentials or a substitute for the exact-program executor gate.
    """
    payload = json.loads(row['payload']) if isinstance(row.get('payload'), str) else row['payload']
    key = stable_key(row['network'], row['program_id'])
    if row.get('queue_id') != key or row.get('operator_authority') != AUTHORITY:
        raise ValueError('discovery_admission_identity_or_authority_mismatch')
    path = Path(journal_dir) / (key + '.json')
    record = json.loads(path.read_text())
    expected = record.get('queue_row') or {}
    if record.get('schema') != ADMISSION_SCHEMA:
        raise ValueError('discovery_admission_schema_invalid')
    if any(row.get(k) != expected.get(k) for k in
           ('queue_id', 'network', 'program_id', 'program_name', 'payload', 'instructions', 'operator_authority')):
        raise ValueError('discovery_admission_queue_drift')
    if payload.get('candidate_sha256') != digest(payload.get('candidate')):
        raise ValueError('discovery_candidate_hash_mismatch')
    # Source is frozen in a journal-owned copy, so weekly refresh cannot
    # invalidate admitted candidates or silently alter their original evidence.
    source = record['source']
    source_path = Path(journal_dir) / 'sources' / (source['sha256'] + '.json')
    if hashlib.sha256(source_path.read_bytes()).hexdigest() != source['sha256']:
        raise ValueError('discovery_source_hash_mismatch')
    if record.get('state') in {'prepared', 'dispatching'}:
        raise DiscoveryAdmissionPending('discovery_admission_not_confirmed')
    if record.get('state') != 'confirmed':
        raise ValueError('discovery_admission_state_invalid')
    return dict(payload['candidate'])


def queue_client_default():
    # Runtime transport is intentionally lazy. Import/help/tests never read auth.
    if str(BUSINESS / 'ops') not in sys.path:
        sys.path.append(str(BUSINESS / 'ops'))
    import n8n_dt
    return n8n_dt


class AdmissionDeferred(Exception):
    """No next queue operation may start in this admission window."""


class _QueueReadDeadline(BaseException):
    # Read adapters may catch Exception and retry; reach our boundary instead.
    pass


def _require_budget(deadline):
    if time.monotonic() >= deadline:
        raise AdmissionDeferred('admission_budget_exhausted')


def _queue_rows(client, field, value, *, deadline):
    _require_budget(deadline)
    where = client.eq(field, value)
    _require_budget(deadline)
    previous_handler = signal.getsignal(signal.SIGALRM)
    if signal.getitimer(signal.ITIMER_REAL)[0]:
        raise RuntimeError('admission_requires_exclusive_read_timer')
    def expire(_signum, _frame):
        raise _QueueReadDeadline()
    signal.signal(signal.SIGALRM, expire)
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AdmissionDeferred('admission_budget_exhausted')
        signal.setitimer(signal.ITIMER_REAL, remaining)
        rows = client.rows(APPLICATION_QUEUE_TABLE, where, limit=10000)
    except _QueueReadDeadline:
        raise AdmissionDeferred('admission_budget_exhausted') from None
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
    _require_budget(deadline)
    if getattr(client, 'last_read_truncated', True):
        raise RuntimeError('application_queue_read_truncated')
    return rows


def _same_operation(observed, expected):
    return all(observed.get(k) == expected.get(k) for k in expected if k != 'status')


def admit_discovery(inventory_paths, *, queue_client=None, dedupe_index=None,
                    journal_dir=JOURNAL_DIR, lock_path=LOCK_PATH, apply=False,
                    deadline_seconds=120):
    deadline = time.monotonic() + deadline_seconds
    client = queue_client
    published_intake = None
    live_admission = apply and dedupe_index is None and queue_client is None
    if dedupe_index is None:
        from scripts import glp_application_dedupe as dedupe
        dedupe_index = dedupe.build(require=dedupe.ALL_LEGS, check_live_manifest=False)
    journal_dir = Path(journal_dir)
    outcomes, seen, remainder = [], set(), []
    lock = contextlib.nullcontext()
    if apply:
        Path(lock_path).parent.mkdir(parents=True, exist_ok=True)
        lock = Path(lock_path).open('a+')
    with lock as handle:
        if apply:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        if live_admission:
            from scripts.glp_published_provider_intake import run as published_run
            published_intake = published_run(apply=True)
        # Freeze the complete supplied denominator before any queue effect.
        # Every observation of an exact rail must pass; first-seen positive
        # evidence cannot discard a later hold, unknown geography or conflict.
        inventories = [read_inventory(path) for path in inventory_paths]
        refusals = {}
        for candidates, _source in inventories:
            for c in candidates:
                try:
                    key = stable_key(c['network'], c['program_id'])
                except ValueError:
                    continue  # the row-local identity refusal below owns this
                reason = candidate_gate(c, dedupe_index)
                if reason:
                    refusals.setdefault(key, set()).add(reason)
        for candidates, source in inventories:
            for c in candidates:
                identity = [c['network'], c['program_id']]
                result = {'network': identity[0], 'program_id': identity[1]}
                outcomes.append(result)
                if time.monotonic() >= deadline:
                    result.update(status='deferred', reason='admission_budget_exhausted')
                    remainder.append({'candidate': c, 'source': source, 'outcome': result})
                    continue
                try:
                    key = stable_key(*identity)
                except ValueError as exc:
                    result.update(status='refused', reason=str(exc))
                    continue
                result['queue_id'] = key
                if key in refusals:
                    result.update(status='refused', reason=';'.join(sorted(refusals[key])))
                    continue
                if key in seen:
                    result.update(status='duplicate', reason='same_network_program')
                    continue
                seen.add(key)
                if not apply:
                    result.update(status='would_admit')
                    continue
                record_path = journal_dir / (key + '.json')
                record = json.loads(record_path.read_text()) if record_path.exists() else None
                insert_started = False
                dispatch_prepared = False
                try:
                    _require_budget(deadline)  # classification/local intent may exhaust it
                    if client is None:
                        client = queue_client_default()
                    if record is None:
                        # The existing operator queue is also an idempotency leg.
                        matches = [r for r in _queue_rows(client, 'program_id', c['program_id'], deadline=deadline)
                                   if inventory_network(r.get('network') or '') == c['network']]
                        if matches:
                            result.update(status='already_queued', reason='existing_network_program')
                            continue
                        payload = {'admission_schema': ADMISSION_SCHEMA, 'candidate': c,
                                   'candidate_sha256': digest(c), 'source': source}
                        queue_row = {
                            'queue_id': key, 'network': c['network'], 'program_id': c['program_id'],
                            'program_name': c['name'], 'payload': json.dumps(payload, sort_keys=True),
                            'instructions': 'Use the configured truthful standard profile through the existing portal executor; preserve every submit-time gate. No email fallback.',
                            'operator_authority': AUTHORITY, 'status': 'queued', 'queued_at_utc': utc(),
                        }
                        source_bytes = Path(source['path']).read_bytes()
                        if hashlib.sha256(source_bytes).hexdigest() != source['sha256']:
                            raise ValueError('inventory_changed_during_admission')
                        source_copy = journal_dir / 'sources' / (source['sha256'] + '.json')
                        if not source_copy.exists():
                            write_bytes(source_copy, source_bytes)
                        record = {'schema': ADMISSION_SCHEMA, 'state': 'prepared',
                                  'source': source, 'queue_row': queue_row}
                        write_json(record_path, record)
                    source_copy = journal_dir / 'sources' / (record['source']['sha256'] + '.json')
                    if hashlib.sha256(source_copy.read_bytes()).hexdigest() != record['source']['sha256']:
                        raise ValueError('discovery_source_hash_mismatch')
                    expected = record['queue_row']
                    landed = _queue_rows(client, 'queue_id', key, deadline=deadline)
                    if not landed:
                        if record['state'] != 'prepared':
                            result.update(status='reconcile_required', reason='queue_effect_uncertain_or_removed')
                            continue
                        _require_budget(deadline)
                        record['state'] = 'dispatching'
                        dispatch_prepared = True
                        write_json(record_path, record)
                        _require_budget(deadline)  # fsync can consume the remaining window
                        insert_started = True
                        try:
                            # Never interrupt an in-flight effect. If it overruns,
                            # leave dispatching for a future exact-target readback.
                            client.insert(APPLICATION_QUEUE_TABLE, expected)
                        except Exception:
                            pass
                        landed = _queue_rows(client, 'queue_id', key, deadline=deadline)
                    if len(landed) != 1 or not _same_operation(landed[0], expected):
                        result.update(status='reconcile_required', reason='queue_readback_missing_or_drift')
                        continue
                    result['queue_status'] = landed[0].get('status')
                    if result['queue_status'] != 'queued':
                        result.update(status='reconcile_required', reason='queue_lifecycle_not_queued')
                        continue  # never requeue claimed, blocked or completed work
                    already = record['state'] == 'confirmed'
                    record['state'] = 'confirmed'
                    write_json(record_path, record)
                    result.update(status='already_admitted' if already else 'admitted')
                except AdmissionDeferred:
                    if dispatch_prepared and not insert_started:
                        # Only our newly journaled, never-started dispatch can
                        # return to prepared. Prior uncertain effects stay held.
                        record['state'] = 'prepared'
                        write_json(record_path, record)
                    uncertain = bool(record and record['state'] != 'prepared')
                    result.update(status='reconcile_required' if uncertain else 'deferred',
                                  reason='admission_budget_exhausted')
                    remainder.append({'candidate': c, 'source': source, 'outcome': result})
        if apply and remainder:
            # Freeze the source denominator even when no per-row intent was
            # created. No queue call is needed to preserve the deferred tail.
            for item in remainder:
                source = item['source']
                frozen = journal_dir / 'sources' / (source['sha256'] + '.json')
                if not frozen.exists():
                    raw = Path(source['path']).read_bytes()
                    if hashlib.sha256(raw).hexdigest() != source['sha256']:
                        raise ValueError('inventory_changed_during_admission')
                    write_bytes(frozen, raw)
            remainder_path = journal_dir / 'deferred' / (digest(remainder) + '.json')
            write_json(remainder_path, {'schema': ADMISSION_SCHEMA, 'status': 'DEFERRED',
                                       'remainder': remainder})
    report = {'schema': ADMISSION_SCHEMA, 'mode': 'admit' if apply else 'dry_run',
            'outcomes': outcomes, 'candidate_count': len(outcomes),
            'effects': {'applications_submitted': 0, 'sheet_writes': 0},
            'status': 'INCOMPLETE' if any(r['status'] in {'deferred', 'reconcile_required'} for r in outcomes) else 'COMPLETE'}
    if published_intake is not None:
        report['published_intake'] = {key: published_intake[key] for key in ('status', 'counts', 'effects', 'output')}
        if published_intake['status'] != 'COMPLETE':
            report['status'] = 'INCOMPLETE'
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inventory', action='append', default=[])
    parser.add_argument('--admit', action='store_true', help='enqueue only; never submit')
    parser.add_argument('--plan', action='store_true', help='compatibility alias for default dry run')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.plan and args.admit:
        parser.error('--plan and --admit are mutually exclusive')
    report = admit_discovery(args.inventory or freshest_inventories(), apply=args.admit)
    write_json(args.output, report)
    print(json.dumps({'status': report['status'], 'candidate_count': report['candidate_count'], 'output': str(args.output)}))
    return 0 if report['status'] == 'COMPLETE' else 2


if __name__ == '__main__':
    sys.exit(main())
