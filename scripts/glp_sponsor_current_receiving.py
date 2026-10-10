#!/usr/bin/env python3
"""Receive the current approved commercial packet; no email, debit or cron effect.

Local coordinator custody, not cryptographic authentication of file writers.
Historical personal approval and technical-successor receipts remain immutable.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import re
import zipfile
from pathlib import Path

SCHEMA = 'sponsor.current_coordinator_receiving.v1'
RECEIPT = 'current-coordinator-receiving.json'
PLAN = 'reports/ops/fable-plan-2-20261009/PLAN.json'
DECISION = 'ops/linear/approvals/OPERATOR-DECISION-20261009-fable2-decisions.md'
PACKET = 'reports/ops/sponsor-machine-20261003/sponsor-review-packet.zip'
MANIFEST = 'ops/sponsor-machine/assets.json'
CONFIG = 'ops/sponsor-machine/campaign.json'
COPY = 'ops/sponsor-machine/copy.json'
PRICING = dict(currency='USD', initial_total=750, setup=250, initial_placement=500,
               term_hours=720, optional_same_scope_renewal=500, auto_charge=False)
SOURCE_PATHS = (CONFIG, COPY, 'scripts/glp_sponsor_campaign.py',
 'scripts/glp_sponsor_current_receiving.py', 'scripts/firecrawl_client.py',
 'scripts/glp328_readonly_evidence.py', 'scripts/glp_sponsor_paypal.py',
 'scripts/glp_sponsor_readiness.py', 'scripts/glp_sponsor_alexandria.py',
 'scripts/bouncer_shared_account_guard.py', 'inbox_control_plane/control_plane.py',
 'inbox_control_plane/sponsor_replies.py', 'inbox_control_plane/gmail_client.py',
 'inbox_control_plane/outbound_style_guard.py')
ASSETS = tuple('reports/ops/sponsor-machine-20261003/assets/'+n for n in
               ('rate-card.pdf', 'placement-template.pdf', 'promo-page-preview.pdf'))
REVIEW = 'reports/ops/sponsor-machine-20261003/REVIEW.md'
SUPPLY = 'SUPPLY.json'


def strict(text):
    def unique(pairs):
        out = {}
        for key, value in pairs:
            if key in out:
                raise ValueError('duplicate_json_key:' + key)
            out[key] = value
        return out
    return json.loads(text, object_pairs_hook=unique)


def current_authority(c):
    plan = strict((c.ROOT / PLAN).read_text())
    if plan.get('schema') != 'glp-fable-plan-2-v1' or plan.get('executor_keys', {}).get('coordinator') != 'TUI or auto-coordinator cron acting on fleet-inbox rows':
        raise RuntimeError('current_coordinator_authority_missing')
    decision = (c.ROOT / DECISION).read_text()
    if 'GO at 25/day' not in decision or 'Mon-Fri 06:00-17:00 America/Los_Angeles' not in decision:
        raise RuntimeError('current_operator_decision_missing')
    return {'plan_sha256': c.digest(c.ROOT / PLAN), 'decision_sha256': c.digest(c.ROOT / DECISION)}


def require_coordinator_execution():
    # Current live TUI identity is resolved now, never a named historical actor.
    # A leaf inherits the active-session pointer but has its own session ID.
    active = os.environ.get('HERMES_TUI_ACTIVE_SESSION_FILE', '')
    session = os.environ.get('HERMES_SESSION_ID', '')
    if os.environ.get('LINEAR_ROLE') != 'orchestrator' or not active or not session:
        raise RuntimeError('current_coordinator_execution_required')
    if strict(Path(active).read_text()).get('session_id') != session:
        raise RuntimeError('current_coordinator_execution_required')
    return session


def packet_binding(c, packet, config):
    # Closed set, safe archive paths, exact installed source AND outward bytes.
    with zipfile.ZipFile(packet) as archive:
        names = archive.namelist()
        allowed = set(SOURCE_PATHS) | set(ASSETS) | {MANIFEST, REVIEW, SUPPLY}
        if len(names) != len(set(names)) or set(names) != allowed:
            raise RuntimeError('current_packet_members_invalid')
        manifest = strict(archive.read(MANIFEST))
        if set(manifest) != allowed - {MANIFEST}:
            raise RuntimeError('current_packet_manifest_invalid')
        for name, pin in manifest.items():
            blob = archive.read(name)
            if hashlib.sha256(blob).hexdigest() != pin:
                raise RuntimeError('current_packet_member_changed:' + name)
            if name != SUPPLY and c.digest(c.ROOT / name) != pin:
                raise RuntimeError('current_packet_installed_binding_changed:' + name)
        if c.digest(c.ROOT / MANIFEST) != hashlib.sha256(archive.read(MANIFEST)).hexdigest():
            raise RuntimeError('current_packet_manifest_not_installed')
        sealed = strict(archive.read(CONFIG))
        copy = strict(archive.read(COPY))
        for item in (sealed, config, copy):
            if item.get('pricing') != PRICING:
                raise RuntimeError('current_packet_pricing_not_approved')
        if sealed.get('postal_address') not in ('2108 N St, Ste N, Sacramento, CA 95816', '2108 N St, Ste N, Sacramento, California 95816, United States') or config.get('postal_address') != sealed['postal_address']:
            raise RuntimeError('current_packet_postal_address_not_approved')
        for key, value in {'sender':c.SENDER, 'schedule':'0 9 * * 1-5', 'timezone':'America/Los_Angeles',
                           'new_provider_daily_cap':25, 'require_bouncer':True,
                           'require_canonical_reply_coverage':True, 'require_full_source_binding':True}.items():
            if sealed.get(key) != value or config.get(key) != value:
                raise RuntimeError('current_packet_campaign_guard_changed:' + key)
        # Only the two activation keys may differ from installed, reviewed config.
        if {k:v for k,v in config.items() if k not in ('send_enabled','packet_sha256')} != {k:v for k,v in sealed.items() if k not in ('send_enabled','packet_sha256')}:
            raise RuntimeError('current_packet_config_changed')
        supply = strict(archive.read(SUPPLY))
        orgs = supply.get('organizations', [])
        if len(orgs) != 12 or len({o['host'] for o in orgs}) != 12 or supply.get('sends') != 0:
            raise RuntimeError('current_packet_imported_supply_invalid')
    return manifest


def approval_ready(c, config):
    path = c.STATE / RECEIPT
    if not path.exists():
        return None
    receipt = strict(path.read_text())
    packet = c.ROOT / PACKET
    fields = {'schema','actor','coordinator_authority','receiving_session_id','packet_sha256',
              'decision','reviewed_source_head','installed_bindings'}
    sha = c.digest(packet)
    # Validate cheap receipt identity before resolving installed packet content.
    if (set(receipt) != fields or receipt.get('schema') != SCHEMA or receipt.get('actor') != 'coordinator'
        or receipt.get('coordinator_authority') != current_authority(c)
        or not re.fullmatch(r'[A-Za-z0-9_\-]{8,100}', receipt.get('receiving_session_id',''))
        or receipt.get('packet_sha256') != sha or config.get('packet_sha256') != sha
        or receipt.get('decision') != 'RECEIVE CURRENT SPONSOR PACKET ' + sha
        or not re.fullmatch(r'[0-9a-f]{40}', receipt.get('reviewed_source_head',''))):
        raise RuntimeError('current_coordinator_receiving_invalid')
    if receipt.get('installed_bindings') != packet_binding(c, packet, config):
        raise RuntimeError('current_coordinator_receiving_invalid')
    return True


def receive(c, packet_sha256, source_head, result):
    session = require_coordinator_execution()
    import fcntl
    import sync_from_repo as sync
    with sync.source_lock(c.ROOT), (c.STATE / 'run.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if result.exists() or (c.STATE / RECEIPT).exists():
            raise RuntimeError('current_receipt_exists_inspect_do_not_overwrite')
        packet = c.ROOT / PACKET
        if c.digest(packet) != packet_sha256 or not re.fullmatch(r'[0-9a-f]{40}', source_head):
            raise RuntimeError('current_receiving_exact_head_or_packet_changed')
        config = c.load(c.CONFIG)
        config.update(send_enabled=True, packet_sha256=packet_sha256)
        bindings = packet_binding(c, packet, config)
        receipt = dict(schema=SCHEMA, actor='coordinator', coordinator_authority=current_authority(c),
                       receiving_session_id=session, packet_sha256=packet_sha256,
                       decision='RECEIVE CURRENT SPONSOR PACKET '+packet_sha256,
                       reviewed_source_head=source_head, installed_bindings=bindings)
        c.save(c.STATE / RECEIPT, receipt)
        # Runtime activation is separate from maintained source config, preserving
        # source hashes and scoped importer baselines.
        c.save(c.STATE / 'activation.json', {'send_enabled':True, 'packet_sha256':packet_sha256})
        if c.approval_ready(config) is not True:
            raise RuntimeError('current_receiving_readback_failed')
        c.save(result, dict(status='CURRENT_PACKET_RECEIVED', captured_at=c.stamp(),
                           packet_sha256=packet_sha256, receipt_sha256=c.digest(c.STATE / RECEIPT),
                           coordinator_session_id=session, sends=0, cron_resumed=False))
    return c.load(result)


def main():
    import glp_sponsor_campaign as c
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--packet-sha256', required=True)
    parser.add_argument('--reviewed-source-head', required=True)
    parser.add_argument('--record-received', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(receive(c, args.packet_sha256, args.reviewed_source_head, args.record_received), indent=2))


if __name__ == '__main__':
    main()
