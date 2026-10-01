#!/usr/bin/env python3
"""Hash-bound cancellation reconciliation; no clients, stores or live effects.

A canonical notice wakes reconciliation. Only a retained authenticated provider
status readback naming the exact relationship can authorize the narrow sole
Sheet writer. Captures are produced by the authorized provider adapter, not by
mail classification. This module verifies custody/binding, not credentials.
"""
from __future__ import annotations

import copy
from datetime import datetime
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

BUSINESS = Path('/home/ubuntu/business')
SCHEMA = 'affiliate-program-status-readback-v1'
DECISIONS = frozenset({'cancelled', 'terminated'})


def evidence_of(queue: dict) -> dict:
    evidence = queue.get('evidence', {})
    return json.loads(evidence) if isinstance(evidence, str) else evidence


def timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('withdrawal_timestamp_timezone_required')
    return parsed


def capture_bytes(ref: dict, root: Path) -> tuple[Path, bytes]:
    if (not isinstance(ref, dict) or not isinstance(ref.get('path'), str)
            or not ref['path'] or not isinstance(ref.get('sha256'), str) or not ref['sha256']):
        raise ValueError('withdrawal_provider_verification_required')
    path = Path(ref['path'])
    path = path if path.is_absolute() else root / path
    if path.is_symlink():
        raise ValueError('withdrawal_capture_symlink_forbidden')
    try:
        path = path.resolve(strict=True)
        if not path.is_relative_to(root.resolve()):
            raise ValueError('withdrawal_capture_outside_custody_root')
        raw = path.read_bytes()
    except (OSError, RuntimeError) as exc:
        # The same typed refusal reaches promotion and already-promoted entries;
        # an unreadable proof must never abort the unrelated valid batch tail.
        raise ValueError(f'withdrawal_capture_unreadable:{type(exc).__name__}') from exc
    if hashlib.sha256(raw).hexdigest() != ref['sha256']:
        raise ValueError('withdrawal_provider_verification_hash_drift')
    return path, raw


def verify(queue: dict, *, root: Path = BUSINESS) -> dict:
    """Revalidate at promotion, direct writer and registry delta boundaries."""
    evidence = evidence_of(queue)
    _, raw = capture_bytes(evidence.get('provider_verification'), root)
    decision = evidence.get('decision_class')
    if decision not in DECISIONS:
        raise ValueError('withdrawal_not_cancellation')
    capture = json.loads(raw)
    if not isinstance(capture, dict):
        raise ValueError('withdrawal_provider_verification_object_required')
    if (capture.get('schema') != SCHEMA
            or capture.get('source_kind') != 'authenticated_provider_api'
            or capture.get('relationship_status') not in DECISIONS):
        raise ValueError('withdrawal_provider_verification_status_invalid')
    response = capture.get('response', {})
    if (not isinstance(response, dict)
            or str(response.get('program_id', '')) != str(capture.get('program_id', ''))
            or response.get('relationship_status') != capture['relationship_status']):
        raise ValueError('withdrawal_provider_response_identity_or_status_mismatch')
    if (not capture.get('network') or not capture.get('program_id') or not capture.get('program_name')
            or str(queue.get('network', '')).casefold() != str(capture['network']).casefold()
            or (queue.get('program_id') and str(queue['program_id']) != str(capture['program_id']))
            or (queue.get('program_name') and str(queue['program_name']).casefold() != str(capture['program_name']).casefold())):
        raise ValueError('withdrawal_provider_identity_mismatch')
    if not evidence.get('event_id') or capture.get('source_event_id') != evidence['event_id']:
        raise ValueError('withdrawal_provider_verification_source_event_mismatch')
    notice_time = evidence.get('received_at_utc') or evidence.get('observed_at_utc') or evidence.get('event_at_utc')
    if timestamp(capture.get('observed_at_utc')) < timestamp(notice_time):
        raise ValueError('withdrawal_provider_verification_predates_notice')
    # Retained notice bytes are a provenance prerequisite, never status proof.
    capture_bytes({'path': evidence.get('path'), 'sha256': evidence.get('raw_sha256')}, root)
    return capture


def promote_verified_notice(notice: dict, verification: dict, *, root: Path = BUSINESS) -> dict:
    """Return the same queue item/ID with a verified narrow writer projection.

    Never enqueue another event. A provider reconciler can attach the readback
    to the existing sole-writer queue item; drain calls this function when that
    evidence arrives. Blank provider IDs are filled only from the readback.
    """
    if notice.get('kind') not in {'affiliate_decision_notice', 'network_cancelled'}:
        raise ValueError('withdrawal_notice_kind_invalid')
    result = copy.deepcopy(notice)
    evidence = copy.deepcopy(evidence_of(notice))
    evidence['provider_verification'] = copy.deepcopy(verification)
    result['evidence'] = evidence
    capture = verify(result, root=root)
    result.update(kind='network_cancelled', network=capture['network'],
                  program_id=str(capture['program_id']), program_name=capture['program_name'])
    evidence.update(event_at_utc=capture['observed_at_utc'], sheet_write_authority=True,
                    source_notice_update_id=notice['update_id'])
    return result


# Closed application namespaces already used by maintained Sheet producers and
# identity tests. Do not infer a rail from a brand, registrable-domain suffix or
# a generic Everflow tenant: the same offer number can exist on another tenant.
APPLICATION_NAMESPACES = {
    'awin': ('ui.awin.com',
             r'/(?:awin/affiliate/[0-9]+/|affiliate/[0-9]+/)?merchant-profile/([0-9]+)(?:/overview)?/?',
             frozenset({'mid', 'awinmid', 'merchantid', 'advertiserid'})),
    'cj': ('members.cj.com', r'/member/publisher/(?:advertiser/([0-9]+)/?)?',
           frozenset({'advertiserid'})),
    'cj affiliate': ('members.cj.com', r'/member/publisher/(?:advertiser/([0-9]+)/?)?',
                     frozenset({'advertiserid'})),
    'katalys': ('app.katalys.com', r'/(?:pub/[A-Za-z0-9_-]+/)?(?:explore|programs|offers)/([0-9]+)/?',
                frozenset({'offerid', 'offer_id'})),
    'maxbounty': ('affiliates.maxbounty.com', r'/campaigns/([0-9]+)/?',
                  frozenset({'campaignid', 'campaign_id'})),
    'xentraffic': ('xentraffic.everflowclient.io', r'/offers/([0-9]+)/?',
                   frozenset({'offerid', 'offer_id'})),
}
PROGRAM_QUERY_KEYS = frozenset({'program_id', 'network_program_id', 'merchantid',
                               'mid', 'awinmid', 'advertiserid', 'campaignid',
                               'campaign_id', 'offerid', 'offer_id'})


def _authoritative_program_ids(application_url: str, network: str) -> set[str]:
    """No IO: bind URL authority and ID namespace before considering numbers."""
    if not application_url:
        raise ValueError('withdrawal_sheet_program_id_binding_missing')
    rule = APPLICATION_NAMESPACES.get(network.casefold())
    try:
        url = urlsplit(application_url)
        if (not rule or url.scheme not in {'http', 'https'}
                or url.hostname != rule[0] or url.username is not None
                or url.password is not None or url.fragment
                or url.port not in (None, 443 if url.scheme == 'https' else 80)):
            raise ValueError('withdrawal_sheet_program_id_url_authority_invalid')
    except ValueError as exc:
        raise ValueError('withdrawal_sheet_program_id_url_authority_invalid') from exc
    match = re.fullmatch(rule[1], unquote(url.path))
    if not match:
        raise ValueError('withdrawal_sheet_program_id_url_namespace_invalid')
    program_ids = {match[1]} if match[1] else set()
    allowed_keys = rule[2] | {'program_id', 'network_program_id'}
    for key, value in parse_qsl(url.query, keep_blank_values=True):
        key = key.casefold()
        if key in PROGRAM_QUERY_KEYS:
            if key not in allowed_keys:
                raise ValueError('withdrawal_sheet_program_id_url_namespace_invalid')
            program_ids.add(value)
    if not program_ids:
        raise ValueError('withdrawal_sheet_program_id_binding_missing')
    return program_ids


def verify_writer_entry(entry: dict, row: list | None = None, *, root: Path = BUSINESS) -> dict:
    evidence = entry.get('evidence', {})
    capture = verify({'network': evidence.get('network'), 'program_id': evidence.get('network_program_id'),
                      'program_name': entry.get('program'), 'evidence': evidence}, root=root)
    marker = f"Withdrawal proof for event {evidence['event_id']}: "
    proof_hash = evidence['provider_verification']['sha256']
    if entry.get('cells') and marker + proof_hash not in str(entry['cells'].get('23', '')):
        raise ValueError('withdrawal_notes_proof_binding_required')
    if row is not None:
        recorded = re.findall(re.escape(marker) + r'([a-f0-9]{64})', str(row[23]))
        if recorded and recorded != [proof_hash]:
            raise ValueError('withdrawal_replayed_notice_proof_conflict')
        if not entry.get('cells') and recorded != [proof_hash]:
            raise ValueError('withdrawal_sheet_readback_proof_binding_required')
        if (str(row[4]).casefold() != capture['network'].casefold()
                or str(row[2]).casefold() != capture['program_name'].casefold()):
            raise ValueError('withdrawal_sheet_identity_mismatch')
        # The Sheet has no separate network-program-ID column. Only declared
        # program components in its Apply URL establish that axis. Historical
        # Notes and issued trackers may refer to siblings; neither is authority.
        program_ids = _authoritative_program_ids(str(row[5]), capture['network'])
        if program_ids != {str(capture['program_id'])}:
            raise ValueError('withdrawal_sheet_program_id_identity_conflict')
        if str(row[10]).casefold().startswith(('hold', 'excluded')):
            raise ValueError('withdrawal_existing_provider_hold_preserved')
    if any(name in capture['program_name'].casefold() for name in ('fella', 'telos', 'healthrx', 'health rx', 'xentraffic')):
        raise ValueError('withdrawal_existing_provider_hold_preserved')
    return capture
