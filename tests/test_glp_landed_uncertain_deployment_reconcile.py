#!/usr/bin/env python3
"""An armed deployment intent must be resolvable from external truth.

_record_deployment_effect_uncertain arms an intent that only the sensor-PASS
path clears, so a deployment that LANDED with a failing sensor blocked every
later run forever. The reconciler resolves it from the current-content-base
pointer and never grants `wired`.
"""
from __future__ import annotations
import json, sys
from pathlib import Path

BUSINESS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUSINESS / "scripts"))
import glp_affiliate_wire_backlog_worker as worker  # noqa: E402

DEP = "2f28ee55-20f7-4ecd-837b-4319de399b3a"
PT = "2026-09-01"


def _setup(tmp_path, monkeypatch, *, pointer_dep=DEP, pointer_release=None):
    release = tmp_path / "release"
    (release / "data").mkdir(parents=True)
    (release / "data/program-registry.json").write_text(json.dumps({"programs": []}))
    pointer = tmp_path / "current-content-base.json"
    pointer.write_text(json.dumps({
        "deployment_id": pointer_dep,
        "release_root": str(pointer_release or release),
    }))
    monkeypatch.setattr(worker, "CURRENT_BASE", pointer)
    ledger = tmp_path / "ledger.json"
    intent = {"operation_id": "wire-deploy:" + "a" * 32, "rows": ["xentraffic:1", "xentraffic:2"]}
    ledger.write_text(json.dumps({
        "schema": worker.LEDGER_SCHEMA if hasattr(worker, "LEDGER_SCHEMA") else "b2-wire-backlog-worked-ledger-v1",
        "rows": {
            "xentraffic:1": {"key": "xentraffic:1", "status": "prepared", "pt_date": PT,
                             "deployment_intent": dict(intent),
                             "deployed_unverified": {"deployment_id": DEP}},
            "xentraffic:2": {"key": "xentraffic:2", "status": "prepared", "pt_date": PT,
                             "deployment_intent": dict(intent),
                             "deployed_unverified": {"deployment_id": DEP}},
        },
        "days": {PT: {"wired_keys": [], "blocked_keys": [], "deployment_intent": dict(intent)}},
    }))
    return ledger, release


def test_resolves_the_intent_and_never_marks_wired(tmp_path, monkeypatch):
    ledger, release = _setup(tmp_path, monkeypatch)
    res = worker.reconcile_landed_uncertain_deployment(
        ledger_path=ledger, pt_date=PT, deployment_id=DEP, release_root=release)
    assert res["status"] == "PASS_RECONCILED_LANDED_WITH_FINDING"
    assert res["rows_marked_wired"] == 0
    data = json.loads(ledger.read_text())
    day = data["days"][PT]
    assert "deployment_intent" not in day
    assert day["wired_keys"] == []
    assert day["deployment"]["status"] == "LANDED_SENSOR_NOT_VERIFIED"
    for key in ("xentraffic:1", "xentraffic:2"):
        row = data["rows"][key]
        assert "deployment_intent" not in row and "deployed_unverified" not in row
        assert row["status"] == "prepared", "rows stay prepared so the next release re-releases them"
        assert row["landed_with_finding"]["deployment_id"] == DEP
        assert row["deployed_unverified_history"]


def test_refuses_a_deployment_the_pointer_does_not_name(tmp_path, monkeypatch):
    ledger, release = _setup(tmp_path, monkeypatch, pointer_dep="00000000-0000-0000-0000-000000000000")
    try:
        worker.reconcile_landed_uncertain_deployment(
            ledger_path=ledger, pt_date=PT, deployment_id=DEP, release_root=release)
    except worker.B2Error as exc:
        assert "reconcile_deployment_id_not_current_base" in str(exc)
    else:
        raise AssertionError("must refuse a deployment id the pointer does not name")


def test_refuses_a_release_root_the_pointer_does_not_name(tmp_path, monkeypatch):
    other = tmp_path / "other"
    (other / "data").mkdir(parents=True)
    ledger, release = _setup(tmp_path, monkeypatch, pointer_release=other)
    try:
        worker.reconcile_landed_uncertain_deployment(
            ledger_path=ledger, pt_date=PT, deployment_id=DEP, release_root=release)
    except worker.B2Error as exc:
        assert "reconcile_release_root_not_current_base" in str(exc)
    else:
        raise AssertionError("must refuse a release root the pointer does not name")


def test_is_idempotent(tmp_path, monkeypatch):
    ledger, release = _setup(tmp_path, monkeypatch)
    worker.reconcile_landed_uncertain_deployment(
        ledger_path=ledger, pt_date=PT, deployment_id=DEP, release_root=release)
    again = worker.reconcile_landed_uncertain_deployment(
        ledger_path=ledger, pt_date=PT, deployment_id=DEP, release_root=release)
    assert again["status"] == "PASS_NO_ARMED_INTENT" and again["changed"] is False
