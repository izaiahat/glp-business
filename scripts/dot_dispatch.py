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
        and row.get('subject') == SUBJECT and row.get('expires', 0) > time.time() + 600
        and (dot['user'] != DOTS['finch']['user'] or
             row.get('channel_id') == dot['channel'] and row.get('guild_id') == GUILD)]
    if len(matches) != 1:
        raise ValueError('exact_live_subscription_not_ready')
    return {key: matches[0][key] for key in ('id', 'expires', 'policy')}


def source_pins():
    return {name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
            for name in ('dot_dispatch.py', 'native_reply_consumer.py',
                         'receive_one_discord_message.py', 'rook_mcp_events.py')}


def check_readback(state, message):
    if (message.get('id') != state.get('request_id') or message.get('channel_id') != state['channel_id']
        or message.get('content') != state['content'] or message.get('author', {}).get('id') != SENDER
        or {m.get('id') for m in message.get('mentions', [])} != {DOTS[state['dot']]['user']}
        or message.get('webhook_id') is not None or message.get('attachments', []) != []):
        raise ValueError('exact_structured_mention_readback_failed')


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
    atomic_json(ticket / 'CONFIG.json', config)
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
            if not state.get('request_id'):
                raise ValueError('uncertain_send_reconcile_first_no_resend')
            fetched = api('GET', f"/channels/{dot['channel']}/messages/{state['request_id']}")
            check_readback(state, fetched)
            if not (ticket / 'CONFIG.json').exists() or state['state'] in {'posting', 'uncertain'}:
                register(ticket, state, fetched)
            return {'ticket': ticket_id, 'ticket_dir': str(ticket), 'request_id': state['request_id'], 'repeat': True}
        for existing in root.glob('*/TICKET.json'):
            old = json.loads(existing.read_text())
            if old.get('dot') == dot_name and old.get('state') not in {'complete', 'blocked'}:
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
    reply = f"# {state['ticket']}\n\n{TRUST}\n\n{value['text']}\n"
    if (ticket / 'REPLY.md').exists() and (ticket / 'REPLY.md').read_text() != reply:
        raise ValueError('reply_conflict')
    atomic_json(ticket / 'COMPLETION.json', completion)
    atomic_text(ticket / 'REPLY.md', reply)
    state.update(state='complete' if value['outcome'] == 'result' else 'blocked',
        reply_id=value['reply_id'], reply_sha256=digest(reply),
        completed_at=time.time(), latency_seconds=time.time() - state['sent_at'])
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
            if state['state'] in {'complete', 'blocked'}:
                continue
            if state['state'] != 'sent':
                output.append({'ticket': state['ticket'], 'status': 'reconciliation_required'})
                continue
            if state['source_pins'] != source_pins():
                output.append({'ticket': state['ticket'], 'status': 'frozen_source_changed_hold'})
                continue
            dot = DOTS[state['dot']]
            argv = ['timeout', '--foreground', '--signal=KILL', '600', dot['python'],
                str(Path(__file__).with_name('native_reply_consumer.py')), '--config', str(config),
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
    a = p.parse_args()
    os.umask(0o077)
    try:
        if a.consume:
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
