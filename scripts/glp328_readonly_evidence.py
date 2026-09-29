"""Read-only evidence projection for the disabled GLP-328 planner.

No sender, scheduler, queue, cursor, schema repair or live-data writer. Legacy
outreach targets are identity references ONLY. Adverse controls are unioned,
never cleared by a second source or an expiry. Incomplete reads fail closed.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from email.utils import getaddresses
from pathlib import Path

import glp328_outreach_policy as policy

BUSINESS = Path('/home/ubuntu/business')
LOCAL = BUSINESS / 'glp-clinic-partner-outreach-machine'


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def read_local_sources(root: Path, now: datetime) -> dict:
    """Strict file reads + one SQLite read transaction; no legacy writer imports.

    Return only control identities, not message bodies, personal contact guesses
    or patient fields. Unknown target references remain errors even where another
    domain is present: that domain cannot establish the missing organization's
    identity. An explicit inactive row never cancels an active row in another
    source. Source positions are 1-based, not potentially sensitive raw rows.
    """
    sources, faults, files = {}, [], {}
    for name in ('targets.jsonl', 'suppression_list.jsonl', 'message_ledger.jsonl',
                 'send_ledger.jsonl', 'reply_ledger.jsonl'):
        rows = []
        try:
            raw = (root / name).read_bytes()
            sources[name] = {'sha256': hashlib.sha256(raw).hexdigest()}
            for number, line in enumerate(raw.decode('utf-8').splitlines(), 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict):
                        raise ValueError('not_object')
                    rows.append((number, row))
                except ValueError:
                    faults.append(f'{name}:{number}:malformed_record')
            sources[name]['rows'] = len(rows)
        except (OSError, UnicodeError) as exc:
            faults.append(f'{name}:{type(exc).__name__}')
        files[name] = rows
    db_targets, db_controls = [], []
    try:
        # mode=ro refuses a missing database; query_only is a second write gate.
        con = sqlite3.connect((root / 'outreach.sqlite').resolve().as_uri() + '?mode=ro', uri=True)
        try:
            con.row_factory = sqlite3.Row
            con.execute('PRAGMA query_only=ON')
            con.execute('BEGIN')
            db_targets = [dict(r) for r in con.execute(
                'SELECT target_id,url FROM targets ORDER BY target_id')]
            db_controls = [dict(r) for r in con.execute(
                'SELECT target_id,email,domain,scope,active FROM suppressions '
                'ORDER BY suppression_id')]
            sources['sqlite'] = {'path': str(root / 'outreach.sqlite'),
                                 'uri_mode': 'ro', 'query_only': con.execute('PRAGMA query_only').fetchone()[0],
                                 'target_rows': len(db_targets), 'suppression_rows': len(db_controls),
                                 'selected_rows_sha256': _digest([db_targets, db_controls])}
        finally:
            con.close()
    except sqlite3.Error as exc:
        faults.append(f'outreach.sqlite:{type(exc).__name__}')
    targets = {}
    for source, rows in [('targets.jsonl', files['targets.jsonl']),
                         ('sqlite.targets', list(enumerate(db_targets, 1)))]:
        for n, row in rows:
            key, host = row.get('target_id'), policy.canonical_host(row.get('url'))
            if not isinstance(key, str) or not key:
                faults.append(f'{source}:{n}:target_id_missing')
            elif key in targets and targets[key] != host:
                faults.append(f'{source}:{n}:target_identity_conflict')
            else:
                targets[key] = host

    def domains(row, source, number):
        found = set()
        key = row.get('target_id')
        if key:
            host = targets.get(key) if isinstance(key, str) else None
            if not host:
                faults.append(f'{source}:{number}:target_identity_unresolved')
            else:
                found.add(host)
        for field in ('email', 'from_addr', 'to_email'):
            if row.get(field):
                addresses = getaddresses([row[field]]) if isinstance(row[field], str) else []
                if not addresses:
                    faults.append(f'{source}:{number}:{field}_invalid')
                for _, address in addresses:
                    email, domain = policy._email(address)
                    if not email:
                        faults.append(f'{source}:{number}:{field}_invalid')
                    else:
                        found.add(domain)
        if row.get('domain'):
            domain = policy.canonical_host(row['domain'])
            if not domain:
                faults.append(f'{source}:{number}:domain_invalid')
            else:
                found.add(domain)
        if not found:
            faults.append(f'{source}:{number}:control_identity_missing')
        return sorted(found)

    suppression, prefix_controls = [], []
    for source, rows in [('suppression_list.jsonl', files['suppression_list.jsonl']),
                         ('sqlite.suppressions', list(enumerate(db_controls, 1)))]:
        for n, row in rows:
            active = row.get('active', True)
            if isinstance(active, str):
                active = active.strip().lower()
            if active is False or (type(active) is int and active == 0) or (
                    isinstance(active, str) and active in ('0', 'false', 'no', 'inactive')):
                continue
            if not (active is None or active is True or (type(active) is int and active == 1)
                    or (isinstance(active, str) and active in ('1', 'true', 'yes', 'active'))):
                faults.append(f'{source}:{n}:active_invalid')
            scope = row.get('scope')
            if scope and scope not in ('email', 'domain', 'organization', 'target'):
                faults.append(f'{source}:{n}:scope_unsupported')
            wildcard = str(row.get('email') or '').strip().lower()
            if (wildcard.endswith('@*') and wildcard[:-2] in policy.FORBIDDEN_PREFIXES
                    and row.get('domain') in ('*', '', None) and not row.get('target_id')):
                # Already enforced by the accepted planner; do not erase this
                # global control or invent a concrete organization for it.
                prefix_controls.append({'source': source, 'position': n, 'prefix': wildcard[:-2]})
                continue
            suppression.extend({'domain': d, 'source': source, 'position': n}
                               for d in domains(row, source, n))
    # Any retained conversation, even an unsent local draft, is conservatively
    # active. Never infer closed/resolved from absence of a send or reply row.
    conversations, sends = [], []
    for name in ('message_ledger.jsonl', 'send_ledger.jsonl', 'reply_ledger.jsonl'):
        for n, row in files[name]:
            ds = domains(row, name, n)
            conversations.extend({'domain': d, 'state': 'active', 'source': name,
                                  'position': n} for d in ds)
            if name == 'send_ledger.jsonl':
                sends.append({'domains': ds, 'provider_message_id': row.get('provider_message_id'),
                              'provider_thread_id': row.get('provider_thread_id'),
                              'source_position': n})
    return {'sources': sources, 'faults': faults, 'policy_prefix_controls': prefix_controls,
            'suppression': {'complete': not faults, 'checked_utc': now.isoformat(),
                            'entries': suppression},
            'conversations': conversations, 'sends': sends,
            'target_hosts': sorted(set(targets.values()) - {''})}


def read_mail_sources(hosts, local_conversations, sends, mailboxes, gmail_factory, now):
    """Read all pages for each official host in both registered mailboxes.

    Coverage is explicitly domain-scoped, all-time and includes spam/trash. Every
    matched message conservatively blocks the organization, including outbound
    or ambiguous messages; headers never prove the thread is closed. No bodies
    are fetched. These snapshots are for initial-plan dedupe, NOT a follow-up
    authorization or a complete reply-stop oracle for network-mediated aliases.
    """
    import re
    expected = {('ops', 'ops@glpclinicscout.com'), ('izaiah', 'izaiah@glpclinicscout.com')}
    actual = {(b.get('key'), b.get('address')) for b in mailboxes}
    entries = list(local_conversations)
    faults, queries, readbacks = [], [], []
    if actual != expected or len(mailboxes) != len(expected):
        faults.append('registered_mailbox_coverage_missing')
    if any(not re.fullmatch(r'[a-z0-9-]+(?:\.[a-z0-9-]+)+', h) for h in hosts):
        faults.append('query_host_invalid')

    def get(messages, message_id):
        if not isinstance(message_id, str) or not message_id:
            raise ValueError('message_id_missing')
        msg = messages.get(userId='me', id=message_id, format='metadata',
                           metadataHeaders=['From', 'To', 'Cc', 'Date', 'Message-ID'],
                           fields='id,threadId,labelIds,internalDate,payload/headers').execute()
        if (msg.get('id') != message_id or not msg.get('threadId')
                or not isinstance(msg.get('labelIds'), list)
                or not isinstance(msg.get('payload', {}).get('headers'), list)
                or not str(msg.get('internalDate', '')).isdigit()):
            raise ValueError('message_readback_incomplete')
        return msg

    if not faults:
        for box in mailboxes:
            try:
                messages = gmail_factory(box).users().messages()
                cache = {}
                for host in hosts:
                    query = f'{{from:{host} to:{host} cc:{host} bcc:{host}}}'
                    receipt = {'mailbox': box['key'], 'host': host, 'query': query,
                               'pages': 0, 'message_ids': [], 'complete': False}
                    queries.append(receipt)
                    tokens, token, ids = set(), None, set()
                    while True:
                        kwargs = dict(userId='me', q=query, includeSpamTrash=True, maxResults=500)
                        if token:
                            kwargs['pageToken'] = token
                        page = messages.list(**kwargs).execute()
                        if not isinstance(page, dict) or ('messages' not in page and
                                page.get('resultSizeEstimate') != 0):
                            raise ValueError('list_response_incomplete')
                        listed = page.get('messages', [])
                        if not isinstance(listed, list):
                            raise ValueError('message_list_invalid')
                        receipt['pages'] += 1
                        for item in listed:
                            mid = item.get('id')
                            msg = cache.get(mid) or get(messages, mid)
                            cache[mid] = msg
                            ids.add(mid)
                        token = page.get('nextPageToken')
                        if not token:
                            break
                        if not isinstance(token, str) or token in tokens or receipt['pages'] >= 100:
                            raise ValueError('pagination_incomplete')
                        tokens.add(token)
                    receipt.update(message_ids=sorted(ids), complete=True)
                    if ids:
                        entries.append({'domain': host, 'state': 'active', 'source': 'gmail_metadata',
                                        'mailbox': box['key'], 'message_count': len(ids)})
                # The partners@ alias belongs to ops. An exact SENT label read
                # is evidence of that message only, not approved copy/recipient,
                # initial-step identity, reply routing or campaign permission.
                if box['key'] == 'ops':
                    for send in sends:
                        if not any(policy._same_org(d, h) for d in send['domains'] for h in hosts):
                            continue
                        mid = send['provider_message_id']
                        msg = cache.get(mid) or get(messages, mid)
                        if ('SENT' not in msg['labelIds'] or
                                msg['threadId'] != send['provider_thread_id']):
                            raise ValueError('sent_readback_mismatch')
                        readbacks.append({'provider_message_id': mid, 'provider_thread_id': msg['threadId'],
                                          'metadata_sha256': _digest(msg), 'sent_label_confirmed': True,
                                          'verified': False, 'send_permission': False,
                                          'reason': 'sent_label_only_not_follow_up_authority'})
            except Exception as exc:
                # API errors may contain identifiers or URLs: retain type only.
                faults.append(f"{box['key']}:{type(exc).__name__}")
    return {'complete': not faults, 'checked_utc': now.isoformat(), 'entries': entries,
            'scope': 'official_host_queries_only_not_network_alias_or_reply_stop_coverage',
            'checked_host_count': len(hosts), 'queries': queries,
            'faults': faults, 'sent_readbacks': readbacks}


def plan_current(values, root, mailboxes, gmail_factory, *, now=None,
                 contacts=(), location_evidence=None):
    """Connect supplied canonical Sheet read + actual source snapshots to policy.

    Explicit contact/location arguments retain the pure planner's contract; this
    adapter does NOT promote legacy targets, discover contacts or infer US status.
    No supplied verified evidence means exclusions, never guessed recipients.
    """
    now = now or datetime.now(timezone.utc)
    sheet = {'headers': values[0] if values else [],
             'rows': [{'row': n, 'values': row} for n, row in enumerate(values[1:], 2)]}
    classified = [policy.classify_row(r['row'], r['values']) for r in sheet['rows']]
    hosts = sorted({r['host'] for r in classified if r['eligible']})
    local = read_local_sources(root, now)
    conversation = {'complete': False, 'checked_utc': now.isoformat(), 'entries': []}
    coverage = {'status': 'NOT_ATTEMPTED_LOCAL_REFUSAL'}
    if local['suppression']['complete']:
        coverage = read_mail_sources(hosts, local['conversations'], local['sends'],
                                     mailboxes, gmail_factory, now)
        conversation = coverage
        recheck = read_local_sources(root, now)
        if recheck['faults'] or recheck['sources'] != local['sources']:
            local['faults'].append('local_sources_changed_during_mail_read')
            local['suppression']['complete'] = False
    result = {'schema': 'glp328.readonly_evidence.v1', 'status': 'REFUSED_INCOMPLETE_SOURCES',
              'mode': 'disabled', 'send_enabled': False, 'template_status': 'UNAPPROVED',
              'generated_utc': now.isoformat(), 'sheet_values_sha256': _digest(values),
              'row_classification': classified, 'candidate_program_rows': sum(r['eligible'] for r in classified),
              'candidate_hosts': len(hosts), 'local': local,
              'contact_evidence': {'supplied_contacts': len(contacts),
                                   'supplied_location_hosts': len(location_evidence or {}),
                                   'legacy_targets_promoted': 0,
                                   'limit': 'No contact or US evidence inferred from targets/Sheet or mailbox headers.'},
              'mailbox_coverage': coverage, 'plan': None}
    try:
        result['plan'] = policy.plan_outreach(sheet, list(contacts), location_evidence or {},
                                             local['suppression'], conversation, now)
    except policy.OutreachPolicyError as exc:
        result['planner_refusal'] = str(exc)
    else:
        result['status'] = 'DISABLED_PLAN_ONLY'
    return result
