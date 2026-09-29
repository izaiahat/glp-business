"""Actual weekly/recovery consumers of the accepted prepared-publication validator."""
from __future__ import annotations

import ast
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

from test_glp73_current_pointer_consumer import evidence, fixture

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def _actual_consumers(monkeypatch: pytest.MonkeyPatch):
    # Only the eager host-only article builder import is isolated. Both actual
    # consumer function bodies and the registered evidence validator execute.
    source = SCRIPTS / "glp_price_index_weekly_successor.py"
    tree = ast.parse(source.read_text(), filename=str(source))
    eager = [node for node in tree.body if isinstance(node, ast.Assign)
             and any(isinstance(t, ast.Name) and t.id == "BUILDER" for t in node.targets)]
    assert len(eager) == 1 and isinstance(eager[0].value, ast.Call)
    assert isinstance(eager[0].value.func, ast.Name) and eager[0].value.func.id == "load_builder"
    tree.body.remove(eager[0])
    successor = types.ModuleType("glp_price_index_weekly_successor")
    successor.__file__ = str(source)
    monkeypatch.setitem(sys.modules, successor.__name__, successor)
    exec(compile(ast.fix_missing_locations(tree), str(source), "exec"), successor.__dict__)
    successor.__dict__["BUILDER"] = None  # legacy size-row fixtures do not call the builder
    recovery_path = SCRIPTS / "glp_price_index_recovery.py"
    spec = importlib.util.spec_from_file_location("glp73_recovery_fixture", recovery_path)
    assert spec is not None and spec.loader is not None
    recovery = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(recovery)
    assert recovery._validate_base_evidence is successor._validate_base_evidence
    return successor, recovery


def test_actual_weekly_and_recovery_prepared_and_legacy_boundaries(tmp_path, monkeypatch):
    successor, recovery = _actual_consumers(monkeypatch)
    _config, _flagship, pointer, pointer_path = fixture(tmp_path, monkeypatch, True)
    release = Path(pointer["release_root"])
    assert {"site_head", "deploy_directory_sha256"} <= set(pointer)
    # Offline stubs only for the network gates, after actual evidence validation.
    network = {"calls": 0}
    def project(_token):
        network["calls"] += 1
        return {"name": successor.PROJECT}
    monkeypatch.setattr(successor, "project_detail", project)
    monkeypatch.setattr(successor, "production_head", lambda _token: {"id": pointer["deployment_id"]})
    monkeypatch.setattr(successor, "deployment_detail", lambda _token, _id: {"id": pointer["deployment_id"]})
    monkeypatch.setattr(successor, "d1_ids", lambda _value: [successor.RUNTIME_D1])
    resolved = successor.validate_base(pointer, "offline-fixture")
    assert resolved["prepared_binding"] == evidence._validate_base_evidence(pointer, release)["prepared_binding"]
    assert len(resolved["prepared_binding"]["files"]) >= 7
    assert recovery._validate_base_evidence(pointer, release)["prepared_binding"] == resolved["prepared_binding"]
    assert network["calls"] == 1

    # Missing partner field, unknown field, and bad site head refuse before a
    # network gate; no prepared subset is silently treated as legacy evidence.
    for altered in ({k: v for k, v in pointer.items() if k != "site_head"},
                    {**pointer, "unknown_authority": True},
                    {**pointer, "site_head": "wrong"}):
        with pytest.raises(RuntimeError):
            successor.validate_base(altered, "offline-fixture")
        with pytest.raises(RuntimeError):
            recovery.validate_pointer_shape(altered, require_release_dirs=True)
    assert network["calls"] == 1

    prepared_path = release.parent / "PREPARED.json"
    original = prepared_path.read_bytes()
    prepared = json.loads(original)
    prepared["site_head"] = "e" * 40
    prepared_path.write_text(json.dumps(prepared))
    with pytest.raises(RuntimeError, match="prepared|registered"):
        successor.validate_base(pointer, "offline-fixture")
    with pytest.raises(RuntimeError, match="prepared|registered"):
        recovery._validate_base_evidence(pointer, release)
    assert network["calls"] == 1
    prepared_path.write_bytes(original)
    worker = release.parent / "deploy-public/_worker.js"
    worker_original = worker.read_bytes()
    worker.write_bytes(b"forged worker\n")
    with pytest.raises(RuntimeError, match="prepared|registered"):
        successor.validate_base(pointer, "offline-fixture")
    with pytest.raises(RuntimeError, match="prepared|registered"):
        recovery._validate_base_evidence(pointer, release)
    assert network["calls"] == 1
    worker.write_bytes(worker_original)
    assert json.loads(pointer_path.read_bytes()) == pointer

    # Legacy stays on the original strict evidence branch, with no prepared grant.
    _config, _flagship, legacy, _path = fixture(tmp_path / "legacy", monkeypatch, False)
    legacy_release = Path(legacy["release_root"])

    assert "prepared_binding" not in successor._validate_base_evidence(legacy, legacy_release)
    assert "prepared_binding" not in recovery._validate_base_evidence(legacy, legacy_release)
    forged_legacy = {**legacy, "unexpected": "PASS"}
    with pytest.raises(RuntimeError, match="fields_invalid"):
        successor.validate_base(forged_legacy, "offline-fixture")
