#!/usr/bin/env python3
"""Registered pre-expiry refresh and class-agnostic R14 automatic adoption."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.request
from urllib.parse import urlsplit, urlunsplit

from glp_evidence_capture_refresh import (
    ROOT, CANDIDATE, CAPTURE_ID as FDA_ID, REQUESTED_URL as FDA_URL,
    VERSIONS, canonical, create_successor, encoded, immutable, sha, utc, visible,
)

OUTPUT = ROOT / 'data/evidence-freshness/scheduled'
STANDING_ACCEPTANCE_RECEIPT = ROOT / 'ops/evidence-freshness/routine-refresh-standing-acceptance.json'
LEAD_DAYS = 7
ADOPTIONS = ROOT / 'ops/evidence-freshness/adoptions.jsonl'
CANDIDATES = (CANDIDATE, ROOT / 'site-system/flagship-v2/candidates/glp208-source-refresh')


def load_inputs():
    return json.loads(subprocess.check_output(
        ['node', str(ROOT / 'scripts/glp_evidence_ledger.mjs'), '--inputs', str(CANDIDATE)], text=True))


def bound(inputs, kind):
    data = Path(inputs[kind + '_path']).read_bytes()
    if sha(data) != inputs[kind + '_sha256']:
        raise ValueError('capture_parent_read_race:' + kind)
    return data


def protected_hashes():
    paths = [candidate / name for candidate in CANDIDATES for name in (
        'src/mystery-shop.mjs', 'data/wave4-source-capture-ledger.json', 'data/mystery-shop.json')]
    paths.append(STANDING_ACCEPTANCE_RECEIPT)
    return {str(path): sha(path.read_bytes()) for path in paths}


def check_successor(entry_paths):
    for candidate in CANDIDATES:
        result = subprocess.run(['node', str(ROOT / 'scripts/glp_evidence_ledger.mjs'),
            '--check-successor', str(candidate), *[str(path) for path in entry_paths]],
            capture_output=True, text=True)
        if result.returncode:
            try:
                detail = json.loads(result.stderr)['error']
            except (json.JSONDecodeError, KeyError):
                raise RuntimeError('consumer_execution_failed:exit_' + str(result.returncode))
            raise ValueError('consumer_refused:' + detail)


class MemberRefused(ValueError):
    """One member cannot be adopted; the rest of the batch may still proceed."""

    def __init__(self, capture_id, reason):
        super().__init__(reason)
        self.capture_id = capture_id
        self.reason = reason


def standing_acceptance():
    receipt_bytes = STANDING_ACCEPTANCE_RECEIPT.read_bytes()
    receipt = json.loads(receipt_bytes)
    authority = Path(receipt['authority_source']).read_bytes()
    if sha(authority) != receipt['authority_sha256'] or authority.decode() != receipt['authority_text']:
        raise ValueError('standing_acceptance_authority_changed')
    return receipt_bytes


def check_member_proof(record, proof):
    """Validate a member's own capture before any ledger state is read or written.

    Order matters and is unchanged from the single-member path: a proof that does not
    support its retained claim is refused on its own terms, never after touching the chain.
    """
    body, _ = check_response(record, proof['response'])
    if proof['content_sha256'] != sha(body.encode()) or missing_excerpts(record['exact_quote'], body):
        raise MemberRefused(record['capture_id'], 'retained_claim_not_supported')
    material = proof['material_change']
    if material['retained_claims_supported'] is not True or material['missing_claims']:
        raise MemberRefused(record['capture_id'], 'retained_claim_not_supported')
    return body


def propose_member(record, proof, ledger_bytes, config_bytes, captures, receipt_bytes, sequence, previous_sha):
    """Build and fully validate ONE link of the chain against the running successor state.

    Every check the single-member path made is made here, against the state this member
    actually lands on, so a later member is never validated against a parent it will not have.
    """
    body = check_member_proof(record, proof)
    current = next(r for r in json.loads(ledger_bytes)['records'] if r['capture_id'] == record['capture_id'])
    if current != record:
        raise MemberRefused(record['capture_id'], 'capture_parent_changed')
    previous_binding = captures.get(record['capture_id'])
    previous = json.loads(bound(previous_binding, 'capture')) if previous_binding else None
    try:
        proposal = create_successor(ledger_bytes, config_bytes, proof['response'],
            proof['fetch_started_at'], proof['fetched_at'], VERSIONS, previous, record['capture_id'])
    except ValueError as exc:
        raise MemberRefused(record['capture_id'], str(exc)) from exc
    capture = json.loads(bound(proposal, 'capture'))
    if previous and visible(previous['response']['data']['markdown']) != visible(body):
        raise MemberRefused(record['capture_id'],
            'material_content_change_review_required:' + proposal['capture_path'])
    if capture['material_change']['retained_claims_supported'] is not True or capture['material_change']['missing_claims']:
        raise MemberRefused(record['capture_id'], 'retained_claim_not_supported')
    entry = {**proposal, 'sequence': sequence,
             'previous_adoption_sha256': previous_sha,
             'standing_acceptance_sha256': sha(receipt_bytes)}
    entry_path = Path(proposal['ledger_path']).parent / 'adoption-entry.json'
    immutable(entry_path, encoded(entry))
    return {'record': record, 'proof': proof, 'entry': entry, 'entry_path': entry_path,
            'proposal': proposal, 'observation_ids': [r['id'] for r in capture['observation_checks']],
            'ledger_bytes': bound(proposal, 'ledger'), 'config_bytes': bound(proposal, 'config'),
            'capture_binding': {'capture_path': proposal['capture_path'],
                                'capture_sha256': proposal['capture_sha256']}}


def build_chain(pairs, receipt_bytes):
    """Chain every ready member onto the previous member's successor, not onto disk."""
    inputs = load_inputs()
    ledger_bytes, config_bytes = bound(inputs, 'ledger'), bound(inputs, 'config')
    captures = dict(inputs.get('captures') or {})
    sequence, previous_sha = inputs['adoption_sequence'], inputs['previous_adoption_sha256']
    links = []
    for record, proof in pairs:
        sequence += 1
        link = propose_member(record, proof, ledger_bytes, config_bytes, captures,
                              receipt_bytes, sequence, previous_sha)
        ledger_bytes, config_bytes = link['ledger_bytes'], link['config_bytes']
        captures[record['capture_id']] = link['capture_binding']
        previous_sha = sha(canonical(link['entry']).encode())
        links.append(link)
    return links


def adopt_batch(pairs, protected):
    """Adopt every ready member as ONE hash-chained transaction.

    Simultaneous expiry is not a set of independent failures: adopting a single member
    leaves every other member's stale clock in the successor, so the consumer refuses in
    any order and no sequence of single-member adoptions reaches a valid state. The chain
    is therefore validated as a whole. Nothing is relaxed -- the consumer remains the sole
    authority on the successor, a member whose own claim is unsupported is dropped and the
    chain rebuilt without it, and a successor the consumer refuses adopts NOTHING.
    """
    receipt_bytes = standing_acceptance()
    refusals, remaining, links = {}, [], []
    # Each member's own capture is judged before any ledger state is read, exactly as the
    # single-member path did: an unsupported claim never reaches the chain.
    for record, proof in pairs:
        try:
            check_member_proof(record, proof)
        except MemberRefused as refusal:
            refusals[refusal.capture_id] = refusal.reason
            continue
        remaining.append((record, proof))
    if not remaining:
        return {}, refusals
    before_log = ADOPTIONS.read_bytes()
    while remaining:
        try:
            links = build_chain(remaining, receipt_bytes)
        except MemberRefused as refusal:
            refusals[refusal.capture_id] = refusal.reason
            remaining = [p for p in remaining if p[0]['capture_id'] != refusal.capture_id]
            links = []
            continue
        break
    if not links:
        return {}, refusals
    # Both existing consumers enforce their own independent pins and limits. A refused
    # successor refuses the WHOLE batch: no partial chain is ever appended.
    check_successor([link['entry_path'] for link in links])
    for link in links:
        for kind in ('ledger', 'config', 'capture'):
            bound(link['proposal'], kind)
    if protected_hashes() != protected:
        raise ValueError('protected_guard_pin_or_history_changed')
    if ADOPTIONS.read_bytes() != before_log:
        raise ValueError('adoption_parent_changed')
    try:
        with ADOPTIONS.open('ab') as output:
            output.write(''.join(canonical(link['entry']) + '\n' for link in links).encode())
            output.flush()
            os.fsync(output.fileno())
        accepted = load_inputs()
        last = links[-1]['entry']
        if accepted['adoption_sequence'] != last['sequence'] or accepted['capture_sha256'] != last['capture_sha256']:
            raise RuntimeError('adoption_readback_mismatch')
    except Exception as exc:
        # An uncertain write is never replayed or followed by another effect.
        raise RuntimeError('adoption_outcome_unknown') from exc
    adopted = {link['record']['capture_id']: {
        'status': 'ADOPTED', 'adoption_sequence': link['entry']['sequence'],
        'accepted_capture_at': link['proof']['fetched_at'], 'adoption_entry': str(link['entry_path']),
        'adopted_capture_path': link['entry']['capture_path'],
        'adopted_capture_sha256': link['entry']['capture_sha256'],
        'batch_size': len(links), 'batch_members': [l['record']['capture_id'] for l in links],
        'observation_ids': link['observation_ids'], 'protected_files_sha256': protected}
        for link in links}
    return adopted, refusals


def adopt_capture(record, proof, protected):
    """One member, adopted through the same transaction as a batch. N=1 is not a second path."""
    adopted, refusals = adopt_batch([(record, proof)], protected)
    if record['capture_id'] in refusals:
        raise ValueError(refusals[record['capture_id']])
    return adopted[record['capture_id']]


def instant(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def plan_refresh(ledger, config, historical_generated_at, now, max_age_days):
    result = []
    for record in ledger['records']:
        observations = [r for r in config['observations'] if r['capture_id'] == record['capture_id']]
        clocks = [{'kind': 'capture', 'at': record['cached_at'] or historical_generated_at}]
        clocks += [{'kind': 'observation', 'id': r['id'], 'at': r['observed_at']} for r in observations]
        for clock in clocks:
            clock['expires_at'] = (instant(clock['at']) + timedelta(days=max_age_days)).isoformat()
        expiry = min(instant(c['expires_at']) for c in clocks)
        accepted = record['disposition'] == 'ACCEPTED'
        result.append({'capture_id': record['capture_id'], 'clocks': clocks,
            'due': accepted and now >= expiry - timedelta(days=LEAD_DAYS),
            'status': 'DUE' if accepted else 'REJECTED_SOURCE_REMAINS_REJECTED'})
    return result


def live_fetch(url):
    from affiliate_direct_evidence_capture import _firecrawl_key
    request = urllib.request.Request('https://api.firecrawl.dev/v2/scrape',
        data=json.dumps({'url': url, 'formats': ['markdown'], 'onlyMainContent': True, 'maxAge': 0}).encode(),
        headers={'Authorization': 'Bearer ' + _firecrawl_key(), 'Content-Type': 'application/json'}, method='POST')
    from firecrawl_client import firecrawl_urlopen
    with firecrawl_urlopen(request, caller='glp_evidence_refresh_cycle', timeout=90) as response:
        return json.load(response)


def check_response(record, response):
    data = response.get('data') or {}
    meta = data.get('metadata') or {}
    if response.get('success') is not True or meta.get('statusCode') != 200:
        raise ValueError('authoritative_fetch_failed')
    if meta.get('sourceURL') != record['requested_url']:
        raise ValueError('authoritative_requested_url_mismatch')
    url = urlsplit(meta.get('url') or meta.get('og:url') or meta.get('sourceURL') or '')
    resolved = urlunsplit((url.scheme, url.netloc, url.path, '', ''))
    if resolved not in (record['requested_url'], record['resolved_url']):
        raise ValueError('authoritative_resolved_url_mismatch')
    if meta.get('cacheState') not in (None, 'miss') or meta.get('cachedAt') is not None:
        raise ValueError('cached_response_refused')
    if not re.fullmatch(r'[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}', str(meta.get('scrapeId', ''))):
        raise ValueError('capture_scrape_id_missing')
    body = data.get('markdown')
    if not isinstance(body, str) or not body.strip():
        raise ValueError('capture_body_missing')
    return body, resolved


def missing_excerpts(excerpt, body):
    # Missing literal evidence requires human review, not a conclusion that the
    # claim is false. Matching excerpts cannot prove whole-page equivalence.
    current = visible(body).casefold()
    return [q for q in excerpt.split('\n\n') if q.strip() and visible(q).casefold() not in current]


def run_cycle(ledger, config, historical_generated_at, now, max_age_days, directory, fetch, adopt):
    # main owns the existing adoption lock. This state does not renew evidence,
    # change pins, or grant acceptance: it suppresses re-fetching unresolved work.
    pending_path = directory.parent / 'review-pending.json'
    pending = json.loads(pending_path.read_text()) if pending_path.exists() else {}
    parent_keys = {r['capture_id']: sha(encoded({'record': r, 'observations': [o for o in config['observations']
                  if o['capture_id'] == r['capture_id']]})) for r in ledger['records']}
    report = {'schema': 'glp-preexpiry-cycle-v1', 'started_at': utc(),
        'standing_acceptance_receipt': str(STANDING_ACCEPTANCE_RECEIPT),
        'lead_days': LEAD_DAYS, 'max_age_days_from_guard': max_age_days,
        'invocation_id': os.environ.get('INVOCATION_ID'),
        'records': plan_refresh(ledger, config, historical_generated_at, now, max_age_days)}
    stopped = False
    ready = []
    for item, record in zip(report['records'], ledger['records']):
        if not item['due']:
            if item['status'] == 'DUE':
                item['status'] = 'NOT_DUE'
            continue
        if stopped:
            item['status'] = 'NOT_ATTEMPTED_AFTER_SOURCE_REFUSAL'
            continue

        previous = pending.get(record['capture_id'], {})
        if (previous.get('parent_sha256') == parent_keys[record['capture_id']]
                and now < instant(previous['next_attempt_at'])):
            item.update(status='REVIEW_PENDING_BACKOFF', decision_required=previous['decision_required'],
                        next_attempt_at=previous['next_attempt_at'],
                        capture_path=previous.get('capture_path'), content_sha256=previous.get('content_sha256'))
            continue

        started = utc()
        proof = {'schema': 'glp-separate-source-capture-v1', 'capture_id': record['capture_id'],
            'parent_record_sha256': sha(encoded(record)), 'parent_record': record,
            'request': {'url': record['requested_url'], 'maxAge': 0}, 'fetch_started_at': started,
            'whole_document_comparison': 'UNKNOWN_NO_HISTORICAL_BODY'}
        try:
            proof['response'] = fetch(record['requested_url'])
            proof['fetched_at'] = utc()
            body, resolved = check_response(record, proof['response'])
            proof.update(source_url=resolved, content_sha256=sha(body.encode()),
                missing_retained_excerpts=missing_excerpts(record['exact_quote'], body))
            proof['observation_checks'] = []
            for observation in config['observations']:
                if observation['capture_id'] != record['capture_id']:
                    continue
                missing = missing_excerpts(observation['source_excerpt'], body)
                proof['observation_checks'].append({'id': observation['id'],
                    'observed_at': observation['observed_at'], 'checked_at': proof['fetched_at'],
                    'parent_observation_sha256': sha(encoded(observation)), 'missing_excerpts': missing,
                    'disposition': 'EXCERPT_MISSING_REVIEW_REQUIRED' if missing else 'EXCERPT_PRESENT_REVIEW_REQUIRED'})
            changed = bool(proof['missing_retained_excerpts'] or any(r['missing_excerpts'] for r in proof['observation_checks']))
            missing = list(dict.fromkeys(proof['missing_retained_excerpts'] + [q
                for check in proof['observation_checks'] for q in check['missing_excerpts']]))
            proof['material_change'] = {'retained_claims_supported': not changed, 'missing_claims': missing}
            unchanged_pending = (previous.get('parent_sha256') == parent_keys[record['capture_id']]
                and previous.get('content_sha256') == proof['content_sha256'])
            item.update(status='ESCALATED' if changed or unchanged_pending else 'READY_FOR_ADOPTION')
            if unchanged_pending:
                item['content_idempotent'] = True
            if changed:
                item['decision_required'] = 'MATERIAL_REVIEW_REQUIRED'
            elif unchanged_pending:
                item['decision_required'] = previous['decision_required']
                item['content_idempotent'] = True
        except Exception as exc:
            # No retry, mirror, cached fallback or substitute after refusal.
            proof['fetched_at'] = utc()
            proof['error'] = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            item.update(status='SOURCE_REFUSED', error=proof['error'])
            stopped = True
        data = encoded(proof)
        capture = directory / record['capture_id'] / (sha(data) + '.json')
        immutable(capture, data)
        item.update(capture_path=str(capture), capture_sha256=sha(data))
        if item['status'] == 'READY_FOR_ADOPTION':
            ready.append((item, record, proof))
    # One transaction for every ready member. Members that expired together cannot be
    # adopted one at a time -- each one's successor still carries the others' stale clocks.
    if ready:
        try:
            adopted, refusals = adopt([(record, proof) for _, record, proof in ready])
            for item, record, _ in ready:
                if record['capture_id'] in adopted:
                    item.update(adopted[record['capture_id']])
                elif record['capture_id'] in refusals:
                    item.update(status='ESCALATED', decision_required=refusals[record['capture_id']])
                else:
                    item.update(status='ESCALATED', decision_required='not_adopted_in_batch')
        except ValueError as exc:
            for item, _, _ in ready:
                item.update(status='ESCALATED', decision_required=str(exc))
        except RuntimeError as exc:
            status = 'EXECUTION_FAILED' if str(exc).startswith('consumer_execution_failed:') else 'ADOPTION_OUTCOME_UNKNOWN'
            for item, _, _ in ready:
                item.update(status=status, error=str(exc))
            stopped = True
    for item in report['records']:
        cid = item['capture_id']
        if item['status'] in ('ESCALATED', 'ADOPTION_OUTCOME_UNKNOWN', 'EXECUTION_FAILED', 'SOURCE_REFUSED'):
            proof_path = Path(item['capture_path']) if item.get('capture_path') else None
            proof = json.loads(proof_path.read_text()) if proof_path else {}
            pending[cid] = {'parent_sha256': parent_keys[cid],
                'decision_required': item.get('decision_required', item.get('error', item['status'])),
                'content_sha256': proof.get('content_sha256'), 'capture_path': item.get('capture_path'),
                'next_attempt_at': (now + timedelta(days=7 if item['status'] == 'ESCALATED' else 1)).isoformat()}
        elif item['status'] != 'REVIEW_PENDING_BACKOFF':
            pending.pop(cid, None)
    pending_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = pending_path.with_suffix('.tmp')
    tmp.write_bytes(encoded(pending))
    os.replace(tmp, pending_path)
    report['finished_at'] = utc()
    report['status'] = ('EXECUTION_FAILED' if any(r['status'] == 'EXECUTION_FAILED' for r in report['records'])
        else 'SOURCE_REFUSED' if stopped else 'COMPLETED_WITH_ESCALATIONS'
        if any(r['status'] in ('ESCALATED', 'REVIEW_PENDING_BACKOFF') for r in report['records']) else 'COMPLETED'
        if any(r['due'] for r in report['records']) else 'NOT_DUE')
    immutable(directory / 'run.json', encoded(report))
    return report


def main():
    # Same fixed, independently anchored reader used by the registered builds.
    lock = ADOPTIONS.with_suffix('.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX)
    inputs = load_inputs()
    ledger, config = json.loads(bound(inputs, 'ledger')), json.loads(bound(inputs, 'config'))
    protected = protected_hashes()
    original = json.loads((CANDIDATE / 'data/wave4-source-capture-ledger.json').read_text())
    guard = (CANDIDATE / 'src/mystery-shop.mjs').read_text()
    days = int(re.search(r'const MAX_AGE_MS = (\d+) \* DAY_MS;', guard).group(1))
    now = datetime.now(timezone.utc)
    directory = OUTPUT / now.strftime('%Y%m%dT%H%M%S.%fZ')
    report = run_cycle(ledger, config, original['generated_at_utc'], now, days, directory,
        live_fetch, lambda pairs: adopt_batch(pairs, protected))
    print(json.dumps({'status': report['status'], 'report': str(directory / 'run.json'), 'invocation_id': report['invocation_id']}), flush=True)
    if report['status'] != 'NOT_DUE':
        from post_discord_message import send, readback
        channel = '1487300132054765629'
        counts = {status: sum(r['status'] == status for r in report['records']) for status in sorted({r['status'] for r in report['records']})}
        escalations = [{ 'capture_id': r['capture_id'], 'reason': r.get('decision_required', r.get('error')) }
            for r in report['records'] if r['status'] in ('ESCALATED', 'SOURCE_REFUSED', 'ADOPTION_OUTCOME_UNKNOWN', 'EXECUTION_FAILED')]
        message = f"GLP-80 scheduled evidence refresh: {report['status']}. {json.dumps(counts)}. Retained claims and independent pins unchanged. Escalations: {json.dumps(escalations)[:1000]}. Report: {directory / 'run.json'}"
        posted = send(channel, message)
        received = readback(channel, posted['id'])
        verified = received.get('channel_id') == channel and received.get('content') == message
        immutable(directory / 'delivery.json', encoded({'channel_id': channel, 'message_id': posted['id'], 'exact_readback': verified}))
        if not verified:
            raise ValueError('discord_exact_readback_failed')
    return 1 if report['status'] in ('SOURCE_REFUSED', 'EXECUTION_FAILED') else 0


if __name__ == '__main__':
    raise SystemExit(main())
