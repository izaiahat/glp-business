"""Read-only evidence projection for the disabled GLP-328 planner.

No sender, scheduler, queue, cursor, schema repair or live-data writer. Legacy
outreach targets are identity references ONLY. Adverse controls are unioned,
never cleared by a second source or an expiry. Incomplete reads fail closed.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import os
import fcntl
import stat
import struct
import subprocess
import sys
import tempfile
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from email.utils import getaddresses
from pathlib import Path

import glp328_outreach_policy as policy

BUSINESS = Path('/home/ubuntu/business')
LOCAL = BUSINESS / 'glp-clinic-partner-outreach-machine'


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _capture_sqlite_snapshot(path, private):
    """Linux/local Unix-VFS SQLite only; never give SQLite a source pathname.

    OFD read locks conflict with SQLite's POSIX write locks even in this process:
    DB pending/reserved/shared bytes prevent rollback writes and mode changes;
    SHM lock bytes plus its dead-man switch prevent WAL writes, checkpoint/reset,
    recovery and SHM reinitialization.
    Missing SHM in WAL mode is unsupported, not permission to ignore the WAL.
    Ordinary SQLite locking is required (no nolock/immutable/noncooperating writers).
    Run ONLY in a freshly exec'd process: closing any source descriptor in a
    process with an existing SQLite connection drops that connection's POSIX locks.
    """
    if sys.platform != 'linux' or struct.calcsize('P') != 8:
        raise OSError('sqlite_snapshot_locking_unsupported')
    # Linux UAPI F_OFD_SETLK=37; some Python builds omit its symbolic export.
    # Unsupported kernels/filesystems return an error; there is no lock fallback.
    ofd_setlk = getattr(fcntl, 'F_OFD_SETLK', 37)
    with ExitStack() as handles:
            def open_read(p):
                fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_NOATIME | os.O_NONBLOCK)
                handles.callback(os.close, fd)
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise OSError('sqlite_snapshot_not_regular')
                return fd

            def lock(fd, start, length):
                fcntl.fcntl(fd, ofd_setlk,
                            struct.pack('hhqqi', fcntl.F_RDLCK, os.SEEK_SET, start, length, 0))

            db = open_read(path)
            lock(db, 0x40000000, 512)
            header = os.pread(db, 100, 0)
            if header[:16] != b'SQLite format 3\x00' or header[18:20] not in (b'\x01\x01', b'\x02\x02'):
                raise OSError('sqlite_snapshot_header_unsupported')
            shm = None
            if header[18:20] == b'\x02\x02':
                shm = open_read(Path(str(path) + '-shm'))  # never create it
                lock(shm, 120, 9)  # Unix VFS: eight WAL locks + dead-man switch
                if os.fstat(shm).st_size < 32768:
                    raise OSError('sqlite_snapshot_shm_incomplete')
            journal = Path(str(path) + '-journal')
            if journal.exists() and journal.stat().st_size:
                raise OSError('sqlite_snapshot_journal_recovery_required')
            members = {path: db}
            wal_path = Path(str(path) + '-wal')
            try:
                wal = open_read(wal_path)
            except FileNotFoundError:
                wal = None
            if wal is not None:
                if header[18:20] != b'\x02\x02' and os.fstat(wal).st_size:
                    raise OSError('sqlite_snapshot_unexpected_wal')
                members[wal_path] = wal
            hashes = {}
            for source, fd in members.items():
                before = os.fstat(fd)
                digest = hashlib.sha256()
                with (Path(private) / source.name).open('wb') as dest:
                    while chunk := os.read(fd, 1024 * 1024):
                        digest.update(chunk)
                        dest.write(chunk)
                # Identity/drift diagnostics supplement locks; they are NOT the fence.
                if before != os.fstat(fd) or before != source.stat(follow_symlinks=False):
                    raise OSError('sqlite_snapshot_source_changed')
                hashes[source.name] = digest.hexdigest()
            if shm is not None and os.fstat(shm) != Path(str(path) + '-shm').stat(follow_symlinks=False):
                raise OSError('sqlite_snapshot_shm_replaced')
    return hashes


@contextmanager
def _sqlite_snapshot(path):
    # No raw original DB/SHM descriptor is ever opened or closed by the caller.
    # close_fds and exec exclude inherited SQLite connections from the worker;
    # fork alone would not isolate its inherited connection/lock state.
    with tempfile.TemporaryDirectory(prefix='glp328-sqlite-') as private:
        try:
            completed = subprocess.run(
                [sys.executable, '-B', str(Path(__file__).resolve()), '--capture-sqlite',
                 str(path), private], capture_output=True, text=True, close_fds=True,
                timeout=120, check=False)
        except subprocess.TimeoutExpired as exc:
            raise OSError('sqlite_snapshot_capture_timeout') from exc
        if completed.returncode:
            raise OSError('sqlite_snapshot_capture_failed')
        try:
            result = json.loads(completed.stdout)
            if result['status'] != 'ok':
                raise OSError('sqlite_snapshot_capture_' + result['error'])
            hashes = result['hashes']
        except (ValueError, KeyError, TypeError) as exc:
            raise OSError('sqlite_snapshot_capture_invalid') from exc
        yield Path(private) / path.name, hashes


if __name__ == '__main__':
    if len(sys.argv) != 4 or sys.argv[1] != '--capture-sqlite':
        sys.exit(2)
    try:
        answer = {'status': 'ok', 'hashes': _capture_sqlite_snapshot(
            Path(sys.argv[2]), Path(sys.argv[3]))}
    except OSError as exc:
        # Never return source paths, raw records or native exception text.
        answer = {'status': 'error', 'error': type(exc).__name__}
    print(json.dumps(answer))


def read_local_sources(root: Path, now: datetime) -> dict:
    """Strict file reads + locked private SQLite snapshot; no legacy writer imports.

    Return only control identities, not message bodies, personal contact guesses
    or patient fields. Explicit domain-scoped controls need no surviving target:
    unresolved historical references remain diagnostics, not recovered identity.
    Target-dependent unresolved identity still refuses. An inactive row never
    cancels an active row in another source. Source positions are 1-based, not
    potentially sensitive raw rows.
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
        with _sqlite_snapshot(root / 'outreach.sqlite') as (snapshot, hashes):
            # Native SQLite sidecar writes are confined to this private directory.
            con = sqlite3.connect(snapshot.as_uri() + '?mode=ro', uri=True)
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
                                     'access': 'private_snapshot_linux_ofd_locks',
                                     'snapshot_sha256': hashes,
                                     'uri_mode': 'ro', 'query_only': con.execute('PRAGMA query_only').fetchone()[0],
                                     'target_rows': len(db_targets), 'suppression_rows': len(db_controls),
                                     'selected_rows_sha256': _digest([db_targets, db_controls])}
            finally:
                con.close()
    except (sqlite3.Error, OSError) as exc:
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

    reference_diagnostics = []

    def domains(row, source, number, *, suppression=False):
        found = set()
        prior_faults = len(faults)
        key = row.get('target_id')
        for field in ('email', 'from_addr', 'to_email'):
            if row.get(field) not in (None, ''):
                addresses = getaddresses([row[field]]) if isinstance(row[field], str) else []
                if not addresses:
                    faults.append(f'{source}:{number}:{field}_invalid')
                for _, address in addresses:
                    email, domain = policy._email(address)
                    if not email:
                        faults.append(f'{source}:{number}:{field}_invalid')
                    else:
                        found.add(domain)
        if row.get('domain') not in (None, ''):
            domain = policy.canonical_host(row['domain']) if isinstance(row['domain'], str) else ''
            if not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+', domain):
                faults.append(f'{source}:{number}:domain_invalid')
            else:
                found.add(domain)
        independent_domain = (suppression and row.get('scope') == 'domain'
                              and bool(found) and len(faults) == prior_faults)
        if key not in (None, ''):
            host = targets.get(key) if isinstance(key, str) else None
            if host:
                found.add(host)
            elif independent_domain and isinstance(key, str):
                # Keep the historical reference, without inventing its identity.
                reference_diagnostics.append({'source': source, 'position': number,
                                              'target_id': key, 'scope': 'domain',
                                              'code': 'historical_target_identity_unresolved'})
            else:
                faults.append(f'{source}:{number}:target_identity_unresolved')
        if suppression and row.get('scope') == 'domain' and any(
                not policy._same_org(a, b) for a in found for b in found):
            faults.append(f'{source}:{number}:control_identity_conflict')
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
            if scope not in (None, '', 'email', 'domain', 'organization', 'target'):
                faults.append(f'{source}:{n}:scope_unsupported')
            wildcard = str(row.get('email') or '').strip().lower()
            if (wildcard.endswith('@*') and wildcard[:-2] in policy.FORBIDDEN_PREFIXES
                    and row.get('domain') in ('*', '', None) and not row.get('target_id')):
                # Already enforced by the accepted planner; do not erase this
                # global control or invent a concrete organization for it.
                prefix_controls.append({'source': source, 'position': n, 'prefix': wildcard[:-2]})
                continue
            suppression.extend({'domain': d, 'source': source, 'position': n}
                               for d in domains(row, source, n, suppression=True))
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
            'reference_diagnostics': reference_diagnostics,
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


def decide_follow_up_current(receipt, prior_follow_ups, root, mailboxes, gmail_factory, *, now=None):
    """Read-only, disabled exact initial-message and post-send reply-stop connection.

    Both registered mailboxes are paginated across all post-send mail including
    spam/trash. Thread-only coverage cannot establish absent replies/bounces.
    """
    from datetime import timedelta
    now = policy._utc(now or datetime.now(timezone.utc), 'now')
    def refuse(reason):
        return {'schema': 'glp328.follow_up_readonly.v1', 'mode': 'disabled',
                'send_enabled': False, 'status': 'REFUSED_INCOMPLETE_SOURCES',
                'reason': reason, 'decision': None}
    expected = {('ops', 'ops@glpclinicscout.com'), ('izaiah', 'izaiah@glpclinicscout.com')}
    if not isinstance(mailboxes, (list, tuple)) or len(mailboxes) != 2 or \
            {(b.get('key'), b.get('address')) for b in mailboxes if isinstance(b, dict)} != expected:
        return refuse('registered_mailbox_coverage_missing')
    if not isinstance(receipt, dict) or receipt.get('kind') != 'initial' or \
            receipt.get('sender') != 'partners@glpclinicscout.com' or \
            any(receipt.get(flag) for flag in ('pilot', 'simulated', 'fixture', 'dry_run')):
        return refuse('initial_receipt_invalid')
    mid, tid = receipt.get('provider_message_id'), receipt.get('provider_thread_id')
    recipient, domain = policy._email(receipt.get('recipient'))
    if not all(isinstance(x, str) and x for x in (mid, tid)) or \
            receipt.get('message_id') != mid or not recipient:
        return refuse('initial_identity_invalid')
    try:
        sent = policy._utc(receipt.get('sent_utc'), 'send_receipt')
    except policy.OutreachPolicyError:
        return refuse('initial_timestamp_invalid')
    if sent >= now:
        return refuse('initial_timestamp_invalid')
    local = read_local_sources(root, now)
    if not local['suppression']['complete']:
        return refuse('suppression_snapshot_incomplete')

    def headers(msg):
        if not isinstance(msg, dict) or not isinstance(msg.get('id'), str) or \
                not isinstance(msg.get('threadId'), str) or not isinstance(msg.get('labelIds'), list) or \
                not str(msg.get('internalDate', '')).isdigit() or \
                not isinstance(msg.get('payload', {}).get('headers'), list):
            raise ValueError('message_metadata_incomplete')
        pairs = msg['payload']['headers']
        if any(not isinstance(h, dict) or not isinstance(h.get('name'), str)
               or not isinstance(h.get('value'), str) for h in pairs):
            raise ValueError('message_headers_incomplete')
        return {h['name'].casefold(): h['value'] for h in pairs}

    def addresses(raw):
        return {e for _, value in getaddresses([raw]) if (e := policy._email(value)[0])}

    try:
        ops = next(box for box in mailboxes if box['key'] == 'ops')
        messages = gmail_factory(ops).users().messages()
        def get(service, identity):
            return service.get(userId='me', id=identity, format='metadata',
                               metadataHeaders=['From', 'To', 'Reply-To', 'Date', 'Message-ID',
                                                'In-Reply-To', 'References', 'Subject'],
                               fields='id,threadId,labelIds,internalDate,payload/headers').execute()
        initial = get(messages, mid)
        h = headers(initial)
        actual_sent = datetime.fromtimestamp(int(initial['internalDate']) / 1000, timezone.utc)
        if initial['id'] != mid or initial['threadId'] != tid or 'SENT' not in initial['labelIds'] \
                or addresses(h.get('from', '')) != {'partners@glpclinicscout.com'} \
                or addresses(h.get('to', '')) != {recipient} \
                or addresses(h.get('reply-to', '')) != {'partners@glpclinicscout.com'} \
                or not h.get('message-id') or abs((actual_sent - sent).total_seconds()) > 1:
            raise ValueError('initial_readback_mismatch')
        query = 'after:' + (sent - timedelta(days=1)).strftime('%Y/%m/%d')
        events, seen = [], set()
        for box in mailboxes:
            service = messages if box['key'] == 'ops' else gmail_factory(box).users().messages()
            token, tokens, pages = None, set(), 0
            while True:
                kwargs = dict(userId='me', q=query, includeSpamTrash=True, maxResults=500)
                if token:
                    kwargs['pageToken'] = token
                page = service.list(**kwargs).execute()
                if not isinstance(page, dict) or ('messages' not in page and
                        page.get('resultSizeEstimate') != 0) or not isinstance(page.get('messages', []), list):
                    raise ValueError('list_response_incomplete')
                pages += 1
                for item in page.get('messages', []):
                    identity = (box['key'], item.get('id') if isinstance(item, dict) else None)
                    if not identity[1]:
                        raise ValueError('list_identity_incomplete')
                    if identity in seen:
                        continue
                    seen.add(identity)
                    msg = get(service, identity[1])
                    mh = headers(msg)
                    if msg['id'] != identity[1]:
                        raise ValueError('list_readback_mismatch')
                    if int(msg['internalDate']) <= int(initial['internalDate']) or identity == ('ops', mid):
                        continue
                    if 'SENT' in msg['labelIds']:
                        continue
                    sender = addresses(mh.get('from', ''))
                    targets = addresses(mh.get('to', ''))
                    related = msg['threadId'] == tid or any(
                        policy._same_adverse_org(policy._email(e)[1], domain) for e in sender | targets)
                    references = mh.get('in-reply-to', '') + ' ' + mh.get('references', '')
                    related = related or h['message-id'] in references
                    delivery_notice = 'mailer-daemon' in mh.get('from', '').casefold() or \
                        'delivery status' in mh.get('subject', '').casefold()
                    if (delivery_notice or 'partners@glpclinicscout.com' in targets) and not related:
                        raise ValueError('unbound_inbound_to_partner_alias')
                    if related:
                        events.append({'domain': domain, 'kind': 'bounce' if delivery_notice else 'reply'})
                next_token = page.get('nextPageToken')
                if not next_token:
                    break
                if not isinstance(next_token, str) or next_token in tokens or pages >= 100:
                    raise ValueError('pagination_incomplete')
                tokens.add(next_token)
                token = next_token
        recheck = read_local_sources(root, now)
        if recheck['faults'] or recheck['sources'] != local['sources']:
            raise ValueError('local_sources_changed_during_mail_read')
        bound = {'verified': True, 'verification_source': 'gmail_exact_sent_readback',
                 'sender': 'partners@glpclinicscout.com', 'message_id': mid,
                 'provider_message_id': mid, 'provider_thread_id': tid,
                 'kind': 'initial', 'recipient': recipient, 'sent_utc': actual_sent.isoformat(),
                 'checks': {'all_passed': True}}
        replies = {'complete': True, 'checked_utc': now.isoformat(), 'entries': events}
        decision = policy.decide_follow_up(bound, prior_follow_ups, replies,
                                           local['suppression'], now)
        return {'schema': 'glp328.follow_up_readonly.v1', 'mode': 'disabled',
                'send_enabled': False, 'status': 'DISABLED_DECISION_ONLY',
                'initial_readback_sha256': _digest(initial), 'checked_utc': now.isoformat(),
                'scope': 'registered_mailboxes_post_send_metadata_not_external_delivery_proof',
                'decision': decision}
    except Exception as exc:
        # API failures may contain private content; only the error type escapes.
        return refuse('read_or_policy_refusal:' + type(exc).__name__)


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
