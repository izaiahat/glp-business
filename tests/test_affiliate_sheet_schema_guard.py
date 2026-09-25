from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path('/home/ubuntu/business')
MISSION = ROOT / 'reports/glp-clinic-scout/affiliate-portfolio-reconciliation-20260722'
GUARD_PATH = ROOT / 'scripts/affiliate_sheet_schema_guard.py'
AUTHORITY_PATH = ROOT / 'scripts/check_affiliate_sheet_writer_authority.py'


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def load_guard():
    return load(GUARD_PATH, 'affiliate_sheet_schema_guard_test')


def test_schema_requests_are_explicit_strict_and_include_date_format():
    guard = load_guard()
    requests = guard.schema_requests(67006763, [3, 2, 3])
    assert len(requests) == 12
    checkbox = requests[:8]
    date_formats = requests[8:10]
    vertical = requests[10:]
    assert all(item['setDataValidation']['rule'] == {
        'condition': {'type': 'BOOLEAN'}, 'strict': True, 'showCustomUi': True,
    } for item in checkbox)
    assert [item['repeatCell']['cell']['userEnteredFormat']['numberFormat'] for item in date_formats] == [
        {'type': 'DATE', 'pattern': 'yyyy-mm-dd'},
        {'type': 'DATE', 'pattern': 'yyyy-mm-dd'},
    ]
    assert all(item['setDataValidation']['rule'] == {
        'condition': {
            'type': 'ONE_OF_LIST',
            'values': [{'userEnteredValue': value} for value in guard.VERTICAL_ALLOWED_VALUES],
        },
        'strict': True,
        'showCustomUi': True,
    } for item in vertical)
    assert [item['setDataValidation']['range']['startColumnIndex'] for item in vertical] == [25, 25]
    assert guard.column_letter(25) == 'Z'
    assert guard.column_letter(26) == 'AA'


def test_api_capabilities_requires_literal_text_or_blank():
    guard = load_guard()
    assert guard.require_api_capabilities_value(None) == ''
    assert guard.require_api_capabilities_value('  GET offers  ') == 'GET offers'
    assert guard.api_capabilities_cell_issues({}, 2) == []
    assert guard.api_capabilities_cell_issues({'userEnteredValue': {'stringValue': 'GET offers'}}, 2) == []
    with pytest.raises(RuntimeError, match='api_capabilities_not_text:int'):
        guard.require_api_capabilities_value(6)
    assert [item['kind'] for item in guard.api_capabilities_cell_issues(
        {'userEnteredValue': {'numberValue': 6}}, 2,
    )] == ['api_capabilities_not_text']


def test_checkbox_guard_rejects_string_boolean_missing_validation_and_non_strict_rule():
    guard = load_guard()
    text = {
        'userEnteredValue': {'stringValue': 'TRUE'},
        'dataValidation': {'condition': {'type': 'BOOLEAN'}, 'strict': True, 'showCustomUi': True},
    }
    assert [item['kind'] for item in guard.checkbox_cell_issues(text, 2, 1)] == ['checkbox_value_not_boolean']
    missing = {'userEnteredValue': {'boolValue': True}}
    assert {item['kind'] for item in guard.checkbox_cell_issues(missing, 2, 1)} == {
        'checkbox_validation_not_boolean', 'checkbox_validation_not_strict',
    }
    non_strict = {
        'userEnteredValue': {'boolValue': True},
        'dataValidation': {'condition': {'type': 'BOOLEAN'}, 'strict': False, 'showCustomUi': True},
    }
    assert [item['kind'] for item in guard.checkbox_cell_issues(non_strict, 2, 1)] == ['checkbox_validation_not_strict']


def test_checkbox_guard_accepts_native_boolean_and_exact_rule():
    guard = load_guard()
    good = {
        'userEnteredValue': {'boolValue': False},
        'dataValidation': {'condition': {'type': 'BOOLEAN'}, 'strict': True, 'showCustomUi': True},
    }
    assert guard.checkbox_cell_issues(good, 2, 20) == []


def vertical_cell(guard, value='general-wellness'):
    return {
        'userEnteredValue': {'stringValue': value},
        'formattedValue': value,
        'dataValidation': {
            'condition': {
                'type': 'ONE_OF_LIST',
                'values': [{'userEnteredValue': item} for item in guard.VERTICAL_ALLOWED_VALUES],
            },
            'strict': True,
            'showCustomUi': True,
        },
    }


def test_vertical_guard_requires_allowlisted_value_and_exact_strict_dropdown():
    guard = load_guard()
    assert guard.vertical_cell_issues(vertical_cell(guard), 2) == []
    assert guard.require_vertical_value('general-wellness') == 'general-wellness'
    with pytest.raises(RuntimeError, match='vertical_invalid:BLANK'):
        guard.require_vertical_value('')
    bad_value = vertical_cell(guard, 'other')
    assert [item['kind'] for item in guard.vertical_cell_issues(bad_value, 2)] == ['vertical_value_not_allowed']
    bad_rule = vertical_cell(guard)
    bad_rule['dataValidation']['strict'] = False
    bad_rule['dataValidation']['condition']['values'] = bad_rule['dataValidation']['condition']['values'][:-1]
    assert {item['kind'] for item in guard.vertical_cell_issues(bad_rule, 2)} == {
        'vertical_validation_allowlist_mismatch', 'vertical_validation_not_strict',
    }


def test_date_guard_rejects_text_numeric_without_format_and_blank_without_format():
    guard = load_guard()
    good = {
        'userEnteredValue': {'numberValue': 46225},
        'effectiveFormat': {'numberFormat': {'type': 'DATE', 'pattern': 'yyyy-mm-dd'}},
    }
    assert guard.date_cell_issues(good, 2) == []
    assert {item['kind'] for item in guard.date_cell_issues({'userEnteredValue': {'stringValue': '2026-07-22'}}, 2)} == {
        'approval_date_not_typed_number', 'approval_date_format_not_canonical',
    }
    assert [item['kind'] for item in guard.date_cell_issues({'userEnteredValue': {'numberValue': 46225}}, 2)] == [
        'approval_date_format_not_canonical',
    ]
    assert [item['kind'] for item in guard.date_cell_issues({}, 2)] == ['approval_date_format_not_canonical']
    boolean_date = {
        'userEnteredValue': {'numberValue': True},
        'effectiveFormat': {'numberFormat': {'type': 'DATE', 'pattern': 'yyyy-mm-dd'}},
    }
    assert [item['kind'] for item in guard.date_cell_issues(boolean_date, 2)] == [
        'approval_date_not_typed_number',
    ]


def test_date_guard_rejects_nonfinite_negative_and_future_serials():
    guard = load_guard()
    for value in (float('nan'), float('inf'), -1, 999999999):
        cell = {
            'userEnteredValue': {'numberValue': value},
            'effectiveFormat': {'numberFormat': {'type': 'DATE', 'pattern': 'yyyy-mm-dd'}},
        }
        assert [item['kind'] for item in guard.date_cell_issues(cell, 2)] == [
            'approval_date_number_out_of_range',
        ]


def test_date_guard_accepts_today_utc_and_rejects_tomorrow():
    guard = load_guard()
    today = guard.max_approval_date_serial()
    canonical = {'type': 'DATE', 'pattern': 'yyyy-mm-dd'}
    assert guard.date_cell_issues({
        'userEnteredValue': {'numberValue': today},
        'effectiveFormat': {'numberFormat': canonical},
    }, 2) == []
    issues = guard.date_cell_issues({
        'userEnteredValue': {'numberValue': today + 1},
        'effectiveFormat': {'numberFormat': canonical},
    }, 2)
    assert [item['kind'] for item in issues] == ['approval_date_number_out_of_range']
    assert issues[0]['maximum'] == today


def test_full_sheet_audit_does_not_skip_partial_row_with_blank_program_cell():
    guard = load_guard()
    cells = [{} for _ in range(27)]
    rule = {'condition': {'type': 'BOOLEAN'}, 'strict': True, 'showCustomUi': True}
    for column in guard.CHECKBOX_COLUMNS:
        cells[column] = {
            'userEnteredValue': {'boolValue': False},
            'formattedValue': 'FALSE',
            'dataValidation': rule,
        }
    cells[1] = {
        'userEnteredValue': {'stringValue': 'TRUE'},
        'formattedValue': 'TRUE',
        'dataValidation': rule,
    }
    cells[3] = {'userEnteredValue': {'stringValue': 'partial payload'}, 'formattedValue': 'partial payload'}
    cells[guard.DATE_COLUMN] = {
        'effectiveFormat': {'numberFormat': {'type': 'DATE', 'pattern': 'yyyy-mm-dd'}},
    }
    cells[guard.VERTICAL_COLUMN] = vertical_cell(guard)
    header = [{'userEnteredValue': {'stringValue': value}, 'formattedValue': value} for value in guard.EXPECTED_HEADERS]
    service = MagicMock()
    service.spreadsheets.return_value.get.return_value.execute.side_effect = [
        {'sheets': [{'properties': {'sheetId': 7, 'title': 'Affiliate Applications', 'gridProperties': {'rowCount': 2, 'columnCount': 27}}}]},
        {'sheets': [{'data': [{'startRow': 0, 'rowData': [{'values': header}, {'values': cells}]}]}]},
    ]
    result = guard.audit_checkbox_schema(service, 'sheet', 'Affiliate Applications')
    assert result['status'] == 'FAIL'
    assert result['populated_rows_checked'] == 1
    assert 'checkbox_value_not_boolean' in {item['kind'] for item in result['issues']}


def test_proposed_27_column_fixture_passes_with_all_fixed_parent_rows():
    guard = load_guard()
    header = [{'userEnteredValue': {'stringValue': value}, 'formattedValue': value} for value in guard.EXPECTED_HEADERS]
    checkbox_rule = {'condition': {'type': 'BOOLEAN'}, 'strict': True, 'showCustomUi': True}
    parent_by_row = {row: name for name, row in guard.REQUIRED_MARKETPLACE_PARENT_ROWS.items()}
    populated_rows = set(parent_by_row)
    for row_number in range(2, 779):
        if len(populated_rows) >= 347:
            break
        populated_rows.add(row_number)
    row_data = [{'values': header}]
    for row_number in range(2, 779):
        cells = [{} for _ in range(27)]
        if row_number not in populated_rows:
            row_data.append({'values': cells})
            continue
        program = parent_by_row.get(row_number, f'Program {row_number}')
        cells[2] = {'userEnteredValue': {'stringValue': program}, 'formattedValue': program}
        if program in guard.REQUIRED_MARKETPLACE_PARENT_NAMES:
            for column in guard.MARKETPLACE_PARENT_REQUIRED_METADATA_COLUMNS:
                cells[column] = {
                    'userEnteredValue': {'stringValue': f'metadata-{column}'},
                    'formattedValue': f'metadata-{column}',
                }
        for column in guard.CHECKBOX_COLUMNS:
            cells[column] = {
                'userEnteredValue': {'boolValue': False},
                'formattedValue': 'FALSE',
                'dataValidation': checkbox_rule,
            }
        cells[guard.DATE_COLUMN] = {
            'effectiveFormat': {'numberFormat': {'type': 'DATE', 'pattern': 'yyyy-mm-dd'}},
        }
        cells[guard.VERTICAL_COLUMN] = vertical_cell(guard)
        row_data.append({'values': cells})
    service = MagicMock()
    service.spreadsheets.return_value.get.return_value.execute.side_effect = [
        {'sheets': [{'properties': {'sheetId': 7, 'title': 'Affiliate Applications', 'gridProperties': {'rowCount': 778, 'columnCount': 27}}}]},
        {'sheets': [{'data': [{'startRow': 0, 'rowData': row_data}]}]},
    ]
    result = guard.audit_checkbox_schema(service, 'sheet', 'Affiliate Applications')
    assert result['status'] == 'PASS'
    assert result['issues'] == []
    assert result['populated_rows_checked'] == 347
    assert result['checkbox_cells_checked'] == 1388
    assert result['approval_date_cells_checked'] == 347
    assert result['vertical_cells_checked'] == 347
    assert result['api_capabilities_cells_checked'] == 347


def test_required_marketplace_parent_identity_invariant_is_exact_and_unique():
    guard = load_guard()
    values = [list(guard.EXPECTED_HEADERS)] + [[] for _ in range(777)]
    for name, row_number in guard.REQUIRED_MARKETPLACE_PARENT_ROWS.items():
        values[row_number - 1] = ['', False, name]
    assert guard.marketplace_parent_identity_issues_from_values(values) == []

    missing = [list(row) for row in values]
    missing[guard.REQUIRED_MARKETPLACE_PARENT_ROWS['CJ Affiliate'] - 1][2] = 'Alias for CJ'
    issues = guard.marketplace_parent_identity_issues_from_values(missing)
    assert issues == [{
        'kind': 'required_marketplace_parent_missing',
        'parent': 'CJ Affiliate',
    }]

    whitespace_alias = [list(row) for row in values]
    whitespace_alias[guard.REQUIRED_MARKETPLACE_PARENT_ROWS['Awin'] - 1][2] = 'Awin '
    assert guard.marketplace_parent_identity_issues_from_values(whitespace_alias) == [{
        'kind': 'required_marketplace_parent_missing',
        'parent': 'Awin',
    }]

    duplicate = [list(row) for row in values]
    duplicate.append(['', False, 'Awin'])
    issues = guard.marketplace_parent_identity_issues_from_values(duplicate)
    assert issues == [{
        'kind': 'required_marketplace_parent_duplicate',
        'parent': 'Awin',
        'rows': [guard.REQUIRED_MARKETPLACE_PARENT_ROWS['Awin'], len(duplicate)],
        'count': 2,
    }]

    moved = [list(row) for row in values]
    awin_row = guard.REQUIRED_MARKETPLACE_PARENT_ROWS['Awin'] - 1
    moved[awin_row] = []
    moved[100] = ['', False, 'Awin']
    assert guard.marketplace_parent_identity_issues_from_values(moved) == [{
        'kind': 'required_marketplace_parent_row_moved',
        'parent': 'Awin',
        'expected_row': guard.REQUIRED_MARKETPLACE_PARENT_ROWS['Awin'],
        'observed_row': 101,
    }]


def test_ordinary_writer_parent_row_mutation_gate_is_absolute():
    guard = load_guard()
    guard.require_marketplace_parent_rows_not_mutated([(2, 23), (776, 10)])
    with pytest.raises(RuntimeError, match='required_marketplace_parent_row_mutation_blocked'):
        guard.require_marketplace_parent_rows_not_mutated([
            (guard.REQUIRED_MARKETPLACE_PARENT_ROWS['Awin'], 24),
        ])


def test_false_checkbox_schema_alone_is_not_business_occupancy():
    guard = load_guard()
    checkbox_rule = {'condition': {'type': 'BOOLEAN'}, 'strict': True, 'showCustomUi': True}
    cells = [{} for _ in range(27)]
    for column in guard.CHECKBOX_COLUMNS:
        cells[column] = {'userEnteredValue': {'boolValue': False}, 'dataValidation': checkbox_rule}
    assert guard._row_has_value([{'values': []}, {'values': cells}], 2) is False


def test_current_live_postrepair_snapshot_has_full_immutable_contract():
    guard = load_guard()
    payload = json.loads((MISSION / 'private/approval-date-format-contract-after.json').read_text())
    rows = payload['sheets'][0]['data'][0]['rowData']
    issues = []
    populated = 0
    for row_number in range(2, len(rows) + 1):
        cells = rows[row_number - 1].get('values') or []
        program = cells[2].get('formattedValue') if len(cells) > 2 else ''
        if not str(program or '').strip():
            continue
        populated += 1
        for column in guard.CHECKBOX_COLUMNS:
            issues.extend(guard.checkbox_cell_issues(cells[column] if column < len(cells) else {}, row_number, column))
        issues.extend(guard.date_cell_issues(cells[guard.DATE_COLUMN] if guard.DATE_COLUMN < len(cells) else {}, row_number))
    assert populated == 334
    assert issues == []


def test_detailed_live_audit_uses_immutable_contract_and_has_zero_issues():
    audit = json.loads((MISSION / 'verification/sheet-integrity-audit-after-repair.json').read_text())
    assert audit['schema'] == 'glp_affiliate_sheet_integrity_audit_v2'
    assert audit['contract_source'] == 'immutable_constants'
    assert audit['status'] == 'PASS'
    assert audit['populated_row_count'] == 334
    assert audit['checkbox_cells_checked'] == 1336
    assert audit['approval_date_cells_checked'] == 334
    assert all(count == 0 for count in audit['issue_counts'].values())


def test_only_five_active_writers_and_all_other_direct_writers_are_hard_disabled():
    authority = load(AUTHORITY_PATH, 'affiliate_sheet_writer_authority_test')
    result = authority.audit()
    assert result['status'] == 'PASS'
    assert result['active_writers'] == sorted({
        'scripts/affiliate_application_invitation_reconciler.py',
        'scripts/affiliate_approval_auto_reconciler.py',
        'scripts/affiliate_marketplace_update_sheet.py',
        'scripts/affiliate_sheet_batch_writer.py',
        'scripts/process_partner_info_replies.py',
    })
    assert result['retired_writer_count'] >= 21
    assert result['prepared_migration_carriers'] == [
        'scripts/apply_affiliate_api_capabilities_migration.py',
        'scripts/restore_affiliate_marketplace_parent_rows.py',
    ]
    assert result['issues'] == []
    assert result['destructive_active_writer_paths'] == []
    assert result['controlled_destructive_active_writer_paths'] == [
        'scripts/affiliate_sheet_batch_writer.py',
    ]


def test_controlled_junk_purge_surface_fails_closed_if_a_required_gate_disappears():
    authority = load(AUTHORITY_PATH, 'affiliate_sheet_writer_destructive_contract_test')
    path = 'scripts/affiliate_sheet_batch_writer.py'
    source = (ROOT / path).read_text()
    damaged = source.replace('sheet_concurrency_drift_before_junk_purge', 'missing_cas_gate')
    analysis = authority._analyse_source(damaged)
    issues = list(authority._controlled_destructive_issues(path, damaged, analysis))
    assert {
        'kind': 'active_writer_controlled_destructive_token_missing',
        'path': path,
        'token': 'sheet_concurrency_drift_before_junk_purge',
    } in issues


def test_comment_only_retirement_marker_is_not_authority(monkeypatch, tmp_path):
    authority = load(AUTHORITY_PATH, 'affiliate_sheet_writer_authority_hard_stop_test')
    writer = tmp_path / 'writer.py'
    writer.write_text(
        "# RETIRED_CANONICAL_AFFILIATE_SHEET_WRITER_V1\n"
        f"SHEET_ID = {authority.SHEET_ID!r}\n"
        "service.spreadsheets().values().update(body={})\n"
    )
    monkeypatch.setattr(authority, 'ROOT', tmp_path)
    result = authority.audit()
    assert result['status'] == 'FAIL'
    assert {'kind': 'retired_writer_not_hard_disabled', 'path': 'writer.py'} in result['issues']


def test_writer_discovery_tolerates_concurrently_removed_workspace_directory(monkeypatch, tmp_path):
    authority = load(AUTHORITY_PATH, 'affiliate_sheet_writer_authority_transient_tree_test')
    scripts = tmp_path / 'scripts'
    scripts.mkdir()
    writer = scripts / 'writer.py'
    writer.write_text(
        f"SHEET_ID = {authority.SHEET_ID!r}\n"
        "service.spreadsheets().values().update(body={})\n"
    )

    def unstable_walk(root, *, topdown, onerror, followlinks):
        assert Path(root) == tmp_path
        assert topdown is True
        assert followlinks is False
        onerror(FileNotFoundError('transient directory disappeared'))
        yield str(scripts), [], ['gone.py', 'writer.py']

    monkeypatch.setattr(authority, 'ROOT', tmp_path)
    monkeypatch.setattr(authority.os, 'walk', unstable_walk)

    assert authority.discover() == [('scripts/writer.py', writer.read_text())]


def test_active_writers_use_typed_raw_schema_critical_writes_and_prepost_guard():
    for rel in (
        'scripts/affiliate_application_invitation_reconciler.py',
        'scripts/affiliate_approval_auto_reconciler.py',
        'scripts/affiliate_marketplace_update_sheet.py',
        'scripts/affiliate_sheet_batch_writer.py',
        'scripts/process_partner_info_replies.py',
    ):
        source = (ROOT / rel).read_text()
        if rel in {
            'scripts/process_partner_info_replies.py',
            'scripts/affiliate_application_invitation_reconciler.py',
        }:
            assert source.count('require_checkbox_schema_pass') >= 2
            assert 'date_serial' not in source
        else:
            assert 'date_serial' in source
            assert source.count('require_checkbox_schema_pass') >= 3
        assert 'EXPECTED_HEADERS' in source
        assert 'EXPECTED_RANGE' in source
        assert 'A:Z' not in source
        assert 'LOCK_EX' in source
        assert 'USER_ENTERED' not in source
        assert "'valueInputOption':'RAW'" in source or '"valueInputOption": "RAW"' in source
        if rel == 'scripts/affiliate_approval_auto_reconciler.py':
            assert 'schema_requests' in source
            assert 'portfolio_sheet_apply_lock_busy' in source
            assert 'repair_non_value_schema_issues' in source
        elif rel == 'scripts/process_partner_info_replies.py':
            assert 'repair_non_value_schema_issues' not in source
            assert 'RAW' in source
        elif rel == 'scripts/affiliate_application_invitation_reconciler.py':
            assert 'INVITATION_MUTABLE_COLUMNS' in source
            assert 'invitation_mutation_envelope_violation' in source
        else:
            assert 'repair_non_value_schema_issues' not in source
            assert 'SKIPPED_WRITER_FAILS_CLOSED' in source


def test_schema_repair_is_value_preserving_and_rejects_typed_value_drift():
    source = GUARD_PATH.read_text()
    assert 'affiliate_sheet_typed_value_repair_required' in source
    assert 'affiliate_sheet_schema_repair_changed_values' in source
    assert 'affiliate_sheet_schema_repair_precondition_changed' in source
    assert 'affiliate_sheet_schema_repair_value_drift_before_apply' in source
    assert 'precondition_revalidated' in source
    assert 'values_unchanged' in source


def test_schema_repair_issue_contract_handles_parent_and_cell_issues_together():
    guard = load_guard()
    audit = {'issues': [
        {'kind': 'required_marketplace_parent_missing', 'parent': 'Awin'},
        {'kind': 'approval_date_format_not_canonical', 'row': 3, 'column_index': 18},
    ]}
    assert set(guard._repair_issue_contract(audit)) == {
        (None, None, 'required_marketplace_parent_missing', 'Awin', ()),
        (3, 18, 'approval_date_format_not_canonical', None, ()),
    }


def test_schema_repair_revalidates_before_batch_update(monkeypatch):
    guard = load_guard()
    service = MagicMock()
    service.spreadsheets.return_value.get.return_value.execute.return_value = {
        'sheets': [{'properties': {'sheetId': 7, 'title': 'Affiliate Applications'}}],
    }
    issue = {'row': 3, 'column_index': guard.DATE_COLUMN, 'kind': 'approval_date_format_not_canonical'}
    audits = iter([
        {'status': 'FAIL', 'issues': [issue]},
        {'status': 'FAIL', 'issues': [issue]},
        {'status': 'PASS', 'issues': []},
    ])
    hashes = iter(['same', 'same', 'same'])
    monkeypatch.setattr(guard, 'audit_checkbox_schema', lambda *_: next(audits))
    monkeypatch.setattr(guard, '_canonical_values_hash', lambda *_: next(hashes))

    result = guard.repair_non_value_schema_issues(service, 'sheet', 'Affiliate Applications')

    assert result['precondition_revalidated'] is True
    assert result['values_unchanged'] is True
    service.spreadsheets.return_value.batchUpdate.assert_called_once()


def test_schema_repair_blocks_value_drift_before_batch_update(monkeypatch):
    guard = load_guard()
    service = MagicMock()
    issue = {'row': 3, 'column_index': guard.DATE_COLUMN, 'kind': 'approval_date_format_not_canonical'}
    audits = iter([
        {'status': 'FAIL', 'issues': [issue]},
        {'status': 'FAIL', 'issues': [issue]},
    ])
    hashes = iter(['before', 'changed'])
    monkeypatch.setattr(guard, 'audit_checkbox_schema', lambda *_: next(audits))
    monkeypatch.setattr(guard, '_canonical_values_hash', lambda *_: next(hashes))

    with pytest.raises(RuntimeError, match='value_drift_before_apply'):
        guard.repair_non_value_schema_issues(service, 'sheet', 'Affiliate Applications')

    service.spreadsheets.return_value.batchUpdate.assert_not_called()


def test_live_audit_exits_nonzero_when_status_is_not_pass():
    source = (MISSION / 'verification/audit_sheet_integrity.py').read_text()
    assert "contract_source': 'immutable_constants'" in source
    assert "return 0 if status == 'PASS' else 2" in source
