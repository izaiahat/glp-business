#!/usr/bin/env python3
"""F04: reconcile the authenticated sole-writer Sheet into detached site source.

No Sheet mutations, tracker requests, activation, or inferred provider identity.
The snapshot is a denial-only relationship overlay. Numeric commercial terms
require an existing successful sole-writer receipt and its authenticated capture.
Existing receipt replay and the site's release carrier own subsequent effects.
"""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit, parse_qs

ROOT = Path('/home/ubuntu/business')
SCHEMA = 'glp-affiliate-registry-sync-v1'
ADVERSE = re.compile(r'cancelled|canceled|terminated|withdraw|relationship_gone|paused|rejected|^hold|^excluded|suppressed', re.I)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def encode(data):
    return (json.dumps(data, indent=2, ensure_ascii=False) + '\n').encode()


def selected_relationships(site):
    """Exact maintained identity crosswalk; current selection wins over history."""
    mappings = {}
    for p in read(site / 'data/affiliate-wire-in-approvals.json')['programs']:
        if p.get('existing_release_alias') and p.get('provider_program_id'):
            mappings[p['existing_release_alias']] = (p['network'].casefold(), str(p['provider_program_id']))
    for p in read(site / 'data/affiliate-activation-selection.json')['programs']:
        mappings[p['program_id']] = (p['network'].casefold(), str(p['network_program_id']))
    for p in read(site / 'data/go-selection-receipt.json')['programs']:
        # Even a held or direct current selection supersedes a historical rail.
        mappings[p['program_id']] = (p['network'].casefold(), str(p.get('network_offer_id') or ''))
    return mappings


def row_identity(cells):
    """Only first-party application namespaces establish the programme ID."""
    network = str(cells[4]).casefold()
    u = urlsplit(str(cells[5]))
    if u.scheme not in ('http', 'https') or u.username is not None or u.password is not None:
        return None
    rules = {
        'awin': ('ui.awin.com', r'/(?:awin/affiliate/\d+/|affiliate/\d+/)?merchant-profile/(\d+)(?:/overview)?/?'),
        'katalys': ('app.katalys.com', r'/(?:pub/[\w-]+/)?(?:explore|programs|offers)/(\d+)/?'),
        'maxbounty': ('affiliates.maxbounty.com', r'/campaigns?/(\d+)/?'),
        'xentraffic': ('xentraffic.everflowclient.io', r'/offers/(\d+)/?'),
    }
    rule = rules.get(network)
    if not rule or u.hostname != rule[0]:
        return None
    match = re.fullmatch(rule[1], u.path)
    if not match:
        return None
    conflicting = [v for k, values in parse_qs(u.query).items()
                   if k.lower() in ('mid', 'offerid', 'offer_id', 'campaign_id', 'campaignid', 'advertiserid', 'program_id') for v in values]
    if any(v != match[1] for v in conflicting):
        return None
    return network, match[1]


def verified_terms(receipt_paths, cells, identity, root=ROOT):
    """Read historical successful F03 ACKs, never synthesize a writer receipt."""
    accepted = []
    for path in receipt_paths:
        r = read(path)
        if (r.get('mode') != 'commission_terms_correction' or r.get('status') != 'PASS'
                or r.get('dry_run') is not False or r.get('readback_mismatches') != []
                or r.get('collateral_changes') != [] or r.get('idempotent_rerun') is not True):
            continue
        plan_path = Path(r['plan_path'])
        if not plan_path.is_file() or digest(plan_path) != r.get('plan_sha256'):
            continue
        plan = read(plan_path)
        for e in r.get('evidence', []):
            if str(e.get('network_program_id')) != identity[1] or e.get('commission_terms') != cells[16]:
                continue
            cap_path = root / e['commission_capture_path']
            if not cap_path.is_file() or digest(cap_path) != e.get('commission_capture_sha256'):
                continue
            cap = read(cap_path)
            if (str(cap.get('network', '')).casefold() != identity[0]
                    or str(cap.get('network_program_id')) != identity[1]
                    or cap.get('fetch_method') != 'network_api'
                    or cap.get('commission_terms') != cells[16]
                    or not cap.get('response')):
                continue
            rows = [p for p in plan.get('rows', []) if p.get('row') == e.get('row')
                    and p.get('cells', {}).get('16') == cells[16]]
            if len(rows) != 1 or plan.get('queue_update_id') not in r.get('queue_update_ids', []):
                continue
            accepted.append({
                'commission_terms': cells[16] or None,
                'cookie_window': cap.get('cookie_window') or None,
                'source_kind': 'authenticated_network_api_via_sole_sheet_writer',
                'source_checked_at': cap.get('fetched_at_utc'),
                'update_id': plan['queue_update_id'],
                'writer_receipt_sha256': digest(path),
                'capture_sha256': digest(cap_path),
            })
    return max(accepted, key=lambda e: e['source_checked_at'] or '') if accepted else None


def project(registry, mappings, snapshot, receipts=(), root=ROOT):
    result = copy.deepcopy(registry)
    indexed = {}
    for row in snapshot['rows']:
        c = row['cells'] + [''] * max(0, 27-len(row['cells']))
        key = row_identity(c)
        if key:
            indexed.setdefault(key, []).append((row['sheet_row'], c))
    changes, unmapped, matched = [], [], set()
    for p in result['programs']:
        identity = mappings.get(p['program_id'])
        if not identity or identity not in indexed:
            continue
        rows = indexed[identity]
        if len(rows) != 1:
            raise ValueError('ambiguous_sheet_identity:' + ':'.join(identity))
        number, cells = rows[0]
        matched.add(identity)
        if 'fella' in (p['program_id'] + p['display_name']).casefold():
            raise ValueError('fella_permanently_excluded')
        previous = p.get('affiliate_relationship_snapshot')
        current = {
            'network': identity[0], 'network_program_id': identity[1],
            'sheet_row': number, 'relationship_status': cells[10] or None,
            'source_checked_at': str(cells[24]) or None,
            'promotion_denied': bool(ADVERSE.search(str(cells[10]))),
            'lifecycle': ('withdrawn' if re.search(r'cancelled|canceled|terminated|withdraw|relationship_gone', str(cells[10]), re.I)
                          else 'paused' if 'paused' in str(cells[10]) else 'reported'),
            'reported_information': True,
        }
        terms = verified_terms(receipts, cells, identity, root)
        if terms:
            current['terms'] = terms
        elif previous and previous.get('terms', {}).get('commission_terms') == cells[16]:
            current['terms'] = previous['terms']
        # Neither an approval nor a higher rate clears a previously recorded denial.
        if previous and previous.get('promotion_denied') and not current['promotion_denied']:
            current['promotion_denied'] = True
            current['reactivation_requires_separate_approval'] = True
        if current != previous:
            p['affiliate_relationship_snapshot'] = current
            if current['lifecycle'] == 'withdrawn':
                p['affiliate_program_state'] = 'withdrawn'
                p['commercial_state'] = 'held'
                p.setdefault('go', {})['entry_state'] = 'held'
                history = {'state':'withdrawn','relationship':':'.join(identity),
                           'source_kind':'authenticated_sole_writer_sheet_readback',
                           'source_checked_at':current['source_checked_at'],
                           'sheet_row':number,'relationship_status':cells[10]}
                p.setdefault('affiliate_program_history', [])
                if history not in p['affiliate_program_history']:p['affiliate_program_history'].append(history)
                for profile in p.get('profiles', {}).values():
                    profile['wired'] = False
                    for surface in ('compare','quiz','calculator','go','review','sitemap'):
                        profile.setdefault('surfaces', {})[surface] = False
                        profile.setdefault('exceptions', {}).setdefault(surface, {
                            'enabled':True,'reason':'Reported selected affiliate relationship withdrawn.',
                            'surface_restriction_basis':'sole_writer_withdrawn_relationship'})
            changes.append({'program_id': p['program_id'], 'before': previous, 'after': current})
    for identity, rows in indexed.items():
        for number, cells in rows:
            if identity not in matched and ADVERSE.search(str(cells[10])):
                unmapped.append({'network': identity[0], 'program_id': identity[1], 'sheet_row': number,
                                 'status': cells[10], 'site_effect': 'no_selected_relationship_not_brand_matched'})
    return result, changes, unmapped


def sync(site, output, apply=False):
    from googleapiclient.discovery import build
    import affiliate_sheet_batch_writer as writer
    from glp_sheet_sync_drain import read_sheet
    import affiliate_terms_fanout as fanout
    service = build('sheets', 'v4', credentials=writer.creds(), cache_discovery=False)
    values = read_sheet(service)  # authenticated read-only; sole writer remains F03
    snapshot = {'headers': values[0], 'rows': [
        {'sheet_row': i, 'cells': writer.pad(row)} for i, row in enumerate(values[1:], 2)]}
    output.mkdir(parents=True, exist_ok=True)
    fanout.atomic(output/'sheet-snapshot.json', encode(snapshot))
    registry = site/'data/program-registry.json'
    raw = registry.read_bytes()
    receipts = list((ROOT/'reports/glp-clinic-scout/affiliate-machine/sheet-sync').glob('drain-*/*commission-terms*-receipt.json'))
    successor, changes, unmapped = project(json.loads(raw), selected_relationships(site), snapshot, receipts)
    if apply and changes:
        if registry.read_bytes() != raw or registry.is_symlink():
            raise ValueError('registry_changed_during_projection')
        fanout.atomic(registry, encode(successor))
    result = {'schema': SCHEMA, 'status': 'PASS', 'apply': apply, 'changed': len(changes),
              'changes': changes, 'unmapped_adverse': unmapped, 'snapshot_sha256': digest(output/'sheet-snapshot.json'),
              'registry_before_sha256': hashlib.sha256(raw).hexdigest(), 'registry_after_sha256': digest(registry),
              'sheet_writes': 0, 'tracking_clicks': 0, 'activations': 0}
    fanout.atomic(output/'registry-sync.json', encode(result))
    print(json.dumps({'status': result['status'], 'changed': len(changes), 'unmapped_adverse': len(unmapped), 'apply': apply}))
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--site-source',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--apply',action='store_true')
    a=p.parse_args()
    sync(a.site_source,a.output,a.apply)

if __name__=='__main__':
    main()
