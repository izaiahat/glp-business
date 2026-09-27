"""GLP96 source snapshot -> real drain -> real sole-writer, fixture transport only."""
import copy
import importlib.util
import json
import re
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from support.google_stubs import google_import_stubs

with google_import_stubs():
    import affiliate_supply_discovery as producer
    import glp_sheet_sync_drain as drain
    from affiliate_marketplace_update_sheet import load_supply_plan

writer = drain.writer
CAPTURE = "reports/ops/glp96-research-intake-followthrough-20260927/two-prospects-report-only.json"
PROSPECTS = json.loads((ROOT / CAPTURE).read_text())


class Request:
    def __init__(self, result):
        self.result = result
    def execute(self):
        return self.result() if callable(self.result) else self.result


class FixtureSheet:
    """Only the Google transport; projections/CAS/writes/readback are unmodified."""
    def __init__(self):
        self.rows = [list(writer.HEADERS), [""] * len(writer.HEADERS)]
        self.rows[1][5] = "https://example.org/programs/1630"  # format source
        self.rows[1][2] = "Format source"
        for row_num in range(4, 13):
            while len(self.rows) < row_num:
                self.rows.append([""] * len(writer.HEADERS))
            self.rows[row_num - 1][2] = f"Reserved parent {row_num}"
        self.writes = 0
    def spreadsheets(self):
        return self
    def values(self):
        return self
    def get(self, **kwargs):
        if "range" in kwargs:
            return Request(lambda: {"values": copy.deepcopy(self.rows)})
        return Request({"sheets": [{"properties": {"title": writer.TAB, "sheetId": 1,
                    "gridProperties": {"rowCount": 12, "columnCount": len(writer.HEADERS)}}}]})
    def batchUpdate(self, **kwargs):
        if "body" in kwargs and "data" in kwargs["body"]:
            def write():
                for item in kwargs["body"]["data"]:
                    match = re.search(r"!([A-Z]+)(\d+)$", item["range"])
                    col = 0
                    for ch in match.group(1):
                        col = col * 26 + ord(ch) - 64
                    row = int(match.group(2))
                    while len(self.rows) < row:
                        self.rows.append([""] * len(writer.HEADERS))
                    self.rows[row - 1][col - 1] = item["values"][0][0]
                    self.writes += 1
                return {}
            return Request(write)
        return Request({})


@pytest.fixture
def lane(monkeypatch, tmp_path):
    sheet = FixtureSheet()
    monkeypatch.setattr(writer, "build", lambda *a, **kw: sheet)
    monkeypatch.setattr(writer, "creds", lambda: None)
    monkeypatch.setattr(writer, "require_checkbox_schema_pass", lambda *a: {"status": "PASS"})
    monkeypatch.setattr(drain, "read_sheet", lambda svc, sleep=None: copy.deepcopy(sheet.rows))
    return sheet, tmp_path


def queue():
    return producer.build_research_only_queue_rows(PROSPECTS, capture_path=CAPTURE)


def run(lane, rows):
    sheet, root = lane
    folder = root / f"run-{len(list(root.iterdir()))}"
    folder.mkdir()
    return drain.drain(rows, folder, apply=True, sleep=lambda _: None)


def test_two_real_sources_create_held_rows_without_application_queue(lane):
    rows = queue()
    assert len(rows) == 2
    assert all(r["kind"] == "research_only_provider" for r in rows)
    assert "application" not in {r["kind"] for r in rows}
    result = run(lane, rows)
    assert [r["status"] for r in result["results"]] == ["applied", "applied"], [r.get("reason") for r in result["results"]]
    sheet = lane[0]
    held = [r for r in sheet.rows if r[10] == "research_only_hold"]
    for row, prospect in zip(held, PROSPECTS):
        assert row[2] == prospect["identity"] and row[10] == "research_only_hold"
        assert row[1] is False and row[11] is False and row[19] is False and row[20] is False
        assert not any(row[c] for c in (5, 8, 9, 12, 13, 14, 15, 16, 17, 18, 22, 26))
        assert row[24] in [o["observed_at"] for o in prospect["source_observations"]]
        assert prospect["unknowns_and_application_hold"] in row[23]
        assert row[4] == "Research-only provider"
    assert "2568723" in held[0][23] and "clinical payable event" in held[0][23]
    assert "null" in held[1][23]  # Mondae programme/rail stays unknown


def test_duplicate_replay_is_zero_writes(lane):
    rows = queue()
    run(lane, rows)
    before = lane[0].writes
    replay = run(lane, rows)
    assert lane[0].writes == before
    assert all(r["status"] == "noop" and r["cells_written"] == 0 for r in replay["results"])


def test_identity_conflict_across_network_refuses(lane):
    row = queue()[0]
    lane[0].rows.append([""] * len(writer.HEADERS))
    lane[0].rows[-1][2] = row["program_name"]
    lane[0].rows[-1][4] = "CJ Affiliate"
    lane[0].rows[-1][10] = "approved_active_tracking_ready"
    result = run(lane, [row])["results"][0]
    assert result["status"] == "refused" and "identity_conflict" in result["reason"]
    assert lane[0].writes == 0


def test_conflicting_existing_research_row_refuses_instead_of_rewriting(lane):
    run(lane, queue()[:1])
    lane[0].rows[2][23] = "different source fact"
    writes = lane[0].writes
    result = run(lane, queue()[:1])
    assert result["results"][0]["status"] == "refused"
    assert lane[0].writes == writes


def test_source_hash_drift_refuses(lane, monkeypatch, tmp_path):
    row = queue()[0]
    altered = tmp_path / "capture.json"
    altered.write_text((ROOT / CAPTURE).read_text().replace("Mondae", "changed"))
    monkeypatch.setattr(writer, "_REPO_ROOT", tmp_path)
    # Use the real relative path and its original digest, not a refreshed digest.
    target = tmp_path / CAPTURE
    target.parent.mkdir(parents=True)
    target.write_bytes(altered.read_bytes())
    result = run(lane, [row])["results"][0]
    assert result["status"] == "refused" and "hash_drift" in result["reason"]
    assert lane[0].writes == 0


def test_raw_source_hash_drift_refuses(lane, monkeypatch, tmp_path):
    import shutil
    row = queue()[0]
    snapshot = tmp_path / CAPTURE
    snapshot.parent.mkdir(parents=True)
    shutil.copyfile(ROOT / CAPTURE, snapshot)
    for observation in PROSPECTS[0]["source_observations"]:
        source = tmp_path / observation["source_path"]
        source.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / observation["source_path"], source)
    (tmp_path / PROSPECTS[0]["source_observations"][1]["source_path"]).write_text("drift")
    monkeypatch.setattr(writer, "_REPO_ROOT", tmp_path)
    result = run(lane, [row])["results"][0]
    assert result["status"] == "refused" and "source_hash_drift" in result["reason"]
    assert lane[0].writes == 0


@pytest.mark.parametrize("field,value", [("14", "https://tracker.example/"), ("1", "TRUE"),
                                          ("10", "ready_to_apply"), ("11", "TRUE"),
                                          ("19", "TRUE"), ("20", "TRUE"),
                                          ("16", "1.00"), ("9", "walgreens-review")])
def test_forbidden_cell_claim_refuses(lane, field, value):
    row = queue()[0]
    entry = drain.build_entry(row["kind"], row, drain.parse_evidence(row), None, None)
    entry["cells"][field] = value
    plan = drain.plan_for(row["kind"], entry, row)
    with pytest.raises(RuntimeError, match="forbidden_or_conflicting_cells"):
        writer.sheet_sync_plan_rows(row["kind"], plan)
    assert lane[0].writes == 0


def test_forged_approval_evidence_refuses(lane):
    row = queue()[0]
    evidence = json.loads(row["evidence"])
    evidence["approval_date"] = "2026-09-27"
    row["evidence"] = json.dumps(evidence)
    result = run(lane, [row])["results"][0]
    assert result["status"] == "refused"
    assert lane[0].writes == 0


def test_queue_network_spoof_refuses(lane):
    row = dict(queue()[1], network="Direct")
    result = run(lane, [row])["results"][0]
    assert result["status"] in {"refused", "escalated"}
    assert lane[0].writes == 0


def test_reproduced_legacy_parser_refusal_and_ready_to_apply_unchanged(tmp_path):
    legacy = tmp_path / "held.json"
    legacy.write_text(json.dumps({"schema": "affiliate-supply-sheet-candidates-v1",
                                  "candidate_status": "research_only_hold",
                                  "never_marked_approved": True, "candidates": [{}]}))
    with pytest.raises(RuntimeError, match="supply_plan_candidate_contract_invalid"):
        load_supply_plan(legacy)
    valid = {"schema": "affiliate-supply-sheet-candidates-v1",
             "candidate_status": "ready_to_apply", "never_marked_approved": True,
             "candidates": [{"provider": "OrderlyMeds", "program_url": "https://orderlymeds.com/affiliate-program/get-started/",
                             "official_url": "https://orderlymeds.com/", "network_route": "Direct affiliate",
                             "hosted_network": "direct", "vertical": "glp1-weightloss",
                             "category": "GLP-1 / metabolic health", "fit_reason": "Discovered program_page; GLP fit tier explicit_glp1.",
                             "commission_terms": "$75 per referral", "evidence": {"tier": "explicit_glp1", "reasons": ["glp_fit:explicit_glp1"], "source": "program_page", "discovered_by": "semaglutide affiliate program", "sha256": "a" * 64}}]}
    legacy.write_text(json.dumps(valid))
    assert load_supply_plan(legacy)["candidate_status"] == "ready_to_apply"
