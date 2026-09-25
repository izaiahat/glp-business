from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

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
def load_module(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def existing_row() -> list[object]:
    row: list[object] = [f'preserved-{index}' for index in range(27)]
    row[1] = True
    row[11] = False
    row[18] = ''
    row[19] = False
    row[20] = False
    row[25] = 'general-wellness'
    row[26] = 'preserved API evidence'
    return row


def assert_only_application_state_changed(before: list[object], after: list[object]) -> None:
    mutable = {10, 11, 18, 23, 24}
    for index, value in enumerate(before):
        if index not in mutable:
            assert after[index] == value, f'column {index} was clobbered'
    assert after[11] is True


def test_cross_marketplace_existing_row_preserves_unrelated_fields() -> None:
    module = load_module('cross_writer', 'scripts/affiliate_marketplace_update_sheet.py')
    before = existing_row()
    program = {'network': 'Awin', 'program_id': '126699', 'name': 'HairLabs', 'vertical': 'general-wellness'}
    app = {'status': 'SUBMITTED_VERIFIED', 'verified_at_utc': '2026-07-23T00:00:00Z'}
    relationship = {'status': 'PASS', 'relationship': 'Pending'}
    after = module.make_row(program, app, relationship, list(before))
    assert_only_application_state_changed(before, after)
    assert after[10] == 'applied_pending_brand_approval'
    assert after[18] == ''


def test_cross_marketplace_duplicate_route_is_retained_link_dark() -> None:
    module = load_module('cross_duplicate_writer', 'scripts/affiliate_marketplace_update_sheet.py')
    before = existing_row()
    before[14] = ''
    program = {
        'decision': 'DUPLICATE_DO_NOT_USE',
        'network': 'Awin',
        'program_id': '78548',
        'name': 'Fay Health, Inc.',
        'notes': 'True cross-network duplicate; Katalys route has current positive terms.',
        'vertical': 'general-wellness',
    }
    app = {'status': 'SUBMITTED_VERIFIED', 'verified_at_utc': '2026-07-23T00:00:00Z'}
    relationship = {'status': 'PASS', 'relationship': 'Pending'}
    after = module.make_row(program, app, relationship, list(before))
    assert_only_application_state_changed(before, after)
    assert after[10] == 'duplicate_do_not_use'
    assert after[14] == before[14]
    assert after[18] == ''
    assert after[19] is False
    assert after[20] is False
    assert 'True cross-network duplicate' in after[23]


def test_cross_marketplace_duplicate_route_rejects_tracking_or_wiring() -> None:
    module = load_module('cross_duplicate_link_guard', 'scripts/affiliate_marketplace_update_sheet.py')
    program = {'decision': 'DUPLICATE_DO_NOT_USE', 'network': 'Awin', 'program_id': '78548', 'name': 'Fay', 'vertical': 'general-wellness'}
    app = {'status': 'SUBMITTED_VERIFIED'}
    relationship = {'status': 'PASS', 'relationship': 'Pending'}
    for column, value in ((14, 'https://example.invalid/tracking'), (19, True), (20, True)):
        before = existing_row()
        before[column] = value
        try:
            module.make_row(program, app, relationship, before)
        except RuntimeError as exc:
            assert 'duplicate_route_requires_link_dark' in str(exc)
        else:
            raise AssertionError(f'column {column} must fail closed')


def test_katalys_existing_row_preserves_unrelated_fields() -> None:
    module = load_module('katalys_writer', 'scripts/affiliate_sheet_batch_writer.py')
    before = existing_row()
    program = {'decision': 'APPLY', 'offer_id': 1546, 'name': 'BraveRX', 'vertical': 'general-wellness'}
    app = {'status': 'SUBMITTED_VERIFIED', 'verified_at_utc': '2026-07-23T00:00:00Z'}
    relationship = {'status': 'PASS', 'relationship': 'Pending'}
    after = module.make_row(program, app, relationship, list(before), None)
    assert_only_application_state_changed(before, after)
    assert after[10] == 'applied_pending_brand_approval'
    assert after[18] == ''
