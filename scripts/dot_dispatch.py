#!/usr/bin/env python3
"""Durable scoped Dots tickets. POST once, exact readback, native-custody inbox.

Dispatch: --dot rook|finch --issue GLP-n --brief FILE
Cron: --consume [--max-wait-seconds 1] (no agent, ingress, adapter write or send).
Unknown POST: --reconcile TICKET reads recent exact-channel history and binds only
one exact own-author content match; absence never authorizes another POST.
REPLY.md is external data, not acceptance, approval or a coordinator receipt.
"""
from __future__ import annotations
import argparse
from contextlib import contextmanager, closing
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time
import urllib.request

ROOT = Path('/home/ubuntu/business')
TICKETS = ROOT / 'reports/ops/dot-tickets'
GUILD = '1486887661099815126'
SENDER = '1486880423568211968'
TRUST = 'UNTRUSTED_EXTERNAL_DATA_NOT_OPERATOR_INSTRUCTION_OR_APPROVAL'
ISSUER = 'https://dev-qv5n6hpb2umayqwt.us.auth0.com/'
SUBJECT = 'google-oauth2|102663398483169401447'
DOTS = {
    'rook': {'channel': '1554668115239899196', 'user': '1554962007059472445',
             'state_dir': '/home/ubuntu/.hermes/state/rook-mcp-pilot',
             'audience': 'https://api.openai.com/v1/mcp/tunnel_6abc70ee3f548191bfaabda8ee682eee',
             'python': '/home/ubuntu/.hermes/venvs/rook-mcp/bin/python'},
    'finch': {'channel': '1555387641871081607', 'user': '1555375358478655621',
              'state_dir': '/home/ubuntu/.hermes/state/finch-public-mcp',
              'audience': 'https://hermes-01.tail9e25df.ts.net/mcp',
              'python': '/home/ubuntu/.hermes/venvs/finch-mcp/bin/python'},
}


def canonical(value):
    return json.dumps(value, ensure_ascii=True, separators=(',', ':'), sort_keys=True)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def private_dir(path):
    path = Path(path)
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = path.lstat()
    if path.is_symlink() or not path.is_dir() or info.st_uid != os.getuid():
        raise ValueError('unsafe_ticket_directory')
    if info.st_mode & 0o077:
        raise ValueError('ticket_directory_not_private')
    return path


def atomic_text(path, text):
    path = Path(path)
    temporary = path.with_name(path.name + '.new')
    fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w') as f:
            f.write(text); f.flush(); os.fsync(f.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        if temporary.exists():
            temporary.unlink()
        raise


def atomic_json(path, value):
    atomic_text(path, json.dumps(value, indent=2, sort_keys=True) + '\n')


@contextmanager
def locked(root):
    private_dir(root)
    fd = os.open(root / '.dispatch.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('discord_redirect_refused')


def discord_api(method, route, payload=None):
    from post_discord_message import load_token
    if not re.fullmatch(r'/channels/[0-9]+/messages(?:/[0-9]+|\?limit=100)?', route):
        raise ValueError('discord_route_refused')
    request = urllib.request.Request('https://discord.com/api/v10' + route,
        data=None if payload is None else canonical(payload).encode(), method=method,
        headers={'Authorization': 'Bot ' + load_token(), 'Content-Type': 'application/json',
                 'User-Agent': 'HermesDotDispatcher/1.0'})
    with urllib.request.build_opener(NoRedirect).open(request, timeout=20) as response:
        return json.loads(response.read(1000000))


def subscription_ready(dot):
    """Inspect exact binding/TTL read-only; never open the adapter's Store writer."""
    from rook_mcp_events import FinchIdentity, OPERATOR, BOT, ROOK_BOT
    policy = {'issuer': ISSUER, 'audience': dot['audience'], 'subject': SUBJECT}
    if dot['user'] == DOTS['finch']['user']:
        policy['identity'] = FinchIdentity(DOTS['finch']['channel'], '1555387660644782121',
            DOTS['finch']['user'], Path('/home/ubuntu/.hermes/secrets/dot-bots/finch.token'),
            (OPERATOR, BOT, ROOK_BOT)).policy_fields()
    expected = digest(canonical(policy))
    db_path = Path(dot['state_dir']) / 'rook.sqlite3'
    with closing(sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True, timeout=1)) as db:
        subscriptions = [json.loads(row[0]) for row in db.execute('SELECT value FROM subscriptions')]
        pending = [json.loads(row[0]) for row in db.execute('SELECT value FROM requests')]
    if any(row.get('state') in {'sending', 'uncertain'} and row.get('policy') == expected for row in pending):
        raise ValueError('native_uncertainty_requires_reconciliation')
    matches = [row for row in subscriptions if row.get('policy') == expected
        and row.get('subject') == SUBJECT and row.get('expires', 0) > time.time() + 60
        and (dot['user'] != DOTS['finch']['user'] or
             row.get('channel_id') == dot['channel'] and row.get('guild_id') == GUILD)]
    if len(matches) != 1:
        raise ValueError('exact_live_subscription_not_ready')
    return {key: matches[0][key] for key in ('id', 'expires', 'policy')}


def consumer_dir():
    # Live source is not a checkout; freeze the installed consumer, not a newer
    # candidate adapter copy. CI has no live workspace and uses its checkout.
    live = ROOT / 'scripts'
    return live if (live / 'native_reply_consumer.py').exists() else Path(__file__).parent


def source_pins():
    pins = {'dot_dispatch.py': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    pins.update({name: hashlib.sha256((consumer_dir() / name).read_bytes()).hexdigest()
                 for name in ('native_reply_consumer.py', 'receive_one_discord_message.py', 'rook_mcp_events.py')})
    return pins


def check_readback(state, message):
    if (message.get('id') != state.get('request_id') or message.get('channel_id') != state['channel_id']
        or message.get('content') != state['content'] or message.get('author', {}).get('id') != SENDER
        or {m.get('id') for m in message.get('mentions', [])} != {DOTS[state['dot']]['user']}
        or message.get('webhook_id') is not None or message.get('attachments', []) != []):
        raise ValueError('exact_structured_mention_readback_failed')


def fleet_config(ticket, state):
    return dict(schema='DOT_FLEET_TICKET_V1', ticket=state['ticket'], issue=state['issue'],
                dot=state['dot'], request_id=state['request_id'], correlation=state['correlation'],
                native_config=str(ticket / 'NATIVE-CONFIG.json'), operator_approval=False)


def migrate_contract(ticket, state):
    """Preserve original native registration while repairing the fleet envelope."""
    native = ticket / 'NATIVE-CONFIG.json'
    old = ticket / 'CONFIG.json'
    if not old.exists():
        return
    value = json.loads(old.read_text())
    if value == fleet_config(ticket, state):
        return
    rows = value.get('registrations', [])
    if set(value) != {'parent', 'receipt_state_dir', 'registrations'} or len(rows) != 1:
        raise ValueError('native_config_migration_refused')
    if rows[0].get('correlation') != state.get('correlation') or rows[0].get('result_key') != state['ticket']:
        raise ValueError('native_config_migration_binding_refused')
    if native.exists() and json.loads(native.read_text()) != value:
        raise ValueError('native_config_migration_conflict')
    if not native.exists():
        atomic_json(native, value)
    atomic_json(old, fleet_config(ticket, state))


def register(ticket, state, message):
    check_readback(state, message)
    dot = DOTS[state['dot']]
    correlation = 'discord-' + state['request_id']
    private_dir(ticket / 'receipts')
    config = {'parent': state['parent'], 'receipt_state_dir': str(ticket / 'receipts'),
        'registrations': [{'correlation': correlation, 'channel': dot['channel'],
            'state_dir': dot['state_dir'], 'issuer': ISSUER, 'audience': dot['audience'],
            'subject': SUBJECT, 'task_owner': state['task_owner'],
            'action': 'Consume ' + state['issue'] + ' terminal evidence only; no acceptance or approval',
            'result_key': state['ticket']}]}
    atomic_json(ticket / 'NATIVE-CONFIG.json', config)
    state['correlation'] = correlation
    atomic_json(ticket / 'CONFIG.json', fleet_config(ticket, state))
    atomic_json(ticket / 'SEND-READBACK.json', {key: message.get(key) for key in
        ('id', 'channel_id', 'content', 'mentions', 'timestamp')})
    state.update(state='sent', correlation=correlation, exact_readback=True,
                 request_timestamp=message.get('timestamp'))
    atomic_json(ticket / 'TICKET.json', state)
    return state


def dispatch(dot_name, issue, brief, *, root=TICKETS, api=discord_api,
             ready=subscription_ready, parent='fable-plan-2-20261009', owner='coordinator'):
    if dot_name not in DOTS or not re.fullmatch(r'GLP-[1-9][0-9]*', issue):
        raise ValueError('invalid_dot_or_issue')
    if not brief.strip() or 'TRUE-BRIDGE FABLE REVIEW REQUEST' in brief.upper():
        raise ValueError('invalid_brief_or_registered_bridge')
    for name in (parent, owner):
        if not re.fullmatch(r'[A-Za-z0-9_.:/-]{1,128}', name):
            raise ValueError('invalid_owner')
    ticket_id = dot_name + '-' + issue.lower() + '-' + digest(canonical([dot_name, issue, brief]))[:16]
    dot = DOTS[dot_name]
    content = (f"<@{dot['user']}> fleet-ticket:{issue} ticket:{ticket_id}\n{brief.strip()}\n"
        f"Use this native request grant only for ONE terminal reply: RESULT {ticket_id} <evidence> or "
        f"BLOCKED {ticket_id} <exact blocker>. No ACK/progress on this grant. Consumer: {owner}.")
    if len(content.encode('utf-16-le')) // 2 > 2000:
        raise ValueError('brief_exceeds_discord_limit')
    root = Path(root)
    with locked(root):
        ticket = root / ticket_id
        if (ticket / 'TICKET.json').exists():
            state = json.loads((ticket / 'TICKET.json').read_text())
            migrate_contract(ticket, state)
            if not state.get('request_id'):
                raise ValueError('uncertain_send_reconcile_first_no_resend')
            fetched = api('GET', f"/channels/{dot['channel']}/messages/{state['request_id']}")
            check_readback(state, fetched)
            if not (ticket / 'NATIVE-CONFIG.json').exists() or state['state'] in {'posting', 'uncertain'}:
                register(ticket, state, fetched)
            return {'ticket': ticket_id, 'ticket_dir': str(ticket), 'request_id': state['request_id'], 'repeat': True}
        for existing in root.glob('*/TICKET.json'):
            old = json.loads(existing.read_text())
            if old.get('dot') == dot_name and old.get('state') not in {'complete', 'blocked', 'expired'}:
                raise ValueError('one_open_ticket_per_dot_reconcile_existing')
        subscription = ready(dot)
        private_dir(ticket)
        atomic_text(ticket / 'BRIEF.md', brief)
        state = {'schema': 'DOT_TICKET_V1', 'ticket': ticket_id, 'issue': issue,
            'dot': dot_name, 'channel_id': dot['channel'], 'content': content,
            'content_sha256': digest(content), 'state': 'posting', 'sent_at': time.time(),
            'parent': parent, 'task_owner': owner, 'subscription': subscription,
            'source_pins': source_pins(), 'operator_approval': False}
        atomic_json(ticket / 'TICKET.json', state)  # reservation fsynced before the only POST
        try:
            posted = api('POST', f"/channels/{dot['channel']}/messages", {'content': content,
                'allowed_mentions': {'parse': [], 'users': [dot['user']], 'replied_user': False}})
            identity = posted.get('id')
            if not isinstance(identity, str) or not re.fullmatch(r'[1-9][0-9]{0,19}', identity):
                raise ValueError('post_missing_id')
            state['request_id'] = identity
            atomic_json(ticket / 'TICKET.json', state)  # recover even if the subsequent GET fails
            fetched = api('GET', f"/channels/{dot['channel']}/messages/{identity}")
            register(ticket, state, fetched)
        except BaseException:
            state['state'] = 'uncertain'
            atomic_json(ticket / 'TICKET.json', state)
            raise
        return {'ticket': ticket_id, 'ticket_dir': str(ticket), 'request_id': identity,
                'correlation': state['correlation'], 'exact_readback': True}


def reconcile(ticket, api=discord_api):
    ticket = Path(ticket)
    with locked(ticket.parent):
        state = json.loads((ticket / 'TICKET.json').read_text())
        if state['state'] == 'expired':
            raise ValueError('expired_ticket_retained_poll_late_reply_no_reopen')
        if state.get('request_id'):
            message = api('GET', f"/channels/{state['channel_id']}/messages/{state['request_id']}")
        else:
            messages = api('GET', f"/channels/{state['channel_id']}/messages?limit=100")
            matches = [m for m in messages if m.get('content') == state['content']
                       and m.get('author', {}).get('id') == SENDER]
            if len(matches) != 1:
                raise ValueError('uncertain_post_no_unique_readback_never_resend')
            message = matches[0]
            state['request_id'] = message['id']
        return register(ticket, state, message)


def migrate_receiving(ticket, plan, *, reader=None):
    """Explicit reviewed Finch receiving migration, never called by consume.

    Parent supplies exact preimage hashes and reviewed replacement pins. Locks
    dispatch AND receipts; audit exact original bytes before config/state writes.
    Interrupted replay admits only audited pre/post bytes, not a new transition.
    Native custody, other tickets, grants and expiry ages are never written.
    """
    import native_reply_consumer as native
    from dataclasses import replace
    ticket = Path(ticket)
    required = {'schema', 'ticket', 'ticket_preimage_sha256', 'native_config_preimage_sha256',
        'native_row_sha256', 'old_source_pins', 'new_source_pins', 'reply_id', 'content_sha256',
        'terminal_attachment', 'recorded_by', 'reason'}
    if (not ticket.is_absolute() or ticket != ticket.resolve()
            or not isinstance(plan, dict) or set(plan) != required
            or plan['schema'] != 'DOT_RECEIVING_MIGRATION_V1' or plan['ticket'] != ticket.name
            or not isinstance(plan['recorded_by'], str) or not re.fullmatch(native.NAME, plan['recorded_by'])
            or not isinstance(plan['reason'], str) or not 1 <= len(plan['reason']) <= 1000
            or plan['new_source_pins'] != source_pins()):
        raise ValueError('receiving_migration_plan_refused')
    with locked(ticket.parent):
        native.private(ticket, True)
        paths = {name: ticket / name for name in ('TICKET.json', 'NATIVE-CONFIG.json')}
        for path in paths.values(): native.private(path)
        current = {name: path.read_bytes().decode('utf-8') for name, path in paths.items()}
        audit_path = ticket / 'RECEIVING-MIGRATION.json'
        audit = None
        if audit_path.exists():
            native.private(audit_path)
            audit = json.loads(audit_path.read_text())
            if audit.get('plan') != plan:
                raise ValueError('receiving_migration_audit_conflict')
            preimages = audit['preimages']
        else:
            preimages = current
        if (digest(preimages['TICKET.json']) != plan['ticket_preimage_sha256']
                or digest(preimages['NATIVE-CONFIG.json']) != plan['native_config_preimage_sha256']):
            raise ValueError('receiving_migration_preimage_refused')
        state = json.loads(preimages['TICKET.json'])
        config = json.loads(preimages['NATIVE-CONFIG.json'])
        dot = DOTS['finch']
        if (state.get('ticket') != ticket.name or state.get('dot') != 'finch'
                or state.get('state') != 'sent' or state.get('exact_readback') is not True
                or state.get('source_pins') != plan['old_source_pins']
                or state.get('channel_id') != dot['channel']
                or state.get('correlation') != 'discord-' + state.get('request_id', '')
                or config.get('parent') != state.get('parent')
                or config.get('receipt_state_dir') != str(ticket / 'receipts')
                or len(config.get('registrations', [])) != 1
                or any((ticket / name).exists() for name in ('REPLY.md', 'COMPLETION.json', 'EXPIRY.json'))):
            raise ValueError('receiving_migration_ticket_refused')
        native.private(ticket / 'CONFIG.json')
        if json.loads((ticket / 'CONFIG.json').read_text()) != fleet_config(ticket, state):
            raise ValueError('receiving_migration_fleet_binding_refused')
        r = config['registrations'][0]
        expected = dict(correlation=state['correlation'], channel=dot['channel'],
            state_dir=dot['state_dir'], issuer=ISSUER, audience=dot['audience'], subject=SUBJECT,
            task_owner=state['task_owner'], result_key=state['ticket'])
        if (set(r) != set(expected) | {'action'} or any(r.get(k) != v for k, v in expected.items())):
            raise ValueError('receiving_migration_registration_refused')
        # Load the preimage through the same strict loader without creating a
        # temporary config or relaxing its privacy check (current may be postimage).
        directory, entries = native.load_registrations(paths['NATIVE-CONFIG.json'])
        if len(entries) != 1:
            raise ValueError('receiving_migration_registration_refused')
        old = replace(entries[0], reply_id=None, content_sha256=None, terminal_attachment=None)
        new = replace(old, reply_id=plan['reply_id'], content_sha256=plan['content_sha256'],
            terminal_attachment=plan['terminal_attachment'])
        changed = dict(config, registrations=[dict(r, reply_id=new.reply_id,
            content_sha256=new.content_sha256, terminal_attachment=new.terminal_attachment)])
        post_config = json.dumps(changed, indent=2, sort_keys=True) + '\n'
        post_state = dict(state, source_pins=plan['new_source_pins'])
        # Audit is stable and independent of current partial-write progress.
        expected_audit = dict(schema='DOT_RECEIVING_MIGRATION_AUDIT_V1', plan=plan, preimages=preimages,
            native_registration_sha256=old.registration_sha256,
            replacement_registration_sha256=new.registration_sha256,
            post_config=post_config, operator_approval=False)
        if audit is not None and audit != expected_audit:
            raise ValueError('receiving_migration_audit_conflict')
        post_state['receiving_migration_sha256'] = digest(canonical(expected_audit))
        post_ticket = json.dumps(post_state, indent=2, sort_keys=True) + '\n'
        if (current['NATIVE-CONFIG.json'] not in (preimages['NATIVE-CONFIG.json'], post_config)
                or current['TICKET.json'] not in (preimages['TICKET.json'], post_ticket)
                or audit is None and current != preimages):
            raise ValueError('receiving_migration_preimage_refused')
        with native.Receipts(directory) as receipts:
            if receipts.db.execute('SELECT COUNT(*) FROM receipts').fetchone()[0]:
                raise ValueError('receiving_migration_receipt_conflict')
            row = native.native_row(old)
            if (not row or row.get('state') != 'confirmed'
                    or digest(canonical(row)) != plan['native_row_sha256']
                    or row.get('reply_id') != new.reply_id or row.get('reply_sha256') != new.content_sha256
                    or not isinstance(row.get('attachment'), dict)
                    or any(row['attachment'].get(k) != v for k, v in new.terminal_attachment.items())):
                raise ValueError('receiving_migration_native_refused')
            if reader is None:
                from rook_mcp_events import discord_request
                reader = lambda channel, identity: discord_request('GET', f'/channels/{channel}/messages/{identity}', finch=new.scope.finch)
            data = reader(new.scope.channel, new.reply_id)
            body = native.parse_reply_content(data.get('content'), new.scope.reply_kind)
            if (data.get('id') != new.reply_id or data.get('channel_id') != new.scope.channel
                    or data.get('guild_id', GUILD) != GUILD or data.get('type') != 0
                    or data.get('webhook_id') is not None or data.get('author', {}).get('id') != dot['user']
                    or not body or body.get('correlation') != state['correlation']
                    or not native.verified_registered_reply_binding(data, new.scope, dot['user'])):
                raise ValueError('receiving_migration_authenticated_reply_refused')
            if native.native_row(old) != row:
                raise ValueError('receiving_migration_native_changed')
            if audit is None:
                atomic_json(audit_path, expected_audit)
            if native.native_row(old) != row:
                raise ValueError('receiving_migration_native_changed')
            atomic_text(paths['NATIVE-CONFIG.json'], post_config)
            if native.native_row(old) != row:
                raise ValueError('receiving_migration_native_changed')
            atomic_text(paths['TICKET.json'], post_ticket)
        return dict(status='receiving_migrated', ticket=ticket.name, audit=str(audit_path),
            receipt_id=new.receipt_id, registration_sha256=new.registration_sha256, sent=False)


def native_expiry_probe(ticket):
    """Use the supported consumer, preserving staged evidence and native custody."""
    import native_reply_consumer as native
    config = ticket / 'NATIVE-CONFIG.json'
    directory, registrations = native.load_registrations(config)
    def refuse_reply_read(*_):
        raise ValueError('expiry_native_confirmation_or_live_effect')
    with native.Receipts(directory) as receipts:
        # collect validates registration/staged custody. Never issue a remote
        # reply read here: a confirmed effect must be reconciled, not expired.
        value = native.collect(registrations[0], receipts, replay=True, reader=refuse_reply_read)
    row = native.native_row(registrations[0])
    return {'status': 'absent' if row is None and value['status'] == 'waiting_native_confirmation'
            else value['status'], 'native_state': row.get('state') if row else None}


def expire(ticket, *, reason, owner, timeout_seconds=21600, api=discord_api,
           probe=native_expiry_probe, handoff_reply_id=None, handoff_content_sha256=None):
    """Explicit orchestrator-only administrative expiry; never a dot-authored reply.

    Refuse uncertain sends, confirmed/staged replies, live native grants/effects,
    incomplete history and early expiry. Do not mutate the native request DB.
    """
    import math
    ticket = Path(ticket)
    if not isinstance(owner, str) or not 1 <= len(owner.strip()) <= 200:
        raise ValueError('recorded_expiry_owner_required')
    if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 1000:
        raise ValueError('recorded_expiry_reason_required')
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError('positive_expiry_timeout_required')
    if handoff_content_sha256 is not None and (handoff_reply_id is None
            or timeout_seconds != 21600
            or not isinstance(handoff_content_sha256, str)
            or not re.fullmatch(r'[0-9a-f]{64}', handoff_content_sha256)):
        raise ValueError('expiry_external_handoff_hash_required')
    with locked(ticket.parent):
        private_dir(ticket)
        state = json.loads((ticket / 'TICKET.json').read_text())
        if state['state'] == 'expired':
            audit = json.loads((ticket / 'EXPIRY.json').read_text())
            if (digest(canonical(audit)) != state.get('expiry_audit_sha256')
                    or audit.get('external_handoff_content_sha256') != handoff_content_sha256):
                raise ValueError('expiry_audit_handoff_conflict')
            if audit.get('external_handoff',{}).get('id') != handoff_reply_id:
                raise ValueError('expiry_audit_handoff_conflict')
            if (audit['reason'], audit['recorded_by'], audit['timeout_seconds']) != (reason, owner, timeout_seconds):
                raise ValueError('expiry_audit_conflict')
            return state
        if state['state'] != 'sent' or not state.get('exact_readback'):
            raise ValueError('expiry_requires_reconciled_sent_ticket')
        now = time.time()
        if now - state['sent_at'] < timeout_seconds:
            raise ValueError('expiry_timeout_not_elapsed')
        if any((ticket / name).exists() for name in ('REPLY.md', 'COMPLETION.json')):
            raise ValueError('expiry_reply_already_present')
        migrate_contract(ticket, state)
        native = probe(ticket)
        if native.get('status') not in {'absent', 'request_expired'}:
            raise ValueError('expiry_native_confirmation_or_live_effect')
        if handoff_content_sha256 is not None and native.get('status') != 'request_expired':
            raise ValueError('expiry_native_confirmation_or_live_effect')
        message = api('GET', f"/channels/{state['channel_id']}/messages/{state['request_id']}")
        check_readback(state, message)
        history = api('GET', f"/channels/{state['channel_id']}/messages?limit=100")
        if not history or min(int(m['id']) for m in history) > int(state['request_id']):
            raise ValueError('expiry_history_does_not_cover_request')
        replies = [m for m in history if m.get('author', {}).get('id') == DOTS[state['dot']]['user']
            and (m.get('message_reference', {}).get('message_id') == state['request_id']
                 or state['ticket'] in m.get('content', '')
                 or state['correlation'] in m.get('content', ''))]
        handoff = None
        if handoff_reply_id is not None:
            if native['status'] != 'request_expired' or len(replies) != 1 or replies[0].get('id') != handoff_reply_id:
                raise ValueError('expiry_external_handoff_binding_refused')
            handoff = api('GET',f"/channels/{state['channel_id']}/messages/{handoff_reply_id}")
            text = handoff.get('content','')
            if not isinstance(text, str) or len(text) > 8000:
                raise ValueError('expiry_external_handoff_binding_refused')
            legacy = (text.startswith('<@'+SENDER+'> DIRECT FALLBACK RESULT '+state['ticket'])
                and 'not a native-correlated receipt' in text)
            exact_terminal = (handoff_content_sha256 is not None
                and state['dot'] == 'rook' and handoff.get('type') == 19
                and re.match(r'^<@'+SENDER+r'> RESULT '+re.escape(state['ticket'])+r'(?:\s+|:\s*)\S', text)
                and 'Delivery is authorized direct fallback, NOT native-correlated:' in text
                and handoff.get('message_reference', {}).get('message_id') == state['request_id']
                and handoff.get('message_reference', {}).get('channel_id') == state['channel_id']
                and handoff.get('message_reference', {}).get('guild_id') == GUILD)
            if (handoff != replies[0] or handoff.get('id') != handoff_reply_id
                    or handoff.get('author', {}).get('id') != DOTS[state['dot']]['user']
                    or handoff.get('channel_id') != state['channel_id']
                    or handoff.get('guild_id', GUILD) != GUILD
                    or handoff.get('webhook_id') is not None or handoff.get('attachments') != []
                    or not (legacy or exact_terminal)
                    or handoff_content_sha256 is not None and digest(text) != handoff_content_sha256):
                raise ValueError('expiry_external_handoff_binding_refused')
        elif replies:
            raise ValueError('expiry_reply_present_requires_reconciliation')
        final_native = probe(ticket)
        if final_native['status'] not in {'absent', 'request_expired'} or (handoff is not None and final_native['status'] != 'request_expired'):
            raise ValueError('expiry_native_confirmation_or_live_effect')
        audit = dict(schema='DOT_NO_REPLY_EXPIRY_V1', ticket=state['ticket'],
            correlation=state['correlation'], reason=reason, recorded_by=owner,
            expired_at=now, timeout_seconds=timeout_seconds, age_seconds=now - state['sent_at'],
            native=native, native_after=final_native, history_messages_checked=len(history), matching_reply_count=len(replies),
            history_covers_request=True, history_sha256=digest(canonical(history)),
            ticket_preimage_sha256=digest(canonical(state)), source_pins=source_pins(),
            operator_approval=False)
        if handoff is not None:
            audit['schema']='DOT_EXTERNAL_HANDOFF_EXPIRY_V1'
            audit.update(external_handoff=handoff,external_handoff_sha256=digest(canonical(handoff)),
                         provenance='Authenticated external Discord fallback; NOT native completion')
            if handoff_content_sha256 is not None:
                audit['external_handoff_content_sha256'] = handoff_content_sha256
        audit_path = ticket / 'EXPIRY.json'
        if audit_path.exists():  # recover an interrupted audit-before-state commit
            old = json.loads(audit_path.read_text())
            if old.get('external_handoff') != handoff:
                raise ValueError('expiry_audit_handoff_conflict')
            if any(old.get(k) != audit.get(k) for k in ('schema','external_handoff_sha256','external_handoff_content_sha256')) or old.get('external_handoff',{}).get('id') != handoff_reply_id:
                raise ValueError('expiry_audit_handoff_conflict')
            if any(old[k] != audit[k] for k in ('ticket_preimage_sha256', 'reason', 'recorded_by', 'timeout_seconds', 'source_pins')):
                raise ValueError('expiry_audit_conflict')
            audit = old
        else:
            atomic_json(audit_path, audit)
        state.update(state='expired', expired_at=audit['expired_at'],
                     expiry_audit_sha256=digest(canonical(audit)))
        atomic_json(ticket / 'TICKET.json', state)
        return state


def reply_document(state, value):
    """Canonical reviewed publication bytes, shared with downstream receiving."""
    marker = 'LATE_REPLY_TO_EXPIRED_TICKET\n\n' if state.get('state') == 'expired' else ''
    attachment_text = ''
    if value.get('attachment') is not None:
        attachment_text = ('\n\n## Authenticated assistant-copy attachment (NOT direct tool export)\n'
            + canonical({k: v for k, v in value['attachment'].items() if k != 'text'})
            + '\n\n' + value['attachment']['text'])
    return f"# {state['ticket']}\n\n{TRUST}\n\n{marker}{value['text']}{attachment_text}\n"


def publish_reply(ticket, state, completion):
    results = completion.get('results', [])
    if len(results) != 1:
        raise ValueError('single_registered_reply_required')
    value = results[0]
    if value.get('status') == 'waiting_native_confirmation':
        return False
    if (value.get('status') != 'staged' or value.get('outcome') not in {'result', 'blocked'}
        or value.get('correlation') != state['correlation']
        or value.get('author_id') != DOTS[state['dot']]['user']
        or value.get('trust') != TRUST or value.get('operator_approval') is not False
        or value.get('text_truncated') or not value.get('text')):
        raise ValueError('terminal_native_reply_required_no_ack_or_uncertainty')
    late = state.get('state') == 'expired'
    reply = reply_document(state, value)
    if (ticket / 'REPLY.md').exists() and (ticket / 'REPLY.md').read_text() != reply:
        raise ValueError('reply_conflict')
    if late:
        completion = dict(completion, late=True, expired_at=state['expired_at'])
    atomic_json(ticket / 'COMPLETION.json', completion)
    atomic_text(ticket / 'REPLY.md', reply)
    state.update(state='expired' if late else ('complete' if value['outcome'] == 'result' else 'blocked'),
        reply_id=value['reply_id'], reply_sha256=digest(reply),
        completed_at=time.time(), latency_seconds=time.time() - state['sent_at'])
    if late:
        state.update(late=True, reply_outcome=value['outcome'])
    atomic_json(ticket / 'TICKET.json', state)
    return True


def consume(root=TICKETS, max_wait_seconds=1):
    if not 1 <= max_wait_seconds <= 600:
        raise ValueError('wait_must_be_1_to_600')
    output = []
    with locked(Path(root)):
        for config in sorted(Path(root).glob('*/CONFIG.json')):
            ticket = config.parent
            state = json.loads((ticket / 'TICKET.json').read_text())
            migrate_contract(ticket, state)
            if state['state'] in {'complete', 'blocked'} or state['state'] == 'expired' and state.get('reply_id'):
                output.append({'ticket': state['ticket'], 'status': 'already_' + state['state']})
                continue
            if state['state'] not in {'sent', 'expired'}:
                output.append({'ticket': state['ticket'], 'status': 'reconciliation_required'})
                continue
            pins = state['source_pins']
            if state['state'] == 'expired':
                audit = json.loads((ticket / 'EXPIRY.json').read_text())
                if digest(canonical(audit)) != state.get('expiry_audit_sha256'):
                    raise ValueError('expiry_audit_integrity_failed')
                # Explicit expiry binds the reviewed poller for late replies only.
                # Never re-pin a live ticket or change native registration/custody.
                pins = audit['source_pins']
            if pins != source_pins():
                output.append({'ticket': state['ticket'], 'status': 'frozen_source_changed_hold'})
                continue
            dot = DOTS[state['dot']]
            argv = ['timeout', '--foreground', '--signal=KILL', '600', dot['python'],
                str(consumer_dir() / 'native_reply_consumer.py'), '--config', str(ticket / 'NATIVE-CONFIG.json'),
                '--max-wait-seconds', str(max_wait_seconds), '--replay']
            # GNU timeout owns the hard deadline. subprocess.run waits/reaps it;
            # its actual completion stdout is the sole publication input.
            proc = subprocess.run(argv, capture_output=True, text=True)
            atomic_json(ticket / 'LAST-CONSUME.json', {'at': time.time(), 'argv': argv,
                'returncode': proc.returncode, 'stdout': proc.stdout, 'stderr': proc.stderr[:2000]})
            if proc.returncode != 0:
                output.append({'ticket': state['ticket'], 'status': 'consumer_failed', 'returncode': proc.returncode})
                continue
            completion = json.loads(proc.stdout)
            try:
                written = publish_reply(ticket, state, completion)
                output.append({'ticket': state['ticket'], 'status': 'reply_written' if written else 'pending'})
            except ValueError as exc:
                # An expired/non-result request stays reconciliation-only: never
                # auto-send a followup or erase an existing effect.
                atomic_json(ticket / 'CONSUMER-BLOCKED.json', {'reason': str(exc), 'completion': completion})
                output.append({'ticket': state['ticket'], 'status': 'reconciliation_required'})
    return {'schema': 'DOT_CONSUME_V1', 'results': output}


def recover_selection(scope, *, root=TICKETS, reason):
    """Audit a selection with no dispatch journal; never send or replay work.

    dispatch journals the full scope-bearing brief before POST. Hold both the
    refill and dispatch locks and refuse any same-scope or post-selection
    same-dot/issue journal, even if terminal. Preserve the claim and audit.
    """
    root = Path(root)
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,160}', scope) or not reason.strip():
        raise ValueError('recovery_scope_and_reason_required')
    directory = root.parent / 'dot-refill'
    ledger = directory / 'CLAIMS.json'
    with locked(directory), locked(root):
        claims = json.loads(ledger.read_text())
        claim = claims.get(scope)
        if not claim or claim.get('status') != 'selecting' or claim.get('ticket') or claim.get('request_id'):
            raise ValueError('recovery_requires_unbound_selection')
        if claim.get('dot') not in DOTS or not isinstance(claim.get('selected_at'), (int, float)):
            raise ValueError('recovery_selection_binding_invalid')
        snapshots = []
        for path in sorted(root.glob('*/TICKET.json')):
            row = json.loads(path.read_text())
            snapshots.append({'ticket':row.get('ticket'), 'sha256':digest(canonical(row))})
            if scope in row.get('content', '') or (row.get('dot') == claim['dot'] and row.get('issue') == claim.get('issue') and row.get('sent_at', 0) >= claim['selected_at']):
                raise ValueError('recovery_dispatch_journal_present_requires_reconciliation')
        audit = dict(schema='DOT_SELECTION_ABORT_V1',scope=scope,reason=reason,
                     selection_preimage_sha256=digest(canonical(claim)),tickets=snapshots,
                     source_pins=source_pins(),recorded_at=time.time(),no_post_journal=True,
                     operator_approval=False)
        audit_path = directory / ('RECOVERY-'+scope+'.json')
        if audit_path.exists():
            old=json.loads(audit_path.read_text())
            if old['selection_preimage_sha256'] != audit['selection_preimage_sha256'] or old['reason'] != reason:
                raise ValueError('recovery_audit_conflict')
            audit=old
        else:
            atomic_json(audit_path,audit)
        claim.update(status='aborted_pre_dispatch',audit_sha256=digest(canonical(audit)))
        atomic_json(ledger,claims)
        return dict(scope=scope,status=claim['status'],audit=str(audit_path),redispatched=False)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--dot', choices=DOTS)
    p.add_argument('--issue')
    p.add_argument('--brief', type=Path)
    p.add_argument('--root', type=Path, default=TICKETS)
    p.add_argument('--parent', default='fable-plan-2-20261009')
    p.add_argument('--owner', default='coordinator')
    p.add_argument('--consume', action='store_true')
    p.add_argument('--max-wait-seconds', type=float, default=1)
    p.add_argument('--reconcile', type=Path)
    p.add_argument('--expire', type=Path, help='Orchestrator audited no-reply expiry; never auto-expires')
    p.add_argument('--recover-selection', help='Audited abort of unbound refill selection; never redispatch')
    p.add_argument('--reason', help='Required recorded administrative expiry/recovery reason')
    p.add_argument('--external-handoff-id', help='Exact authenticated direct-fallback reply; expiry only, not native completion')
    p.add_argument('--external-handoff-sha256', help='Parent-reviewed exact full-wire hash required for external RESULT alternative')
    p.add_argument('--migrate-receiving', type=Path, help='Explicit parent-reviewed exact pending Finch ticket')
    p.add_argument('--receiving-migration-plan', type=Path, help='Private reviewed DOT_RECEIVING_MIGRATION_V1 plan')
    p.add_argument('--expiry-timeout-seconds', type=float, default=21600,
                   help='Minimum no-reply age before explicit expiry (default: 6 hours)')
    a = p.parse_args()
    os.umask(0o077)
    try:
        if sum(bool(v) for v in (a.consume, a.reconcile, a.expire, a.recover_selection, a.migrate_receiving)) > 1:
            raise ValueError('choose_one_dispatcher_action')
        if a.migrate_receiving:
            import native_reply_consumer as native
            if not a.receiving_migration_plan:
                raise ValueError('receiving_migration_plan_required')
            native.private(a.receiving_migration_plan)
            result = migrate_receiving(a.migrate_receiving, json.loads(a.receiving_migration_plan.read_text()))
        elif a.recover_selection:
            if not a.reason:
                raise ValueError('recovery_reason_required')
            result = recover_selection(a.recover_selection,root=a.root,reason=a.reason)
        elif a.expire:
            result = expire(a.expire, reason=a.reason, owner=a.owner,
                            timeout_seconds=a.expiry_timeout_seconds,handoff_reply_id=a.external_handoff_id,
                            handoff_content_sha256=a.external_handoff_sha256)
        elif a.consume:
            result = consume(a.root, a.max_wait_seconds)
        elif a.reconcile:
            result = reconcile(a.reconcile)
        else:
            if not all((a.dot, a.issue, a.brief)):
                p.error('--dot, --issue and --brief required for dispatch')
            result = dispatch(a.dot, a.issue, a.brief.read_text(), root=a.root, parent=a.parent, owner=a.owner)
        print(canonical(result), flush=True)
        return 0
    except Exception as exc:
        # HTTP exceptions may contain credentials/body in their representation.
        print(canonical({'status': 'refused', 'error_type': type(exc).__name__,
            'reason': str(exc) if isinstance(exc, ValueError) else 'details_suppressed_read_ticket_before_retry'}), flush=True)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
