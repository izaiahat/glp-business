from __future__ import annotations

import importlib.util
import hashlib
import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path("/home/ubuntu/business")
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


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
    spec.loader.exec_module(module)
    return module


def valid_plan() -> dict:
    return {
        "schema": "affiliate-approved-unlinked-disposition-plan-v1",
        "authority_sha256": "a" * 64,
        "tracking_clicks": 0,
        "provider_writes": 0,
        "rows": [{
            "row": 333,
            "program": "GLP-1 Patches - Premium Weight Management Support",
            "cells": {
                "10": "blocked_provider_tracking_link_unavailable",
                "19": "FALSE",
                "23": "Provider link dialog exposes no approved destination, so no link can be issued.",
                "24": "2026-08-16T17:00:00Z",
            },
        }],
    }


def sheet_values(module, *, status: str = "approved_active_not_wired", link: str = "") -> list[list[object]]:
    values: list[list[object]] = [list(module.HEADERS)] + [[] for _ in range(332)]
    row: list[object] = [""] * len(module.HEADERS)
    row[2] = "GLP-1 Patches - Premium Weight Management Support"
    row[10] = status
    row[11] = True
    row[14] = link
    row[19] = False
    row[20] = False
    row[25] = "glp1-weightloss"
    values[332] = row
    return values


def test_terminal_disposition_is_narrow_link_dark_and_idempotent() -> None:
    module = load("affiliate_sheet_disposition", "scripts/affiliate_sheet_batch_writer.py")
    plan = valid_plan()
    rows = module.approved_unlinked_disposition_rows(plan)
    values = sheet_values(module)

    module.require_unlinked_disposition_preconditions(values, rows)
    changes = module.wire_in_changed_cells(values, rows)

    assert {(row, column) for row, column, _ in changes} == {(333, 10), (333, 23), (333, 24)}
    assert all(column != 14 for _, column, _ in changes)
    for _, column, expected in changes:
        values[332][column] = module.wire_in_write_value(column, expected)
    module.require_unlinked_disposition_preconditions(values, rows)
    assert module.wire_in_changed_cells(values, rows) == []


def test_terminal_disposition_cannot_create_readiness_or_overwrite_a_link() -> None:
    module = load("affiliate_sheet_disposition_fail_closed", "scripts/affiliate_sheet_batch_writer.py")
    plan = valid_plan()
    plan["rows"][0]["cells"]["19"] = "TRUE"
    with pytest.raises(RuntimeError, match="must_remain_not_ready"):
        module.approved_unlinked_disposition_rows(plan)

    rows = module.approved_unlinked_disposition_rows(valid_plan())
    with pytest.raises(RuntimeError, match="tracking_link_present"):
        module.require_unlinked_disposition_preconditions(
            sheet_values(module, link="https://provider.example/issued"), rows
        )


def test_approved_unlinked_health_check_excludes_marketplace_parent_but_not_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    sensor = load("business_output_health", "scripts/business_output_health_sensor.py")
    import glp_affiliate_wire_backlog_worker as worker

    headers = [
        "Program / Network", "Status", "Signup Complete?", "Tracking Link / Deep Link",
        "Wire-In Ready?", "Wired Live?", "Vertical",
    ]
    healthtrader = [
        "HealthTrader", "approved_marketplace_access", True, "", False, False, "glp1-weightloss",
    ]
    provider = [
        "Provider", "approved_active_not_wired", True, "", False, False, "glp1-weightloss",
    ]
    backrun = tmp_path / "backrun"
    backrun.mkdir()
    export = backrun / "sheet-full-row-export.json"
    export.write_text(json.dumps({"headers": headers, "rows": {"347": healthtrader, "333": provider}}))
    monkeypatch.setattr(worker, "latest_complete_backrun", lambda: backrun)

    result = sensor.affiliate_approved_but_unlinked()
    assert result["ok"] is False
    assert "1 stranded" in result["actual"]

    export.write_text(json.dumps({"headers": headers, "rows": {"347": healthtrader}}))
    result = sensor.affiliate_approved_but_unlinked()
    assert result["ok"] is True


def test_approved_unlinked_health_check_excludes_network_rail_and_legal_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    sensor = load("business_output_health_rail", "scripts/business_output_health_sensor.py")
    import glp_affiliate_wire_backlog_worker as worker

    headers = [
        "Program / Network", "Category", "Status", "Signup Complete?",
        "Tracking Link / Deep Link", "Wire-In Ready?", "Wired Live?", "Vertical",
    ]
    network_rail = [
        "XenTraffic", "Network rail", "approved_active_not_wired", True,
        "", False, False, "glp1-weightloss",
    ]
    legal_duplicate = [
        "HealthRX", "GLP-1 / metabolic health", "approved_superseded_by_existing_placement", True,
        "", False, False, "glp1-weightloss",
    ]
    provider = [
        "Provider", "GLP-1 / metabolic health", "approved_active_not_wired", True,
        "", False, False, "glp1-weightloss",
    ]
    backrun = tmp_path / "backrun"
    backrun.mkdir()
    export = backrun / "sheet-full-row-export.json"
    export.write_text(json.dumps({
        "headers": headers,
        "rows": {"407": network_rail, "489": legal_duplicate, "333": provider},
    }))
    monkeypatch.setattr(worker, "latest_complete_backrun", lambda: backrun)

    result = sensor.affiliate_approved_but_unlinked()
    assert result["ok"] is False
    assert result["actual"] == "1 stranded: Provider"

    export.write_text(json.dumps({
        "headers": headers,
        "rows": {"407": network_rail, "489": legal_duplicate},
    }))
    result = sensor.affiliate_approved_but_unlinked()
    assert result["ok"] is True


def test_r2_sensor_requires_output_not_fresh_working_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    sensor = load("business_output_health_r2", "scripts/business_output_health_sensor.py")
    run = tmp_path / "reports/glp-clinic-scout/seo-machine/r2-update-carrier/2026-08-21/day-3"
    run.mkdir(parents=True)
    (run / "working.json").write_text("{}")
    output = tmp_path / ".hermes/cron/output/a0e87c0a4569/latest.md"
    output.parent.mkdir(parents=True)
    output.write_text("PARTIAL · 0 refreshed")
    monkeypatch.setattr(sensor, "BUSINESS", tmp_path)
    monkeypatch.setattr(sensor, "HERMES", tmp_path / ".hermes")
    monkeypatch.setattr(sensor, "now_pt", lambda: sensor.dt.datetime(
        2026, 8, 21, 18, 0, tzinfo=sensor.PT,
    ))

    result = sensor.seo_update_carrier()
    assert result["ok"] is False

    receipt = {
        "schema": "glp-seo-r2-day-receipt-v1",
        "status": "PASS_NEVER_ZERO_QUOTA_IN_PROGRESS",
        "pt_date": "2026-08-21",
        "required": 2,
        "completed": 0,
        "published": 1,
        "deficit": 2,
    }
    receipt["receipt_sha256"] = hashlib.sha256(json.dumps(
        receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode()).hexdigest()
    (run / "r2-day-receipt.json").write_text(json.dumps(receipt))
    result = sensor.seo_update_carrier()
    assert result["ok"] is True
    assert "published=1" in result["actual"]

    tampered = json.loads((run / "r2-day-receipt.json").read_text())
    tampered["published"] = 2
    (run / "r2-day-receipt.json").write_text(json.dumps(tampered))
    result = sensor.seo_update_carrier()
    assert result["ok"] is False
    assert "hash_ok=False" in result["actual"]


def test_sensor_carries_no_retired_business_checks() -> None:
    """Boring AI / Allocant / TLCC are retired businesses (operator 2026-09-09).

    This sensor is GLP-only. A retired lane's check can never produce, so it
    would report red forever and train the operator to scroll past the sensor —
    the exact failure the sensor exists to prevent.
    """
    sensor = load("business_output_health_glp_only", "scripts/business_output_health_sensor.py")
    names = [fn.__name__ for fn in sensor.CHECKS]
    assert not [n for n in names if any(
        token in n for token in ("boring", "allocant", "tlcc", "avatar")
    )], names
    assert not hasattr(sensor, "boring_finder")
    assert not hasattr(sensor, "boring_promotion")
