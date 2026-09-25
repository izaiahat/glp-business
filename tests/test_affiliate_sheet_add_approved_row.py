"""Contract tests for the add-approved-row Sheet writer mode.

The mode exists because the 2026-08-18 approval backfill audit found the
invisibility bug: Katalys 1575 (System Labs Brand Program) was `approved` in
the portal and had NO Sheet row at all, so every downstream machine - the
wire-backlog worker included - projected from a Sheet that did not know the
approval existed. No governed writer could create a row, so the approval could
never become visible without an ungoverned hand edit.

These tests pin the shape of the hole-fill. It is the only mode allowed to
create a row, so it is also the most tightly bounded:
  * it refuses if ANY existing row already claims the program, on any of three
    identity axes - it creates, it never reconciles,
  * it can only write a pre-live approved status, never a wired/live claim,
  * it always writes Wire-In Ready? and Wired Live? FALSE, leaving promotion to
    the wire-in mode's own gates,
  * the portal capture that proves the approval must exist, hash-match, carry a
    capture timestamp and an approved relationship, and be cited in the row's
    own Notes/Blocker cell.

Pure-function tests only - no Google service, no network, no Sheet write.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path('/home/ubuntu/business')
SCRIPTS = str(ROOT / 'scripts')
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)


from support.google_stubs import google_import_stubs, with_google_import_stubs
import pytest


@pytest.fixture(autouse=True)
def _scoped_google_imports():
    with google_import_stubs():
        yield




@with_google_import_stubs
def load_module():
    spec = importlib.util.spec_from_file_location(
        'add_approved_row_writer', ROOT / 'scripts/affiliate_sheet_batch_writer.py')
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MODULE = load_module()

# A real, already-committed portal capture: the enumeration that proves 1575 is
# approved. Using the genuine artifact keeps the hash gate honest in test too.
CAPTURE_PATH = (
    'reports/glp-clinic-scout/affiliate-machine/'
    'enumeration-20260817T143520Z/katalys-inventory.json'
)
CAPTURE_SHA = hashlib.sha256((ROOT / CAPTURE_PATH).read_bytes()).hexdigest()
CAPTURED_AT = '2026-08-17T14:36:09Z'
NOTE = (
    f'Portal-verified approval with no prior Sheet row (approval backfill audit '
    f'2026-08-18). Katalys relationship=approved per {CAPTURE_PATH} captured '
    f'{CAPTURED_AT}. Not wire-ready until research + tracking link land.'
)


def entry(**overrides):
    row = {
        'program': 'System Labs Brand Program',
        'network': 'Katalys',
        'provider_program_id': '1575',
        'evidence': {
            'portal_capture_path': CAPTURE_PATH,
            'portal_capture_sha256': CAPTURE_SHA,
            'captured_at_utc': CAPTURED_AT,
            'relationship': 'approved',
        },
        'cells': {
            '1': 'TRUE',
            '2': 'System Labs Brand Program',
            '3': 'Prescription telehealth / peptides',
            '4': 'Katalys',
            '5': 'https://app.katalys.com/pub/org/explore/1575',
            '6': 'https://systemlabs.com/',
            '7': 'Katalys Weight Loss + Rx categories; peptide telehealth.',
            '10': 'approved_active_not_wired',
            '11': 'TRUE',
            '19': 'FALSE',
            '20': 'FALSE',
            '23': NOTE,
            '24': '2026-08-18T02:00:00Z',
            '25': 'glp1-weightloss',
        },
    }
    row.update(overrides)
    return row


def plan(rows=None, **overrides):
    document = {
        'schema': 'affiliate-add-approved-row-plan-v1',
        'authority_sha256': 'a' * 64,
        'tracking_clicks': 0,
        'provider_writes': 0,
        'rows': rows if rows is not None else [entry()],
    }
    document.update(overrides)
    return document


def sheet(rows=None):
    """A sheet with a header and 400 rows, all blank unless populated."""
    header = list(MODULE.HEADERS)
    values = [header] + [[''] * len(header) for _ in range(900)]
    for row_num, cells in (rows or {}).items():
        row = MODULE.pad([])
        for column, value in cells.items():
            row[column] = value
        values[row_num - 1] = row
    return values


# --- plan validation ---------------------------------------------------------


def test_valid_plan_passes_and_returns_its_rows():
    rows = MODULE.add_approved_row_rows(plan())
    assert [item['provider_program_id'] for item in rows] == ['1575']


@pytest.mark.parametrize('field,value,expected', [
    ('schema', 'affiliate-network-paused-disposition-plan-v1', 'schema_invalid'),
    ('authority_sha256', 'not-a-hash', 'authority_sha256_invalid'),
    ('tracking_clicks', 1, 'tracking_clicks_must_be_zero'),
    ('provider_writes', 1, 'provider_writes_must_be_zero'),
    ('rows', [], 'rows_empty'),
])
def test_plan_envelope_is_fail_closed(field, value, expected):
    document = plan()
    document[field] = value
    with pytest.raises(RuntimeError, match=expected):
        MODULE.add_approved_row_rows(document)


def test_mode_cannot_write_a_live_or_wired_claim():
    """The whole point of the narrowness: this mode never talks a row up."""
    for status in ('approved_active_wired_live', 'live_wired_verified'):
        document = plan()
        document['rows'][0]['cells']['10'] = status
        with pytest.raises(RuntimeError, match='status_not_allowed'):
            MODULE.add_approved_row_rows(document)

    for column in ('19', '20'):
        document = plan()
        document['rows'][0]['cells'][column] = 'TRUE'
        with pytest.raises(RuntimeError, match='must_not_claim_live'):
            MODULE.add_approved_row_rows(document)


def test_tracking_ready_status_requires_a_real_tracking_link():
    document = plan()
    document['rows'][0]['cells']['10'] = 'approved_active_tracking_ready'
    with pytest.raises(RuntimeError, match='tracking_link_required'):
        MODULE.add_approved_row_rows(document)

    document['rows'][0]['cells']['14'] = 'https://track.revoffers.com/aff_c?offer_id=1575&aff_id=13031'
    assert MODULE.add_approved_row_rows(document)

    # A non-https link is refused even under the not-wired status, where a
    # link is optional: an optional field is still never allowed to be junk.
    document['rows'][0]['cells']['10'] = 'approved_active_not_wired'
    document['rows'][0]['cells']['14'] = 'http://track.revoffers.com/aff_c?offer_id=1575'
    with pytest.raises(RuntimeError, match='tracking_link_invalid'):
        MODULE.add_approved_row_rows(document)


def test_portal_capture_must_exist_and_hash_match():
    document = plan()
    document['rows'][0]['evidence']['portal_capture_path'] = 'reports/does-not-exist.json'
    with pytest.raises(RuntimeError, match='evidence_capture_missing'):
        MODULE.add_approved_row_rows(document)

    document = plan()
    document['rows'][0]['evidence']['portal_capture_sha256'] = 'b' * 64
    with pytest.raises(RuntimeError, match='evidence_capture_hash_drift'):
        MODULE.add_approved_row_rows(document)

    document = plan()
    document['rows'][0]['evidence']['portal_capture_path'] = '/etc/passwd'
    with pytest.raises(RuntimeError, match='evidence_path_invalid'):
        MODULE.add_approved_row_rows(document)


def test_portal_capture_must_carry_timestamp_and_approved_relationship():
    document = plan()
    document['rows'][0]['evidence']['captured_at_utc'] = 'yesterday'
    with pytest.raises(RuntimeError, match='evidence_timestamp_invalid'):
        MODULE.add_approved_row_rows(document)

    document = plan()
    document['rows'][0]['evidence']['relationship'] = 'pending'
    with pytest.raises(RuntimeError, match='evidence_relationship_invalid'):
        MODULE.add_approved_row_rows(document)


def test_note_must_carry_the_capture_path_and_timestamp():
    document = plan()
    document['rows'][0]['cells']['23'] = 'Approved on the portal, trust me.'
    with pytest.raises(RuntimeError, match='note_missing_evidence'):
        MODULE.add_approved_row_rows(document)

    document = plan()
    document['rows'][0]['cells']['23'] = 'too short'
    with pytest.raises(RuntimeError, match='note_too_short'):
        MODULE.add_approved_row_rows(document)


def test_identity_cells_must_agree_with_the_plan_identity():
    document = plan()
    document['rows'][0]['cells']['2'] = 'Some Other Program'
    with pytest.raises(RuntimeError, match='program_cell_mismatch'):
        MODULE.add_approved_row_rows(document)

    document = plan()
    document['rows'][0]['cells']['4'] = 'Awin'
    with pytest.raises(RuntimeError, match='network_cell_mismatch'):
        MODULE.add_approved_row_rows(document)


def test_vertical_and_official_url_are_schema_bound():
    document = plan()
    document['rows'][0]['cells']['25'] = 'not-a-vertical'
    with pytest.raises(RuntimeError, match='vertical_invalid'):
        MODULE.add_approved_row_rows(document)

    document = plan()
    document['rows'][0]['cells']['6'] = 'systemlabs.com'
    with pytest.raises(RuntimeError, match='official_url_invalid'):
        MODULE.add_approved_row_rows(document)


def test_approval_date_column_is_out_of_scope_for_this_mode():
    document = plan()
    document['rows'][0]['cells']['18'] = '2026-08-17'
    with pytest.raises(RuntimeError, match='forbidden_column'):
        MODULE.add_approved_row_rows(document)


def test_duplicate_identity_inside_one_plan_is_refused():
    document = plan(rows=[entry(), entry()])
    with pytest.raises(RuntimeError, match='duplicate_identity'):
        MODULE.add_approved_row_rows(document)


def test_missing_required_columns_are_refused():
    document = plan()
    document['rows'][0]['cells'].pop('23')
    with pytest.raises(RuntimeError, match='columns_invalid'):
        MODULE.add_approved_row_rows(document)


# --- existing-row refusal ----------------------------------------------------


def test_refuses_when_a_row_already_claims_the_program_by_apply_url():
    values = sheet({50: {2: 'System Labs', 4: 'Katalys',
                         5: 'https://app.katalys.com/pub/org/explore/1575'}})
    matches = MODULE.add_approved_row_existing_matches(values, entry())
    assert [item['row'] for item in matches] == [50]
    assert 'apply_url_program_id' in matches[0]['axes']
    with pytest.raises(RuntimeError, match='program_already_present'):
        MODULE.require_add_approved_row_preconditions(values, [entry()], {'System Labs Brand Program': 60})


def test_refuses_when_a_row_already_claims_the_program_by_name():
    values = sheet({324: {2: 'System Labs Brand Program', 4: 'Katalys',
                          10: 'applied_pending_brand_approval'}})
    matches = MODULE.add_approved_row_existing_matches(values, entry())
    assert [item['row'] for item in matches] == [324]
    assert 'program_name' in matches[0]['axes']


def test_refuses_when_a_row_already_carries_the_tracking_link():
    tracking = 'https://track.revoffers.com/aff_c?offer_id=1575&aff_id=13031'
    candidate = entry()
    candidate['cells']['10'] = 'approved_active_tracking_ready'
    candidate['cells']['14'] = tracking
    values = sheet({77: {2: 'Unrelated Name', 4: 'Awin', 14: tracking}})
    matches = MODULE.add_approved_row_existing_matches(values, candidate)
    assert [item['row'] for item in matches] == [77]
    assert 'tracking_link' in matches[0]['axes']


def test_same_name_on_a_different_network_is_not_a_match():
    values = sheet({50: {2: 'System Labs Brand Program', 4: 'Awin'}})
    assert MODULE.add_approved_row_existing_matches(values, entry()) == []


def test_required_marketplace_parent_blocks_create_despite_route_label_difference():
    candidate = {
        'program': 'MaxBounty',
        'network': 'MaxBounty',
        'provider_program_id': 'network-account',
        'cells': {'14': ''},
    }
    values = sheet({470: {2: 'MaxBounty', 4: 'MaxBounty Network',
                          10: 'applied_pending_brand_approval'}})
    matches = MODULE.add_approved_row_existing_matches(values, candidate)
    assert matches == [{'row': 470, 'program': 'MaxBounty',
                        'axes': ['canonical_parent_row']}]
    with pytest.raises(RuntimeError, match='program_already_present'):
        MODULE.require_add_approved_row_preconditions(
            values, [candidate], {'MaxBounty': 702})


def test_blank_sheet_accepts_the_add_and_target_row_must_be_blank():
    values = sheet()
    MODULE.require_add_approved_row_preconditions(
        values, [entry()], {'System Labs Brand Program': 800})

    occupied = sheet({800: {2: 'Someone Else', 4: 'Impact'}})
    with pytest.raises(RuntimeError, match='target_row_not_blank'):
        MODULE.require_add_approved_row_preconditions(
            occupied, [entry()], {'System Labs Brand Program': 800})


def test_rerun_after_a_successful_write_is_idempotent_not_a_duplicate():
    """The row this mode created is a match - but only at its own target row."""
    # Post-write the sheet returns real booleans in the checkbox columns, which
    # is exactly what add_approved_row_write_value put there.
    written = {
        int(column): MODULE.add_approved_row_write_value(int(column), value)
        for column, value in entry()['cells'].items()
    }
    values = sheet({800: written})
    MODULE.require_add_approved_row_preconditions(
        values, [entry()], {'System Labs Brand Program': 800})
    assert MODULE.add_approved_row_changed_cells(
        values, [{**entry(), 'row': 800}]) == []


# --- cell typing -------------------------------------------------------------


def test_all_four_checkbox_columns_are_written_as_real_booleans():
    for column in MODULE.ADD_APPROVED_ROW_CHECKBOX_COLUMNS:
        assert MODULE.add_approved_row_write_value(column, 'TRUE') is True
        assert MODULE.add_approved_row_write_value(column, 'FALSE') is False
    assert MODULE.add_approved_row_write_value(10, 'approved_active_not_wired') == \
        'approved_active_not_wired'


def test_a_string_in_a_checkbox_cell_is_never_certified_as_a_match():
    assert MODULE.add_approved_row_cell_equal(19, False, 'FALSE') is True
    assert MODULE.add_approved_row_cell_equal(19, '', 'FALSE') is True
    assert MODULE.add_approved_row_cell_equal(19, 'FALSE', 'FALSE') is False
    assert MODULE.add_approved_row_cell_equal(19, True, 'FALSE') is False


def test_changed_cells_tolerates_a_blank_target_row():
    """wire_in_changed_cells refuses a blank row; this mode requires one."""
    values = sheet()
    changes = MODULE.add_approved_row_changed_cells(values, [{**entry(), 'row': 800}])
    # Every planned cell is a change except the two FALSE checkboxes, which a
    # blank cell already satisfies - Sheets renders an unchecked box as blank.
    assert {column for _, column, _ in changes} == {
        int(column) for column in entry()['cells']
    } - {19, 20}
    assert {row for row, _, _ in changes} == {800}


def test_plan_never_declares_provider_or_tracking_side_effects():
    document = plan()
    assert document['tracking_clicks'] == 0
    assert document['provider_writes'] == 0
    serialized = json.dumps(document)
    assert 'aff_c' not in serialized or 'track.revoffers.com' in serialized


def test_mode_is_registered_in_the_cli_and_lock_path():
    source = (ROOT / 'scripts/affiliate_sheet_batch_writer.py').read_text()
    assert '--add-approved-row' in source
    assert 'run_add_approved_row(Path(args.add_approved_row), out, args.apply)' in source
    # It must sit inside the same exclusive-lock branch as the other modes.
    lock_branch = source[source.index('or args.add_approved_row'):]
    assert 'portfolio_sheet_apply_lock_busy' in lock_branch


def test_existing_modes_still_cannot_create_a_row():
    """Guard the invariant this mode is the deliberate single exception to."""
    source = (ROOT / 'scripts/affiliate_sheet_batch_writer.py').read_text()
    for runner in ('run_wire_in', 'run_approved_unlinked_disposition',
                   'run_network_paused_disposition'):
        start = source.index(f'def {runner}(')
        end = source.index('\ndef ', start + 1)
        body = source[start:end]
        assert 'wire_in_changed_cells' in body
        assert 'copyPaste' not in body, runner
        assert 'appendDimension' not in body, runner


def test_deep_copy_of_plan_is_not_mutated_by_validation():
    document = plan()
    snapshot = copy.deepcopy(document)
    MODULE.add_approved_row_rows(document)
    assert document == snapshot


def test_tonum_same_rail_label_variant_is_one_relationship():
    """`Tonum AWIN` must bind row 677 `Tonum (US)` on canonical Awin."""
    candidate = entry(
        program='Tonum AWIN', network='Awin', provider_program_id='122076',
        cells={'14': ''},
    )
    values = sheet({677: {
        2: 'Tonum (US)', 4: 'AWIN',
        5: 'https://ui.awin.com/awin/affiliate/2979387/merchant-profile/122076',
        10: 'approved_active_not_wired',
    }})
    matches = MODULE.add_approved_row_existing_matches(values, candidate)
    assert matches == [{'row': 677, 'program': 'Tonum (US)', 'axes': ['apply_url_program_id']}]


@pytest.mark.parametrize('candidate,existing', [
    (
        {'program': 'HealthRX', 'network': 'Awin', 'provider_program_id': '129105',
         'cells': {'14': ''}},
        {2: 'HealthRX', 4: 'Katalys', 5: 'https://app.katalys.com/pub/org/explore/1630'},
    ),
    (
        {'program': 'Hume Health US', 'network': 'Awin', 'provider_program_id': '102781',
         'cells': {'14': ''}},
        {2: 'Hume Health', 4: 'CJ Affiliate', 5: 'https://members.cj.com/member/publisher/advertiser/7865389'},
    ),
])
def test_same_provider_on_a_different_rail_is_a_distinct_relationship(candidate, existing):
    """HealthRX/Hume prove provider×marketplace identity, not provider alone."""
    assert MODULE.add_approved_row_existing_matches(sheet({87: existing}), candidate) == []
