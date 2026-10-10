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
ASSET_REVIEW = 'ASSET-REVIEW.json'
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
        allowed = set(SOURCE_PATHS) | set(ASSETS) | {MANIFEST, REVIEW, SUPPLY, ASSET_REVIEW}
        if len(names) != len(set(names)) or set(names) != allowed:
            raise RuntimeError('current_packet_members_invalid')
        manifest = strict(archive.read(MANIFEST))
        if set(manifest) != allowed - {MANIFEST}:
            raise RuntimeError('current_packet_manifest_invalid')
        for name, pin in manifest.items():
            blob = archive.read(name)
            if hashlib.sha256(blob).hexdigest() != pin:
                raise RuntimeError('current_packet_member_changed:' + name)
            if name not in (SUPPLY, ASSET_REVIEW) and c.digest(c.ROOT / name) != pin:
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
        # Fresh commercial bytes cannot borrow predecessor art approval. The
        # independently retained native-art review must bind these exact PDFs
        # and current economics/address; the author never produces this PASS.
        art = strict(archive.read(ASSET_REVIEW))
        art_fields = {'schema','status','reviewer','pricing','postal_address','asset_sha256'}
        if (set(art) != art_fields or art.get('schema') != 'sponsor.current_native_assets_review.v1'
            or art.get('status') != 'PASS' or not art.get('reviewer')
            or art.get('pricing') != PRICING or art.get('postal_address') != sealed['postal_address']
            or art.get('asset_sha256') != {name:manifest[name] for name in ASSETS}):
            raise RuntimeError('current_native_assets_independent_review_required')
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
    if strict((c.STATE/'approval.json').read_bytes()) != receipt:
        raise RuntimeError('current_coordinator_receiving_invalid:approval_binding_disagree')
    if receipt.get('installed_bindings') != packet_binding(c, packet, config):
        raise RuntimeError('current_coordinator_receiving_invalid')
    return True


def receive(c, packet_sha256, source_head, result):
    session = require_coordinator_execution()
    import fcntl
    import shutil
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
        preimages=result.parent/'canonical-preimages'
        preimages.mkdir(parents=True,exist_ok=True)
        for name in ('approval.json','activation.json'):
            original=c.STATE/name
            if original.is_file():
                shutil.copyfile(original,preimages/(name+'.'+c.digest(original)))
        c.save(c.STATE / RECEIPT, receipt)
        c.save(c.STATE / 'approval.json', receipt)
        # Runtime activation is separate from maintained source config, preserving
        # source hashes and scoped importer baselines.
        c.save(c.STATE / 'activation.json', {'send_enabled':True, 'packet_sha256':packet_sha256})
        if c.approval_ready(config) is not True:
            raise RuntimeError('current_receiving_readback_failed')
        c.save(result, dict(status='CURRENT_PACKET_RECEIVED', captured_at=c.stamp(),
                           packet_sha256=packet_sha256, receipt_sha256=c.digest(c.STATE / RECEIPT),
                           coordinator_session_id=session, sends=0, cron_resumed=False))
    return c.load(result)


def build_packet(c, *, site_root, capture_path, supply_path, output, receiver_checkout=None, asset_review=None):
    """Build-only source preview; never install, receive, enable or send."""
    import importlib.util
    import shutil
    import subprocess
    site_root, output = Path(site_root), Path(output)
    if output.exists() and any(output.iterdir()):
        raise RuntimeError('packet_output_exists_choose_fresh_directory')
    config = c.load(c.CONFIG)
    pricing = strict((site_root/'data/sponsor-pricing.json').read_bytes())
    expected = dict(currency='USD', initial=750, setup=250, placement=500,
                    renewal=500, termHours=720, autoCharge=False)
    if config['pricing'] != PRICING or pricing != expected:
        raise RuntimeError('installed_and_native_economics_disagree')
    producer = site_root/'scripts/glp327_sponsor_packet.py'
    spec = importlib.util.spec_from_file_location('native_sponsor_packet', producer)
    native = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(native)
    cap = native.validate_capture(site_root, c.ROOT, capture_path)
    supply = strict(Path(supply_path).read_bytes())
    if len({r['host'] for r in supply['organizations']}) != 12 or supply['sends'] != 0:
        raise RuntimeError('twelve_observed_organizations_required')
    bundle = output/'installation-proposal'
    bundle.mkdir(parents=True, exist_ok=True)
    overlay_paths = {'scripts/glp_sponsor_campaign.py','scripts/glp_sponsor_current_receiving.py'}
    for name in SOURCE_PATHS:
        parent = Path(receiver_checkout) if receiver_checkout and name in overlay_paths else c.ROOT
        target = bundle/name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(parent/name, target)
    for surface, filename in native.SURFACES.items():
        target = bundle/'reports/ops/sponsor-machine-20261003/assets'/filename
        target.parent.mkdir(parents=True, exist_ok=True)
        rows = sorted([r for r in cap['checks'] if r['surface']==surface], key=lambda r:r['viewport']!='mobile')
        native.capture_pdf(target, rows, c.ROOT, first_only=surface=='providers-sponsored-panel')
    return finish_packet(c, bundle, config, cap, supply, producer, output, asset_review, bool(receiver_checkout))


def finish_packet(c, bundle, config, cap, supply, producer, output, asset_review, source_overlay_uninstalled):
    review = ('# Current sponsor packet\n\n'
              '$750 initial total: $250 setup and $500 placement; $500 affirmative renewal. '
              '720 hours; no automatic charge. Independent comparison publisher, not a clinic.\n\n'
              f"Postal address: {config['postal_address']}\n"
              'Sender partners@glpclinicscout.com. 12 imported verified organizations, not 25/day supply proof. '
              'Any reply, opt-out, bounce, complaint or uncertain send stops the sequence. '
              'Weekdays 06:00 inclusive to 17:00 exclusive Pacific. Cron remains paused.\n\n'
              'Native PDFs use the retained approved renderer template and current installed business economics. '
              'This is not a deployment. Receiver-source preview overlays are uninstalled until reviewed merge. '
              'Independent source and exact native-asset acceptance plus genuine coordinator receiving are required.\n')
    (bundle/REVIEW).write_text(review)
    c.save(bundle/SUPPLY, supply)
    asset_hashes = {n:c.digest(bundle/n) for n in ASSETS}
    art = strict(Path(asset_review).read_bytes()) if asset_review else dict(
        schema='sponsor.current_native_assets_review.v1', status='PENDING_INDEPENDENT_REVIEW',
        reviewer=None, pricing=PRICING, postal_address=config['postal_address'], asset_sha256=asset_hashes)
    c.save(bundle/ASSET_REVIEW, art)
    manifest = {str(p.relative_to(bundle)):c.digest(p) for p in sorted(bundle.rglob('*')) if p.is_file()}
    c.save(bundle/MANIFEST, manifest)
    packet = output/'sponsor-review-packet.zip'
    with zipfile.ZipFile(packet,'w',zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(set(manifest)|{MANIFEST}):
            item=zipfile.ZipInfo(name, (2026,10,9,0,0,0))
            item.compress_type=zipfile.ZIP_DEFLATED
            archive.writestr(item, (bundle/name).read_bytes())
    c.save(output/'NATIVE-CAPTURE.json', cap)
    result=dict(status='CURRENT_PACKET_PREVIEW_REVIEW_REQUIRED', packet_sha256=c.digest(packet),
                native_producer_sha256=c.digest(producer), source_overlay_uninstalled=source_overlay_uninstalled,
                native_capture_head=cap['head'], asset_sha256=asset_hashes,
                organizations=12, pricing=PRICING, postal_address=config['postal_address'],
                sends=0, cron_resumed=False, receiver_ready=False)
    c.save(output/'PACKAGING.json', result)
    return result


def main():
    import glp_sponsor_campaign as c
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build', action='store_true')
    parser.add_argument('--site-root', type=Path)
    parser.add_argument('--capture', type=Path)
    parser.add_argument('--supply', type=Path)
    parser.add_argument('--out', type=Path)
    parser.add_argument('--receiver-checkout', type=Path)
    parser.add_argument('--asset-review', type=Path)
    parser.add_argument('--packet-sha256')
    parser.add_argument('--reviewed-source-head')
    parser.add_argument('--record-received', type=Path)
    args = parser.parse_args()
    if args.build:
        if not all((args.site_root,args.capture,args.supply,args.out)):
            parser.error('build requires site-root, capture, supply and out')
        result=build_packet(c, site_root=args.site_root, capture_path=args.capture,
                            supply_path=args.supply, output=args.out,
                            receiver_checkout=args.receiver_checkout, asset_review=args.asset_review)
    else:
        if not all((args.packet_sha256,args.reviewed_source_head,args.record_received)):
            parser.error('receiving requires exact packet-sha256, reviewed-source-head and record-received')
        result=receive(c,args.packet_sha256,args.reviewed_source_head,args.record_received)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
