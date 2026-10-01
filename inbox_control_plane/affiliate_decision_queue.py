"""Evidence-only affiliate decision notices for the existing single-writer queue.

A notice is not permission to change a relationship or commercial terms. The
Sheet drain explicitly escalates notices for verified provider reconciliation;
no free-text rate, approval or sender is promoted to Sheet authority here.
Durable intent uses the canonical ledger's existing mailbox_actions table.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from inbox_control_plane.affiliate_network_registry import network_for_domain

TABLE = 'hJiP3eJKBvtZ6bQU'  # registered sheet_update_queue, never the Sheet
ACTION = 'affiliate_sheet_decision_notice'
MAILBOXES = frozenset({'glp_ops_gmail', 'glp_izaiah_gmail'})
KINDS = {
    'approval': 'approval_confirmed', 'rejection': 'rejection',
    'rate_change': 'commission_terms_correction', 'paused': 'network_paused',
    'cancelled': 'network_cancelled', 'terminated': 'network_cancelled',
}
# Match asserted outcomes, not a keyword in an invitation/marketing message.
PATTERNS = (
    ('terminated', r'\b(?:program(?:me)?|partnership|relationship|agreement)\b[^\n.!?]{0,180}\b(?:has been|is|was|will be)\s+terminated\b|\bhas terminated your (?:partnership|relationship|agreement)\b'),
    ('cancelled', r'\b(?:program(?:me)?|partnership|relationship|agreement)\b[^\n.!?]{0,180}\b(?:has been|is|was|will be)\s+cancelled\b|\b(?:program(?:me)?|partnership|relationship|agreement)\b[^\n.!?]{0,180}\b(?:has been|is|was|will be)\s+canceled\b'),
    ('paused', r'\b(?:program(?:me)?|partnership)\b[^\n.!?]{0,180}\b(?:has been|is|was|will be)\s+paused\b'),
    ('rejection', r'\bmembership rejection\b|\b(?:application|request)\b[^\n.!?]{0,180}\b(?:has been|is|was)\s+(?:rejected|declined|denied)\b|\bhas (?:rejected|declined|denied) your application\b|\bdecided not to move forward\b'),
    ('rate_change', r'\bchanges? to your commission\b|\b(?:commission(?: rate)?|payout(?: rate)?|rate)\b[^\n.!?]{0,100}\b(?:has|have|is|was|will be)\s+(?:(?:been|being)\s+)?(?:changed|updated|increased|decreased|reduced)\b|\byour (?:updated|new) commission (?:rate|terms)\b'),
    ('approval', r'\b(?:application|request)\b[^\n.!?]{0,180}\b(?:has been|is|was)\s+(?:approved|accepted)\b|\bhas approved your application\b|\byour membership\b[^\n.!?]{0,180}\bis now active\b|\bwelcome to (?:the )?[^\n.!?]{1,120}(?:affiliate|partner) program\b'),
)
COMPILED = tuple((kind, re.compile(pattern, re.I)) for kind, pattern in PATTERNS)


def detect(subject: str, newest_body: str, *, network_root: str = '') -> str | None:
    # The caller removes quoted history using the canonical parser. Ignore a
    # reply subject: an old "approved" subject must not overrule the new reply.
    headline = '' if re.match(r'^\s*(?:re|fw|fwd)\s*:', subject, re.I) else subject
    text = headline + '\n' + newest_body
    # Preserve within-name/domain dots (eMed.com), not sentence boundaries.
    text = re.sub(r'(?<=\w)\.(?=\w)', '\u2024', text)
    # Conditional/educational/negated state is not an observed decision.
    text = re.sub(r'[^\n.!?]*\b(?:if|unless|when|example|not yet|not been|not approved|not paused|not cancelled|not canceled|not terminated)\b[^\n.!?]*', '', text, flags=re.I)
    found = {kind for kind, pattern in COMPILED if pattern.search(text)}
    # Maintained MaxBounty cancellation template: exact network + headline +
    # advertiser-required expiry with a date/time, never a bare cancellation word.
    if (network_root == 'maxbounty.com'
            and re.fullmatch(r'\s*Campaign Cancellation Notice\s*', headline, re.I)
            and re.search(r'^The advertiser for campaign [^\n!?]{1,180} requires that it expire on \d{2}/\d{2}/\d{4} \d{2}:\d{2}\.?\s*$', text, re.I | re.M)):
        found.add('cancelled')
    # Conflicting positive/adverse claims require reconciliation, not guessing.
    return next(iter(found)) if len(found) == 1 else None


def stage(ledger: Any, *, event_id: str, mailbox: dict, parsed: dict,
          classification: dict, raw_path: str, raw_sha256: str,
          provider_message_id: str, now: str, newest_body: str,
          bootstrap: bool, historical: bool, attribution_status: str) -> bool:
    if bootstrap or historical or attribution_status != 'registry_exact_match':
        return False
    if mailbox.get('mailbox_id') not in MAILBOXES or parsed.get('quarantined'):
        return False
    if classification.get('category') not in {'affiliate_network_response', 'newsletter', 'partner_vendor_action'}:
        return False
    network = network_for_domain(str(parsed.get('sender_domain') or ''))
    if not network:
        return False
    # An explicit promotional sender/edition is not a relationship notice.
    # Do not reject newsletter classification or bulk/unsubscribe headers alone:
    # networks use those headers for genuine account/program status templates.
    if (re.match(r'^(?:newsletters?|marketing|marketplace)@', str(parsed.get('sender') or ''), re.I)
            or re.search(r'\b(?:newsletter|digest|webinar)\b', str(parsed.get('subject') or ''), re.I)):
        return False
    # Permanent exclusion and suppression/terminal handling remain intact.
    if re.search(r'\bfella(?:\s+health)?\b', str(parsed.get('subject') or '') + '\n' + newest_body, re.I):
        return False
    state = ledger.conn.execute('SELECT state FROM inbox_events WHERE event_id=?', (event_id,)).fetchone()
    if not state or state['state'] in {'quarantined', 'held', 'archived', 'resolved_externally'}:
        return False
    decision = detect(str(parsed.get('subject') or ''), newest_body, network_root=network[0])
    if decision is None:
        return False
    update_id = 'inbox-decision-' + hashlib.sha256(
        f"{mailbox['mailbox_id']}|{provider_message_id}|{raw_sha256}".encode()).hexdigest()[:32]
    evidence = {
        'schema': 'canonical-affiliate-decision-notice-v1',
        'decision_class': decision, 'intended_writer_kind': KINDS[decision],
        'event_id': event_id, 'mailbox_id': mailbox['mailbox_id'],
        'path': raw_path, 'raw_sha256': raw_sha256,
        'observed_at_utc': parsed['received_at'],
        'network_root': network[0], 'requires_provider_verification': True,
        'sheet_write_authority': False,
    }
    row = {
        'update_id': update_id, 'kind': 'affiliate_decision_notice',
        'network': network[1], 'program_id': '', 'program_name': '',
        'sheet_row': None, 'evidence': json.dumps(evidence, sort_keys=True),
        'status': 'queued', 'queued_by': 'canonical_inbox',
        'queued_at_utc': now, 'resolved_at_utc': '',
        'note': 'Evidence-only decision notice; verify program identity and authority before the single writer acts.',
    }
    # No personal sender, subject, body, or guessed program identity in the queue.
    cursor = ledger.conn.execute(
        '''INSERT OR IGNORE INTO mailbox_actions
           (action_id,event_id,action,status,provider_receipt_json,attempts,created_at,updated_at)
           VALUES(?,?,?,?,?,0,?,?)''',
        (update_id, event_id, ACTION, 'staged', json.dumps(row, sort_keys=True), now, now))
    return cursor.rowcount == 1


def _matches(found: list[dict], expected: dict) -> bool:
    # Writer acknowledgement changes status/note; immutable identity and evidence
    # must still match, including for terminal queue statuses.
    return len(found) == 1 and all(found[0].get(k) == expected[k] for k in (
        'update_id', 'kind', 'network', 'program_id', 'program_name', 'evidence', 'queued_by'))


class RegisteredQueueClient:
    """Use the registered native one-mutation transport, never MCP replay fallback."""
    def __init__(self, transport: Any):
        self.transport = transport

    def eq(self, column: str, value: str) -> dict:
        return self.transport.eq(column, value)

    def rows(self, table: str, filt: dict, limit: int = 2) -> list:
        return self.transport.rows(table, filt, limit=limit)

    def insert(self, table: str, row: dict) -> dict:
        # This registered helper performs one native POST and readback only after
        # uncertainty. The public MCP fallback insert can replay after absence.
        return self.transport._native_insert_after_reconciliation(
            table, row, self.eq('update_id', row['update_id']))


def dispatch(ledger: Any, *, live: bool, client: Any = None, limit: int = 25) -> dict:
    if not live:
        return {'status': 'pass', 'delivered': 0, 'uncertain': 0, 'failed': 0}
    rows = list(ledger.conn.execute(
        '''SELECT a.* FROM mailbox_actions a JOIN inbox_events e USING(event_id)
           WHERE a.action=? AND a.status IN ('staged','uncertain')
             AND e.state NOT IN ('quarantined','held','archived','resolved_externally')
           ORDER BY a.created_at,a.action_id LIMIT ?''', (ACTION, limit)))
    delivered = uncertain = failed = 0
    if rows and client is None:
        # Registered host transport, curated by the source-drift lane separately.
        try:
            from ops import n8n_dt
            client = RegisteredQueueClient(n8n_dt)
        except ImportError:
            return {'status': 'blocked', 'delivered': 0, 'uncertain': 0,
                    'failed': len(rows), 'reason': 'registered_queue_transport_unavailable'}
    for action in rows:
        expected = json.loads(action['provider_receipt_json'])
        try:
            found = client.rows(TABLE, client.eq('update_id', expected['update_id']), limit=2)
            if found:
                if not _matches(found, expected):
                    raise RuntimeError('affiliate_queue_identity_conflict')
            elif action['status'] == 'uncertain':
                # Absence after an uncertain mutation is NOT permission to replay.
                uncertain += 1
                continue
            else:
                with ledger.conn:
                    claimed = ledger.conn.execute(
                        "UPDATE mailbox_actions SET status='uncertain',attempts=attempts+1 WHERE action_id=? AND status='staged'",
                        (action['action_id'],))
                if claimed.rowcount != 1:
                    uncertain += 1
                    continue
                client.insert(TABLE, expected)
                found = client.rows(TABLE, client.eq('update_id', expected['update_id']), limit=2)
                if not _matches(found, expected):
                    raise RuntimeError('affiliate_queue_readback_mismatch')
            with ledger.conn:
                ledger.conn.execute("UPDATE mailbox_actions SET status='verified' WHERE action_id=?", (action['action_id'],))
            delivered += 1
        except Exception:
            # No raw provider exception or mail content in receipts.
            failed += 1
    return {'status': 'pass' if not failed and not uncertain else 'blocked',
            'delivered': delivered, 'uncertain': uncertain, 'failed': failed}
