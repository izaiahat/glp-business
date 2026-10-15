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


HOME = Path('/home/ubuntu/.hermes')
PROC = Path('/proc')


def process_identity(pid):
    """Kernel launch metadata, not mutable receiver environment overrides."""
    base = PROC / str(pid)
    stat = (base/'stat').read_text().rsplit(')', 1)[1].split()
    env = dict(x.split('=', 1) for x in (base/'environ').read_bytes().decode().split('\0') if '=' in x)
    return {'pid': int(pid), 'ppid': int(stat[1]), 'start': stat[19],
            'argv': (base/'cmdline').read_bytes().decode().rstrip('\0').split('\0'), 'env': env}


def live_coordinator(expected=None):
    """Resolve exact receipt custody, or the caller's kernel TUI ancestry.

    Multiple independent default-profile TUIs are normal. Receiving selects its
    own terminal ancestor; a scheduled reader selects the full existing receipt
    identity. Neither mutable caller exports nor a globally unique TUI grant
    authority. A restart/session switch still invalidates receiving.

    This is local same-UID custody, not cryptographic authentication.
    """
    import sqlite3
    found = []
    for base in PROC.iterdir():
        if not base.name.isdigit():
            continue
        try:
            row = process_identity(base.name)
            if row['argv'] != [str(HOME/'hermes-agent/venv/bin/python'), '-m', 'tui_gateway.entry']:
                continue
            if row['env'].get('HERMES_HOME') != str(HOME):
                continue
            active = Path(row['env']['HERMES_TUI_ACTIVE_SESSION_FILE'])
            if active.is_symlink() or active.parent.resolve() != (HOME/'cache/scratch').resolve():
                continue
            session = strict(active.read_text())['session_id']
            with sqlite3.connect(f'file:{HOME / "state.db"}?mode=ro', uri=True) as db:
                entry = db.execute('SELECT parent_session_id, source FROM sessions WHERE id=?', (session,)).fetchone()
            if not entry or entry[0] is not None or entry[1] not in ('cli', 'tui', 'claude-code'):
                continue
            if process_identity(base.name) != row or strict(active.read_text())['session_id'] != session:
                continue
            found.append({'pid': row['pid'], 'start': row['start'], 'session_id': session,
                          'active_file': str(active)})
        except (OSError, ValueError, KeyError, sqlite3.Error):
            continue
    if expected is not None:
        found = [row for row in found if row == expected]
    else:
        # Select only the original terminal launch beneath a verified TUI.
        # A leaf exporting its parent's session cannot alter /proc/environ.
        ancestors = []
        pid, seen = os.getppid(), set()
        try:
            while pid > 1 and pid not in seen:
                seen.add(pid)
                row = process_identity(pid)
                ancestors.append(row)
                if any(row['ppid'] == candidate['pid'] for candidate in found):
                    break  # No need to read unrelated privileged launcher ancestry.
                pid = row['ppid']
            found = [candidate for candidate in found if any(
                row['ppid'] == candidate['pid']
                and row['env'].get('HERMES_SESSION_ID') == candidate['session_id']
                and row['env'].get('HERMES_TUI_ACTIVE_SESSION_FILE') == candidate['active_file']
                for row in ancestors)]
            if any(process_identity(row['pid']) != row for row in ancestors):
                found = []
        except (OSError, ValueError, KeyError):
            found = []
    if len(found) != 1:
        raise RuntimeError('current_coordinator_execution_required:live_TUI_ambiguous_or_missing')
    return found[0]


AUTOMATION_DECISION = 'ops/linear/approvals/OPERATOR-DECISION-20261011-grilling-round1.md'
OPERATOR_SCHEMA = 'sponsor.recorded_operator_receiving.v1'
OPERATOR_SOURCE_REVIEW = 'reports/ops/fable-plan-2-20261009/lanes/S4/SOURCE-REVIEW.json'


def recorded_operator_authority(c):
    """Recorded operator decisions authorize unattended work, not a TUI epilogue.

    Exact commercial/art and independent installed-source review are separate
    gates below. This is same-UID local evidence, not cryptographic identity.
    Changes to either recorded decision invalidate an existing receipt.
    """
    required = {
        DECISION: ('6. GLP-328: GO at 25/day',
                   '15. Outbound sponsor/outreach email sends only Mon-Fri 06:00-17:00 America/Los_Angeles.'),
        AUTOMATION_DECISION: ('5. Sponsor outreach: up to 25 first-touch/day, weekdays 06:00–17:00 PT; automatic follow-ups day 4 and day 10 in the same window, stopped on reply. Operator must be notified of every reply via the existing inbox monitor so replies are handled.',),
    }
    decisions = {}
    for name, phrases in required.items():
        path = c.ROOT/name
        try:
            text = path.read_text()
            if path.is_symlink() or any(not any(line.startswith(phrase) for line in text.splitlines()) for phrase in phrases):
                raise ValueError('scope_missing')
            decisions[name] = c.digest(path)
        except (OSError, ValueError) as exc:
            raise RuntimeError('recorded_operator_decision_missing:'+name) from exc
    return dict(actor='Izaiah Tilton', decisions=decisions,
                timezone='America/Los_Angeles', weekdays=[0,1,2,3,4],
                start_hour=6, end_hour_exclusive=17, new_provider_daily_cap=25,
                follow_up_days=[4,10], stop_on_reply=True, suppression_required=True)


def current_authority(c, coordinator=None):
    plan = strict((c.ROOT / PLAN).read_text())
    if plan.get('schema') != 'glp-fable-plan-2-v1' or plan.get('executor_keys', {}).get('coordinator') != 'TUI or auto-coordinator cron acting on fleet-inbox rows':
        raise RuntimeError('current_coordinator_authority_missing')
    decision = (c.ROOT / DECISION).read_text()
    if 'GO at 25/day' not in decision or 'Mon-Fri 06:00-17:00 America/Los_Angeles' not in decision:
        raise RuntimeError('current_operator_decision_missing')
    return {'plan_sha256': c.digest(c.ROOT / PLAN), 'decision_sha256': c.digest(c.ROOT / DECISION),
            'coordinator': live_coordinator(coordinator)}


def require_coordinator_execution():
    try:
        current = live_coordinator()
        session = current['session_id']
        if (os.environ.get('LINEAR_ROLE') != 'orchestrator'
            or os.environ.get('HERMES_SESSION_ID') != session
            or os.environ.get('HERMES_TUI_ACTIVE_SESSION_FILE') != current['active_file']):
            raise ValueError('caller_identity')
        # Find the actual terminal launched by the backend. Shell exports in a
        # leaf command cannot rewrite its original kernel environment.
        pid, seen = os.getppid(), set()
        while pid > 1 and pid not in seen:
            seen.add(pid)
            row = process_identity(pid)
            if row['ppid'] == current['pid']:
                if (row['env'].get('HERMES_SESSION_ID') != session
                    or row['env'].get('HERMES_TUI_ACTIVE_SESSION_FILE') != current['active_file']
                    or live_coordinator() != current):
                    raise ValueError('terminal_identity')
                return session
            pid = row['ppid']
    except (OSError, ValueError, KeyError):
        pass
    raise RuntimeError('current_coordinator_execution_required')


def packet_binding(c, packet, config, *, require_installed_manifest=True):
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
        if require_installed_manifest and c.digest(c.ROOT / MANIFEST) != hashlib.sha256(archive.read(MANIFEST)).hexdigest():
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


SOURCE_REVIEW = 'reports/ops/fable-plan-2-20261009/lanes/S1/SOURCE-REVIEW.json'
SUCCESSOR_SOURCE_REVIEW = 'reports/ops/fable-plan-2-20261009/lanes/RV-S2/RECEIVER-SOURCE-REVIEW.json'
READINESS_PATH = 'scripts/glp_sponsor_readiness.py'
# PR513's exact budget-guard transition, not permission to renew arbitrary code.
READINESS_RENEWAL = ('4f988082d932ffb1da57dce951827cfd7cfc7fd068c4964864f171583acf3284',
                     '6fe29410f4e61dea28ec1af580461e07e8aba6f008022183a695e9b0c4c480bf')
RENEWABLE_SOURCES = {'scripts/glp_sponsor_campaign.py', 'scripts/glp_sponsor_current_receiving.py',
                     'inbox_control_plane/sponsor_replies.py', 'inbox_control_plane/control_plane.py', 'scripts/firecrawl_client.py'}


def readiness_renewal(before, after):
    if before.get(READINESS_PATH) == after.get(READINESS_PATH):
        return
    pair = tuple(hashlib.sha256(members.get(READINESS_PATH, b'')).hexdigest()
                 for members in (before, after))
    if pair != READINESS_RENEWAL:
        raise RuntimeError('source_packet_readiness_drift')


def refresh_source_packet(c, predecessor_sha256, output):
    """Build-only renewal: retained art/copy/authority, exact installed sources.

    No installation or receiving. The new receiver requires independent source
    acceptance and a genuine coordinator act with an exact predecessor receipt.
    """
    import io
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise RuntimeError('source_packet_output_exists')
    original = (c.ROOT/PACKET).read_bytes()
    if hashlib.sha256(original).hexdigest() != predecessor_sha256:
        raise RuntimeError('source_packet_predecessor_changed')
    with zipfile.ZipFile(io.BytesIO(original)) as archive:
        names = archive.namelist()
        allowed = set(SOURCE_PATHS)|set(ASSETS)|{MANIFEST,REVIEW,SUPPLY,ASSET_REVIEW}
        if len(names) != len(set(names)) or set(names) != allowed:
            raise RuntimeError('current_packet_members_invalid')
        members = {name:archive.read(name) for name in names}
    manifest = strict(members[MANIFEST])
    if set(manifest) != allowed-{MANIFEST} or any(
        hashlib.sha256(members[name]).hexdigest()!=pin for name,pin in manifest.items()):
        raise RuntimeError('current_packet_manifest_invalid')
    readiness_renewal(members, {READINESS_PATH:(c.ROOT/READINESS_PATH).read_bytes()})
    for name in SOURCE_PATHS:
        installed = (c.ROOT/name).read_bytes()
        if name not in RENEWABLE_SOURCES|{READINESS_PATH} and installed != members[name]:
            raise RuntimeError('source_packet_nontechnical_drift:'+name)
        members[name] = installed
    members[MANIFEST] = (json.dumps({name:hashlib.sha256(blob).hexdigest()
        for name,blob in members.items() if name!=MANIFEST},indent=2,sort_keys=True)+'\n').encode()
    with output.open('xb') as stream, zipfile.ZipFile(stream,'w',zipfile.ZIP_DEFLATED) as archive:
        for name,blob in sorted(members.items()):
            archive.writestr(name,blob)
    return dict(status='UNRECEIVED_SOURCE_ONLY_PACKET',packet_sha256=c.digest(output),sends=0)


def reviewed_source_head(c, *, recorded_operator=False):
    successor = (c.ROOT/SUCCESSOR_SOURCE_REVIEW).exists()
    review_path = OPERATOR_SOURCE_REVIEW if recorded_operator else (SUCCESSOR_SOURCE_REVIEW if successor else SOURCE_REVIEW)
    review = strict((c.ROOT/review_path).read_bytes())
    pins = {'campaign_sender_sha256':'scripts/glp_sponsor_campaign.py',
            'current_receiver_sha256':'scripts/glp_sponsor_current_receiving.py',
            'nuimage_sender_sha256':'scripts/glp_sponsor_nuimage_reply.py',
            'readiness_sha256':READINESS_PATH}
    if successor or recorded_operator:
        pins['sponsor_replies_sha256'] = 'inbox_control_plane/sponsor_replies.py'
    if recorded_operator:
        pins['canonical_inbox_sha256'] = 'inbox_control_plane/control_plane.py'
        pins['firecrawl_client_sha256'] = 'scripts/firecrawl_client.py'
    head = review.get('reviewed_source_head', '')
    if (review.get('status') != 'PASS' or not review.get('reviewer') or review['reviewer'] in ('S1', 'RV-S2', 'S3', 'S4')
        or not re.fullmatch('[0-9a-f]{40}', head)
        or any(review.get(key) != c.digest(c.ROOT/name) for key, name in pins.items())):
        raise RuntimeError('independent_installed_source_review_required')
    return head


def approval_ready(c, config):
    path = c.STATE / RECEIPT
    if not path.exists():
        return None
    receipt = strict(path.read_text())
    packet = c.ROOT / PACKET
    fields = {'schema','actor','coordinator_authority','receiving_session_id','packet_sha256',
              'decision','reviewed_source_head','installed_bindings'}
    sha = c.digest(packet)
    # Recorded-operator custody is stable across TUI shutdown/restarts. Legacy
    # receipts remain legacy; no automatic conversion or copied approval.
    if receipt.get('schema') == OPERATOR_SCHEMA:
        fields = {'schema','actor','operator_authority','packet_sha256','decision','reviewed_source_head','installed_bindings'}
        if (set(receipt) != fields or receipt.get('actor') != 'Izaiah Tilton'
            or receipt.get('operator_authority') != recorded_operator_authority(c)
            or receipt.get('packet_sha256') != sha or config.get('packet_sha256') != sha
            or receipt.get('decision') != 'RECEIVE RECORDED OPERATOR SPONSOR PACKET '+sha
            or receipt.get('reviewed_source_head') != reviewed_source_head(c,recorded_operator=True)
            or strict((c.STATE/'approval.json').read_bytes()) != receipt
            or receipt.get('installed_bindings') != packet_binding(c,packet,config)):
            raise RuntimeError('recorded_operator_receiving_invalid')
        return True
    # Legacy receiving still requires genuine live coordinator evidence.
    coordinator = receipt.get('coordinator_authority', {}).get('coordinator')
    if not isinstance(coordinator, dict):
        raise RuntimeError('current_coordinator_receiving_invalid')
    authority = current_authority(c, coordinator)
    if (set(receipt) != fields or receipt.get('schema') != SCHEMA or receipt.get('actor') != 'coordinator'
        or receipt.get('coordinator_authority') != authority
        or receipt.get('receiving_session_id') != authority['coordinator']['session_id']
        or receipt.get('packet_sha256') != sha or config.get('packet_sha256') != sha
        or receipt.get('decision') != 'RECEIVE CURRENT SPONSOR PACKET ' + sha
        or receipt.get('reviewed_source_head') != reviewed_source_head(c)):
        raise RuntimeError('current_coordinator_receiving_invalid')
    if strict((c.STATE/'approval.json').read_bytes()) != receipt:
        raise RuntimeError('current_coordinator_receiving_invalid:approval_binding_disagree')
    if receipt.get('installed_bindings') != packet_binding(c, packet, config):
        raise RuntimeError('current_coordinator_receiving_invalid')
    return True


def receive(c, packet_sha256, source_head, result, *, successor_packet=None, replace_receipt_sha256=None, recorded_operator=False):
    # Unattended renewal is explicitly requested and re-reads recorded operator
    # authority. It never borrows a TUI identity or modifies its scratch file.
    session = None if recorded_operator else require_coordinator_execution()
    if recorded_operator:
        recorded_operator_authority(c)
    import fcntl
    import io
    import shutil
    import sync_from_repo as sync
    with sync.source_lock(c.ROOT), (c.STATE / 'run.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        renewing = successor_packet is not None and replace_receipt_sha256 is not None
        if (successor_packet is None) != (replace_receipt_sha256 is None):
            raise RuntimeError('current_renewal_requires_packet_and_predecessor')
        if result.exists() or ((c.STATE / RECEIPT).exists() and not renewing):
            raise RuntimeError('current_receipt_exists_inspect_do_not_overwrite')
        packet = c.ROOT / PACKET
        # Authenticate the consumed buffer, not a path re-read after validation.
        candidate = Path(successor_packet).read_bytes() if renewing else packet.read_bytes()
        if hashlib.sha256(candidate).hexdigest() != packet_sha256 or source_head != (reviewed_source_head(c,recorded_operator=True) if recorded_operator else reviewed_source_head(c)):
            raise RuntimeError('current_receiving_exact_head_or_packet_changed')
        config = c.load(c.CONFIG)
        config.update(send_enabled=True, packet_sha256=packet_sha256)
        bindings = packet_binding(c, io.BytesIO(candidate), config, require_installed_manifest=not renewing)
        if renewing:
            previous = (c.STATE/RECEIPT).read_bytes()
            old = strict(previous)
            activation = strict((c.STATE/'activation.json').read_bytes())
            if (hashlib.sha256(previous).hexdigest()!=replace_receipt_sha256
                or strict((c.STATE/'approval.json').read_bytes())!=old
                or old.get('schema') not in (SCHEMA,OPERATOR_SCHEMA) or old.get('packet_sha256')!=c.digest(packet)
                or activation!={'send_enabled':True,'packet_sha256':old['packet_sha256']}):
                raise RuntimeError('current_renewal_predecessor_changed')
            before = c.technical_packet_members(packet)
            after = c.technical_packet_members(io.BytesIO(candidate))
            if (not recorded_operator and any(before.get(name)!=after.get(name)
                for name in ('inbox_control_plane/control_plane.py','scripts/firecrawl_client.py'))):
                raise RuntimeError('current_renewal_extended_source_requires_recorded_review')
            readiness_renewal(before, after)
            if set(before)!=set(after) or any(before[name]!=after[name]
                for name in before if name not in RENEWABLE_SOURCES|{MANIFEST,READINESS_PATH}):
                raise RuntimeError('current_renewal_outward_bytes_changed')
        if recorded_operator:
            receipt = dict(schema=OPERATOR_SCHEMA, actor='Izaiah Tilton', operator_authority=recorded_operator_authority(c),
                           packet_sha256=packet_sha256, decision='RECEIVE RECORDED OPERATOR SPONSOR PACKET '+packet_sha256,
                           reviewed_source_head=source_head, installed_bindings=bindings)
        else:
            receipt = dict(schema=SCHEMA, actor='coordinator', coordinator_authority=current_authority(c),
                           receiving_session_id=session, packet_sha256=packet_sha256,
                           decision='RECEIVE CURRENT SPONSOR PACKET '+packet_sha256,
                           reviewed_source_head=source_head, installed_bindings=bindings)
            if receipt['coordinator_authority']['coordinator']['session_id'] != session:
                raise RuntimeError('current_coordinator_changed_during_receiving')
        preimages=result.parent/'canonical-preimages'
        preimages.mkdir(parents=True,exist_ok=True)
        originals=[c.STATE/name for name in ('approval.json','activation.json',RECEIPT)]
        if renewing:originals += [packet,c.ROOT/MANIFEST]
        for original in originals:
            if original.is_file():
                backup=preimages/(original.name+'.'+c.digest(original))
                if not backup.exists():shutil.copyfile(original,backup)
                if c.digest(backup)!=c.digest(original):raise RuntimeError('current_preimage_archive_changed')
        if renewing:
            # A partial write is fail-closed against the old receipt; never
            # replay an ambiguous renewal without inspecting these preimages.
            packet.write_bytes(candidate)
            with zipfile.ZipFile(io.BytesIO(candidate)) as archive:
                (c.ROOT/MANIFEST).write_bytes(archive.read(MANIFEST))
            packet_binding(c, packet, config)
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
    parser.add_argument('--refresh-source-packet', type=Path, help='Build-only source renewal output ZIP')
    parser.add_argument('--predecessor-packet-sha256')
    parser.add_argument('--successor-packet', type=Path)
    parser.add_argument('--replace-receipt-sha256')
    parser.add_argument('--recorded-operator',action='store_true',help='Explicit recorded-decision custody, no live TUI prerequisite')
    args = parser.parse_args()
    if args.refresh_source_packet:
        if not args.predecessor_packet_sha256:
            parser.error('source refresh requires predecessor-packet-sha256')
        result=refresh_source_packet(c,args.predecessor_packet_sha256,args.refresh_source_packet)
    elif args.build:
        if not all((args.site_root,args.capture,args.supply,args.out)):
            parser.error('build requires site-root, capture, supply and out')
        result=build_packet(c, site_root=args.site_root, capture_path=args.capture,
                            supply_path=args.supply, output=args.out,
                            receiver_checkout=args.receiver_checkout, asset_review=args.asset_review)
    else:
        if not all((args.packet_sha256,args.reviewed_source_head,args.record_received)):
            parser.error('receiving requires exact packet-sha256, reviewed-source-head and record-received')
        result=receive(c,args.packet_sha256,args.reviewed_source_head,args.record_received,
                       successor_packet=args.successor_packet, replace_receipt_sha256=args.replace_receipt_sha256,
                       recorded_operator=args.recorded_operator)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
