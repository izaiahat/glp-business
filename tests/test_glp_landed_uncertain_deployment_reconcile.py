#!/usr/bin/env python3
"""Offline landed-intent regressions; no deploy, provider or Sheet operations.

Compile the exact checkout's reconciler and its real local ledger helpers. The
curated mirror omits the worker's host-only imports. This is function-contract
coverage of the non-zero/unknown sensor branch, not whole-module or live proof.
"""
from __future__ import annotations

import ast
import contextlib
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import types
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts/glp_affiliate_wire_backlog_worker.py"


def load_reconciler(source=None):
    tree = ast.parse(SOURCE.read_text() if source is None else source)
    functions = {
        "B2Error", "utc_now", "iso_utc", "read_json", "atomic_json",
        "day_deployment_history", "record_day_deployment", "locked_ledger",
        "fresh_ledger", "load_ledger", "save_ledger", "day_state",
        "reconcile_landed_uncertain_deployment",
    }
    constants = {"LEDGER_SCHEMA", "LANDED_SENSOR_PENDING"}
    selected = []
    found = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in functions:
            selected.append(node)
            found.add(node.name)
        elif isinstance(node, ast.Assign):
            names = {n.id for n in node.targets if isinstance(n, ast.Name)}
            if names & constants:
                selected.append(node)
                found.update(names & constants)
    assert found == functions | constants, "source dependency set changed"
    module = types.ModuleType("landed_reconcile_contract")
    module.__dict__.update(
        Path=Path, Any=Any, dt=dt, fcntl=fcntl, json=json, os=os,
        contextlib=contextlib, CURRENT_BASE=None,
    )
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(SOURCE), "exec"), module.__dict__)
    return module


@pytest.fixture
def worker():
    return load_reconciler()


DEP = "2f28ee55-20f7-4ecd-837b-4319de399b3a"
PT = "2026-09-01"


def setup(tmp_path, worker, *, pointer_dep=DEP, pointer_release=None):
    release = tmp_path / "release"
    (release / "data").mkdir(parents=True)
    (release / "data/program-registry.json").write_text(json.dumps({"programs": []}))
    pointer = tmp_path / "current-content-base.json"
    pointer.write_text(json.dumps({
        "deployment_id": pointer_dep,
        "release_root": str(pointer_release or release),
    }))
    worker.CURRENT_BASE = pointer
    ledger = tmp_path / "ledger.json"
    intent = {"operation_id": "wire-deploy:" + "a" * 32, "rows": ["xentraffic:1", "xentraffic:2"]}
    ledger.write_text(json.dumps({
        "schema": worker.LEDGER_SCHEMA,
        "rows": {
            key: {"key": key, "status": "prepared", "pt_date": PT,
                  "deployment_intent": dict(intent),
                  "deployed_unverified": {"deployment_id": DEP}}
            for key in intent["rows"]
        },
        "days": {PT: {"wired_keys": [], "blocked_keys": [], "deployment_intent": dict(intent)}},
    }))
    return ledger, release


def reconcile(worker, ledger, release, **kwargs):
    return worker.reconcile_landed_uncertain_deployment(
        ledger_path=ledger, pt_date=PT, deployment_id=DEP, release_root=release, **kwargs)


@pytest.mark.parametrize("failures", [None, 8])
def test_resolves_the_intent_and_never_marks_wired(tmp_path, worker, failures):
    ledger, release = setup(tmp_path, worker)
    result = reconcile(worker, ledger, release, sensor_failure_count=failures)
    assert result["status"] == "PASS_RECONCILED_LANDED_WITH_FINDING"
    assert result["rows_marked_wired"] == 0
    data = json.loads(ledger.read_text())
    day = data["days"][PT]
    assert "deployment_intent" not in day
    assert day["wired_keys"] == []
    assert day["deployment"]["status"] == "LANDED_SENSOR_NOT_VERIFIED"
    for row in data["rows"].values():
        assert "deployment_intent" not in row and "deployed_unverified" not in row
        # The existing September 2 contract supersedes the old prepared state:
        # a known landing must not be replayed just because its sensor failed.
        assert row["status"] == "landed_sensor_pending"
        assert "wired_at_utc" not in row
        pending = row["landed_sensor_pending"]
        assert pending["deployment_id"] == DEP
        assert pending["release"] == str(release)
        assert pending["sensor_failure_count"] == failures
        assert row["landed_with_finding"]["deployment_id"] == DEP
        assert row["deployed_unverified_history"]


@pytest.mark.parametrize("mismatch", ["deployment", "release"])
def test_pointer_disagreement_refuses_without_changing_ledger(tmp_path, worker, mismatch):
    ledger, release = setup(
        tmp_path, worker,
        pointer_dep="other-deployment" if mismatch == "deployment" else DEP,
        pointer_release=tmp_path / "other" if mismatch == "release" else None,
    )
    before = ledger.read_bytes()
    error = "reconcile_deployment_id_not_current_base" if mismatch == "deployment" else "reconcile_release_root_not_current_base"
    with pytest.raises(worker.B2Error, match=error):
        reconcile(worker, ledger, release)
    assert ledger.read_bytes() == before


def test_is_idempotent(tmp_path, worker):
    ledger, release = setup(tmp_path, worker)
    reconcile(worker, ledger, release)
    before = ledger.read_bytes()
    again = reconcile(worker, ledger, release)
    assert again["status"] == "PASS_NO_ARMED_INTENT" and again["changed"] is False
    assert ledger.read_bytes() == before


@pytest.mark.parametrize("bad_state", ["prepared", "wired"])
def test_regression_rejects_redeploy_or_false_wired_mutation(tmp_path, bad_state):
    source = SOURCE.read_text()
    node = next(n for n in ast.parse(source).body
                if isinstance(n, ast.FunctionDef)
                and n.name == "reconcile_landed_uncertain_deployment")
    original = ast.get_source_segment(source, node)
    assert original is not None
    target = 'state["status"] = LANDED_SENSOR_PENDING'
    assert original.count(target) == 1
    mutated = original.replace(target, f'state["status"] = {bad_state!r}')
    worker = load_reconciler(source.replace(original, mutated, 1))
    with pytest.raises(AssertionError):
        test_resolves_the_intent_and_never_marks_wired(tmp_path, worker, 8)


def test_missing_release_registry_refuses_without_changing_ledger(tmp_path, worker):
    ledger, release = setup(tmp_path, worker)
    (release / "data/program-registry.json").unlink()
    before = ledger.read_bytes()
    with pytest.raises(worker.B2Error, match="reconcile_release_root_invalid"):
        reconcile(worker, ledger, release)
    assert ledger.read_bytes() == before


def test_unrelated_prepared_row_is_not_reclassified(tmp_path, worker):
    ledger, release = setup(tmp_path, worker)
    data = json.loads(ledger.read_text())
    unrelated = {"key": "xentraffic:3", "status": "prepared", "pt_date": PT}
    data["rows"]["xentraffic:3"] = unrelated
    ledger.write_text(json.dumps(data))
    reconcile(worker, ledger, release, sensor_failure_count=8)
    assert json.loads(ledger.read_text())["rows"]["xentraffic:3"] == unrelated

