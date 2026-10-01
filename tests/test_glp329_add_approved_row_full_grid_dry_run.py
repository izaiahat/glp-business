"""GLP-329: add_approved_row dry-run on a full grid must not touch the Sheet.

Calls the real run_add_approved_row body with fixture Google transport only.
Plan validation (which hash-checks host-only /home/ubuntu/business portal
captures) is replaced by a pass-through; it is not what this regression pins.
"""
import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from support.google_stubs import google_import_stubs

with google_import_stubs():
    import affiliate_sheet_batch_writer as writer


class Request:
    def __init__(self, result):
        self.result = result
    def execute(self):
        return self.result() if callable(self.result) else self.result


class FullGridSheet:
    """Every grid row below the header carries business value; no blank row."""
    def __init__(self, row_count):
        self.rows = [list(writer.HEADERS)]
        for row_num in range(2, row_count + 1):
            row = [""] * len(writer.HEADERS)
            row[2] = f"Existing provider {row_num}"
            self.rows.append(row)
        self.rows[1][5] = "https://example.org/programs/1630"  # format source
        self.grid_row_count = row_count
        self.structural_calls = []
        self.value_calls = []
    def spreadsheets(self):
        return self
    def values(self):
        return self
    def get(self, **kwargs):
        if "range" in kwargs:
            return Request(lambda: {"values": copy.deepcopy(self.rows)})
        return Request({"sheets": [{"properties": {"title": writer.TAB, "sheetId": 7,
                    "gridProperties": {"rowCount": self.grid_row_count,
                                       "columnCount": len(writer.HEADERS)}}}]})
    def batchUpdate(self, **kwargs):
        body = copy.deepcopy(kwargs.get("body") or {})
        if "data" in body:
            return Request(lambda: self.value_calls.append(body) or {})
        def structure():
            self.structural_calls.append(body)  # appendDimension/schema/copyPaste
            for request in body.get("requests", []):
                if "appendDimension" in request:
                    self.grid_row_count += request["appendDimension"]["length"]
            return {}
        return Request(structure)


def approved_entry(program, provider_id):
    capture = "reports/fixture/katalys-inventory.json"
    captured_at = "2026-09-01T00:00:00Z"
    return {
        "program": program, "network": "Katalys", "provider_program_id": provider_id,
        "evidence": {"portal_capture_path": capture, "portal_capture_sha256": "b" * 64,
                     "captured_at_utc": captured_at, "relationship": "approved"},
        "cells": {"1": "TRUE", "2": program, "3": "Telehealth", "4": "Katalys",
                  "5": f"https://app.katalys.com/pub/org/explore/{provider_id}",
                  "6": "https://example.com/", "7": "Fixture category",
                  "10": "approved_active_not_wired", "11": "TRUE",
                  "19": "FALSE", "20": "FALSE",
                  "23": f"Portal-verified approval per {capture} captured {captured_at}.",
                  "24": "2026-09-01T00:00:00Z", "25": "glp1-weightloss"},
    }


def test_full_grid_dry_run_projects_created_rows_without_any_sheet_mutation(monkeypatch, tmp_path):
    sheet = FullGridSheet(row_count=40)
    monkeypatch.setattr(writer, "build", lambda *a, **kw: sheet)
    monkeypatch.setattr(writer, "creds", lambda: None)
    monkeypatch.setattr(writer, "require_checkbox_schema_pass", lambda *a: {"status": "PASS"})
    monkeypatch.setattr(writer, "add_approved_row_rows", lambda plan: plan["rows"])
    preimage_rows = copy.deepcopy(sheet.rows)
    preimage_grid = sheet.grid_row_count
    assert not any(not writer.row_has_business_value(row) for row in sheet.rows[1:])

    plan = {"schema": writer.ADD_APPROVED_ROW_PLAN_SCHEMA, "authority_sha256": "a" * 64,
            "tracking_clicks": 0, "provider_writes": 0,
            "rows": [approved_entry("Fixture Program One", "9001"),
                     approved_entry("Fixture Program Two", "9002")]}
    plan_path, receipt_path = tmp_path / "plan.json", tmp_path / "receipt.json"
    plan_path.write_text(json.dumps(plan))

    assert writer.run_add_approved_row(plan_path, receipt_path, apply=False) == 0
    receipt = json.loads(receipt_path.read_text())
    assert receipt["dry_run"] is True and receipt["no_sheet_write"] is True
    assert receipt["no_value_write"] is True
    # Both rows project into one growth reservation past the original grid.
    assert receipt["created_rows"] == [preimage_grid + 1, preimage_grid + 2]
    assert receipt["grid_before"]["rowCount"] == preimage_grid
    assert sheet.grid_row_count == preimage_grid
    assert sheet.rows == preimage_rows
    assert sheet.structural_calls == []
    assert sheet.value_calls == []
