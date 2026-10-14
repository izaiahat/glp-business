"""Record-only receiver for the retained GLP-125 S1 external SENT effect.

Not a sender, approval importer, or inbound poll. Only exact GETs are allowed;
no caller-authored authority JSON can replace retained runtime custody.
"""
from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from email.parser import BytesParser
from email.policy import default
from email.utils import getaddresses
from pathlib import Path

EVENT = 'evt_ca7c3fca8e7b1ac4dd9de4bc07e0'
MAILBOX = 'glp_ops_gmail'
THREAD = '1a0e883504e54e66'
SENT = '1a13037e99fe9c58'
SENDER = 'partners@glpclinicscout.com'
TO = 'andreas@nuimagemedical.com'
SOURCE_SHA = '47a08fba9e53dfac6585ebd5c0974d90d70ccdf1506e597cea1b2f951fecafb5'
SENT_RAW_SHA = '6815797c4a897c5fd0170864c2252465c235515b740e58328ec14c9908272a9c'
BODY_SHA = 'c7004388dcab9180ce146d9825bd9d67f992d06ffdfd5fa5c889d8c0eb292f39'
ARTIFACT_SHA = '7df84f73964c4ff851131d2ce2bfd1ca571995235cf740f9a73deeb7b7b5bf32'
APPROVED_SHA = '011a8380afea3ec2f9fba4048581ee30e321c6830551f124511a9eca0ade9afd'
BUSINESS = Path('/home/ubuntu/business')
LANES = BUSINESS / 'reports/ops/fable-plan-2-20261009/lanes'
SESSION_DB = Path('/home/ubuntu/.hermes/state.db')
S1_SESSION = '20261010_040906_c274e5'
OPERATOR_SESSION = '20260922_191215_fffbbb'
AUTHORITY_BINDINGS = (
    (5098910, OPERATOR_SESSION, 1791582366.9324443, 'c965c6f306997c2f25543153dd5b565b9d1ad27ec825ea133d92c4b7360c7ba6'),
    (5137413, S1_SESSION, 1791605347.5131953, 'ae63a754fe632cb5bab5d0359b513dc1fec7b6efbe08fbe03c696b9e032bb73a'),
)
RETAINED = {
    'L8/GLP125-address-edit-receipt.json': '66302edd33d3e729ea5ed3ded8053a396207fd4d582166eb3868ee5332b27e4c',
    'L8/ADDRESS-CORRECTION.json': 'bab555ae3ab7105942e887d756004cd75468a4f18d56674418b4f6e23d3174d4',
    'S1/GLP125-word-diff.json': 'd20c3cf890b1979d52631ee5f56bc28e6d761531828bd538060bcad27c2d3b91',
    'S1/GLP125-SEND.json': '53570598355d91d745900c0fc2554fe9eb3d75f6a3736ab8c02a47fda31a1d5c',
    'S1/GLP125-QUEUE.json': '75de42faa6c90f5658a14b2abed9e752aaeaddf7f6f5404a0f035c8bbf6dd28b',
}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise ValueError(reason)


def authenticate_authority() -> dict:
    # Read the fixed runtime-owned DB, not an editable exported authority file.
    with sqlite3.connect(SESSION_DB.as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        for session, parent in ((OPERATOR_SESSION, None), (S1_SESSION, OPERATOR_SESSION)):
            row = db.execute('SELECT parent_session_id FROM sessions WHERE id=?', (session,)).fetchone()
            require(row is not None and row['parent_session_id'] == parent, 'external_sent_authority_session_mismatch')
        bindings = AUTHORITY_BINDINGS
        for mid, session, epoch, sha in bindings:
            row = db.execute('SELECT session_id,role,timestamp,content FROM messages WHERE id=?', (mid,)).fetchone()
            require(row is not None and row['role'] == 'user' and row['session_id'] == session
                    and row['timestamp'] == epoch and digest(row['content'].encode()) == sha,
                    'external_sent_retained_authority_mismatch')
        # Bind original S1 executed queue receipt to its real assistant tool call.
        call = db.execute('SELECT session_id,role,tool_calls FROM messages WHERE id=5138937').fetchone()
        result = db.execute('SELECT session_id,role,tool_call_id,tool_name,content FROM messages WHERE id=5138938').fetchone()
        require(call is not None and result is not None and call['session_id'] == result['session_id'] == S1_SESSION
                and call['role'] == 'assistant' and result['role'] == 'tool' and result['tool_name'] == 'terminal',
                'external_sent_s1_custody_missing')
        calls = json.loads(call['tool_calls'])
        matching = [c for c in calls if c.get('id') == result['tool_call_id']
                    and c.get('function', {}).get('name') == 'terminal']
        require(len(matching) == 1 and json.loads(matching[0]['function']['arguments']).get('workdir') == str(BUSINESS)
                and 'queue_nu_reply.py' in json.loads(matching[0]['function']['arguments']).get('command', ''),
                'external_sent_s1_tool_binding_mismatch')
        receipt = json.loads(result['content'])
        require(receipt.get('exit_code') == 0, 'external_sent_s1_queue_not_verified')
        original_queue = json.loads(receipt['output'])
        require(original_queue.get('artifact_sha256') == ARTIFACT_SHA and original_queue.get('body_sha256') == BODY_SHA
                and original_queue.get('thread_id') == THREAD and original_queue.get('sender') == SENDER
                and original_queue.get('to') == TO, 'external_sent_s1_queue_binding_mismatch')
    return {'kind': 'retained_operator_TUI_with_address_correction_and_S1_word_steering',
            'operator_message_id': 5098910, 'operator_session_id': OPERATOR_SESSION,
            'operator_timestamp': bindings[0][2], 'operator_content_sha256': bindings[0][3],
            's1_session_id': S1_SESSION, 'steering_message_id': 5137413,
            'steering_content_sha256': bindings[1][3], 'queue_tool_call_message_id': 5138937,
            'queue_tool_result_message_id': 5138938,
            'queue_tool_result_sha256': digest(result['content'].encode()),
            'proposal_is_operator_approval': False}


def source_binding(conn: sqlite3.Connection) -> dict:
    row = conn.execute('SELECT * FROM inbox_events WHERE event_id=?', (EVENT,)).fetchone()
    require(row is not None and row['mailbox_id'] == MAILBOX and row['provider_message_id'] == THREAD
            and row['provider_thread_id'] == THREAD and row['raw_sha256'] == SOURCE_SHA,
            'external_sent_source_binding_mismatch')
    wire = Path(row['raw_path']).read_bytes()
    require(digest(wire) == SOURCE_SHA, 'external_sent_source_raw_mismatch')
    parsed = BytesParser(policy=default).parsebytes(wire)
    require(getaddresses(parsed.get_all('From', [])) == [('', TO)]
            or [a.lower() for _, a in getaddresses(parsed.get_all('From', []))] == [TO],
            'external_sent_source_sender_mismatch')
    require(str(parsed['Message-ID']).strip() == row['rfc_message_id'], 'external_sent_source_rfc_mismatch')
    return dict(row)


def verify_sent_row(row: dict, queue: dict, source: dict, candidate: bytes) -> None:
    require(row.get('id') == SENT and row.get('threadId') == THREAD and 'SENT' in row.get('labelIds', [])
            and 'DRAFT' not in row.get('labelIds', []) and row.get('internalDate') == '1791810332000',
            'external_sent_provider_identity_mismatch')
    wire = base64.urlsafe_b64decode(row['raw'] + '===')
    require(digest(wire) == SENT_RAW_SHA, 'external_sent_provider_raw_mismatch')
    parsed = BytesParser(policy=default).parsebytes(wire)
    part = parsed.get_body(preferencelist=('plain',))
    body = part.get_content().replace('\r\n', '\n') if part else ''
    require(digest(body.encode()) == BODY_SHA and candidate.split(b'\n\n', 1)[1].decode() == body,
            'external_sent_provider_body_mismatch')
    require([a.lower() for _, a in getaddresses(parsed.get_all('From', []))] == [SENDER]
            and [a.lower() for _, a in getaddresses(parsed.get_all('To', []))] == [TO]
            and not parsed.get_all('Cc') and not parsed.get_all('Bcc')
            and str(parsed['Subject']) == queue['subject']
            and str(parsed['In-Reply-To']).strip() == source['rfc_message_id']
            and not list(parsed.iter_attachments()), 'external_sent_provider_envelope_mismatch')


def collect_evidence(conn: sqlite3.Connection) -> dict:
    source = source_binding(conn)
    authority = authenticate_authority()
    retained = {}
    for name, expected in RETAINED.items():
        wire = (LANES / name).read_bytes()
        require(digest(wire) == expected, 'external_sent_retained_artifact_mismatch')
        retained[name] = json.loads(wire)
    approved = (LANES / 'L8/GLP125-NuImage-reply.txt').read_bytes()
    candidate = (LANES / 'S1/GLP125-NuImage-reply.txt').read_bytes()
    require(digest(approved) == APPROVED_SHA and digest(candidate) == ARTIFACT_SHA
            and approved.replace(b'follow-ups', b'follow ups').replace(b'glp-clinic-scout', b'GLP Clinic Scout') == candidate,
            'external_sent_exact_word_correction_mismatch')
    queue = retained['S1/GLP125-QUEUE.json']
    sent = retained['S1/GLP125-SEND.json']
    require(sent.get('status') == 'SENT_RAW_VERIFIED_NOT_DELIVERY_PROOF' and sent.get('sends') == 1
            and sent.get('message_id') == SENT and sent.get('thread_id') == THREAD
            and sent.get('draft_id') == queue.get('draft_id'), 'external_sent_sender_receipt_mismatch')
    from inbox_control_plane.gmail_client import GmailClient, load_registry
    mailbox = next((m for m in load_registry() if m.mailbox_id == MAILBOX), None)
    require(mailbox is not None and mailbox.canonical_address == 'ops@glpclinicscout.com',
            'external_sent_unknown_mailbox')
    client = GmailClient.from_mailbox(mailbox, mode='read')
    profile = client.preflight()
    require(profile.get('status') == 'pass' and profile.get('mailbox_id') == MAILBOX
            and profile.get('account') == mailbox.canonical_address and profile.get('mode') == 'read',
            'external_sent_profile_mismatch')
    row = client.get_message(SENT, format='raw')  # Exactly one message GET; no list/thread scan.
    verify_sent_row(row, queue, source, candidate)
    return {'schema': 'external-sent-observation.v1', 'event_id': EVENT, 'mailbox_id': MAILBOX,
            'source_message_id': THREAD, 'thread_id': THREAD, 'sent_message_id': SENT,
            'source_raw_sha256': SOURCE_SHA, 'sent_raw_sha256': SENT_RAW_SHA,
            'body_sha256': BODY_SHA, 'artifact_sha256': ARTIFACT_SHA, 'approval': authority,
            'retained_artifact_sha256': RETAINED, 'sent_at': '2026-10-12T13:05:32Z',
            'delivery_proven': False, 'provider_mutations': 0, 'outbound_send': False,
            'local_draft_created': False, 'operator_decision_created': False}


def observe(db_path: Path, *, apply: bool = False) -> dict:
    from inbox_control_plane.control_plane import Ledger
    # No constructor: no migrations, policy reset, registry sync, directory writes.
    with sqlite3.connect(db_path.resolve().as_uri() + '?mode=ro', uri=True) as reader:
        reader.row_factory = sqlite3.Row
        reader.execute('PRAGMA query_only=ON')
        payload = collect_evidence(reader)
    if not apply:
        return {'status': 'pass', 'verified': True, 'recorded': False, 'delivery_proven': False,
                'provider_mutations': 0, 'outbound_send': False, 'evidence': payload}
    conn = sqlite3.connect(db_path.resolve().as_uri() + '?mode=rw', uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('BEGIN IMMEDIATE')
        source_binding(conn)  # Revalidate under shared DB's write lock.
        existing = conn.execute("SELECT payload_json,event_hash FROM audit_events WHERE event_type='external_sent_observed' AND object_type='inbox_event' AND object_id=?", (EVENT,)).fetchall()
        if existing:
            require(len(existing) == 1 and json.loads(existing[0]['payload_json']) == payload,
                    'external_sent_observation_conflict')
            audit_hash = existing[0]['event_hash']
            conn.rollback()
        else:
            require(conn.execute('SELECT COUNT(*) FROM local_drafts WHERE event_id=?', (EVENT,)).fetchone()[0] == 0,
                    'external_sent_local_draft_requires_existing_send_path')
            # Reuse the existing immutable hash-chain producer; only one audit row.
            ledger = Ledger.__new__(Ledger)
            ledger.conn = conn
            audit_hash = ledger.audit('external_sent_observed', 'inbox_event', EVENT, payload,
                                      actor='external-sent-observation')
        return {'status': 'pass', 'verified': True, 'recorded': True, 'idempotent_replay': bool(existing),
                'audit_event_hash': audit_hash, 'delivery_proven': False,
                'provider_mutations': 0, 'outbound_send': False}
    finally:
        conn.rollback()
        conn.close()
