#!/usr/bin/env python3
"""Registered authoritative capture producer. Versions stay inert until adoption."""
from __future__ import annotations

import argparse
import base64
import copy
from datetime import datetime, timezone
import difflib
import hashlib
import json
from pathlib import Path
import re
import subprocess
import urllib.request

ROOT = Path('/home/ubuntu/business')
CANDIDATE = ROOT / 'site-system/flagship-v2/candidates/glp34-provider-families-20260912'
VERSIONS = ROOT / 'data/evidence-freshness/versions'
MATERIAL_PROPOSALS = ROOT / 'data/evidence-freshness/material-versions'
CAPTURE_ID = 'fda-unapproved-glp1-warning-20260802'
REQUESTED_URL = 'https://www.fda.gov/drugs/postmarket-drug-safety-information-patients-and-providers/fdas-concerns-unapproved-glp-1-drugs-used-weight-loss'
RESOLVED_URL = 'https://www.fda.gov/drugs/drug-alerts-and-statements/fdas-concerns-unapproved-glp-1-drugs-used-weight-loss'


def utc():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def sha(value):
    return hashlib.sha256(value).hexdigest()


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode()


def immutable(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open('xb') as output:
            output.write(data)
    except FileExistsError:
        if path.read_bytes() != data:
            raise ValueError('immutable_version_collision')


def visible(markdown):
    markdown = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', markdown)
    return ' '.join(re.sub(r'[*_#`]', '', markdown).split())


def create_successor(ledger_bytes, config_bytes, response, started_at, fetched_at,
                     versions, previous_capture=None, capture_id=CAPTURE_ID,
                     material=None, original_capture_bytes=None):
    ledger = json.loads(ledger_bytes)
    config = json.loads(config_bytes)
    qualification = (material or {}).get('schema') == 'glp-named-ww-renewal-v1' and capture_id == 'weightwatchers-clinic-pricing-20260802'
    old = next((row for row in ledger.get('records', []) if row['capture_id'] == capture_id), None)
    requested_url = old['requested_url'] if old else REQUESTED_URL
    # Preserve refused transport evidence too; never replace it with guessed
    # metadata or lose the response needed to diagnose a refused refresh.
    fetch_bytes = encoded({'schema': 'glp-capture-fetch-v1',
        'request': {'url': requested_url, 'maxAge': 0},
        'fetch_started_at': started_at, 'fetched_at': fetched_at, 'response': response})
    immutable(versions / 'fetches' / (sha(fetch_bytes) + '.json'), fetch_bytes)
    data = response.get('data') or {}
    meta = data.get('metadata') or {}
    if response.get('success') is not True or meta.get('statusCode') != 200:
        raise ValueError('authoritative_fetch_failed')
    # REST omits MCP cache annotations; the actual request always uses maxAge=0.
    if meta.get('cacheState') not in (None, 'miss') or meta.get('cachedAt') is not None:
        raise ValueError('cached_response_refused')
    if old is None or old['disposition'] != 'ACCEPTED':
        raise ValueError('new_or_unaccepted_capture_class')
    if meta.get('sourceURL') != requested_url:
        raise ValueError('authoritative_requested_url_mismatch')
    from urllib.parse import urlsplit, urlunsplit
    resolved = urlsplit(meta.get('url') or meta.get('og:url') or meta.get('sourceURL') or '')
    resolved_url = urlunsplit((resolved.scheme, resolved.netloc, resolved.path, '', ''))
    if resolved_url not in (requested_url, old['resolved_url']):
        raise ValueError('authoritative_resolved_url_mismatch')
    if not re.fullmatch(r'[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}', str(meta.get('scrapeId', ''))):
        raise ValueError('capture_scrape_id_missing')
    body = data.get('markdown')
    if not isinstance(body, str) or not body.strip():
        raise ValueError('capture_body_missing')
    if datetime.fromisoformat(fetched_at.replace('Z', '+00:00')) < datetime.fromisoformat(started_at.replace('Z', '+00:00')):
        raise ValueError('fetch_clock_invalid')
    body_text = visible(body)
    excerpts, missing = [], []
    for excerpt in (material['exact_quote'] if material else old['exact_quote']).split('\n\n'):
        quote = visible(excerpt)
        index = body_text.casefold().find(quote.casefold())
        if index < 0:
            missing.append(excerpt)
        else:
            excerpts.append(body_text[index:index + len(quote)])
    old_body = (previous_capture or {}).get('response', {}).get('data', {}).get('markdown')
    change = {
        'retained_claims_supported': not missing, 'missing_claims': missing,
        'retained_excerpt_changed': '\n\n'.join(excerpts) != old['exact_quote'],
        'whole_document_comparison': 'COMPARED' if old_body is not None else 'UNKNOWN_NO_HISTORICAL_BODY',
        'whole_document_changed': body != old_body if old_body is not None else None,
        'diff': list(difflib.unified_diff(old_body.splitlines(), body.splitlines(), fromfile='previous', tofile='current')) if old_body is not None else [],
    }
    observation_checks = []
    for observation in config['observations']:
        if observation['capture_id'] != capture_id:
            continue
        if qualification:
            if observation['id'] != material['observation_id'] or observation['source_excerpt'] != material['historical_quote']:
                raise ValueError('material_observation_parent_binding_invalid')
            # The dated original is retained, not refreshed or represented as current.
            observation_checks.append({'id': observation['id'], 'disposition': 'HISTORICAL_LINEAGE_PRESERVED'})
            continue
        absent = [q for q in observation['source_excerpt'].split('\n\n')
                  if q.strip() and visible(q).casefold() not in body_text.casefold()]
        missing.extend(q for q in absent if q not in missing)
        observation_checks.append({'id': observation['id'], 'checked_at': fetched_at,
                                   'missing_claims': absent})
    change.update(retained_claims_supported=not missing, missing_claims=missing)
    capture = {'schema': 'glp-authoritative-capture-v1', 'capture_id': capture_id,
        'request': {'url': requested_url, 'maxAge': 0}, 'fetch_started_at': started_at,
        'fetched_at': fetched_at, 'source_url': resolved_url, 'content_sha256': sha(body.encode()),
        'response': response, 'material_change': change, 'observation_checks': observation_checks}
    if material is not None:
        capture['original_capture_base64'] = base64.b64encode(original_capture_bytes).decode()
        capture['named_material'] = material
        subprocess.run(['node', str(ROOT / 'scripts/glp_material_acceptance.mjs')],
            input=json.dumps({'material': material, 'proof': capture, 'retained': old}),
            text=True, check=True, capture_output=True)
    capture_bytes = encoded(capture)
    version = (versions / sha(capture_bytes)).resolve()
    immutable(version / 'capture.json', capture_bytes)
    if missing:
        raise ValueError('retained_claim_not_supported:' + str(version / 'capture.json'))
    new = copy.deepcopy(ledger)
    new['generated_at_utc'] = fetched_at
    row = next(row for row in new['records'] if row['capture_id'] == capture_id)
    # Routine successors retain the quote; material proposals require the
    # separately validated exact replacement and remain inert until acceptance.
    if not qualification:
        row.update(cached_at=fetched_at, scrape_id=meta['scrapeId'], resolved_url=resolved_url)
    if material is not None and not qualification:
        row.update(exact_quote=material['exact_quote'], exact_quote_sha256=sha(material['exact_quote'].encode()))
    payload = {k: v for k, v in new.items() if k != 'ledger_payload_sha256'}
    new['ledger_payload_sha256'] = sha(canonical(payload).encode())
    new_bytes = encoded(new)
    config['source_capture_ledger'].update(source_file=str(version / 'ledger.json'),
        generated_at_utc=fetched_at, ledger_payload_sha256=new['ledger_payload_sha256'],
        ledger_file_sha256=sha(new_bytes))
    records = {row['capture_id']: row for row in new['records']}
    capture_keys = ('provider', 'capture_id', 'scrape_id', 'disposition', 'requested_url',
                    'resolved_url', 'status_code', 'exact_quote', 'exact_quote_sha256')
    for observation in config['observations']:
        capture_row = records[observation['capture_id']]
        if observation['capture_id'] == capture_id and not qualification:
            observation.update(observed_at=fetched_at, scrape_id=meta['scrapeId'],
                               resolved_url=resolved_url)
        envelope = {'evidence_schema': config['schema'], 'evidence_scope': config['evidence_scope'],
            'public_only': config['public_only'], 'source_capture_ledger': config['source_capture_ledger'],
            'observation': {key: value for key, value in observation.items() if key != 'evidence_sha256'},
            'accepted_firecrawl_capture': {key: capture_row[key] for key in capture_keys}}
        observation['evidence_sha256'] = sha(canonical(envelope).encode())
    config_out = encoded(config)
    binding = {'schema': 'glp-evidence-adoption-v1', 'version': version.name,
        'ledger_path': str(version / 'ledger.json'), 'ledger_sha256': sha(new_bytes),
        'config_path': str(version / 'config.json'), 'config_sha256': sha(config_out),
        'capture_path': str(version / 'capture.json'), 'capture_sha256': sha(capture_bytes),
        'parent_ledger_sha256': sha(ledger_bytes), 'parent_config_sha256': sha(config_bytes)}
    if material is not None:
        binding['named_material'] = material
    immutable(version / 'ledger.json', new_bytes)
    immutable(version / 'config.json', config_out)
    immutable(version / 'proposal.json', encoded(binding))
    return binding


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--capture-id')
    parser.add_argument('--capture-file', type=Path)
    parser.add_argument('--material-request', type=Path,
                        help='Exact proposed quote and formulation limits JSON; never acceptance')
    parser.add_argument('--proposal-dir', type=Path, help='Isolated proposal output root; normal material proposals use the fixed reader root')
    args = parser.parse_args()
    inputs = json.loads(subprocess.check_output(['node', str(ROOT / 'scripts/glp_evidence_ledger.mjs'), '--inputs', str(CANDIDATE)], text=True))
    if any([args.capture_id, args.capture_file, args.material_request, args.proposal_dir]):
        if not all([args.capture_id, args.capture_file, args.material_request]):
            parser.error('material proposal requires capture-id, capture-file and material-request')
        original = args.capture_file.read_bytes()
        proof = json.loads(original)
        material = json.loads(args.material_request.read_bytes())
        if material['capture_id'] != args.capture_id or proof['capture_id'] != args.capture_id:
            raise ValueError('material_named_capture_mismatch')
        if material['original_capture_sha256'] != sha(original):
            raise ValueError('material_original_capture_hash_invalid')
        if material.get('schema') != 'glp-named-ww-renewal-v1':
            deals = json.loads((CANDIDATE / 'data/deals.json').read_bytes())
            offer = next((o for o in deals['offers'] if o['id'] == material['offer_id']), None)
            if offer is None or sha(canonical(offer).encode()) != material['parent_offer_sha256'] or offer['source']['capture_id'] != args.capture_id:
                raise ValueError('material_offer_parent_binding_invalid')
        parent = {}
        for kind in ('ledger', 'config'):
            parent[kind] = Path(inputs[kind + '_path']).read_bytes()
            if sha(parent[kind]) != inputs[kind + '_sha256']:
                raise ValueError('capture_parent_read_race:' + kind)
        result = create_successor(parent['ledger'], parent['config'], proof['response'],
            proof['fetch_started_at'], proof['fetched_at'], (args.proposal_dir or MATERIAL_PROPOSALS).resolve(),
            capture_id=args.capture_id, material=material, original_capture_bytes=original)
        entry = {**result, 'sequence': inputs['adoption_sequence'] + 1,
                 'previous_adoption_sha256': inputs['previous_adoption_sha256']}
        proposal_path = Path(result['ledger_path']).parent / 'material-acceptance-entry.json'
        immutable(proposal_path, encoded(entry))
        print(json.dumps({'status': 'MATERIAL_PROPOSED_NOT_ACCEPTED', 'acceptance_proposal': str(proposal_path), **entry}))
        return
    from affiliate_direct_evidence_capture import _firecrawl_key
    started = utc()
    request = urllib.request.Request('https://api.firecrawl.dev/v2/scrape',
        data=json.dumps({'url': REQUESTED_URL, 'formats': ['markdown'], 'onlyMainContent': True, 'maxAge': 0}).encode(),
        headers={'Authorization': 'Bearer ' + _firecrawl_key(), 'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(request, timeout=90) as fetched:
        response = json.load(fetched)
    finished = utc()
    def bound_bytes(kind):
        value = Path(inputs[kind + '_path']).read_bytes()
        if sha(value) != inputs[kind + '_sha256']:
            raise ValueError('capture_parent_read_race:' + kind)
        return value
    previous = json.loads(bound_bytes('capture')) if inputs.get('capture_path') else None
    result = create_successor(bound_bytes('ledger'), bound_bytes('config'),
        response, started, finished, VERSIONS, previous)
    proposal_path = Path(result['ledger_path']).parent / 'adoption-entry.json'
    immutable(proposal_path, encoded({**result, 'sequence': inputs['adoption_sequence'] + 1,
        'previous_adoption_sha256': inputs['previous_adoption_sha256']}))
    print(json.dumps({'status': 'SUCCESSOR_PRODUCED_NOT_ADOPTED', 'entry_point': str(Path(__file__).resolve()),
                      'adoption_proposal': str(proposal_path), **result}))


if __name__ == '__main__':
    main()
