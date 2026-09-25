"""Hostile delegated-ID and current-writer authority regressions.

These tests are provider-free. They exercise only static discovery and import-time
hard stops; no Google client is constructed and no Sheet call can execute.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path("/home/ubuntu/business")
AUTHORITY_PATH = ROOT / "scripts/check_affiliate_sheet_writer_authority.py"

SPECIFIED_UNCOVERED_WRITERS = {
    "scripts/run_awin_autonomous_pipeline_20260804.py",
    "scripts/reconcile_named_approval_dispositions_20260804.py",
    "scripts/repair_affiliate_sheet_vertical_gap_20260731.py",
}

# Static analysis also reaches five more delegated/direct one-shot carriers that
# obtain the canonical ID from an imported carrier rather than a local literal.
ALL_DELEGATED_CURRENT_ONE_SHOTS = SPECIFIED_UNCOVERED_WRITERS | {
    "scripts/apply_mrmr_operator_skip_20260731.py",
    "scripts/apply_section10_marketplace_sheet_20260731.py",
    "scripts/apply_section10_provider_p1_sheet_20260731.py",
    "scripts/correct_mrmr_sheet_criterion_20260731.py",
    "scripts/reconcile_human_wall_duplicate_append_20260802.py",
}


def load_authority(name: str):
    spec = importlib.util.spec_from_file_location(name, AUTHORITY_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_hostile_delegated_sheet_id_writer_is_discovered_and_rejected(monkeypatch, tmp_path: Path) -> None:
    authority = load_authority("authority_hostile_delegated_id")
    (tmp_path / "shared.py").write_text(f"SHEET_ID = {authority.SHEET_ID!r}\n")
    writer = tmp_path / "delegated_bypass.py"
    writer.write_text(
        "from shared import SHEET_ID as TARGET\n"
        "service.spreadsheets().values().update(\n"
        "    spreadsheetId=TARGET, range=\"'Affiliate Applications'!A2\", body={'values': [['x']]},\n"
        ")\n"
    )
    monkeypatch.setattr(authority, "ROOT", tmp_path)
    monkeypatch.setattr(authority, "ACTIVE_ALLOWLIST", set())
    monkeypatch.setattr(authority, "PREPARED_MIGRATION_ALLOWLIST", {})

    assert authority.discover() == [("delegated_bypass.py", writer.read_text())]
    result = authority.audit()

    assert result["status"] == "FAIL"
    assert result["writer_classifications"]["delegated_bypass.py"] == "unapproved_canonical_writer"
    assert {"kind": "unapproved_writer_not_retired", "path": "delegated_bypass.py"} in result["issues"]


def test_affiliate_affine_indeterminate_direct_writer_fails_closed(monkeypatch, tmp_path: Path) -> None:
    authority = load_authority("authority_indeterminate_target")
    writer = tmp_path / "affiliate_runtime_writer.py"
    writer.write_text(
        "TAB = 'Affiliate Applications'\n"
        "def mutate(service, delegated_sheet_id):\n"
        "    service.spreadsheets().values().batchUpdate(\n"
        "        spreadsheetId=delegated_sheet_id, body={'valueInputOption': 'RAW', 'data': []},\n"
        "    )\n"
    )
    monkeypatch.setattr(authority, "ROOT", tmp_path)
    monkeypatch.setattr(authority, "ACTIVE_ALLOWLIST", set())
    monkeypatch.setattr(authority, "PREPARED_MIGRATION_ALLOWLIST", {})

    result = authority.audit()

    assert result["status"] == "FAIL"
    assert result["writer_classifications"]["affiliate_runtime_writer.py"] == "indeterminate_direct_writer"
    assert {"kind": "direct_writer_target_indeterminate", "path": "affiliate_runtime_writer.py"} in result["issues"]


def test_exact_current_delegated_writer_paths_are_discovered_and_hard_retired() -> None:
    authority = load_authority("authority_current_delegated_paths")
    result = authority.audit()
    discovered = {path for path, _source in authority.discover()}

    assert result["status"] == "PASS"
    assert SPECIFIED_UNCOVERED_WRITERS <= discovered
    assert ALL_DELEGATED_CURRENT_ONE_SHOTS <= discovered
    assert ALL_DELEGATED_CURRENT_ONE_SHOTS <= set(result["retired_writer_paths"])
    assert result["destructive_active_writer_paths"] == []
    assert result["controlled_destructive_active_writer_paths"] == [
        "scripts/affiliate_sheet_batch_writer.py",
    ]
    assert result["indeterminate_writer_paths"] == []
    assert (
        len(result["active_guarded_writer_paths"])
        + len(result["prepared_migration_carriers"])
        + len(result["retired_writer_paths"])
    ) == result["direct_writer_count"]


def test_active_partner_reply_writer_envelope_rejects_identity_and_column_escape() -> None:
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        spec = importlib.util.spec_from_file_location(
            "partner_reply_envelope", ROOT / "scripts/process_partner_info_replies.py",
        )
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)

    before = [""] * 27
    before[2] = "OrderlyMeds"
    intended = {14: "https://example.invalid/tracking", 19: True}
    after = list(before)
    for column, value in intended.items():
        after[column] = value
    module.require_partner_reply_postimage(before, after, intended)

    identity_drift = list(after)
    identity_drift[2] = "Impact.com"
    with pytest.raises(RuntimeError, match="partner_reply_non_target_drift"):
        module.require_partner_reply_postimage(before, identity_drift, intended)
    with pytest.raises(RuntimeError, match="partner_reply_mutation_envelope_violation"):
        module.require_partner_reply_postimage(before, before, {2: "Impact.com"})


@pytest.mark.parametrize("relative", sorted(ALL_DELEGATED_CURRENT_ONE_SHOTS))
def test_consumed_delegated_one_shot_cannot_import_or_activate(relative: str) -> None:
    path = ROOT / relative
    spec = importlib.util.spec_from_file_location(f"retired_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    with pytest.raises(SystemExit, match="^RETIRED_CANONICAL_AFFILIATE_SHEET_WRITER_V1$"):
        spec.loader.exec_module(module)
