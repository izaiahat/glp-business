"""Metadata contracts and a registered single-writer identity repair CLI.

Header names are authoritative. Fit classification is an explicit hash-bound
ruling over reported catalogue facts, never a clinical inference from a sector.
"""
from __future__ import annotations
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from affiliate_sheet_schema_guard import EXPECTED_HEADERS, VERTICAL_ALLOWED_VALUES
from affiliate_provider_row_scope import provider_row_scope

FIT_KIND = 'provider_fit_reclassification'
SLUG_KIND = 'provider_slug_provisioning'
SLUG_CORRECTION_KIND = 'provider_slug_correction'
MAXBOUNTY_IDENTITY_KIND = 'maxbounty_identity_category_repair'
SLUG_ONLY_KINDS = frozenset({SLUG_KIND, SLUG_CORRECTION_KIND})
KINDS = frozenset({FIT_KIND, MAXBOUNTY_IDENTITY_KIND, *SLUG_ONLY_KINDS})
COL = {name: i for i, name in enumerate(EXPECTED_HEADERS)}
FIT_COLUMNS = tuple(COL[n] for n in ('Status', 'Vertical', 'Last Checked UTC', 'Notes / Blocker'))
SLUG_COLUMNS = (COL['SEO Slug'],)
SLUG_APPROVED_STATUSES = frozenset({
    'approved_active_not_wired', 'approved_active_tracking_ready',
    'applied_approved_not_wired', 'approved_active_wired_live', 'live_wired_verified',
    'approved_no_positive_commission_configured',
})


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def cell(row, name):
    return row[COL[name]]


def padded(row):
    if len(row) > len(EXPECTED_HEADERS):
        raise RuntimeError('provider_metadata_row_width_drift')
    return list(row) + [''] * (len(EXPECTED_HEADERS) - len(row))


def timestamp(value):
    if not isinstance(value, str) or not value.endswith('Z'):
        raise RuntimeError('provider_metadata_timestamp_invalid')
    try:
        datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise RuntimeError('provider_metadata_timestamp_invalid') from exc
    return value


def read_bound(root, relative, sha):
    root = Path(root).resolve()
    path = (root / str(relative)).resolve()
    if not str(relative).startswith('reports/') or not path.is_relative_to(root):
        raise RuntimeError('provider_metadata_evidence_path')
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise RuntimeError('provider_metadata_evidence_unreadable') from exc
    if not re.fullmatch('[a-f0-9]{64}', str(sha)) or hashlib.sha256(raw).hexdigest() != sha:
        raise RuntimeError('provider_metadata_evidence_hash_drift')
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise RuntimeError('provider_metadata_evidence_json_invalid') from exc
    return value


def excluded(row):
    # Also catch a decorated alias whose official domain identifies Fella.
    from affiliate_payout_policy import PERMANENT_EXCLUSIONS
    name = str(cell(row, 'Program / Network')).casefold()
    host = (urlsplit(str(cell(row, 'Official URL'))).hostname or '').casefold()
    tokens = {name, re.split(r'\s*[|—–:]\s*', name)[0], host, host.removeprefix('www.').split('.')[0]}
    return 'fella' in name or host == 'fellahealth.com' or host.endswith('.fellahealth.com') or bool(tokens & PERMANENT_EXCLUSIONS)


def slug_eligible(row_number, row, approved_statuses):
    row = padded(row)
    return bool(provider_row_scope(row_number, row)['eligible'] and not excluded(row)
                and cell(row, 'Status') in approved_statuses
                and cell(row, 'Vertical') == 'glp1-weightloss'
                and not str(cell(row, 'SEO Slug') or '').strip())


def valid_slug(slug):
    return isinstance(slug, str) and bool(re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*-review', slug))


def derive_slug(values, row_number, row):
    from glp_maxbounty_lane import official_hostname, provider_review_slug
    host = official_hostname(str(cell(row, 'Official URL') or ''))
    matches = set()
    if host:
        for i, raw in enumerate(values[1:], 2):
            if i == row_number:
                continue
            other = padded(raw)
            slug = str(cell(other, 'SEO Slug') or '')
            if slug and official_hostname(str(cell(other, 'Official URL') or '')) == host:
                if not valid_slug(slug):
                    raise RuntimeError('provider_slug_existing_same_domain_invalid')
                matches.add(slug)
    if len(matches) > 1:
        raise RuntimeError('provider_slug_same_domain_ambiguous')
    slug = next(iter(matches)) if matches else provider_review_slug(str(cell(row, 'Program / Network')))
    if not valid_slug(slug):
        raise RuntimeError('provider_slug_derived_invalid')
    for i, raw in enumerate(values[1:], 2):
        other = padded(raw)
        if i != row_number and cell(other, 'SEO Slug') == slug:
            if not host or official_hostname(str(cell(other, 'Official URL') or '')) != host:
                raise RuntimeError('provider_slug_collision_other_identity')
    return slug, 'same_official_domain_existing_site_slug' if matches else 'derived_from_programme_name'


def load_manifest(kind, evidence, root):
    manifest = read_bound(root, evidence.get('metadata_manifest_path'), evidence.get('metadata_manifest_sha256'))
    if not isinstance(manifest, dict) or manifest.get('schema') != 'glp.provider_metadata.v1' or manifest.get('kind') != kind:
        raise RuntimeError('provider_metadata_manifest_schema')
    if manifest.get('headers') != list(EXPECTED_HEADERS):
        raise RuntimeError('provider_metadata_header_drift')
    if type(manifest.get('sheet_row')) is not int or manifest['sheet_row'] < 2:
        raise RuntimeError('provider_metadata_row_invalid')
    source = manifest.get('source_row')
    if not isinstance(source, list) or len(source) != len(EXPECTED_HEADERS):
        raise RuntimeError('provider_metadata_source_projection')
    if not cell(source, 'Program / Network') or excluded(source):
        raise RuntimeError('provider_metadata_excluded_or_blank')
    if kind == FIT_KIND:
        validate_fit_ruling(manifest, root)
    if kind == SLUG_CORRECTION_KIND:
        validate_slug_correction(manifest, root)
    if kind == MAXBOUNTY_IDENTITY_KIND:
        validate_maxbounty_identity_repair(manifest, root)
    return manifest


def validate_maxbounty_identity_repair(manifest, root):
    """Bind an approved campaign to an authorized row; no relationship mutation."""
    source = manifest['source_row']
    authority = read_bound(root, manifest.get('authority_path'), manifest.get('authority_sha256'))
    capture = read_bound(root, manifest.get('capture_path'), manifest.get('capture_sha256'))
    program_id = manifest.get('network_program_id')
    if not isinstance(program_id, str) or not re.fullmatch(r'[0-9]+', program_id):
        raise RuntimeError('maxbounty_identity_repair_program_id_invalid')
    url = f'https://affiliates.maxbounty.com/campaign/{program_id}'
    category = manifest.get('category')
    expected = {'task': manifest.get('task', 'S33'), 'sheet_row': manifest['sheet_row'], 'network': 'MaxBounty',
                'network_program_id': program_id, 'apply_url': url, 'category': category,
                'source_row_sha256': digest(source),
                'authority_type': 'explicit_operator_identity_category_repair'}
    if not isinstance(authority, dict) or any(authority.get(k) != v for k, v in expected.items()):
        raise RuntimeError('maxbounty_identity_repair_authority_mismatch')
    held_bistromd = (manifest.get('task') == 'GLP-250'
                       and manifest['sheet_row'] == 800 and program_id == '25152'
                       and cell(source, 'Program / Network') == 'BistroMD - Meal Delivery - CPS (US)'
                       and cell(source, 'Status') == 'hold_sister_site_not_live'
                       and cell(source, 'Vertical') == 'womens-health'
                       and cell(source, 'Category') == category)
    if (manifest.get('apply_url') != url or not isinstance(category, str) or not category.strip()
            or cell(source, 'Network / Route') != 'MaxBounty'
            or cell(source, 'Apply URL') not in {'', url}
            or (cell(source, 'Status') not in SLUG_APPROVED_STATUSES | {'approved', 'applied_pending_brand_approval'}
                and not held_bistromd)
            or excluded(source)
            or str(cell(source, 'Wired Live?')).upper() == 'TRUE'
            or (held_bistromd and str(cell(source, 'Tracking Link / Deep Link') or '').strip())):
        raise RuntimeError('maxbounty_identity_repair_outside_authorized_mapping')
    # The approval chain's authenticated, immutable targeted capture stores one
    # programme under `programs`, unlike the earlier S33 singular API capture.
    targeted = capture.get('schema') == 'glp.maxbounty.targeted-approval-capture.v1'
    if targeted:
        programs = capture.get('programs')
        if (not isinstance(programs, list) or len(programs) != 1
                or capture.get('sheet_row') != manifest['sheet_row']
                or capture.get('event_id') != 'evt_f278de1567bb0dbcf8c04a7c7bd0'
                or f"Portal capture={manifest.get('capture_path')}." not in str(cell(source, 'Notes / Blocker'))
                or not held_bistromd):
            raise RuntimeError('maxbounty_identity_repair_capture_mismatch')
        program = programs[0]
        state = capture.get('campaign_detail_state') or {}
        capture_id = state.get('program_id')
    else:
        program = capture.get('program') or {}
        state = capture.get('state') or {}
        capture_id = capture.get('program_id')
    if (capture_id != program_id or program.get('program_id') != program_id
            or program.get('name') != cell(source, 'Program / Network')
            or program.get('official_url') != cell(source, 'Official URL')
            or program.get('detail_url') != url or state.get('detail_url') != url
            or state.get('campaign_api_status') != 200
            or (state.get('campaign_api_relationship') or {}).get('affiliate_campaign_status') != 'Approved'):
        raise RuntimeError('maxbounty_identity_repair_capture_mismatch')


def validate_slug_correction(manifest, root):
    """Q57's explicit mapping only; not a general nonblank normalization gate."""
    source = manifest['source_row']
    authority = read_bound(root, manifest.get('authority_path'), manifest.get('authority_sha256'))
    expected = {'task': 'Q57', 'network': 'Awin', 'network_program_id': '128761',
                'registry_id': 'awin-128761', 'sheet_row': 346,
                'before': 'provider/coivas', 'after': 'coivas',
                'source_row_sha256': digest(source),
                'authority_type': 'explicit_operator_slug_correction'}
    if not isinstance(authority, dict) or any(authority.get(k) != v for k, v in expected.items()):
        raise RuntimeError('provider_slug_correction_authority_mismatch')
    if (manifest['sheet_row'] != 346 or manifest.get('network_program_id') != '128761'
            or manifest.get('registry_id') != 'awin-128761' or manifest.get('seo_slug') != 'coivas'
            or cell(source, 'Program / Network') != 'Coivas'
            or cell(source, 'Network / Route') != 'Awin'
            or cell(source, 'SEO Slug') != 'provider/coivas'
            or cell(source, 'Official URL') != 'https://www.coivas.com/'
            or cell(source, 'Apply URL') != 'https://ui.awin.com/awin/affiliate/2979387/merchant-profile/128761'):
        raise RuntimeError('provider_slug_correction_outside_authorized_mapping')
    if (cell(source, 'Status') not in SLUG_APPROVED_STATUSES
            or cell(source, 'Vertical') != 'glp1-weightloss'
            or not provider_row_scope(346, source)['eligible'] or excluded(source)):
        raise RuntimeError('provider_slug_correction_source_ineligible')


def validate_fit_ruling(manifest, root):
    ruling = read_bound(root, manifest.get('ruling_path'), manifest.get('ruling_sha256'))
    if not isinstance(ruling, dict):
        raise RuntimeError('provider_fit_ruling_schema')
    capture = read_bound(root, ruling.get('capture_path'), ruling.get('capture_sha256'))
    source = manifest['source_row']
    if ruling.get('schema') != 'glp.provider_fit_ruling.v1':
        raise RuntimeError('provider_fit_ruling_schema')
    # Source is a captured network catalogue record, not an arbitrary narrative.
    if not isinstance(capture, dict) or capture.get('schema') != 'glp.network_catalogue_capture.v1' or capture.get('source_type') != 'first_party_network_catalogue':
        raise RuntimeError('provider_fit_capture_source_type')
    timestamp(capture.get('captured_at_utc'))
    timestamp(ruling.get('assessed_at_utc'))
    source_url = capture.get('source_url')
    if not isinstance(source_url, str) or not source_url.startswith('https://'):
        raise RuntimeError('provider_fit_capture_source_url')
    record = capture.get('record') or {}
    if not isinstance(record, dict):
        raise RuntimeError('provider_fit_catalogue_record_type')
    raw_catalogue = read_bound(root, capture.get('raw_capture_path'), capture.get('raw_capture_sha256'))
    pointer = capture.get('record_pointer')
    if not isinstance(pointer, str) or not pointer.startswith('/'):
        raise RuntimeError('provider_fit_raw_record_pointer')
    try:
        raw_record = raw_catalogue
        for part in pointer[1:].split('/'):
            part = part.replace('~1', '/').replace('~0', '~')
            raw_record = raw_record[int(part)] if isinstance(raw_record, list) else raw_record[part]
    except (KeyError, ValueError, IndexError, TypeError) as exc:
        raise RuntimeError('provider_fit_raw_record_missing') from exc
    if not isinstance(raw_record, dict):
        raise RuntimeError('provider_fit_raw_record_type')
    # Explicit adapter for Awin's actual catalogue field names. Other networks
    # must supply the canonical record schema; never infer their field mapping.
    if record.get('network') == 'Awin':
        if urlsplit(source_url).hostname != 'api.awin.com':
            raise RuntimeError('provider_fit_awin_source_host')
        projection = {'network': 'Awin', 'program_id': str(raw_record.get('id')),
                      'program_name': raw_record.get('name'), 'official_url': raw_record.get('displayUrl'),
                      'primary_sector': raw_record.get('primarySector'), 'description': raw_record.get('description')}
    else:
        projection = raw_record
    if record != projection:
        raise RuntimeError('provider_fit_record_not_raw_projection')
    required = {'network', 'program_id', 'program_name', 'official_url', 'primary_sector', 'description'}
    if any(not isinstance(record.get(k), str) or not record[k].strip() for k in required):
        raise RuntimeError('provider_fit_catalogue_fields_missing')
    for key, header in [('network', 'Network / Route'), ('program_name', 'Program / Network'), ('official_url', 'Official URL')]:
        if record[key] != cell(source, header):
            raise RuntimeError('provider_fit_catalogue_identity_mismatch:' + key)
    # The network-scoped id must occur as a standalone token in the row's
    # pre-existing Apply URL, not in the tracking link or a proposed new cell.
    if not re.search(r'(?<![A-Za-z0-9])' + re.escape(record['program_id']) + r'(?![A-Za-z0-9])', str(cell(source, 'Apply URL'))):
        raise RuntimeError('provider_fit_program_id_not_bound_to_apply_url')
    if ruling.get('network') != record['network'] or ruling.get('program_id') != record['program_id']:
        raise RuntimeError('provider_fit_ruling_identity_mismatch')
    if ruling.get('sheet_row') != manifest['sheet_row'] or ruling.get('source_row_sha256') != digest(source):
        raise RuntimeError('provider_fit_ruling_source_mismatch')
    if ruling.get('vertical') not in VERTICAL_ALLOWED_VALUES or ruling.get('vertical') == 'glp1-weightloss':
        raise RuntimeError('provider_fit_reclassification_may_not_grant_core_fit')
    expected_status = ('hold_out_of_scope' if ruling['vertical'] == 'out-of-scope'
                       else 'hold_sister_site_not_live')
    if ruling.get('status') != expected_status:
        raise RuntimeError('provider_fit_status_vertical_disagree')
    if ruling.get('provenance') != 'Reported information' or ruling.get('clinical_claims_inferred') is not False:
        raise RuntimeError('provider_fit_ruling_provenance')
    reason = ruling.get('fit_reason')
    if not isinstance(reason, str) or len(reason.strip()) < 24:
        raise RuntimeError('provider_fit_reason_missing')
    # Explicit adjudication is required; sector/name keywords are not a verdict.
    if not str(ruling.get('scope_ruling_authority') or '').strip():
        raise RuntimeError('provider_fit_scope_ruling_authority_missing')
    quotes = ruling.get('evidence_quotes')
    if not isinstance(quotes, dict) or set(quotes) != {'primary_sector', 'description'}:
        raise RuntimeError('provider_fit_evidence_quotes_missing')
    if any(not isinstance(q, str) or not q.strip() or q not in record[k] for k, q in quotes.items()):
        raise RuntimeError('provider_fit_quote_not_in_capture')
    manifest['_ruling'] = ruling


def manifest_cells(kind, manifest):
    if kind == MAXBOUNTY_IDENTITY_KIND:
        return {str(COL['Category']): manifest['category'],
                str(COL['Apply URL']): manifest['apply_url']}
    if kind == SLUG_CORRECTION_KIND:
        if manifest.get('seo_slug') != 'coivas':
            raise RuntimeError('provider_slug_correction_target_invalid')
        return {str(COL['SEO Slug']): 'coivas'}
    if kind == SLUG_KIND:
        if not valid_slug(manifest.get('seo_slug')):
            raise RuntimeError('provider_slug_target_invalid')
        return {str(COL['SEO Slug']): manifest['seo_slug']}
    ruling = manifest['_ruling']
    historical = str(cell(manifest['source_row'], 'Notes / Blocker') or '')
    note = (f"Reported information: provider fit reclassified to {ruling['vertical']}; "
            f"basis={ruling['fit_reason']}; "
            f"ruling={manifest['ruling_path']}; assessed={ruling['assessed_at_utc']}. "
            "Fit Reason, tracking and live flags unchanged.")
    return {str(COL['Vertical']): ruling['vertical'], str(COL['Status']): ruling['status'],
            str(COL['Last Checked UTC']): ruling['assessed_at_utc'],
            str(COL['Notes / Blocker']): historical + ('\n' if historical else '') + note}


def validate_entry(kind, entry, root, values=None, approved_statuses=()):
    manifest = load_manifest(kind, entry['evidence'], root)
    source = manifest['source_row']
    if manifest['sheet_row'] != entry.get('row') or cell(source, 'Program / Network') != entry.get('program'):
        raise RuntimeError('provider_metadata_entry_identity_mismatch')
    expected = manifest_cells(kind, manifest)
    if entry.get('cells') != expected:
        raise RuntimeError('provider_metadata_cells_not_exact_projection')
    if values is None:
        return
    if not values or values[0] != list(EXPECTED_HEADERS):
        raise RuntimeError('provider_metadata_header_drift')
    index = manifest['sheet_row']
    if index > len(values):
        raise RuntimeError('provider_metadata_target_missing')
    actual = padded(values[index - 1])
    post = list(source)
    for k, v in expected.items():
        post[int(k)] = v
    # Full source or full exact postimage only, never arbitrary partial replay.
    if actual != source and actual != post:
        raise RuntimeError('provider_metadata_source_drift')
    if kind == SLUG_KIND:
        if not slug_eligible(index, source, approved_statuses):
            raise RuntimeError('provider_slug_source_ineligible')
        slug, basis = derive_slug(values, index, source)
        if slug != manifest['seo_slug'] or basis != manifest.get('slug_basis'):
            raise RuntimeError('provider_slug_derivation_drift')
    elif kind not in {SLUG_CORRECTION_KIND, MAXBOUNTY_IDENTITY_KIND}:
        scope = provider_row_scope(index, source)
        if scope['row_class'] in {'blank', 'permanently_excluded', 'marketplace_parent', 'network_account'}:
            raise RuntimeError('provider_fit_not_provider_row')
        # An existing legal/operator/other hold cannot be renamed into a lesser
        # classification hold. Exact existing target is allowed for replay only.
        status = str(cell(source, 'Status'))
        fit_sources = {'approved_active_not_wired', 'approved_active_tracking_ready',
                       'applied_approved_not_wired'}
        if status not in fit_sources and status != manifest['_ruling']['status']:
            raise RuntimeError('provider_fit_existing_hold_preserved')
        if status in {'live_wired_verified', 'approved_active_wired_live'} or str(cell(source, 'Wired Live?')).upper() == 'TRUE':
            raise RuntimeError('provider_fit_live_source_forbidden')


def queue_fact(kind, manifest, path, sha, *, queued_at):
    # Identity is the immutable manifest, not a date or mutable gate/link state.
    source = manifest['source_row']
    return {'update_id': 'provider-metadata-' + kind + '-' + sha[:32], 'kind': kind,
            'network': cell(source, 'Network / Route'), 'program_id': '',
            'program_name': cell(source, 'Program / Network'), 'sheet_row': str(manifest['sheet_row']),
            'evidence': json.dumps({'metadata_manifest_path': str(path), 'metadata_manifest_sha256': sha}, sort_keys=True),
            'status': 'queued', 'queued_by': 'glp_wire_in_ready_queue', 'queued_at_utc': queued_at,
            'resolved_at_utc': '', 'note': 'Metadata only; no approval, gate or live claim.'}


def main(argv=None):
    import argparse
    from datetime import timezone

    parser = argparse.ArgumentParser(description='Repair an approved MaxBounty campaign identity through the registered single writer.')
    parser.add_argument('--row', type=int, required=True, help='Sheet row to repair')
    parser.add_argument('--program-id', required=True, help='Approved MaxBounty campaign ID')
    parser.add_argument('--manifest', type=Path, required=True, help='Existing hash-bound identity repair manifest')
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    path = args.manifest.resolve()
    relative = str(path.relative_to(root))
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    evidence = {'metadata_manifest_path': relative, 'metadata_manifest_sha256': sha}
    manifest = load_manifest(MAXBOUNTY_IDENTITY_KIND, evidence, root)
    if manifest['sheet_row'] != args.row or manifest['network_program_id'] != args.program_id:
        raise RuntimeError('maxbounty_identity_repair_cli_identity_mismatch')
    entry = {'row': args.row, 'program': cell(manifest['source_row'], 'Program / Network'),
             'evidence': evidence, 'cells': manifest_cells(MAXBOUNTY_IDENTITY_KIND, manifest)}
    from affiliate_approval_auto_reconciler import drain_single_writer_queue_row
    from glp_wire_in_ready_queue import read_live_sheet
    validate_entry(MAXBOUNTY_IDENTITY_KIND, entry, root, values=read_live_sheet())
    fact = queue_fact(MAXBOUNTY_IDENTITY_KIND, manifest, relative, sha,
                      queued_at=datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'))
    result = drain_single_writer_queue_row(fact, False)
    actual = padded(read_live_sheet()[args.row - 1])
    post = list(manifest['source_row'])
    for column, value in entry['cells'].items():
        post[int(column)] = value
    if actual != post:
        raise RuntimeError('maxbounty_identity_repair_post_readback_mismatch')
    print(json.dumps({'sheet_row': args.row, 'program_id': args.program_id,
                      'identity_readback_verified': True, 'single_writer': result}, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
