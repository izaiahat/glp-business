from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

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
def load_module(name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cross_marketplace_same_name_never_substitutes_for_exact_id() -> None:
    module = load_module('cross_identity_guard', 'scripts/affiliate_marketplace_update_sheet.py')
    target = {'network': 'Awin', 'program_id': '222', 'name': 'Same Brand'}
    names = {'Awin:samebrand': [17]}
    collisions = module.legacy_identity_collisions([target], {'Awin:111': 17}, names)
    assert collisions == [{'identity': 'Awin:222', 'name': 'Same Brand', 'rows': [17]}]
    assert module.legacy_identity_collisions([target], {'Awin:222': 17}, names) == []


def test_katalys_same_name_never_substitutes_for_exact_offer() -> None:
    module = load_module('katalys_identity_guard', 'scripts/affiliate_sheet_batch_writer.py')
    target = {'offer_id': 222, 'name': 'Same Brand'}
    names = {'samebrand': [17]}
    collisions = module.legacy_identity_collisions([target], {'111': 17}, names)
    assert collisions == [{'offer_id': '222', 'name': 'Same Brand', 'rows': [17]}]
    assert module.legacy_identity_collisions([target], {'222': 17}, names) == []


def test_cross_marketplace_identical_rerun_plans_zero_value_writes() -> None:
    module = load_module('cross_zero_write', 'scripts/affiliate_marketplace_update_sheet.py')
    existing = [f'value-{index}' for index in range(len(module.HEADERS))]
    values = [module.HEADERS, existing]
    assignment = (2, {'network': 'Awin', 'program_id': '222'}, list(existing))
    assert module.changed_cells(values, [assignment]) == []
    service = MagicMock()
    assert module.apply_value_assignments(service, []) is False
    service.spreadsheets.assert_not_called()


def test_cross_marketplace_full_sheet_expected_hash_detects_unrelated_clobber() -> None:
    module = load_module('cross_full_sheet_proof', 'scripts/affiliate_marketplace_update_sheet.py')
    untouched = [f'untouched-{index}' for index in range(len(module.HEADERS))]
    old = [f'old-{index}' for index in range(len(module.HEADERS))]
    expected = list(old)
    expected[10] = 'applied_pending_brand_approval'
    assignment = (3, {'network': 'Awin', 'program_id': '222'}, expected)
    expected_sheet = module.expected_values_after_assignments(
        [module.HEADERS, untouched, old], [assignment]
    )
    observed = [list(row) for row in expected_sheet]
    assert module.values_sha(expected_sheet) == module.values_sha(
        module.normalized_sheet_values(observed, len(expected_sheet))
    )
    observed[1][7] = 'clobbered unrelated value'
    assert module.values_sha(expected_sheet) != module.values_sha(
        module.normalized_sheet_values(observed, len(expected_sheet))
    )


def test_katalys_identical_rerun_plans_zero_value_writes() -> None:
    module = load_module('katalys_zero_write', 'scripts/affiliate_sheet_batch_writer.py')
    existing = [f'value-{index}' for index in range(len(module.HEADERS))]
    values = [module.HEADERS, existing]
    assignment = (2, {'offer_id': 222}, list(existing))
    assert module.changed_cells(values, [assignment]) == []
    service = MagicMock()
    assert module.apply_value_assignments(service, []) is False
    service.spreadsheets.assert_not_called()


def test_katalys_full_sheet_expected_hash_detects_unrelated_clobber() -> None:
    module = load_module('katalys_full_sheet_proof', 'scripts/affiliate_sheet_batch_writer.py')
    untouched = [f'untouched-{index}' for index in range(len(module.HEADERS))]
    old = [f'old-{index}' for index in range(len(module.HEADERS))]
    expected = list(old)
    expected[10] = 'applied_pending_brand_approval'
    assignment = (3, {'offer_id': 222}, expected)
    expected_sheet = module.expected_full_sheet_values(
        [module.HEADERS, untouched, old], [assignment], 3,
    )
    observed = [list(row) for row in expected_sheet]
    assert module.values_sha(expected_sheet) == module.values_sha(
        module.normalized_full_sheet_values(observed, len(expected_sheet))
    )
    observed[1][7] = 'clobbered unrelated value'
    assert module.values_sha(expected_sheet) != module.values_sha(
        module.normalized_full_sheet_values(observed, len(expected_sheet))
    )


def test_katalys_pass_gate_requires_idempotent_second_plan_and_full_sheet_match() -> None:
    module = load_module('katalys_pass_gate', 'scripts/affiliate_sheet_batch_writer.py')
    valid = dict(
        all_cells=True,
        all_validations=True,
        schema_status='PASS',
        duplicates={},
        full_sheet_post_write_match=True,
        idempotent_rerun=True,
        row_count_matches=True,
        column_count_matches=True,
    )
    assert module.write_verification_pass(**valid) is True
    assert module.write_verification_pass(**{**valid, 'idempotent_rerun': False}) is False
    assert module.write_verification_pass(**{**valid, 'full_sheet_post_write_match': False}) is False


def test_cross_marketplace_changed_or_created_row_writes_once() -> None:
    module = load_module('cross_value_write', 'scripts/affiliate_marketplace_update_sheet.py')
    old = [''] * len(module.HEADERS)
    expected = list(old)
    expected[2] = 'New Program'
    assignment = (2, {'network': 'Awin', 'program_id': '222'}, expected)
    changed = module.changed_cells([module.HEADERS, old], [assignment], created_rows=[2])
    assert changed == [(2, assignment[1], 2, 'New Program')]
    service = MagicMock()
    assert module.apply_value_assignments(service, changed) is True
    service.spreadsheets.return_value.values.return_value.batchUpdate.assert_called_once()


def test_cross_marketplace_zero_sheet_write_uses_current_plan_only() -> None:
    module = load_module('cross_zero_sheet_write', 'scripts/affiliate_marketplace_update_sheet.py')
    assert module.zero_sheet_write([], [], 0, 0) is True
    assert module.zero_sheet_write([(2, {}, 10, 'changed')], [], 0, 0) is False
    assert module.zero_sheet_write([], [{'row': 2}], 0, 0) is False
    assert module.zero_sheet_write([], [], 1, 0) is False
    assert module.zero_sheet_write([], [], 0, 1) is False


def test_katalys_zero_sheet_write_uses_current_plan_only() -> None:
    module = load_module('katalys_zero_sheet_write', 'scripts/affiliate_sheet_batch_writer.py')
    assert module.zero_sheet_write([], [], 0, 0) is True
    assert module.zero_sheet_write([(2, {}, 10, 'changed')], [], 0, 0) is False
    assert module.zero_sheet_write([], [{'row': 2}], 0, 0) is False
    assert module.zero_sheet_write([], [], 1, 0) is False
    assert module.zero_sheet_write([], [], 0, 1) is False


def test_katalys_false_only_checkbox_row_is_available_for_business_data() -> None:
    module = load_module('katalys_false_only_row', 'scripts/affiliate_sheet_batch_writer.py')
    row = [''] * len(module.HEADERS)
    for column in (1, 11, 19, 20):
        row[column] = False
    assert module.row_has_business_value(row) is False
    row[2] = 'Program'
    assert module.row_has_business_value(row) is True


def test_katalys_schema_materialization_allows_only_new_row_checkbox_false() -> None:
    module = load_module('katalys_schema_materialization', 'scripts/affiliate_sheet_batch_writer.py')
    header = list(module.HEADERS)
    before = [header]
    prepared = [''] * len(module.HEADERS)
    for column in (1, 11, 19, 20):
        prepared[column] = False
    assert module.schema_materialization_is_safe(before, [header, prepared], [2]) is True
    corrupted = list(prepared)
    corrupted[2] = 'unexpected business data'
    assert module.schema_materialization_is_safe(before, [header, corrupted], [2]) is False
    changed_header = list(header)
    changed_header[0] = 'drifted'
    assert module.schema_materialization_is_safe(before, [changed_header, prepared], [2]) is False


def test_append_writers_fail_closed_on_schema_drift_without_auto_repair() -> None:
    for relative_path in (
        'scripts/affiliate_marketplace_update_sheet.py',
        'scripts/affiliate_sheet_batch_writer.py',
    ):
        source = (ROOT / relative_path).read_text()
        assert 'repair_non_value_schema_issues' not in source
        assert 'SKIPPED_WRITER_FAILS_CLOSED' in source
        assert 'UNFORMATTED_VALUE' in source
        assert 'previous.get(' not in source


def test_katalys_changed_or_created_row_writes_once() -> None:
    module = load_module('katalys_value_write', 'scripts/affiliate_sheet_batch_writer.py')
    old = [''] * len(module.HEADERS)
    expected = list(old)
    expected[2] = 'New Program'
    assignment = (2, {'offer_id': 222}, expected)
    changed = module.changed_cells([module.HEADERS, old], [assignment], created_rows=[2])
    assert changed == [(2, assignment[1], 2, 'New Program')]
    service = MagicMock()
    assert module.apply_value_assignments(service, changed) is True
    service.spreadsheets.return_value.values.return_value.batchUpdate.assert_called_once()


def test_cross_marketplace_exact_id_is_embedded_in_new_row() -> None:
    module = load_module('cross_create_row', 'scripts/affiliate_marketplace_update_sheet.py')
    program = {'network': 'Awin', 'program_id': '222', 'name': 'Program 222', 'vertical': 'general-wellness'}
    application = {'status': 'SUBMITTED_VERIFIED', 'verified_at_utc': '2026-07-23T00:00:00Z'}
    relationship = {'status': 'PASS', 'relationship': 'Pending'}
    row = module.make_row(program, application, relationship, [''] * len(module.HEADERS))
    assert row[4] == 'Awin'
    assert 'Awin offer 222' in row[23]
    assert row[25] == 'general-wellness'
    assert module.identity_from_row(row) == 'Awin:222'


def test_cross_marketplace_reads_canonical_cj_note_identity_without_name_fallback() -> None:
    module = load_module('cross_cj_existing_identity', 'scripts/affiliate_marketplace_update_sheet.py')
    row = [''] * len(module.HEADERS)
    row[2] = 'Name is not identity'
    row[4] = 'CJ Affiliate'
    row[23] = 'CJ Affiliate programme 6146589; application receipt=SUBMITTED_VERIFIED'
    assert module.identity_from_row(row) == 'CJ Affiliate:6146589'


def test_cross_marketplace_unavailable_provider_state_is_not_pending() -> None:
    module = load_module('cross_unavailable_row', 'scripts/affiliate_marketplace_update_sheet.py')
    existing = [f'preserved-{index}' for index in range(len(module.HEADERS))]
    existing[1] = True
    existing[11] = True
    existing[18] = ''
    existing[19] = False
    existing[20] = False
    existing[25] = 'supplements-nutrition'
    program = {'network': 'Awin', 'program_id': '76634', 'name': 'Culturelle, Estroven, And AZO', 'vertical': 'supplements-nutrition'}
    application = {'status': 'PROVIDER_RECORD_UNAVAILABLE_VERIFIED', 'verified_at_utc': '2026-07-23T08:09:39Z'}
    relationship = {'status': 'PASS', 'relationship': 'unavailable'}
    row = module.make_row(program, application, relationship, list(existing))
    assert row[10] == 'provider_record_unavailable_or_retired'
    assert row[11] is True
    assert row[18] == ''
    for index, value in enumerate(existing):
        if index not in {10, 11, 18, 23, 24}:
            assert row[index] == value


def test_katalys_exact_offer_is_embedded_in_new_row() -> None:
    module = load_module('katalys_create_row', 'scripts/affiliate_sheet_batch_writer.py')
    program = {'decision': 'APPLY', 'offer_id': 222, 'name': 'Program 222', 'vertical': 'glp1-weightloss'}
    application = {'status': 'SUBMITTED_VERIFIED', 'verified_at_utc': '2026-07-23T00:00:00Z'}
    relationship = {'status': 'PASS', 'relationship': 'Pending'}
    row = module.make_row(program, application, relationship, [''] * len(module.HEADERS), None)
    assert row[4] == 'Katalys'
    assert module.offer_from_url(row[5]) == '222'
    assert 'Katalys offer 222' in row[23]
    assert row[25] == 'glp1-weightloss'


def test_katalys_duplicate_route_review_status_is_link_dark() -> None:
    module = load_module('katalys_duplicate_review', 'scripts/affiliate_sheet_batch_writer.py')
    program = {
        'decision': 'DUPLICATE_ROUTE_REVIEW', 'offer_id': 222, 'name': 'Duplicate route',
        'duplicate_reason': 'Equivalent brand route is already pending.',
    }
    existing = [''] * len(module.HEADERS)
    existing[2] = 'Duplicate route'
    existing[4] = 'Katalys'
    existing[5] = 'https://app.katalys.com/programs/222'
    existing[19] = False
    existing[20] = False
    row = module.make_row(program, {'status': 'SUBMITTED_VERIFIED'}, {'relationship': 'Pending'}, existing, None)
    assert row[10] == 'operator_review_duplicate_route_pending'
    assert row[14] == ''
    assert row[19] is False
    assert row[20] is False
    assert 'Equivalent brand route is already pending.' in row[23]


def test_katalys_final_route_determinations_preserve_link_dark_cells() -> None:
    module = load_module('katalys_final_route_determination', 'scripts/affiliate_sheet_batch_writer.py')
    for decision, expected_status in (
        ('DISTINCT_ROUTE_KEEP_ACTIVE', 'applied_pending_brand_approval'),
        ('DUPLICATE_DO_NOT_USE', 'duplicate_do_not_use'),
    ):
        program = {
            'decision': decision, 'offer_id': 222, 'name': 'Route',
            'route_determination': 'Evidence-backed final determination.',
        }
        existing = [''] * len(module.HEADERS)
        existing[2] = 'Route'
        existing[4] = 'Katalys'
        existing[5] = 'https://app.katalys.com/programs/222'
        existing[19] = False
        existing[20] = False
        row = module.make_row(program, {'status': 'SUBMITTED_VERIFIED'}, {'relationship': 'Pending'}, existing, None)
        assert row[10] == expected_status
        assert row[14] == ''
        assert row[19] is False
        assert row[20] is False
        assert 'Evidence-backed final determination.' in row[23]


def test_katalys_final_route_determination_fails_closed_if_link_exists() -> None:
    module = load_module('katalys_final_route_with_link', 'scripts/affiliate_sheet_batch_writer.py')
    program = {'decision': 'DUPLICATE_DO_NOT_USE', 'offer_id': 222, 'name': 'Route'}
    existing = [''] * len(module.HEADERS)
    existing[2] = 'Route'
    existing[14] = 'https://tracking.example.invalid'
    try:
        module.make_row(program, {'status': 'SUBMITTED_VERIFIED'}, {'relationship': 'Pending'}, existing, None)
    except RuntimeError as exc:
        assert str(exc) == 'route_determination_requires_link_dark_existing_row:222'
    else:
        raise AssertionError('expected link-dark guard to fail closed')
