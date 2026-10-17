#!/usr/bin/env python3
"""Read-only operator acceptance capture; never posts, authorizes effects or closes.

Usage: linear_operator_acceptance.py GLP-n /absolute/typed/RESULT.json REPLY_ID
Print the authenticated binding for RESULT.protected_review.discord. Then the
coordinator must bind binding_sha256(binding) in its ordinary protected acceptance
comment as `Independent operator Discord acceptance SHA256: <digest>`.
"""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sys
import os
import sqlite3
from datetime import datetime, timezone, timedelta

from post_discord_message import readback

# Authenticated operator decision: OPERATOR-DECISION-20261015-discord-1560425290050699384.
# These are trust anchors, never caller-supplied identities or report labels.
CHANNEL_ID = '1524254375453786294'
OPERATOR_ID = '392067741500309517'
REQUEST_BOT_ID = '1486880423568211968'
MAX_AGE = timedelta(days=7)
# Host-wide durable authority: never relative to a disposable source checkout, and
# never cleared on reopening, uncertain transport, source upgrade or process exit.
CONSUMED_DB = Path.home() / 'business/ops/linear/operator-acceptance-consumed.sqlite3'


def _unused(db, binding):
    reply = binding['approval']
    _require(db.execute('SELECT 1 FROM consumed WHERE channel_id=? AND reply_id=?',
                        (CHANNEL_ID, reply['id'])).fetchone() is None,
             'operator reply already consumed; inspect attempt and obtain a new message')
    last = db.execute('SELECT MAX(consumed_at) FROM consumed WHERE issue=?',
                      (binding['issue'],)).fetchone()[0]
    _require(last is None or datetime.fromisoformat(reply['timestamp']) > datetime.fromisoformat(last),
             'fresh operator message required after the previous closure attempt')


def _verify_unused(binding):
    # Check-only/capture must not create or mutate the ledger. An unavailable or
    # malformed existing ledger is a refusal, never an invitation to rebuild it.
    if not CONSUMED_DB.exists():
        return
    with closing(sqlite3.connect(CONSUMED_DB.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        _unused(db, binding)


def consume(binding, issue, evidence, result_sha256):
    """Burn once BEFORE any Linear write; uncertain attempts stay burned forever.

    Revalidation plus a serialized durable insert closes concurrent check/use races.
    A new message must also postdate the prior consumed attempt for this issue.
    """
    try:
        verify(binding, issue, evidence)
        CONSUMED_DB.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(CONSUMED_DB, timeout=10)) as db:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE IF NOT EXISTS consumed ('
                       'channel_id TEXT NOT NULL, reply_id TEXT NOT NULL, issue TEXT NOT NULL, '
                       'evidence_sha256 TEXT NOT NULL, result_sha256 TEXT NOT NULL, '
                       'consumed_at TEXT NOT NULL, PRIMARY KEY(channel_id, reply_id))')
            _unused(db, binding)
            db.execute('INSERT INTO consumed VALUES (?,?,?,?,?,?)',
                       (CHANNEL_ID, binding['approval']['id'], issue, binding['evidence_sha256'],
                        result_sha256, datetime.now(timezone.utc).isoformat()))
            db.commit()
        # Preserve newly created directory entries as well as SQLite's FULL commit.
        fd = os.open(CONSUMED_DB.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except (OSError, sqlite3.Error) as exc:
        raise ValueError('operator Discord acceptance consumption unavailable: ' + type(exc).__name__) from None


def binding_sha256(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _require(condition, message):
    if not condition:
        raise ValueError('operator Discord acceptance refused: ' + message)


def _message(message_id, *, operator):
    _require(isinstance(message_id, str) and re.fullmatch(r'[0-9]{16,22}', message_id), 'exact message ID required')
    try:
        row = readback(CHANNEL_ID, message_id)
    except Exception as exc:
        raise ValueError('operator Discord acceptance readback unavailable: ' + type(exc).__name__) from None
    _require(isinstance(row, dict) and row.get('id') == message_id and row.get('channel_id') == CHANNEL_ID,
             'authenticated exact decisions message required')
    author = row.get('author') or {}
    _require(author.get('id') == (OPERATOR_ID if operator else REQUEST_BOT_ID), 'forged/non-operator author')
    _require(author.get('bot', False) is (False if operator else True) and
             not author.get('system') and not row.get('webhook_id') and not row.get('application_id'),
             'bot/system/webhook/application approval forbidden')
    _require('edited_timestamp' in row and row['edited_timestamp'] is None, 'edited or unverified edit state')
    _require(row.get('type') == (19 if operator else 0) and isinstance(row.get('content'), str),
             'ordinary request and native human reply required')
    _require(not row.get('message_snapshots') and not (row.get('flags', 0) & 16384),
             'forwarded/copied messages forbidden')
    created = datetime.fromisoformat(row['timestamp'].replace('Z', '+00:00'))
    _require(created.tzinfo is not None, 'authenticated timestamp required')
    # Preserve approval content and security metadata in the final immutable receipt;
    # mutable profile fields such as avatar/display name are deliberately excluded.
    return {'id': row['id'], 'channel_id': row['channel_id'], 'author_id': author['id'],
            'bot': author.get('bot', False), 'type': row['type'], 'content': row['content'],
            'timestamp': row['timestamp'], 'edited_timestamp': row['edited_timestamp'],
            'message_reference': row.get('message_reference')}


def capture(issue, evidence, reply_id):
    """Authenticate original reply AND request over Discord REST, never a local copy.

    An eight-character response is accepted only against the full SHA256 and path
    in its original unedited, known-bot request. One primary runtime evidence file
    is allowed; additional runtime evidence requires a new explicit acceptance.
    """
    _require(isinstance(issue, str) and re.fullmatch(r'GLP-[1-9][0-9]*', issue), 'exact issue required')
    runtime = [e for e in evidence if e.get('kind') == 'runtime']
    _require(len(runtime) == 1, 'one explicitly approved runtime evidence file required')
    entry = runtime[0]
    sha = entry.get('sha256')
    _require(isinstance(sha, str) and re.fullmatch(r'[a-f0-9]{64}', sha), 'full evidence hash required')
    # The validator separately checks actual reports bytes, path custody and output.
    source = Path(entry['path'])

    # Caller can use a test/workspace root only via the validator's absolute evidence
    # paths; never infer authentication from that root or accept a cached message.
    parts = source.parts
    _require(source.is_absolute() and 'reports' in parts, 'absolute reports evidence required')
    rel = 'reports/' + '/'.join(parts[parts.index('reports') + 1:])
    _require('..' not in source.parts and rel != 'reports/', 'exact evidence path required')
    reply = _message(reply_id, operator=True)
    command = 'ACCEPT ' + issue + ' ' + sha[:8]
    _require(reply['content'] in (command, 'ACCEPT ' + issue + ' ' + sha), 'wrong issue/hash or non-exact approval')
    ref = reply['message_reference'] or {}
    _require(ref.get('channel_id') == CHANNEL_ID and ref.get('message_id') != reply_id,
             'native reply to exact decisions request required')
    request = _message(ref.get('message_id'), operator=False)
    approved = datetime.fromisoformat(reply['timestamp'])
    _require(approved > datetime.fromisoformat(request['timestamp']),
             'approval must follow the request')
    now = datetime.now(timezone.utc)
    _require(timedelta(0) <= now - approved <= MAX_AGE,
             'fresh approval required within 7 days, never in the future')
    _require(source.is_file() and approved.timestamp() > source.stat().st_mtime,
             'fresh approval must be newer than the evidence file mtime')
    # The full evidence digest/path may not be supplied by the accepting agent.
    # Exactly one primary-evidence block avoids cross-issue prefix ambiguity.
    content = request['content']
    _require(re.findall(r'^(?:• )?Evidence: (.+)$', content, re.M) == [rel] and
             re.findall(r'^(?:• )?SHA256: (.+)$', content, re.M) == [sha] and
             re.findall(r'^ACCEPT .+$', content, re.M) == [command],
             'original request must bind exact issue, full hash and evidence path')
    binding = {'schema': 'linear-operator-discord-acceptance-v1', 'issue': issue,
               'evidence_path': str(source), 'evidence_sha256': sha,
               'request': request, 'approval': reply}
    try:
        _verify_unused(binding)
    except (OSError, sqlite3.Error) as exc:
        raise ValueError('operator Discord acceptance consumption unavailable: ' + type(exc).__name__) from None
    return binding


def verify(binding, issue, evidence):
    """Re-fetch exact originals and refuse stale snapshots, edits or identity changes."""
    _require(isinstance(binding, dict), 'authenticated binding required')
    fresh = capture(issue, evidence, binding['approval']['id'])
    _require(fresh == binding, 'retained original request/approval changed or forged')
    return binding_sha256(fresh)


def main(argv):
    if len(argv) != 3:
        raise SystemExit(__doc__)
    issue, result, reply = argv
    data = json.loads(Path(result).read_text())
    _require(data.get('issue') == issue, 'exact result issue required')
    print(json.dumps({'discord': capture(issue, data['evidence'], reply)}, indent=2))


if __name__ == '__main__':
    main(sys.argv[1:])
