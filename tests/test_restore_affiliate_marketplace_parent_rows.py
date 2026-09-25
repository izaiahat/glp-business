from __future__ import annotations

import copy
import json
import sys
from argparse import Namespace
from pathlib import Path

import pytest

ROOT = Path('/home/ubuntu/business')
sys.path.insert(0, str(ROOT / 'scripts'))
import affiliate_sheet_schema_guard as guard  # noqa: E402
import restore_affiliate_marketplace_parent_rows as restore  # noqa: E402


def typed_row(values: list) -> dict:
    cells: list[dict] = []
    checkbox_rule = {
        'condition': {'type': 'BOOLEAN'},
        'strict': True,
        'showCustomUi': True,
    }
    vertical_rule = {
        'condition': {
            'type': 'ONE_OF_LIST',
            'values': [{'userEnteredValue': value} for value in guard.VERTICAL_ALLOWED_VALUES],
        },
        'strict': True,
        'showCustomUi': True,
    }
    for column, value in enumerate(restore.pad(values)):
        cell: dict = {}
        entered = restore.user_value(value)
        if entered:
            cell['userEnteredValue'] = entered
            cell['formattedValue'] = str(value).upper() if isinstance(value, bool) else str(value)
        if column in guard.CHECKBOX_COLUMNS:
            cell['dataValidation'] = copy.deepcopy(checkbox_rule)
        if column == guard.DATE_COLUMN:
            cell['effectiveFormat'] = {'numberFormat': dict(guard.DATE_NUMBER_FORMAT)}
        if column == guard.VERTICAL_COLUMN:
            cell['dataValidation'] = copy.deepcopy(vertical_rule)
        cells.append(cell)
    return {'values': cells}


def business_row(name: str, *, vertical: str = 'general-wellness') -> list:
    row = [''] * restore.EXPECTED_COLUMN_COUNT
    row[0] = 'A'
    row[1] = False
    row[2] = name
    row[3] = 'Network rail' if name in guard.REQUIRED_MARKETPLACE_PARENT_NAMES else 'fixture'
    row[4] = name
    if name in guard.REQUIRED_MARKETPLACE_PARENT_NAMES:
        row[6] = f'https://example.test/{name.lower().replace(" ", "-")}'
    row[10] = 'fixture'
    row[11] = False
    row[19] = False
    row[20] = False
    if name in guard.REQUIRED_MARKETPLACE_PARENT_NAMES:
        row[22] = 'Affiliate disclosure required.'
        row[23] = 'Operational marketplace access metadata.'
        row[24] = '2026-08-11T00:00:00+00:00'
    row[25] = vertical
    return row


def synthetic_preimage() -> tuple[list[list], dict]:
    # values.get and GridData end at the populated row 776. The restoration
    # appends exactly two new grid rows before writing parent rows 777/778.
    values = [list(guard.EXPECTED_HEADERS)] + [[] for _ in range(775)]
    values[1] = business_row('CoreAge Rx GLP-1 [1632]', vertical='glp1-weightloss')
    values[2] = business_row('T2D [direct-T2D]', vertical='glp1-weightloss')
    for name, row_number in restore.PRESERVED_PARENT_ROWS.items():
        values[row_number - 1] = business_row(name)
    values[775] = business_row('Fixture row 776')
    values = restore.normalized_api_values(values)

    row_data = [{'values': [
        {
            'userEnteredValue': {'stringValue': header},
            'formattedValue': header,
        }
        for header in guard.EXPECTED_HEADERS
    ]}]
    for row_number in range(2, restore.EXPECTED_PRE_GRID_ROW_COUNT + 1):
        row = restore.row_at(values, row_number)
        row_data.append(typed_row(row) if not restore.row_is_strictly_blank(row) else {'values': []})
    full = {
        'spreadsheetId': restore.SHEET_ID,
        'sheets': [{
            'properties': {
                'sheetId': 67006763,
                'title': restore.TAB,
                'index': 0,
                'sheetType': 'GRID',
                'gridProperties': {
                    'rowCount': restore.EXPECTED_PRE_GRID_ROW_COUNT,
                    'columnCount': restore.EXPECTED_COLUMN_COUNT,
                },
            },
            'basicFilter': {
                'range': {
                    'sheetId': 67006763,
                    'startRowIndex': 0,
                    'endRowIndex': restore.EXPECTED_CURRENT_MAX_POPULATED_ROW,
                    'startColumnIndex': 0,
                    'endColumnIndex': restore.EXPECTED_COLUMN_COUNT,
                },
            },
            'merges': [{
                'sheetId': 67006763,
                'startRowIndex': 100,
                'endRowIndex': 101,
                'startColumnIndex': 7,
                'endColumnIndex': 9,
            }],
            'data': [{'startRow': 0, 'startColumn': 0, 'rowData': row_data}],
        }],
    }
    return values, full


def synthetic_postimage(
    before_values: list[list], before_full: dict, sources: dict[str, dict],
) -> tuple[list[list], dict]:
    values = restore.expected_post_values(before_values, sources)
    full = copy.deepcopy(before_full)
    sheet = restore.selected_sheet(full)
    rows = sheet['data'][0]['rowData']
    rows.extend({'values': []} for _ in range(
        restore.REQUIRED_GRID_ROW_COUNT - restore.EXPECTED_PRE_GRID_ROW_COUNT
    ))
    sheet['properties']['gridProperties']['rowCount'] = restore.REQUIRED_GRID_ROW_COUNT
    for source in sources.values():
        rows[int(source['target_row']) - 1] = typed_row(source['values'])
    filter_after = restore.extended_filter(restore.selected_sheet(before_full))
    assert filter_after is not None
    sheet['basicFilter'] = filter_after
    return values, full


def build_fixture_plan():
    sources = restore.load_parking_sources()
    values, full = synthetic_preimage()
    plan = restore.build_restoration_plan(
        values,
        full,
        sources,
        expected_precondition_values_sha256=restore.values_sha256(values),
    )
    return sources, values, full, plan


def test_exact_applied_parking_sources_are_hash_verified_and_lineage_bound() -> None:
    sources = restore.load_parking_sources()
    assert set(sources) == set(restore.RESTORED_PARENT_NAMES)
    assert len(sources) == 22
    for name, source in sources.items():
        assert len(source['values']) == 27
        assert source['values'][2] == name
        assert restore.row_hash(source['values']) == source['row_sha256']
        assert source['applied_record_line'] > source['lineage_record_line']


def test_parking_source_loader_rejects_any_ledger_byte_drift(tmp_path: Path) -> None:
    tampered = tmp_path / 'parking.jsonl'
    tampered.write_bytes(restore.PARKING.read_bytes() + b'\n')
    with pytest.raises(RuntimeError, match='parking_ledger_sha256_mismatch'):
        restore.load_parking_sources(tampered)


def test_plan_restores_exact_rows_with_two_row_append_and_binds_rows_2_3() -> None:
    sources, values, full, plan = build_fixture_plan()
    assert plan['status'] == 'PASS'
    assert plan['operation_count'] == 22
    assert plan['required_parent_count_after'] == 30
    assert plan['preconditions']['grid_row_count'] == 776
    assert plan['mutation_contract']['grid_row_appends'] == 2
    assert plan['preconditions']['maximum_populated_row'] == 776
    assert plan['preconditions']['targets_blank'] == list(restore.TARGET_ROWS)
    assert plan['preconditions']['rows_2_3_sha256'] == {
        '2': restore.row_hash(restore.row_at(values, 2)),
        '3': restore.row_hash(restore.row_at(values, 3)),
    }
    assert {(item['parent'], item['target_row']) for item in plan['operations']} == {
        (name, target) for name, _, target in restore.RESTORATION_LAYOUT
    }
    assert plan['plan_sha256'] == restore.sha256_bytes(restore.canonical({
        key: value for key, value in plan.items() if key != 'plan_sha256'
    }))
    assert sources['Impact.com']['target_row'] == 777
    assert sources['CJ Affiliate']['target_row'] == 778


def test_plan_fails_closed_on_unexpected_pre_restoration_grid_size() -> None:
    sources = restore.load_parking_sources()
    values, full = synthetic_preimage()
    restore.selected_sheet(full)['properties']['gridProperties']['rowCount'] = 777
    with pytest.raises(RuntimeError, match='pre_restoration_grid_row_count_mismatch:777:776'):
        restore.build_restoration_plan(
            values,
            full,
            sources,
            expected_precondition_values_sha256=restore.values_sha256(values),
        )


def test_plan_fails_closed_if_a_target_is_not_blank() -> None:
    sources = restore.load_parking_sources()
    values, full = synthetic_preimage()
    values[3] = business_row('Unexpected row 4')
    full_rows = restore.selected_sheet(full)['data'][0]['rowData']
    full_rows[3] = typed_row(values[3])
    with pytest.raises(RuntimeError, match='restoration_target_not_blank:4'):
        restore.build_restoration_plan(
            values,
            full,
            sources,
            expected_precondition_values_sha256=restore.values_sha256(values),
        )


def test_mutation_is_one_batch_with_exact_append_cell_schema_and_filter_requests() -> None:
    sources, _, full, plan = build_fixture_plan()
    requests = restore.build_mutation_requests(plan, sources, full)
    assert len(requests) == 1 + 22 + (22 * 6) + 1
    assert all(len(request) == 1 for request in requests)
    assert {next(iter(request)) for request in requests} <= {
        'appendDimension', 'updateCells', 'setDataValidation', 'repeatCell', 'setBasicFilter',
    }
    assert requests[0] == {'appendDimension': {
        'sheetId': 67006763,
        'dimension': 'ROWS',
        'length': 2,
    }}
    assert not any('insertDimension' in request or 'updateSheetProperties' in request for request in requests)
    value_updates = [
        request for request in requests
        if request.get('updateCells', {}).get('fields') == 'userEnteredValue'
    ]
    assert len(value_updates) == 22
    assert sorted(
        item['updateCells']['range']['startRowIndex'] + 1 for item in value_updates
    ) == list(restore.TARGET_ROWS)
    impact = next(
        item for item in value_updates
        if item['updateCells']['range']['startRowIndex'] + 1 == 777
    )
    assert impact['updateCells']['rows'][0]['values'][2]['userEnteredValue'] == {
        'stringValue': 'Impact.com',
    }


def test_exact_postimage_passes_and_detects_unrelated_grid_drift() -> None:
    sources, values, full, _ = build_fixture_plan()
    post_values, post_full = synthetic_postimage(values, full, sources)
    result = restore.verify_postimage(values, post_values, full, post_full, sources)
    assert result['status'] == 'PASS'
    assert result['restored_rows_exact'] == 22
    assert result['required_marketplace_parents_exactly_once'] == 30
    assert result['rows_2_3_unchanged'] is True
    assert result['typed_schema']['status'] == 'PASS'

    drifted = copy.deepcopy(post_full)
    row_2 = restore.selected_sheet(drifted)['data'][0]['rowData'][1]['values'][2]
    row_2['note'] = 'unrelated mutation'
    with pytest.raises(RuntimeError, match='unrelated_grid_cell_drift'):
        restore.verify_postimage(values, post_values, full, drifted, sources)


def test_postimage_accepts_equivalent_split_griddata_blocks_after_append() -> None:
    sources, values, full, _ = build_fixture_plan()
    post_values, post_full = synthetic_postimage(values, full, sources)
    sheet = restore.selected_sheet(post_full)
    all_rows = sheet['data'][0]['rowData']
    sheet['data'] = [
        {'startRow': 0, 'startColumn': 0, 'rowData': all_rows[:500]},
        {'startRow': 500, 'startColumn': 0, 'rowData': all_rows[500:]},
    ]

    result = restore.verify_postimage(values, post_values, full, post_full, sources)

    assert result['status'] == 'PASS'
    assert result['typed_schema']['status'] == 'PASS'


def test_postimage_detects_unrelated_value_drift() -> None:
    sources, values, full, _ = build_fixture_plan()
    post_values, post_full = synthetic_postimage(values, full, sources)
    post_values[99] = business_row('Unrelated changed row 100')
    with pytest.raises(RuntimeError, match='full_values_mismatch'):
        restore.verify_postimage(values, post_values, full, post_full, sources)


def test_rollback_uses_full_grid_preimage_fields_and_removes_only_appended_rows() -> None:
    _, _, full, plan = build_fixture_plan()
    requests = restore.rollback_requests(full, plan)
    assert len(requests) == 24
    assert sorted(
        request['updateCells']['range']['startRowIndex'] + 1
        for request in requests if 'updateCells' in request
    ) == list(restore.TARGET_ROWS)
    assert all(
        request['updateCells']['fields'] == 'userEnteredValue,userEnteredFormat,dataValidation'
        for request in requests if 'updateCells' in request
    )
    assert {next(iter(request)) for request in requests} == {
        'updateCells', 'setBasicFilter', 'deleteDimension',
    }
    assert requests[-1] == {'deleteDimension': {'range': {
        'sheetId': 67006763,
        'dimension': 'ROWS',
        'startIndex': 776,
        'endIndex': 778,
    }}}
    restore.require_rollback_exact([], full, [], copy.deepcopy(full))


def test_rollback_exact_ignores_computed_format_drift_but_rejects_writable_drift() -> None:
    values, full = synthetic_preimage()
    before = copy.deepcopy(full)
    before_row = restore.selected_sheet(before)['data'][0]['rowData'][3]
    before_row['values'] = [{} for _ in range(restore.EXPECTED_COLUMN_COUNT)]
    before_cell = before_row['values'][0]
    before_cell['effectiveFormat'] = {'numberFormat': {'type': 'NUMBER'}}
    rollback = copy.deepcopy(before)
    rollback_cell = restore.selected_sheet(rollback)['data'][0]['rowData'][3]['values'][0]
    rollback_cell.pop('effectiveFormat')

    restore.require_rollback_exact(values, before, values, rollback)

    writable_drift = copy.deepcopy(rollback)
    drift_cell = restore.selected_sheet(writable_drift)['data'][0]['rowData'][3]['values'][0]
    drift_cell['userEnteredValue'] = {'stringValue': 'unexpected'}
    with pytest.raises(RuntimeError, match='target_writable_cells'):
        restore.require_rollback_exact(values, before, values, writable_drift)


def test_cli_defaults_to_dry_run_and_apply_requires_hash_and_pause_confirmation() -> None:
    args = restore.build_parser().parse_args([])
    assert args.apply is False
    restore.validate_cli_args(args)

    missing = Namespace(
        apply=True,
        expected_plan_sha256=None,
        confirm_writers_paused=False,
    )
    with pytest.raises(RuntimeError, match='apply_requires_exact_expected_plan_sha256'):
        restore.validate_cli_args(missing)

    no_pause = Namespace(
        apply=True,
        expected_plan_sha256='a' * 64,
        confirm_writers_paused=False,
    )
    with pytest.raises(RuntimeError, match='apply_requires_confirm_writers_paused'):
        restore.validate_cli_args(no_pause)

    valid = Namespace(
        apply=True,
        expected_plan_sha256='a' * 64,
        confirm_writers_paused=True,
    )
    restore.validate_cli_args(valid)


def test_guard_invariant_is_shared_by_active_writer_prepost_gate() -> None:
    source = (restore.ROOT / 'scripts/affiliate_sheet_schema_guard.py').read_text()
    assert 'REQUIRED_MARKETPLACE_PARENT_NAMES' in source
    assert 'required_marketplace_parent_missing' in source
    assert 'required_marketplace_parent_duplicate' in source
    for relative in (
        'scripts/affiliate_approval_auto_reconciler.py',
        'scripts/affiliate_marketplace_update_sheet.py',
        'scripts/affiliate_sheet_batch_writer.py',
        'scripts/process_partner_info_replies.py',
    ):
        writer_source = (restore.ROOT / relative).read_text()
        assert writer_source.count('require_checkbox_schema_pass') >= 2


def test_restoration_carrier_has_no_non_sheet_provider_effect_path() -> None:
    source = (restore.ROOT / 'scripts/restore_affiliate_marketplace_parent_rows.py').read_text()
    assert 'marketplace_adapters' not in source
    assert 'provider_writes": 0' in source
    assert 'tracking_clicks": 0' in source
    assert 'insertDimension' not in source
    assert 'appendDimension' in source
    assert 'deleteDimension' in source
    assert 'glp-affiliate-sheet.lock' in source
    assert 'affiliate_sheet.lock' in source
