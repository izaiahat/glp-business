#!/usr/bin/env python3
"""Detect network program paused/ended/cancelled notices and drive site removal.

Reads (read-only) the email-monitor inbox_events and the affiliate-approval
reconciler DBs plus an authenticated read-only Sheet snapshot. For each
program's LATEST lifecycle notice that is adverse and not yet recorded on its
single exact Sheet row, it queues one `network_paused` row for the sole Sheet
writer (sheet_sync_drain). The registered affiliate sync job
(/etc/cron.d/glp-affiliate-terms-sync) then projects the Sheet into the site
registry and ships the canonical release. Every run also reads the live site
and reports the sponsored CTA count for every adverse Sheet program.

No Sheet writes, no tracking-link visits, no sends.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import sys
import urllib.error
import urllib.request

ROOT = Path('/home/ubuntu/business')
INBOX_DB = Path('/home/ubuntu/.hermes/state/email-monitor.sqlite3')
RECONCILER_DB = Path('/home/ubuntu/.hermes/state/affiliate-approval-reconciler.sqlite3')
POINTER = ROOT / 'reports/glp-clinic-scout/seo-machine/current-content-base.json'
REPORTS = ROOT / 'reports/glp-clinic-scout/affiliate-machine/paused-program-monitor'
SITE = 'https://glpclinicscout.com'

# (sender domain, subject pattern, lifecycle). Group 1 is the program name.
NOTICES = [
    ('katalys.com', re.compile(r'^The Program (.+?)\s+Has Been Paused', re.I), 'paused'),
    ('katalys.com', re.compile(r'^Your Contract for (.+?) has ended', re.I), 'ended'),
    ('katalys.com', re.compile(r'^Advertiser .+? has Activated Their Program (.+?)\s*!', re.I), 'active'),
    ('via-everflow.io', re.compile(r'^Offer (.+?) changed status from \w+ to paused', re.I), 'paused'),
    ('via-everflow.io', re.compile(r'^Offer (.+?) changed status from \w+ to active', re.I), 'active'),
    ('cj.com', re.compile(r'^Notice: (.+?) \(\d+\) Program has been deactivated', re.I), 'cancelled'),
]
NETWORK_OF = {'katalys.com': 'katalys', 'via-everflow.io': 'xentraffic', 'cj.com': 'cj affiliate'}
ADVERSE = re.compile(r'paused|cancel|terminat|withdr|ended|relationship_gone|suppressed|removed_from_site|^hold', re.I)
# Only rows that currently promote on the site need removal; other rows keep
# their existing dispositions/holds untouched.
LIVE = re.compile(r'^(live_wired_verified|approved_active_wired_live)$')
SHEET_QUEUE = 'hJiP3eJKBvtZ6bQU'  # n8n sheet_update_queue (sole-writer transport)


def norm(value):
    return re.sub(r'[^a-z0-9]+', '', str(value or '').lower())


def classify(sender_domain, subject):
    for domain, pattern, lifecycle in NOTICES:
        if sender_domain == domain:
            match = pattern.search(str(subject or '').strip())
            if match:
                return lifecycle, match.group(1).strip()
    return None


def lifecycle_events(inbox_db=INBOX_DB, reconciler_db=RECONCILER_DB):
    """Latest lifecycle notice per (network, program name); read-only."""
    inbox = sqlite3.connect(f'file:{inbox_db}?mode=ro', uri=True)
    rows = inbox.execute('select event_id, sender_domain, subject, received_at, provider_message_id '
                         'from inbox_events order by received_at, event_id').fetchall()
    reconciler = {}
    if Path(reconciler_db).exists():
        db = sqlite3.connect(f'file:{reconciler_db}?mode=ro', uri=True)
        reconciler = {e: (s, r) for e, s, r in db.execute('select event_id, status, receipt_path from events')}
    latest = {}
    for event_id, domain, subject, received_at, message_id in rows:
        found = classify(domain, subject)
        if not found:
            continue
        lifecycle, program = found
        key = (NETWORK_OF[domain], norm(program))
        latest[key] = {'event_id': event_id, 'network': NETWORK_OF[domain], 'program': program,
                       'lifecycle': lifecycle, 'event_at_utc': received_at, 'subject': subject,
                       'message_id': message_id, 'reconciler': list(reconciler.get(event_id, ()))}
    return list(latest.values())


def sheet_rows(snapshot, event):
    return [r for r in snapshot['rows']
            if norm(r['cells'][2]) == norm(event['program'])
            and str(r['cells'][4]).strip().casefold() == event['network']]


def decide(snapshot, events, queued_ids=frozenset()):
    """Pure decision: which notices need a sole-writer queue row."""
    queue, outcomes = [], []
    for event in events:
        if event['lifecycle'] == 'active':
            outcomes.append({**event, 'decision': 'latest_notice_active'})
            continue
        rows = sheet_rows(snapshot, event)
        if len(rows) != 1:
            outcomes.append({**event, 'decision': 'no_single_exact_sheet_row', 'matches': len(rows)})
            continue
        row = rows[0]
        status = str(row['cells'][10])
        base = {**event, 'sheet_row': row['sheet_row'], 'sheet_status': status}
        if ADVERSE.search(status):
            outcomes.append({**base, 'decision': 'already_recorded'})
        elif not LIVE.match(status):
            outcomes.append({**base, 'decision': 'row_not_live_no_site_effect'})
        elif event['lifecycle'] != 'paused':
            # Ended/cancelled need their own authenticated writer modes.
            outcomes.append({**base, 'decision': 'operator_writer_required_for_' + event['lifecycle']})
        else:
            update_id = 'paused-monitor-' + event['event_id']
            outcomes.append({**base, 'decision': 'already_queued' if update_id in queued_ids else 'queued',
                             'update_id': update_id})
            if update_id not in queued_ids:
                queue.append(queue_row(event, row, update_id))
    return queue, outcomes


def queue_row(event, row, update_id):
    capture = f'reports/glp-clinic-scout/affiliate-machine/paused-program-monitor/notices/{event["event_id"]}.json'
    return {'update_id': update_id, 'kind': 'network_paused', 'network': str(row['cells'][4]).strip(),
            'program_id': '', 'program_name': str(row['cells'][2]),
            'evidence': json.dumps({'message_id': f'inbox:{event["event_id"]}', 'path': capture,
                                    'event_at_utc': event['event_at_utc']}, sort_keys=True),
            'queued_by': 'glp_paused_program_monitor', 'queued_at_utc': utc(),
            'note': f'Network notice {event["event_id"]} "{event["subject"]}" received {event["event_at_utc"]}.',
            'status': 'queued', 'resolved_at_utc': '', 'sheet_row': row['sheet_row']}


def utc():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def read_snapshot():
    sys.path.insert(0, str(ROOT / 'scripts'))
    from googleapiclient.discovery import build
    import affiliate_sheet_batch_writer as writer
    from glp_sheet_sync_drain import read_sheet
    values = read_sheet(build('sheets', 'v4', credentials=writer.creds(), cache_discovery=False))
    return {'rows': [{'sheet_row': i, 'cells': writer.pad(r)} for i, r in enumerate(values[1:], 2)]}


def live_census(snapshot):
    """Live sponsored-CTA count for every adverse Sheet program the site renders."""
    sys.path.insert(0, str(ROOT / 'scripts'))
    from glp_affiliate_registry_sync import selected_relationships
    release = Path(json.loads(POINTER.read_text())['release_root'])
    mappings = selected_relationships(release)
    programs = {p['program_id']: p for p in json.loads((release / 'data/program-registry.json').read_text())['programs']}
    adverse = {}
    for r in snapshot['rows']:
        if ADVERSE.search(str(r['cells'][10])):
            match = re.search(r'/(?:explore|programs|offers|campaigns?|merchant-profile)/(\d+)', str(r['cells'][5]))
            if match:
                adverse[(str(r['cells'][4]).strip().casefold(), match.group(1))] = r['sheet_row']
    rows = []
    for program_id, identity in sorted(mappings.items()):
        if identity not in adverse or program_id not in programs:
            continue
        slug = programs[program_id].get('review_slug') or program_id
        for path in (f'/providers/{slug}/', f'/reviews/{slug}/'):
            try:
                with urllib.request.urlopen(urllib.request.Request(SITE + path, headers={'User-Agent': 'glp-paused-monitor/1'}), timeout=30) as resp:
                    status, body = resp.status, resp.read().decode('utf-8', 'replace')
            except urllib.error.HTTPError as exc:
                status, body = exc.code, ''
            rows.append({'program_id': program_id, 'network': identity[0], 'offer_id': identity[1],
                         'sheet_row': adverse[identity], 'path': path, 'http': status,
                         'offer_id_refs': body.count(f'offer_id={identity[1]}') if identity[0] == 'katalys' else None,
                         'sponsored_refs': len(re.findall(r'rel="[^"]*sponsored', body))})
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true', help='decide and census only; queue nothing')
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT))
    from ops import n8n_dt
    snapshot = read_snapshot()
    events = lifecycle_events()
    pending, _ = decide(snapshot, events)
    queued = {r['update_id'] for r in pending if n8n_dt.rows(SHEET_QUEUE, n8n_dt.eq('update_id', r['update_id']), limit=3)}
    queue, outcomes = decide(snapshot, events, frozenset(queued))
    run = REPORTS / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    run.mkdir(parents=True, exist_ok=False)
    inserted = []
    for row in queue:
        event = next(e for e in events if 'paused-monitor-' + e['event_id'] == row['update_id'])
        capture = ROOT / json.loads(row['evidence'])['path']
        capture.parent.mkdir(parents=True, exist_ok=True)
        if not capture.exists():
            capture.write_text(json.dumps({'schema': 'glp.paused_program_notice.v1', 'source_db': str(INBOX_DB),
                                           'captured_at_utc': utc(), 'internal_event': event}, indent=2) + '\n')
        if not args.dry_run:
            n8n_dt.insert(SHEET_QUEUE, row)
            inserted.append(row['update_id'])
    census = live_census(snapshot)
    live_ctas = sum((r['offer_id_refs'] or 0) for r in census)
    result = {'schema': 'glp.paused_program_monitor.v1', 'at': utc(), 'dry_run': args.dry_run,
              'status': 'PASS' if live_ctas == 0 else 'PENDING_RELEASE', 'notices': len(events),
              'queued': inserted, 'live_adverse_offer_refs': live_ctas,
              'outcomes': [{k: o.get(k) for k in ('program', 'network', 'lifecycle', 'event_id', 'sheet_row', 'sheet_status', 'decision')} for o in outcomes],
              'live': census, 'sheet_writes': 0, 'tracking_clicks': 0}
    (run / 'RESULT.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({k: result[k] for k in ('status', 'notices', 'queued', 'live_adverse_offer_refs')} | {'receipt': str(run / 'RESULT.json')}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
