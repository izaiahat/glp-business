"""Phase 1b: the bridge-authorized widening of the marketplace-parent guard.

`require_marketplace_parent_rows_not_mutated` refuses every ordinary governed
mode on a marketplace PARENT row — the account-level rows (Awin 4, Skimlinks
427, Admitad 433, Digistore24 358, MaxBounty 470 …) that the whole Sheet's
identity hangs off. Until 2026-08-20 exactly ONE mode was exempt,
`marketplace_parent_relabel`, and only for the rows in its own plan.

That left four backlog rows stuck (PHASE1-PROOF §7.1): a portal readback and an
operator's own written ruling both say something that is ONLY ever true about an
account-level row, and neither could say it.

Bridge authorization, 2026-08-20, recorded in
`reports/glp-clinic-scout/affiliate-machine/PHASE1B-BUILD-20260819.md`:
`portal_readback_confirmed` and `operator_status_ruling` may mutate their own
canonical marketplace parent row, evidence-cited, under the SAME narrowing
`marketplace_parent_relabel` uses — never a child programme row, never another
marketplace's parent row, never a parent row the plan does not name under its
canonical name. Direct operator authorization on 2026-08-21 extended that exact
per-row rule to `approval_confirmed` so account-level network approvals can land
on their existing canonical parent instead of creating a duplicate row.

These tests pin the narrowing, not the widening: the interesting assertions are
the ones that still REFUSE.
"""

from __future__ import annotations

import hashlib
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
def load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


W = load('parent_authorization_writer_under_test', 'scripts/affiliate_sheet_batch_writer.py')

#: The four rows PHASE1-PROOF §7.1 recorded as blocked, with their canonical
#: parent names straight out of `REQUIRED_MARKETPLACE_PARENT_ROWS`.
BLOCKED_FOUR = {'Digistore24': 358, 'Skimlinks': 427, 'Admitad': 433,
                'MaxBounty': 470}

AUTHORIZED_MODES = ('approval_confirmed', 'portal_readback_confirmed',
                    'operator_status_ruling')


def plan(row: int, program: str) -> list[dict]:
    return [{'row': row, 'program': program}]


def changes(*pairs) -> list[tuple[int, int, object]]:
    return [(row, column, 'value') for row, column in pairs]


# ---------------------------------------------------------------------------
# The identity table the exemption is computed from
# ---------------------------------------------------------------------------


def test_the_four_blocked_rows_are_the_canonical_parent_rows_they_claim_to_be():
    for program, row in BLOCKED_FOUR.items():
        assert W.REQUIRED_MARKETPLACE_PARENT_ROWS[program] == row


def test_only_the_explicitly_authorized_modes_carry_the_new_flag():
    flagged = sorted(key for key, spec in W.SHEET_SYNC_MODES.items()
                     if spec.parent_authorized_on_own_canonical_row)
    assert flagged == sorted(AUTHORIZED_MODES), (
        'only the explicit per-parent modes may carry the flag; got %s' % flagged)


def test_the_original_exemption_is_untouched():
    """`marketplace_parent_relabel` keeps its own, broader flag. Widening one
    guard must not quietly re-shape another."""
    assert W.SHEET_SYNC_MODES['marketplace_parent_relabel'].parent_authorized is True
    for key in AUTHORIZED_MODES:
        assert W.SHEET_SYNC_MODES[key].parent_authorized is False, (
            '%s must NOT get the broad plan-wide exemption' % key)


# ---------------------------------------------------------------------------
# canonical_parent_rows: per row, from the identity table
# ---------------------------------------------------------------------------


def test_a_planned_row_that_is_its_own_parent_row_is_authorized():
    assert W.canonical_parent_rows(plan(427, 'Skimlinks')) == frozenset({427})


def test_a_parent_name_on_the_wrong_row_number_is_not_authorized():
    """The exemption is an identity check, not a name check. A queue row that
    says 'Skimlinks' but points at row 428 is exactly the corruption the guard
    exists to stop."""
    assert W.canonical_parent_rows(plan(428, 'Skimlinks')) == frozenset()


def test_an_ordinary_programme_row_is_never_authorized():
    assert W.canonical_parent_rows(plan(346, 'Coivas')) == frozenset()


def test_a_row_with_no_programme_name_is_never_authorized():
    assert W.canonical_parent_rows([{'row': 427, 'program': ''}]) == frozenset()
    assert W.canonical_parent_rows([{'row': 427}]) == frozenset()


# ---------------------------------------------------------------------------
# require_parent_row_protection: what the widening actually permits
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('mode', AUTHORIZED_MODES)
@pytest.mark.parametrize('program,row', sorted(BLOCKED_FOUR.items()))
def test_each_of_the_four_blocked_rows_now_passes_under_authorized_modes(mode, program, row):
    spec = W.SHEET_SYNC_MODES[mode]
    W.require_parent_row_protection(
        spec, changes((row, 10), (row, 23), (row, 24)), plan(row, program))


@pytest.mark.parametrize('mode', AUTHORIZED_MODES)
def test_an_authorized_mode_may_not_touch_a_DIFFERENT_parent_row(mode):
    """A batch that names Skimlinks' parent row exempts THAT row and nothing
    else. Awin's parent row (4) riding along in the same write is refused."""
    spec = W.SHEET_SYNC_MODES[mode]
    with pytest.raises(RuntimeError, match='required_marketplace_parent_row_mutation_blocked'):
        W.require_parent_row_protection(
            spec, changes((427, 10), (4, 10)), plan(427, 'Skimlinks'))


@pytest.mark.parametrize('mode', AUTHORIZED_MODES)
def test_an_authorized_mode_on_an_unplanned_parent_row_is_still_refused(mode):
    spec = W.SHEET_SYNC_MODES[mode]
    with pytest.raises(RuntimeError, match='required_marketplace_parent_row_mutation_blocked'):
        W.require_parent_row_protection(spec, changes((433, 10)), plan(346, 'Coivas'))


@pytest.mark.parametrize('mode', AUTHORIZED_MODES)
def test_an_authorized_mode_may_not_relabel_a_parent_row_it_misnames(mode):
    """`program` comes from the queue row. Claiming row 433 is 'Skimlinks' does
    not make it Skimlinks — the identity table says 433 is Admitad, so the plan
    is not on its OWN canonical row and the guard holds."""
    spec = W.SHEET_SYNC_MODES[mode]
    with pytest.raises(RuntimeError, match='required_marketplace_parent_row_mutation_blocked'):
        W.require_parent_row_protection(spec, changes((433, 10)), plan(433, 'Skimlinks'))


def test_every_other_mode_is_still_refused_on_the_same_four_rows():
    """The negative control for the whole change: the guard is not weaker for
    anyone else. Every governed mode WITHOUT the flag still bounces off row 427."""
    unflagged = [key for key, spec in W.SHEET_SYNC_MODES.items()
                 if not spec.parent_authorized
                 and not spec.parent_authorized_on_own_canonical_row]
    assert len(unflagged) > 5, 'expected many unflagged modes, got %d' % len(unflagged)
    for key in unflagged:
        with pytest.raises(RuntimeError,
                           match='required_marketplace_parent_row_mutation_blocked'):
            W.require_parent_row_protection(
                W.SHEET_SYNC_MODES[key], changes((427, 10)), plan(427, 'Skimlinks'))


@pytest.mark.parametrize('mode', AUTHORIZED_MODES)
def test_an_ordinary_programme_row_still_passes_untouched(mode):
    """These two modes run mostly on ordinary rows; the widening must not change
    anything about that path."""
    W.require_parent_row_protection(
        W.SHEET_SYNC_MODES[mode], changes((346, 10), (346, 23)), plan(346, 'Coivas'))


def test_the_broad_exemption_still_works_the_way_it_always_did():
    spec = W.SHEET_SYNC_MODES['marketplace_parent_relabel']
    W.require_parent_row_protection(spec, changes((427, 10)), plan(427, 'Skimlinks'))
    with pytest.raises(RuntimeError,
                       match='required_marketplace_parent_row_mutation_blocked'):
        W.require_parent_row_protection(spec, changes((433, 10)), plan(427, 'Skimlinks'))


# ---------------------------------------------------------------------------
# What the two modes may still not do, parent row or not
# ---------------------------------------------------------------------------


def test_no_authorized_mode_can_write_a_tracking_link_or_claim_live():
    """The exemption is about WHICH ROW, never about WHAT MAY BE SAID."""
    for key in AUTHORIZED_MODES:
        assert 14 not in W.SHEET_SYNC_MODES[key].mutable_columns, key   # tracking link
    # Both provider-truth modes write columns 19 and 20 and pin both FALSE, so
    # the only thing either can say about wiring is "not wired".
    readback = W.SHEET_SYNC_MODES['portal_readback_confirmed']
    assert readback.required_flags == {19: 'FALSE', 20: 'FALSE'}
    approval = W.SHEET_SYNC_MODES['approval_confirmed']
    assert approval.required_flags == {19: 'FALSE', 20: 'FALSE'}
    assert W.SHEET_SYNC_MODES['operator_status_ruling'].mutable_columns == (10, 23, 24)


def test_account_approval_identity_binds_to_exact_canonical_parent_without_synthetic_id_in_row():
    capture = ROOT / 'reports/glp-affiliate-auto-reconciler/maxbounty-approval-capture-20260821.json'
    entry = {
        'row': 470,
        'program': 'MaxBounty',
        'evidence': {
            'approval_capture_path': str(capture.relative_to(ROOT)),
            'approval_capture_sha256': hashlib.sha256(capture.read_bytes()).hexdigest(),
            'network_program_id': 'network-account',
        },
    }
    actual = W.pad([])
    actual[2] = 'MaxBounty'
    actual[4] = 'MaxBounty Network'
    # Restored parent rows predate the synthetic `network-account` marker; exact
    # parent row + name + capture identity is the account-level binding.
    W.require_program_identity_binding(
        W.SHEET_SYNC_MODES['approval_confirmed'], entry, 470, actual)
    with pytest.raises(RuntimeError, match='row_does_not_name_the_programme'):
        W.require_program_identity_binding(
            W.SHEET_SYNC_MODES['approval_confirmed'], entry, 469, actual)


def test_both_modes_still_demand_their_hash_bound_capture():
    for key in AUTHORIZED_MODES:
        fields = dict(W.SHEET_SYNC_MODES[key].evidence_fields)
        assert any(rule.startswith('sha256_of:') for rule in fields.values()), key


def test_the_operator_ruling_still_has_to_be_quoted_from_the_live_cell():
    """The only thing standing between 'the operator ruled' and 'someone typed a
    status' is that the quoted words must be in the row's LIVE Notes at write
    time. A parent-row exemption must not relax it."""
    assert W.SHEET_SYNC_MODES['operator_status_ruling'].require_operator_note_quote is True
