"""GLP-328 sponsorship outreach planner: pure, disabled, fail-closed.

Consumes only caller-supplied read-only Sheet rows, evidenced business
contacts and complete suppression/conversation snapshots.  It never fetches,
invents contacts, writes, schedules or sends.  Approved and pending affiliate
statuses are candidate populations only; nothing here grants send permission.
"""
from __future__ import annotations

import copy
import re
from datetime import datetime, timedelta, timezone
from ipaddress import ip_address
from typing import Any, Iterable
from urllib.parse import urlsplit

from affiliate_provider_row_scope import provider_row_scope
from affiliate_sheet_schema_guard import (
    EXPECTED_COLUMN_COUNT, EXPECTED_HEADERS, VERTICAL_COLUMN, require_vertical_value,
)

PLAN_SCHEMA = 'glp328.outreach_plan.v1'
PREVIEW_SCHEMA = 'glp328.outreach_preview.v1'
FOLLOW_UP_SCHEMA = 'glp328.follow_up_decision.v1'
GLP_VERTICAL = 'glp1-weightloss'
STATUS_COLUMN = EXPECTED_HEADERS.index('Status')
PROGRAM_COLUMN = EXPECTED_HEADERS.index('Program / Network')
OFFICIAL_URL_COLUMN = EXPECTED_HEADERS.index('Official URL')
SNAPSHOT_MAX_AGE = timedelta(hours=24)
FOLLOW_UP_MIN_DELAY = timedelta(days=7)
MAX_FOLLOW_UPS = 1
TEMPLATE_STATUS = 'UNAPPROVED'
TEMPLATE_FIELDS = frozenset({'organization', 'contact_name', 'official_host'})
NEGATIVE_STATUS_TOKENS = frozenset({
    'rejected', 'declined', 'denied', 'cancelled', 'canceled', 'terminated',
    'inactive', 'paused', 'closed', 'not', 'removed', 'suspended', 'expired',
})
FREEMAIL_DOMAINS = frozenset({
    'gmail.com', 'googlemail.com', 'yahoo.com', 'outlook.com', 'hotmail.com',
    'live.com', 'aol.com', 'icloud.com', 'me.com', 'proton.me', 'protonmail.com',
    'gmx.com', 'mail.com', 'yandex.com', 'zoho.com',
})
UNREACHABLE_LOCAL_PARTS = frozenset({'noreply', 'no-reply', 'donotreply', 'do-not-reply', 'mailer-daemon', 'postmaster'})
STOP_EVENT_KINDS = frozenset({'reply', 'opt_out', 'unsubscribe', 'bounce', 'complaint'})
_EMAIL = re.compile(r'^[a-z0-9._%+-]+@([a-z0-9-]+(?:\.[a-z0-9-]+)+)$')
_FIELD = re.compile(r'\{([^{}]*)\}')


class OutreachPolicyError(RuntimeError):
    """Malformed global input; the whole plan fails closed."""


def canonical_host(url: Any) -> str:
    """Canonical official business host; blank on anything unusable."""
    raw = str(url or '').strip()
    if not raw:
        return ''
    if '://' not in raw:
        raw = 'https://' + raw
    try:
        parts = urlsplit(raw)
        host = (parts.hostname or '').rstrip('.').casefold()
    except ValueError:
        return ''
    if parts.scheme not in {'http', 'https'} or '.' not in host or host == 'localhost':
        return ''
    try:
        ip_address(host)
        return ''
    except ValueError:
        pass
    return host[4:] if host.startswith('www.') else host


def _raise(reason: str) -> str:
    raise OutreachPolicyError(reason)


def _same_org(domain: str, host: str) -> bool:
    return bool(domain and host) and (domain == host or domain.endswith('.' + host)
                                      or host.endswith('.' + domain))


def _email(value: Any) -> tuple[str, str]:
    email = str(value or '').strip().casefold()
    match = _EMAIL.match(email)
    return (email, match.group(1)) if match else ('', '')


def _utc(value: Any, label: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value or '').replace('Z', '+00:00'))
        except ValueError as exc:
            raise OutreachPolicyError(f'{label}_timestamp_invalid') from exc
    if parsed.tzinfo is None:
        raise OutreachPolicyError(f'{label}_timestamp_not_timezone_aware')
    return parsed.astimezone(timezone.utc)


def _checked_snapshot(snapshot: Any, label: str, now: datetime,
                      not_before: datetime | None = None) -> list[dict[str, Any]]:
    """Require a complete, checked, fresh snapshot or raise."""
    if not isinstance(snapshot, dict) or snapshot.get('complete') is not True:
        raise OutreachPolicyError(f'{label}_snapshot_incomplete')
    checked = _utc(snapshot.get('checked_utc'), f'{label}_snapshot')
    if checked > now:
        raise OutreachPolicyError(f'{label}_snapshot_from_future')
    if now - checked > SNAPSHOT_MAX_AGE:
        raise OutreachPolicyError(f'{label}_snapshot_stale')
    if not_before is not None and checked <= not_before:
        raise OutreachPolicyError(f'{label}_snapshot_not_after_send')
    entries = snapshot.get('entries')
    if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
        raise OutreachPolicyError(f'{label}_snapshot_entries_invalid')
    return entries


def _entry_domain(entry: dict[str, Any]) -> str:
    email, domain = _email(entry.get('email'))
    if email:
        return domain
    return canonical_host(entry.get('domain'))


def _entry_hits(entries: Iterable[dict[str, Any]], host: str) -> list[dict[str, Any]]:
    """Organization-level match: any address/domain at the same organization."""
    hits = []
    for entry in entries:
        domain = _entry_domain(entry)
        if not domain:
            raise OutreachPolicyError('snapshot_entry_identity_missing')
        if _same_org(domain, host):
            hits.append(entry)
    return hits


def _status_population(status: str) -> str:
    tokens = set(re.split(r'[^a-z]+', status.casefold())) - {''}
    if 'hold' in tokens:
        return 'hold'
    if not tokens or tokens & NEGATIVE_STATUS_TOKENS:
        return ''
    if 'approved' in tokens:
        return 'approved'
    if 'pending' in tokens:
        return 'pending'
    return ''


def classify_row(row_number: int, values: list[Any]) -> dict[str, Any]:
    """Row-level candidacy; exclusions reuse the canonical provider scope gate."""
    if len(values) > EXPECTED_COLUMN_COUNT:
        return {'row': row_number, 'eligible': False, 'reason': 'row_wider_than_schema'}
    values = list(values) + [''] * (EXPECTED_COLUMN_COUNT - len(values))
    cell = lambda index: str(values[index] or '').strip()
    base = {'row': row_number, 'program': cell(PROGRAM_COLUMN),
            'host': canonical_host(cell(OFFICIAL_URL_COLUMN))}
    population = _status_population(cell(STATUS_COLUMN))
    if population == 'hold':
        return {**base, 'eligible': False, 'reason': 'status_hold_preserved', 'blocks_org': True}
    scope = provider_row_scope(row_number, values)
    if not scope['eligible']:
        return {**base, 'eligible': False, 'reason': scope['reason'],
                'blocks_org': scope['row_class'] == 'permanently_excluded'}
    try:
        vertical = require_vertical_value(values[VERTICAL_COLUMN])
    except RuntimeError as exc:
        return {**base, 'eligible': False, 'reason': str(exc)}
    if vertical != GLP_VERTICAL:
        return {**base, 'eligible': False, 'reason': f'non_glp_vertical:{vertical}'}
    if not population:
        return {**base, 'eligible': False, 'reason': 'status_not_candidate_population'}
    if not base['host']:
        return {**base, 'eligible': False, 'reason': 'official_host_missing'}
    return {**base, 'eligible': True, 'population': population}


def _contact_problem(contact: dict[str, Any], host: str) -> str:
    email, domain = _email(contact.get('email'))
    if not email:
        return 'contact_email_invalid'
    if contact.get('contact_type') != 'business':
        return 'contact_not_business'
    if domain in FREEMAIL_DOMAINS or not _same_org(domain, host):
        return 'contact_domain_not_official_host'
    if email.split('@', 1)[0] in UNREACHABLE_LOCAL_PARTS:
        return 'contact_unreachable_mailbox'
    evidence = contact.get('evidence')
    if not isinstance(evidence, dict):
        return 'contact_evidence_missing'
    source = str(evidence.get('source_url') or '')
    if urlsplit(source).scheme != 'https' or not _same_org(canonical_host(source), host):
        return 'contact_evidence_not_official_source'
    if not str(evidence.get('observed_utc') or '').strip():
        return 'contact_evidence_unobserved'
    return ''


def _location_problem(evidence: Any) -> str:
    if not isinstance(evidence, dict):
        return 'us_location_evidence_missing'
    if str(evidence.get('country') or '').strip().upper() != 'US':
        return 'us_location_not_evidenced'
    if urlsplit(str(evidence.get('source_url') or '')).scheme != 'https':
        return 'us_location_evidence_source_missing'
    if not str(evidence.get('observed_utc') or '').strip():
        return 'us_location_evidence_unobserved'
    return ''


def plan_outreach(sheet: dict[str, Any], contacts: list[dict[str, Any]],
                  location_evidence: dict[str, Any], suppression_snapshot: dict[str, Any],
                  conversation_snapshot: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Disabled plan of at most one evidenced contact per organization."""
    sheet, contacts, location_evidence = copy.deepcopy((sheet, contacts, location_evidence))
    now = _utc(now, 'now')
    if not isinstance(sheet, dict) or tuple(sheet.get('headers') or ()) != EXPECTED_HEADERS:
        raise OutreachPolicyError('sheet_headers_not_canonical')
    suppressions = _checked_snapshot(suppression_snapshot, 'suppression', now)
    conversations = _checked_snapshot(conversation_snapshot, 'conversation', now)
    rows = sheet.get('rows')
    if not isinstance(rows, list):
        raise OutreachPolicyError('sheet_rows_invalid')

    excluded: list[dict[str, Any]] = []
    orgs: dict[str, dict[str, Any]] = {}
    blocked: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('row'), int) \
                or not isinstance(row.get('values'), list):
            raise OutreachPolicyError('sheet_row_shape_invalid')
        verdict = classify_row(row['row'], row['values'])
        if verdict.pop('blocks_org', False) and verdict['host']:
            blocked.setdefault(verdict['host'], verdict['reason'])
        if not verdict['eligible']:
            excluded.append(verdict)
            continue
        org = orgs.setdefault(verdict['host'], {'host': verdict['host'], 'rows': [],
                                                'programs': [], 'populations': set()})
        org['rows'].append(verdict['row'])
        org['programs'].append(verdict['program'])
        org['populations'].add(verdict['population'])

    by_host: dict[str, list[dict[str, Any]]] = {}
    for contact in contacts:
        if not isinstance(contact, dict):
            raise OutreachPolicyError('contact_shape_invalid')
        by_host.setdefault(canonical_host(contact.get('organization_host')), []).append(contact)

    candidates = []
    for host in sorted(orgs):
        org = orgs[host]
        record = {'host': host, 'rows': sorted(org['rows']), 'programs': org['programs']}
        reason = blocked.get(host) or _location_problem(location_evidence.get(host))
        if not reason and _entry_hits(suppressions, host):
            reason = 'organization_opted_out'
        if not reason and any(str(e.get('state') or 'active') != 'closed'
                              for e in _entry_hits(conversations, host)):
            reason = 'organization_active_conversation'
        rejected, usable = [], []
        if not reason:
            for contact in by_host.get(host, []):
                problem = _contact_problem(contact, host)
                (rejected if problem else usable).append((problem, contact))
            usable.sort(key=lambda item: _email(item[1].get('email'))[0])
            if not usable:
                reason = rejected[0][0] if rejected else 'no_evidenced_business_contact'
        if reason:
            excluded.append({**record, 'eligible': False, 'reason': reason})
            continue
        chosen = usable[0][1]
        candidates.append({
            **record, 'eligible': True,
            'population': 'approved' if 'approved' in org['populations'] else 'pending',
            'contact': {'email': _email(chosen['email'])[0],
                        'name': str(chosen.get('name') or '').strip(),
                        'evidence': chosen['evidence']},
            'contacts_not_selected': len(usable) - 1,
            'send_permission': False,
        })
    return {'schema': PLAN_SCHEMA, 'mode': 'disabled', 'send_enabled': False,
            'generated_utc': now.isoformat(), 'candidates': candidates,
            'excluded': excluded}


def render_preview(candidate: dict[str, Any], template: dict[str, Any]) -> dict[str, Any]:
    """Local preview from an explicit UNAPPROVED template; never sendable."""
    if not isinstance(template, dict) or template.get('status') != TEMPLATE_STATUS:
        raise OutreachPolicyError('template_not_explicitly_unapproved')
    if template.get('source') != 'local':
        raise OutreachPolicyError('template_not_local')
    if not candidate.get('eligible') or candidate.get('send_permission') is not False:
        raise OutreachPolicyError('preview_candidate_invalid')
    fields = {'organization': (candidate.get('programs') or [''])[0],
              'contact_name': candidate['contact'].get('name') or 'there',
              'official_host': candidate['host']}
    rendered = {}
    for part in ('subject', 'body'):
        text = template.get(part)
        if not isinstance(text, str) or not text.strip():
            raise OutreachPolicyError(f'template_{part}_missing')
        unknown = set(_FIELD.findall(text)) - TEMPLATE_FIELDS
        if unknown:
            raise OutreachPolicyError(f'template_fields_unknown:{",".join(sorted(unknown))}')
        rendered[part] = _FIELD.sub(lambda m: fields[m.group(1)], text)
    return {'schema': PREVIEW_SCHEMA, 'template_status': TEMPLATE_STATUS,
            'sendable': False, 'send_permission': False,
            'banner': 'PREVIEW ONLY - UNAPPROVED TEMPLATE - NOT SENDABLE',
            'to': candidate['contact']['email'], **rendered}


def decide_follow_up(send_receipt: dict[str, Any], prior_follow_ups: list[dict[str, Any]],
                     reply_snapshot: dict[str, Any], suppression_snapshot: dict[str, Any],
                     now: datetime) -> dict[str, Any]:
    """At most one follow-up, >=7 days after a verified send; still unsendable."""
    now = _utc(now, 'now')
    decision = lambda eligible, reason: {
        'schema': FOLLOW_UP_SCHEMA, 'eligible': eligible, 'reason': reason,
        'sendable': False, 'send_permission': False}
    if not isinstance(send_receipt, dict) or send_receipt.get('verified') is not True \
            or not str(send_receipt.get('message_id') or '').strip() \
            or not str(send_receipt.get('verification_source') or '').strip():
        return decision(False, 'send_receipt_not_verified')
    if any(send_receipt.get(flag) for flag in ('pilot', 'simulated', 'fixture', 'dry_run')):
        return decision(False, 'send_receipt_not_real')
    if send_receipt.get('kind') != 'initial':
        return decision(False, 'send_receipt_not_initial')
    email, domain = _email(send_receipt.get('recipient'))
    if not email:
        return decision(False, 'send_receipt_recipient_invalid')
    sent = _utc(send_receipt.get('sent_utc'), 'send_receipt')
    if sent > now:
        return decision(False, 'send_receipt_from_future')
    replies = _checked_snapshot(reply_snapshot, 'reply', now, not_before=sent)
    suppressions = _checked_snapshot(suppression_snapshot, 'suppression', now, not_before=sent)
    if _entry_hits(suppressions, domain):
        return decision(False, 'stop_opt_out')
    for event in _entry_hits(replies, domain):
        kind = str(event.get('kind') or '').strip().casefold()
        if kind not in STOP_EVENT_KINDS:
            raise OutreachPolicyError(f'reply_event_kind_unknown:{kind or "BLANK"}')
        return decision(False, f'stop_{kind}')
    if not isinstance(prior_follow_ups, list):
        raise OutreachPolicyError('prior_follow_ups_invalid')
    prior = [f for f in prior_follow_ups
             if _same_org(_entry_domain({'email': f.get('recipient'), 'domain': f.get('domain')})
                          or _raise('prior_follow_up_identity_missing'), domain)]
    if len(prior) >= MAX_FOLLOW_UPS:
        return decision(False, 'follow_up_limit_reached')
    if now - sent < FOLLOW_UP_MIN_DELAY:
        return decision(False, 'follow_up_too_soon')
    return {**decision(True, 'follow_up_allowed_disabled'), 'recipient': email,
            'earliest_utc': (sent + FOLLOW_UP_MIN_DELAY).isoformat()}
