#!/usr/bin/env python3
"""Published-provider feeder into existing acquisition intake. No applications/sends.

Source observations and relationship facts are separate. Existing queue statuses
are retained, and unknown rail coverage never authorizes a direct fallback.
"""
from __future__ import annotations

from collections import Counter
from urllib.parse import urlsplit
import re


def host(value):
    return (urlsplit(str(value or '')).hostname or '').lower().removeprefix('www.')


def matches(provider, row):
    identity = provider['provider_id']
    domain = identity.removeprefix('domain:')
    if str(row.get('program_id') or row.get('provider_id') or '') == identity:
        return True
    # Exact official merchant host only; network/tracking URLs cannot identify a provider.
    if any(host(row.get(k)) == domain for k in ('official_url', 'website', 'official')):
        return True
    slugs = provider.get('slugs') or []
    if row.get('slug') and row['slug'] in slugs:
        return True
    label = str(row.get('program_name') or row.get('name') or '')
    normalize = lambda text: re.sub(r'[^a-z0-9]+', '', str(text).lower())
    aliases = provider.get('aliases') or [provider.get('name', '')]
    # A contradicting first-party URL prevents a name-only identity join.
    official = host(row.get('official_url'))
    return bool(label and not official and any(normalize(label) == normalize(a) for a in aliases))


def reconcile(published, *, sheet, application_queue, sheet_queue, ledger,
              relationships, holds):
    grouped = {}
    for item in published:
        key = item['provider_id']
        if key not in grouped:
            grouped[key] = {**item, 'pages': [], 'slugs': list(item.get('slugs') or [])}
        grouped[key]['pages'] = sorted(set(grouped[key]['pages']) | set(item['pages']))
    providers = []
    for key, item in sorted(grouped.items()):
        found = []
        for label, rows in [('sheet', sheet), ('application_queue', application_queue),
                            ('sheet_update_queue', sheet_queue), ('applications_ledger', ledger)]:
            for row in rows:
                if matches(item, row):
                    found.append({'source': label, 'identity': row.get('queue_id') or row.get('update_id')
                                  or row.get('sheet_row') or row.get('program_id'),
                                  'network': row.get('network'), 'program_id': row.get('program_id'),
                                  'status': row.get('status')})
        related = [r for r in relationships if matches(item, r)]
        held = [r for r in holds if matches(item, r)]
        excluded = bool(re.search(r'\bfella\b', item['name'] + ' ' + key, re.I))
        scope = item.get('scope', 'directory_provider')
        if excluded or held:
            disposition, action = 'held', 'preserve_hold'
        elif item.get('identity_conflict'):
            disposition, action = 'unresolved', 'identity_research'
        elif scope in {'secondary_research_reference_only', 'not_established_as_clinical_provider',
                       'historical_identity_unresolved'}:
            disposition, action = 'outside_new_feed', 'retain_source_role'
        elif found:
            disposition, action = 'matched', 'existing_intake'
        else:
            disposition, action = 'missing', 'research_only_intake'
        providers.append({**item, 'disposition': disposition, 'next_action': action,
                          'intake_matches': found, 'relationships': related, 'holds': held,
                          'relationship_state': 'verified_approved' if any(r.get('verified_approved') is True
                              for r in related) else 'UNKNOWN',
                          'acquisition_order': ['existing_rail_search', 'eligible_authorized_application',
                                               'direct_only_after_complete_no_rail_proof'],
                          'direct_fallback_authorized': False})
    counts = Counter(p['disposition'] for p in providers)
    return {'schema': 'glp.published-provider-intake.v1', 'providers': providers,
            'counts': {'published': len(providers), **dict(counts)},
            'status': 'INCOMPLETE' if counts['missing'] else 'COMPLETE',
            'effects': {'applications_submitted': 0, 'emails_sent': 0, 'sheet_direct_writes': 0,
                        'provider_deploys': 0}}


def capture_published(policy, output, *, fetch=None, timeout=10):
    """Complete sitemap read, including article-only identities; no outgoing GETs."""
    import hashlib
    import json
    import time
    import xml.etree.ElementTree as ET
    from concurrent.futures import ThreadPoolExecutor
    from pathlib import Path
    from urllib.request import Request, build_opener, HTTPRedirectHandler
    from datetime import datetime, timezone
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            raise RuntimeError('published_source_redirect_refused')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    opener = build_opener(NoRedirect())
    def live_get(url):
        with opener.open(Request(url, headers={'User-Agent': 'GLPClinicScout-source-observer'}), timeout=timeout) as response:
            if response.status != 200:
                raise RuntimeError('published_source_not_200')
            body = response.read(4_000_001)
            if len(body) > 4_000_000:
                raise RuntimeError('published_source_too_large')
            return body
    getter = fetch or live_get
    receipts = []
    deadline = time.monotonic() + 90
    def read(url):
        if time.monotonic() >= deadline:
            raise RuntimeError('published_source_observation_budget_exhausted')
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.netloc != 'glpclinicscout.com' or parsed.query or parsed.fragment:
            raise RuntimeError('published_source_url_not_allowlisted')
        if re.search(r'/(go|out|affiliate|click|track)(/|$)', parsed.path, re.I):
            raise RuntimeError('tracking_source_refused')
        body = getter(url)
        sha = hashlib.sha256(body).hexdigest()
        (output / (sha + '.html')).write_bytes(body)
        receipt = {'url': url, 'sha256': sha,
                   'observed_at_utc': datetime.now(timezone.utc).isoformat(), 'http_status': 200}
        return body, receipt
    todo, seen, urls = ['https://glpclinicscout.com/sitemap.xml'], set(), set()
    while todo:
        url = todo.pop(0)
        if url in seen:
            raise RuntimeError('published_sitemap_cycle_or_duplicate')
        seen.add(url)
        if len(seen) > 20:
            raise RuntimeError('published_sitemap_limit')
        body, receipt = read(url)
        receipts.append(receipt)
        xml = ET.fromstring(body)
        locations = [e.text for e in xml.iter() if e.tag.rsplit('}', 1)[-1] == 'loc']
        if xml.tag.endswith('sitemapindex'):
            todo.extend(locations)
        else:
            urls.update(locations)
    selected = {u for u in urls if urlsplit(u).path.split('/')[1] in
                ('providers', 'reviews', 'guides', 'best', 'insights')}
    # Retained article identity pages absent from a sitemap must still be observed.
    selected.update(u for p in policy.values() for u in p['pages'])
    if len(selected) > 1000:
        raise RuntimeError('published_page_inventory_exceeds_bound')
    with ThreadPoolExecutor(max_workers=4) as executor:
        captured = list(executor.map(lambda u: (u, *read(u)), sorted(selected)))
    observed = []
    policy_urls = {u for p in policy.values() for u in p['pages']}
    for key, p in policy.items():
        active = []
        for u, body, receipt in captured:
            if u not in p['pages']:
                continue
            # A guide must retain a source mention, not merely return HTTP 200.
            if '/providers/' in u or '/reviews/' in u or key.removeprefix('domain:').encode() in body.lower():
                active.append(u)
        if active:
            observed.append({**p, 'pages': active})
    for u, body, receipt in captured:
        if u not in policy_urls and re.fullmatch(r'/(providers|reviews)/[^/]+/', urlsplit(u).path):
            slug = urlsplit(u).path.rstrip('/').split('/')[-1]
            observed.append({'provider_id': 'published:' + slug, 'name': slug, 'slugs': [slug],
                             'pages': [u], 'identity_conflict': 'domain_identity_not_established'})
        receipts.append(receipt)
    result = {'sitemap_urls': len(urls), 'pages_read': len(captured),
              'source_observations': receipts, 'tracking_clicks': 0, 'complete': True}
    (output / 'observations.json').write_text(json.dumps(result, indent=2) + '\n')
    return observed, result


def unmatched(result):
    return any(p['disposition'] == 'missing' or
               (p['disposition'] == 'unresolved' and not p['intake_matches'])
               for p in result['providers'])


def queue_fact(row, *, observed_at):
    return {**row, 'status': 'queued', 'queued_at_utc': observed_at,
            'note': 'GLP96 source-bound research intake; not applied or approved.'}


def repairable_queue_row(current, original):
    metadata = ('status', 'queued_at_utc', 'note')
    return (current.get('kind') == 'research_only_provider'
            and all(current.get(k) in (None, '') for k in metadata)
            and all(current.get(k) == v for k, v in original.items()))


def run(*, output=None, apply=False):
    """Natural source read plus exact existing-queue reconciliation; research only."""
    import hashlib
    import json
    import os
    from pathlib import Path
    from datetime import datetime, timezone
    import sys
    root = Path('/home/ubuntu/business')
    sys.path.insert(0, str(root / 'scripts'))
    sys.path.insert(0, str(root))
    from glp_maxbounty_lane import read_sheet_snapshot
    from ops import n8n_dt
    from affiliate_supply_discovery import build_research_only_queue_rows
    folder = Path(output) if output else root / 'reports/glp-clinic-scout/affiliate-machine/published-intake' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(folder, 0o700)
    source_root = Path(__file__).resolve().parents[1]
    policy = json.loads((source_root / 'ops/glp/published-provider-intake-policy.json').read_text())['providers']
    published, observation = capture_published(policy, folder / 'sources')
    snapshot = read_sheet_snapshot()
    sheet = []
    for row in snapshot['rows']:
        cells = row['cells']
        sheet.append({'sheet_row': row['sheet_row'], 'program_name': cells[2],
                      'network': cells[4], 'official_url': cells[6], 'slug': cells[9],
                      'program_id': str(cells[13]), 'status': cells[10]})
    queues = {}
    for label, table in [('application_queue', 'wuDGU2Xg1LvwXuk6'), ('sheet_update_queue', 'hJiP3eJKBvtZ6bQU')]:
        queues[label] = n8n_dt.rows(table, limit=5000)
        if n8n_dt.last_read_truncated:
            raise RuntimeError('published_intake_existing_queue_incomplete:' + label)
        # Evidence-bearing rows retain their exact network/program keys and statuses.
        for row in queues[label]:
            try:
                evidence = json.loads(row.get('evidence') or '{}')
            except (ValueError, TypeError):
                evidence = {}
            if isinstance(evidence, dict) and evidence.get('provider_id'):
                row['provider_id'] = evidence['provider_id']
    index_path = root / 'reports/glp-clinic-scout/affiliate-machine/applications/queued-index.json'
    ledger_doc = json.loads(index_path.read_text()) if index_path.exists() else {}
    ledger = [r for r in ledger_doc.values() if isinstance(r, dict)]
    for field in ('entries', 'applications'):
        if isinstance(ledger_doc.get(field), dict):
            ledger = list(ledger_doc[field].values())
            break
    holds = [r for r in sheet if re.search(r'hold|pause|denied|blocked|rejected|excluded', str(r['status']), re.I)]
    # A Sheet status is a retained claim, not proof of current network approval.
    relationships = [r for r in sheet if re.search(r'approved|pending|applied', str(r['status']), re.I)]
    result = reconcile(published, sheet=sheet, application_queue=queues['application_queue'],
                       sheet_queue=queues['sheet_update_queue'], ledger=ledger,
                       relationships=relationships, holds=holds)
    result.update(observation=observation, inventory_counts={'sheet': len(sheet),
        'application_queue': len(queues['application_queue']), 'sheet_update_queue': len(queues['sheet_update_queue']),
        'local_ledger': len(ledger)}, queue_rows=[], output=str(folder))
    # Only genuine, source-bound care subjects from the existing research-only
    # contract can enter the existing Sheet intake. All other gaps remain explicit.
    capture = 'reports/ops/glp96-research-intake-followthrough-20260927/two-prospects-report-only.json'
    prospects = json.loads((root / capture).read_text())
    eligible = {p['provider_id'] for p in result['providers'] if p['disposition'] == 'missing'}
    for original in build_research_only_queue_rows(prospects, capture_path=capture):
        prior = [r for r in queues['sheet_update_queue'] if r.get('update_id') == original['update_id']]
        legacy = (len(prior) == 1 and repairable_queue_row(prior[0], original)
                  and any(p['provider_id'] == original['program_id'] and p['disposition'] == 'matched'
                          for p in result['providers']))
        if original['program_id'] not in eligible and not legacy:
            continue
        row = queue_fact(original, observed_at=observation['source_observations'][0]['observed_at_utc'])
        if apply:
            from cron_bridge_action import same_sheet_operation
            existing = n8n_dt.rows('hJiP3eJKBvtZ6bQU', n8n_dt.eq('update_id', row['update_id']), limit=3)
            if existing and legacy:
                if len(existing) != 1 or not repairable_queue_row(existing[0], original):
                    raise RuntimeError('published_intake_legacy_preimage_changed')
                patch = {k: row[k] for k in ('status', 'queued_at_utc', 'note')}
                n8n_dt.update('hJiP3eJKBvtZ6bQU', n8n_dt.eq('update_id', row['update_id']), patch)
                result['effects']['queue_metadata_repaired'] = result['effects'].get('queue_metadata_repaired', 0) + 1
            if not existing:
                try:
                    n8n_dt.insert('hJiP3eJKBvtZ6bQU', row)
                except n8n_dt.WriteReconciliationError:
                    pass  # Exact-target readback decides; never blindly repeat a write.
            landed = n8n_dt.rows('hJiP3eJKBvtZ6bQU', n8n_dt.eq('update_id', row['update_id']), limit=3)
            if len(landed) != 1 or not same_sheet_operation(landed[0], row):
                raise RuntimeError('published_intake_exact_queue_readback_failed')
            result['queue_rows'].append(row)
            result.setdefault('confirmed_queue_statuses', {})[row['update_id']] = landed[0]['status']
        else:
            result.setdefault('planned_queue_rows', []).append(row)
    result['effects']['research_queue_rows'] = len(result['queue_rows'])
    if result['queue_rows']:
        for provider in result['providers']:
            if provider['provider_id'] in {r['program_id'] for r in result['queue_rows']}:
                matching = next(r for r in result['queue_rows'] if r['program_id'] == provider['provider_id'])
                provider['intake_matches'].append({'source': 'sheet_update_queue',
                    'identity': matching['update_id'], 'status': result['confirmed_queue_statuses'][matching['update_id']],
                    'network': matching['network'], 'program_id': matching['program_id']})
                provider.update(disposition='matched', next_action='existing_research_queue')
        counts = Counter(p['disposition'] for p in result['providers'])
        result['counts'] = {'published': len(result['providers']), **dict(counts)}
        result['status'] = 'INCOMPLETE' if counts['missing'] else 'COMPLETE'
    result['status'] = 'INCOMPLETE' if unmatched(result) or result.get('planned_queue_rows') else 'COMPLETE'
    result['producer'] = {'registered_command': 'python3 scripts/glp_published_provider_intake.py' + (' --admit-research' if apply else ''),
                          'source': 'scripts/glp_published_provider_intake.py'}
    (folder / 'RESULT.json').write_text(json.dumps(result, indent=2) + '\n')
    os.chmod(folder / 'RESULT.json', 0o600)
    return result


def main(argv=None):
    import argparse
    import json
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output')
    parser.add_argument('--admit-research', action='store_true')
    options = parser.parse_args(argv)
    # Standalone registered manual runs share the existing admission writer lock.
    # The scheduled caller already owns it before invoking run().
    if options.admit_research:
        import fcntl
        from affiliate_auto_apply import LOCK_PATH
        LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOCK_PATH.open('a+') as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = run(output=options.output, apply=True)
    else:
        result = run(output=options.output, apply=False)
    print(json.dumps({k: result[k] for k in ('status', 'counts', 'effects', 'inventory_counts', 'output')}))
    return 0 if result['status'] == 'COMPLETE' else 2


if __name__ == '__main__':
    raise SystemExit(main())
