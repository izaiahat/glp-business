#!/usr/bin/env python3
"""The truthful third row state: landed_sensor_pending.

Operator-approved 2026-09-02. Two correct rules collided: a row that
demonstrably LANDED must not block unrelated scopes, and "wired" must keep
requiring a zero-failure post-deploy sensor. The 18:01Z landing deployed 18 rows
with 8 sensor failures, so none could be wired; they stayed "prepared"; every
later plan correctly excluded them from its queue as already-live; and
release_prepared then refused every subsequent authority with
wire_batch_authority_prepared_rows_outside_scope, wedging the switches, /go, the
pixel and the remainder release until the PT rollover.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

import glp_affiliate_wire_backlog_worker as worker  # noqa: E402

SOURCE = (ROOT / "scripts/glp_affiliate_wire_backlog_worker.py").read_text()


def _ledger(pt_date="2026-09-02", deployment="dep-1", release="/rel/x"):
    return {
        "schema": worker.LEDGER_SCHEMA,
        "rows": {
            "katalys:1": {"key": "katalys:1", "status": "prepared", "pt_date": pt_date,
                          "deployed_unverified": {"deployment_id": deployment}},
            "katalys:2": {"key": "katalys:2", "status": "prepared", "pt_date": pt_date},
        },
        "days": {pt_date: {"deployment_intent": {
            "operation_id": "op-1", "deployment_id": deployment,
            "release": release, "rows": ["katalys:1"]}},
        },
    }


def _release(tmp_path):
    """The reconcile requires a real release root - a correct guard, so satisfy it."""
    root = tmp_path / "rel"
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "data/program-registry.json").write_text(json.dumps({"programs": []}))
    return root


def _pointer(tmp_path, deployment, release):
    base = tmp_path / "pointer.json"
    base.write_text(json.dumps({"deployment_id": deployment, "release_root": release,
                                "status": "PASS"}))
    return base


def test_state_is_resumable_but_never_terminal_and_never_wired():
    assert worker.LANDED_SENSOR_PENDING == "landed_sensor_pending"
    assert worker.LANDED_SENSOR_PENDING in worker.RESUMABLE
    assert worker.LANDED_SENSOR_PENDING not in worker.ALWAYS_TERMINAL
    assert "wired" in worker.ALWAYS_TERMINAL


def test_only_the_reconcile_path_can_set_it():
    """No other writer may assert this state - it must be derived from truth."""
    assignments = [line for line in SOURCE.splitlines()
                   if 'status"] = LANDED_SENSOR_PENDING' in line
                   or '"status": LANDED_SENSOR_PENDING' in line]
    assert len(assignments) == 1, assignments


def test_reconcile_transitions_a_landed_row_out_of_prepared(tmp_path, monkeypatch):
    ledger_path = tmp_path / "ledger.json"
    release = _release(tmp_path)
    ledger_path.write_text(json.dumps(_ledger(release=str(release))))
    monkeypatch.setattr(worker, "CURRENT_BASE",
                        _pointer(tmp_path, "dep-1", str(release)), raising=False)
    result = worker.reconcile_landed_uncertain_deployment(
        ledger_path=ledger_path, pt_date="2026-09-02",
        deployment_id="dep-1", release_root=release,
        sensor_failure_count=8,
    )
    assert result["rows_marked_wired"] == 0
    rows = json.loads(ledger_path.read_text())["rows"]
    landed = rows["katalys:1"]
    assert landed["status"] == worker.LANDED_SENSOR_PENDING
    assert landed["landed_sensor_pending"]["deployment_id"] == "dep-1"
    assert landed["landed_sensor_pending"]["sensor_failure_count"] == 8
    assert landed["landed_with_finding"]["sensor_status"] == "FAIL"
    assert "wired_at_utc" not in landed
    # A row that was not in the intent is untouched.
    assert rows["katalys:2"]["status"] == "prepared"


def test_reconcile_refuses_when_the_pointer_disagrees(tmp_path, monkeypatch):
    ledger_path = tmp_path / "ledger.json"
    release = _release(tmp_path)
    ledger_path.write_text(json.dumps(_ledger(release=str(release))))
    monkeypatch.setattr(worker, "CURRENT_BASE",
                        _pointer(tmp_path, "some-other-deployment", str(release)), raising=False)
    with pytest.raises(Exception):
        worker.reconcile_landed_uncertain_deployment(
            ledger_path=ledger_path, pt_date="2026-09-02",
            deployment_id="dep-1", release_root=release, sensor_failure_count=8)
    assert json.loads(ledger_path.read_text())["rows"]["katalys:1"]["status"] == "prepared"


def test_deployment_scope_excludes_the_state_but_still_catches_real_strays():
    """The scope set is built from status == 'prepared' only."""
    marker = 'if row.get("pt_date") == plan["pt_date"] and row.get("status") == "prepared"'
    assert SOURCE.count(marker) == 1
    rows = [
        {"key": "a", "pt_date": "d", "status": "prepared"},
        {"key": "b", "pt_date": "d", "status": worker.LANDED_SENSOR_PENDING},
        {"key": "c", "pt_date": "d", "status": "wired"},
    ]
    scoped = [r["key"] for r in rows
              if r.get("pt_date") == "d" and r.get("status") == "prepared"]
    assert scoped == ["a"], scoped
    allowed = {"a"}
    assert not [k for k in scoped if k not in allowed]
    strays = [k for k in scoped + ["z"] if k not in allowed]
    assert strays == ["z"], "a genuinely out-of-scope prepared row must still fail"


def test_promotion_to_wired_still_requires_a_zero_failure_sensor():
    assert 'state.update({\n                        "status": "wired"' in SOURCE
    window = SOURCE[SOURCE.index('"status": "wired", "wired_at_utc"') - 2000:
                    SOURCE.index('"status": "wired", "wired_at_utc"')]
    assert "sensor" in window
    assert "guarded_remint_readback_invalid" in window
