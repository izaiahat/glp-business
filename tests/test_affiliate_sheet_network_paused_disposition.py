"""Contract tests for the network-paused disposition Sheet writer mode.

The mode exists because the Katalys pause of 2026-08-17 proved a hole: no
governed writer could move a LIVE WIRED row DOWN, so the Sheet kept claiming
three paused programs were live. These tests pin the shape of the hole-fill:
it may only go one direction, only from a genuinely live row, only with the
network's own evidence attached, and it may never blank the tracking link.

Pure-function tests only - no Google service, no network, no Sheet write.
"""

from __future__ import annotations

import copy
import importlib.util
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
        'network_paused_writer', ROOT / 'scripts/affiliate_sheet_batch_writer.py')
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MODULE = load_module()

MESSAGE_ID = '1a01015cf8123a0d'
EVIDENCE_PATH = 'reports/glp-clinic-scout/affiliate-machine/katalys-pause-20260817/index.json'
NOTE = (f'Network paused this program. Katalys pause notice message_id={MESSAGE_ID}; '
        f'evidence {EVIDENCE_PATH}; site surfaces already removed.')


def plan(**overrides):
    entry = {
        'row': 103,
        'program': 'Synergy Rx',
        'evidence': {'message_id': MESSAGE_ID, 'path': EVIDENCE_PATH},
        'cells': {
            '10': 'paused_network_program_paused',
            '19': 'FALSE',
            '20': 'FALSE',
            '21': 'link_retired_network_program_paused',
            '23': NOTE,
            '24': '2026-08-18T01:00:00Z',
        },
    }
    document = {
        'schema': 'affiliate-network-paused-disposition-plan-v1',
        'authority_sha256': 'a' * 64,
        'tracking_clicks': 0,
        'provider_writes': 0,
        'rows': [entry],
    }
    document.update(overrides)
    return document


def live_sheet(cell_overrides=None):
    """A sheet whose row 103 is exactly the live, linked, GLP row the plan names."""
    header = list(MODULE.HEADERS)
    values = [header] + [[''] * len(header) for _ in range(102)]
    row = MODULE.pad([])
    row[2] = 'Synergy Rx'
    row[4] = 'Katalys'
    row[10] = 'live_wired_verified'
    row[11] = True
    row[14] = 'https://track.revoffers.com/aff_c?offer_id=1520&aff_id=13031'
    row[19] = True
    row[20] = True
    row[21] = 'live_direct_link_verified_20260725'
    row[23] = 'prior note'
    row[24] = '2026-07-25T08:24:17Z'
    row[25] = 'glp1-weightloss'
    for column, value in (cell_overrides or {}).items():
        row[int(column)] = value
    values[102] = row
    return values


# --- plan validation --------------------------------------------------------


def test_valid_plan_is_accepted():
    rows = MODULE.network_paused_disposition_rows(plan())
    assert [entry['row'] for entry in rows] == [103]


@pytest.mark.parametrize('field,value,message', (
    ('schema', 'affiliate-approved-unlinked-disposition-plan-v1', 'schema_invalid'),
    ('authority_sha256', 'not-a-sha', 'authority_sha256_invalid'),
    ('tracking_clicks', 1, 'tracking_clicks_must_be_zero'),
    ('provider_writes', 1, 'provider_writes_must_be_zero'),
    ('rows', [], 'rows_empty'),
))
def test_plan_envelope_fails_closed(field, value, message):
    with pytest.raises(RuntimeError, match=message):
        MODULE.network_paused_disposition_rows(plan(**{field: value}))


def test_mode_can_never_talk_a_row_up_into_approval():
    for status in ('approved_active_wired_live', 'live_wired_verified',
                   'approved_active_tracking_ready'):
        document = plan()
        document['rows'][0]['cells']['10'] = status
        with pytest.raises(RuntimeError, match='status_not_allowed'):
            MODULE.network_paused_disposition_rows(document)


def test_claim_columns_must_be_retracted_not_kept():
    for column in ('19', '20'):
        document = plan()
        document['rows'][0]['cells'][column] = 'TRUE'
        with pytest.raises(RuntimeError, match='must_retract_claim'):
            MODULE.network_paused_disposition_rows(document)


def test_tracking_link_column_is_not_mutable_by_this_mode():
    assert 14 not in MODULE.NETWORK_PAUSED_DISPOSITION_MUTABLE_COLUMNS
    document = plan()
    document['rows'][0]['cells']['14'] = ''
    with pytest.raises(RuntimeError, match='columns_invalid'):
        MODULE.network_paused_disposition_rows(document)


def test_required_columns_must_all_be_present():
    for column in ('10', '19', '20', '23', '24'):
        document = plan()
        document['rows'][0]['cells'].pop(column)
        with pytest.raises(RuntimeError, match='columns_invalid'):
            MODULE.network_paused_disposition_rows(document)


def test_link_qa_value_is_bounded():
    document = plan()
    document['rows'][0]['cells']['21'] = 'live_direct_link_verified_20260725'
    with pytest.raises(RuntimeError, match='link_qa_not_allowed'):
        MODULE.network_paused_disposition_rows(document)


def test_evidence_reference_is_mandatory_and_must_reach_the_sheet():
    missing = plan()
    missing['rows'][0].pop('evidence')
    with pytest.raises(RuntimeError, match='evidence_missing'):
        MODULE.network_paused_disposition_rows(missing)

    no_id = plan()
    no_id['rows'][0]['evidence']['message_id'] = ''
    with pytest.raises(RuntimeError, match='evidence_message_id_missing'):
        MODULE.network_paused_disposition_rows(no_id)

    bad_path = plan()
    bad_path['rows'][0]['evidence']['path'] = '/tmp/somewhere.json'
    with pytest.raises(RuntimeError, match='evidence_path_invalid'):
        MODULE.network_paused_disposition_rows(bad_path)

    unreferenced = plan()
    unreferenced['rows'][0]['cells']['23'] = (
        'Network paused this program; no receipt attached to this note.')
    with pytest.raises(RuntimeError, match='blocker_missing_evidence'):
        MODULE.network_paused_disposition_rows(unreferenced)


def test_duplicate_and_invalid_row_numbers_fail_closed():
    duplicated = plan()
    duplicated['rows'].append(copy.deepcopy(duplicated['rows'][0]))
    with pytest.raises(RuntimeError, match='duplicate_row'):
        MODULE.network_paused_disposition_rows(duplicated)

    header_row = plan()
    header_row['rows'][0]['row'] = 1
    with pytest.raises(RuntimeError, match='row_number_invalid'):
        MODULE.network_paused_disposition_rows(header_row)


# --- live-sheet preconditions ----------------------------------------------


def test_preconditions_accept_the_exact_live_linked_glp_row():
    rows = MODULE.network_paused_disposition_rows(plan())
    MODULE.require_network_paused_disposition_preconditions(live_sheet(), rows)


def test_refuses_a_row_that_was_never_live():
    rows = MODULE.network_paused_disposition_rows(plan())
    with pytest.raises(RuntimeError, match='current_status_not_live'):
        MODULE.require_network_paused_disposition_preconditions(
            live_sheet({10: 'applied_pending_brand_approval'}), rows)


def test_refuses_a_row_whose_wired_live_flag_is_already_false():
    rows = MODULE.network_paused_disposition_rows(plan())
    with pytest.raises(RuntimeError, match='row_not_wired_live'):
        MODULE.require_network_paused_disposition_preconditions(
            live_sheet({20: False}), rows)


def test_refuses_a_link_dark_row_that_belongs_to_the_unlinked_mode():
    rows = MODULE.network_paused_disposition_rows(plan())
    with pytest.raises(RuntimeError, match='tracking_link_absent'):
        MODULE.require_network_paused_disposition_preconditions(
            live_sheet({14: ''}), rows)


def test_refuses_a_name_mismatch_and_a_non_glp_vertical():
    rows = MODULE.network_paused_disposition_rows(plan())
    with pytest.raises(RuntimeError, match='program_mismatch'):
        MODULE.require_network_paused_disposition_preconditions(
            live_sheet({2: 'Some Other Program'}), rows)
    with pytest.raises(RuntimeError, match='vertical_not_glp1'):
        MODULE.require_network_paused_disposition_preconditions(
            live_sheet({25: 'mens-health-ed'}), rows)


def test_preconditions_are_re_runnable_after_the_write():
    """Post-write the row is no longer live - that must satisfy, not trip."""
    rows = MODULE.network_paused_disposition_rows(plan())
    after = live_sheet({10: 'paused_network_program_paused', 19: False, 20: False,
                          21: 'link_retired_network_program_paused'})
    MODULE.require_network_paused_disposition_preconditions(after, rows)
    assert MODULE.wire_in_changed_cells(after, [
        {'row': 103, 'cells': {k: v for k, v in rows[0]['cells'].items() if k in ('10', '19', '20', '21')}}
    ]) == []


def test_second_run_plans_zero_writes_for_an_applied_row():
    rows = MODULE.network_paused_disposition_rows(plan())
    applied = live_sheet({
        10: 'paused_network_program_paused', 19: False, 20: False,
        21: 'link_retired_network_program_paused', 23: NOTE, 24: '2026-08-18T01:00:00Z',
    })
    assert MODULE.wire_in_changed_cells(applied, rows) == []


def test_checkbox_columns_are_written_as_real_booleans():
    assert MODULE.wire_in_write_value(19, 'FALSE') is False
    assert MODULE.wire_in_write_value(20, 'FALSE') is False
    # A string "FALSE" left in a checkbox cell is a schema regression, so the
    # readback comparison must reject it rather than call it a match.
    assert MODULE.wire_in_cell_equal(20, 'FALSE', 'FALSE') is False
    assert MODULE.wire_in_cell_equal(20, False, 'FALSE') is True


# --- blank typed-cell repair (the gate that fails every writer closed) ------


def rail_row(overrides=None):
    row = MODULE.pad([])
    row[1] = True
    row[2] = 'GoAffPro'
    row[3] = 'Affiliate Marketplace'
    row[4] = 'Network signup'
    row[11] = True
    row[19] = True
    row[20] = ''
    row[25] = ''
    for column, value in (overrides or {}).items():
        row[int(column)] = value
    return [list(MODULE.HEADERS), row]


BLANK_CHECKBOX_ISSUE = {'row': 2, 'column_index': 20, 'header': 'Wired Live?',
                        'kind': 'checkbox_value_not_boolean', 'observed_type': 'BLANK'}
BLANK_VERTICAL_ISSUE = {'row': 2, 'column_index': 25, 'header': 'Vertical',
                        'kind': 'vertical_value_not_allowed', 'observed': 'BLANK'}


def test_blank_checkbox_repairs_to_false_and_rail_vertical_to_general_wellness():
    repairs = MODULE.blank_schema_cell_repairs(
        rail_row(), [BLANK_CHECKBOX_ISSUE, BLANK_VERTICAL_ISSUE])
    assert repairs == [(2, 20, False), (2, 25, 'general-wellness')]


def test_blank_repair_refuses_to_invent_a_vertical_for_a_merchant_program_row():
    program = rail_row({3: 'Prescription telehealth', 4: 'Katalys'})
    with pytest.raises(RuntimeError, match='vertical_not_a_marketplace_rail'):
        MODULE.blank_schema_cell_repairs(program, [BLANK_VERTICAL_ISSUE])


def test_blank_repair_refuses_a_rail_row_that_carries_a_link_or_status():
    with pytest.raises(RuntimeError, match='vertical_not_a_marketplace_rail'):
        MODULE.blank_schema_cell_repairs(
            rail_row({14: 'https://track.example.invalid/x'}), [BLANK_VERTICAL_ISSUE])
    with pytest.raises(RuntimeError, match='vertical_not_a_marketplace_rail'):
        MODULE.blank_schema_cell_repairs(
            rail_row({10: 'approved_active_wired_live'}), [BLANK_VERTICAL_ISSUE])


def test_blank_repair_never_overwrites_a_cell_that_is_not_blank():
    with pytest.raises(RuntimeError, match='checkbox_not_blank'):
        MODULE.blank_schema_cell_repairs(
            rail_row({20: True}), [dict(BLANK_CHECKBOX_ISSUE, observed_type='boolValue')])
    with pytest.raises(RuntimeError, match='vertical_not_blank'):
        MODULE.blank_schema_cell_repairs(
            rail_row({25: 'not-a-vertical'}),
            [dict(BLANK_VERTICAL_ISSUE, observed='not-a-vertical')])


def test_blank_repair_refuses_any_other_schema_issue_kind():
    with pytest.raises(RuntimeError, match='issue_kind_not_repairable'):
        MODULE.blank_schema_cell_repairs(
            rail_row(), [{'row': 2, 'kind': 'checkbox_validation_not_boolean',
                          'column_index': 20}])
