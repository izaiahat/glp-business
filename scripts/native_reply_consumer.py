#!/usr/bin/env python3
"""Consume only explicitly registered native replies; no ingress/sends/attachments.

Run from frozen reviewed source via parent terminal(background=True, notify=True)
(or confirmed process handoff), under GNU timeout --foreground --signal=KILL
600s. That parent-owned process deadline includes DNS, GET, response read and
receipt staging; --max-wait-seconds alone is only a cooperative poll budget.
Exit 137 means killed, NOT successful notification or absence of a receipt.
After reaping the process, the parent must run the SAME frozen command with
--replay under the SAME timeout: SQLite recovers any interrupted transaction,
committed staged receipts replay without another GET, and unstaged reads retry.
Never record received until the actual replay/completion stdout is read; only
then may the named owner record applied/rejected. No automatic retry or send.
Completion stdout is escaped external DATA, not operator approval. One bounded
native-state wait per launch; the parent rearms.
Native requests/cursors remain read-only. Separate consumer receipt DB extends
private custody without acquiring or bypassing the adapter's writer lock.
Same Unix owner is the trust boundary, not cryptographic owner impersonation
protection. A staged record is NOT proof a notification reached its parent.
"""
from __future__ import annotations
import argparse
from contextlib import closing
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import time
from receive_one_discord_message import Scope, GUILD, CHANNEL, ROOK_BOT, TEXT_LIMIT, parse_reply_content, verified_registered_reply_binding

FINCH_CHANNEL = '1555387641871081607'
SHARED_CHANNEL = '1555387660644782121'
FINCH_BOT = '1555375358478655621'
NAME = r'[A-Za-z0-9_.:/-]{1,128}'
SHA = r'[0-9a-f]{64}'
TRUST = 'UNTRUSTED_EXTERNAL_DATA_NOT_OPERATOR_INSTRUCTION_OR_APPROVAL'
# Supported Linux parent launch prefix; duration then Python argv follow it.
# SIGKILL needs no interpreter cooperation, including during blocking DNS.
PARENT_DEADLINE_PREFIX = ('timeout', '--foreground', '--signal=KILL')


def canonical(value):
    return json.dumps(value, ensure_ascii=True, separators=(',', ':'), sort_keys=True)


def private(path, directory=False):
    info = Path(path).lstat()
    if (info.st_uid != os.getuid() or info.st_mode & 0o077
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))):
        raise ValueError('unsafe_private_custody')


@dataclass(frozen=True)
class Registration:
    scope: Scope
    parent: str
    task_owner: str
    action: str
    reply_id: str | None = None
    content_sha256: str | None = None

    def __post_init__(self):
        s = self.scope
        if (not s.mcp_state_dir or s.research or s.origin_kind != 'author'
                or s.guild != GUILD or s.channel not in {CHANNEL, FINCH_CHANNEL, SHARED_CHANNEL}
                or s.bot != (FINCH_BOT if s.finch else ROOK_BOT)
                or s.channel != CHANNEL and not s.finch
                or s.after_message != s.correlation.removeprefix('discord-')
                or any(not re.fullmatch(NAME, value) for value in (self.parent, self.task_owner))
                or not isinstance(self.action, str) or not 1 <= len(self.action) <= 500
                or (self.reply_id is None) != (self.content_sha256 is None)
                or self.reply_id is not None and (not re.fullmatch(r'[1-9][0-9]{0,19}', self.reply_id)
                    or not re.fullmatch(SHA, self.content_sha256))):
            raise ValueError('invalid_registration')

    @property
    def receipt_id(self):
        s = self.scope
        return hashlib.sha256(canonical([s.mcp_state_dir, s.correlation, s.channel]).encode()).hexdigest()

    @property
    def registration_sha256(self):
        s = self.scope
        return hashlib.sha256(canonical([s.correlation, s.guild, s.channel, s.bot, s.reply_kind,
            s.mcp_state_dir, s.mcp_issuer, s.mcp_audience, s.mcp_subject,
            s.finch.policy_fields() if s.finch else None, self.parent, self.task_owner,
            self.action, self.reply_id, self.content_sha256]).encode()).hexdigest()


def native_row(registration):
    s = registration.scope
    directory = Path(s.mcp_state_dir)
    private(directory, True)
    path = directory / 'rook.sqlite3'
    private(path)
    with closing(sqlite3.connect(path.absolute().as_uri() + '?mode=ro', uri=True, timeout=1)) as db:
        row = db.execute('SELECT value FROM requests WHERE id=?', (s.correlation,)).fetchone()
    return json.loads(row[0]) if row else None


class Receipts:
    """One private consumer writer, not the native adapter DB or Hermes STATE."""
    def __init__(self, directory):
        directory = Path(directory)
        private(directory, True)
        self.lock = os.open(directory / 'reply-consumers.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            private(directory / 'reply-consumers.lock')
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            path = directory / 'reply-consumers.sqlite3'
            fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            os.close(fd)
            private(path)
            self.db = sqlite3.connect(path)
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.execute('CREATE TABLE IF NOT EXISTS receipts (id TEXT PRIMARY KEY, value TEXT NOT NULL)')
            self.db.commit()
        except Exception:
            os.close(self.lock)
            raise

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.db.close()
        os.close(self.lock)

    def get(self, identity):
        row = self.db.execute('SELECT value FROM receipts WHERE id=?', (identity,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, value):
        self.db.execute('INSERT OR REPLACE INTO receipts VALUES (?,?)', (value['receipt_id'], canonical(value)))
        self.db.commit()

    def record(self, identity, owner, status, evidence):
        row = self.get(identity)
        if (not row or status not in {'received', 'applied', 'rejected'}
                or not isinstance(evidence, str) or not 1 <= len(evidence) <= 1000
                or owner != (row['parent'] if status == 'received' else row['task_owner'])):
            raise ValueError('owner_or_disposition_refused')
        if row['status'] == status and row.get('evidence') == evidence and row.get('recorded_by') == owner:
            return row  # exact repeat, not another effect
        if row['status'] != ('staged' if status == 'received' else 'received'):
            raise ValueError('invalid_disposition_transition')
        row.update(status=status, evidence=evidence, recorded_by=owner, recorded_at=time.time())
        if status == 'received':
            row['coordinator_receipt'] = {'owner': owner, 'evidence': evidence, 'at': row['recorded_at']}
        self.put(row)
        return row


def collect(registration, receipts, *, reader=None, replay=False):
    s = registration.scope
    old = receipts.get(registration.receipt_id)
    if old:
        if old['registration_sha256'] != registration.registration_sha256:
            raise ValueError('registration_conflict')
        if replay and old['status'] == 'staged':
            return dict(old, replay=True)
        return {'status': 'awaiting_coordinator_ack' if old['status'] == 'staged' else old['status'],
            'receipt_id': old['receipt_id'], 'correlation': s.correlation}
    row = native_row(registration)
    if not row or row.get('state') != 'confirmed':
        return {'status': 'waiting_native_confirmation', 'correlation': s.correlation}
    identity = row.get('reply_id')
    if (not isinstance(identity, str) or not re.fullmatch(r'[1-9][0-9]{0,19}', identity)
            or int(identity) <= int(s.after_message) or row.get('attachment') is not None
            or registration.reply_id is not None and (identity != registration.reply_id
                or row.get('reply_sha256') != registration.content_sha256)):
        raise ValueError('native_reply_refused')
    if reader is None:
        from rook_mcp_events import discord_request
        reader = lambda channel, message: discord_request('GET', f'/channels/{channel}/messages/{message}', finch=s.finch)
    data = reader(s.channel, identity)  # exact message, never channel history or attachments
    body = parse_reply_content(data.get('content'), s.reply_kind)
    if (data.get('id') != identity or data.get('channel_id') != s.channel
            or data.get('guild_id', s.guild) != s.guild or data.get('type') != 0
            or data.get('webhook_id') is not None or data.get('author', {}).get('id') != s.bot
            or data.get('attachments') != [] or not isinstance(body, dict)
            or body.get('correlation') != s.correlation or body.get('kind') != s.reply_kind
            or not isinstance(body.get('text'), str) or not body['text'].strip()
            or not verified_registered_reply_binding(data, s, s.bot)):
        raise ValueError('live_reply_or_custody_refused')
    value = {'status': 'staged', 'receipt_id': registration.receipt_id,
        'registration_sha256': registration.registration_sha256,
        'parent': registration.parent, 'task_owner': registration.task_owner, 'action': registration.action,
        'correlation': s.correlation, 'channel_id': s.channel, 'reply_id': identity, 'author_id': s.bot,
        'content_sha256': hashlib.sha256(data['content'].encode()).hexdigest(), 'trust': TRUST,
        'text': body['text'][:TEXT_LIMIT], 'text_truncated': len(body['text']) > TEXT_LIMIT,
        'staged_at': time.time(), 'operator_approval': False}
    receipts.put(value)  # durable before stdout; crash => explicit replay, never silently applied
    return value


def load_registrations(path):
    private(path)
    with open(path) as source:
        raw = source.read(64001)
    if len(raw) > 64000:
        raise ValueError('registration_file_too_large')
    config = json.loads(raw)
    if set(config) != {'parent', 'receipt_state_dir', 'registrations'} or not 1 <= len(config['registrations']) <= 3:
        raise ValueError('invalid_registration_file')
    from rook_mcp_events import FinchIdentity, OPERATOR, BOT
    finch = FinchIdentity(FINCH_CHANNEL, SHARED_CHANNEL, FINCH_BOT,
        Path('/home/ubuntu/.hermes/secrets/dot-bots/finch.token'), (OPERATOR, BOT, ROOK_BOT))
    entries = []
    required = {'correlation', 'channel', 'state_dir', 'issuer', 'audience', 'subject', 'task_owner', 'action'}
    for r in config['registrations']:
        if not required <= set(r) or set(r) - required - {'reply_id', 'content_sha256'}:
            raise ValueError('invalid_registration_fields')
        if r['channel'] not in {CHANNEL, FINCH_CHANNEL, SHARED_CHANNEL}:
            raise ValueError('unauthorized_channel')
        is_finch = r['channel'] in finch.channels
        scope = Scope(r['correlation'], 'finch.reply' if is_finch else 'rook.reply',
            r['correlation'].removeprefix('discord-'), 'author', FINCH_BOT if is_finch else ROOK_BOT,
            1, r['state_dir'], r['issuer'], r['audience'], r['subject'], None,
            finch if is_finch else None, r['channel'] if is_finch else None)
        entries.append(Registration(scope, config['parent'], r['task_owner'], r['action'], r.get('reply_id'), r.get('content_sha256')))
    if len({r.receipt_id for r in entries}) != len(entries):
        raise ValueError('duplicate_registration')
    return config['receipt_state_dir'], entries


def run(config, max_wait_seconds, replay=False):
    if not 1 <= max_wait_seconds <= 600:
        raise ValueError('wait_must_be_1_to_600')
    directory, registrations = load_registrations(config)
    deadline = time.monotonic() + max_wait_seconds
    with Receipts(directory) as receipts:
        while True:
            values = [collect(r, receipts, replay=replay) for r in registrations]
            if any(v['status'] == 'staged' for v in values) or all(v['status'] != 'waiting_native_confirmation' for v in values):
                return {'status': 'native_consumer_batch', 'results': values}
            if time.monotonic() >= deadline:
                return {'status': 'deadline', 'results': values}
            time.sleep(min(1, max(0, deadline - time.monotonic())))


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', required=True, type=Path)
    p.add_argument('--max-wait-seconds', type=float, default=600)
    p.add_argument('--replay', action='store_true', help='Explicitly recover staged but unacknowledged notification')
    p.add_argument('--receipt-id')
    p.add_argument('--record', choices=['received', 'applied', 'rejected'])
    p.add_argument('--owner')
    p.add_argument('--evidence')
    a = p.parse_args(argv)
    os.umask(0o077)
    try:
        if any((a.receipt_id, a.record, a.owner, a.evidence)):
            if not all((a.receipt_id, a.record, a.owner, a.evidence)) or a.replay:
                raise ValueError('complete_owner_disposition_required')
            directory, registrations = load_registrations(a.config)
            if a.receipt_id not in {r.receipt_id for r in registrations}:
                raise ValueError('unregistered_receipt')
            with Receipts(directory) as receipts:
                result = receipts.record(a.receipt_id, a.owner, a.record, a.evidence)
        else:
            result = run(a.config, a.max_wait_seconds, a.replay)
        print(canonical(result), flush=True)
        return 0
    except Exception:
        print(canonical({'status': 'refused', 'reason': 'consumer_failed_details_suppressed'}), flush=True)
        return 2


if __name__ == '__main__':
    sys.exit(main())
