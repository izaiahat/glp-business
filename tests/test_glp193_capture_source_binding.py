"""Offline source-loader regressions; no host imports, network or queue writes."""
import ast
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_queue(root):
    # Compile exact checkout function bodies, not a copy of the validator. The
    # full queue module imports host-only Sheet writers omitted from this mirror.
    spec = importlib.util.spec_from_file_location("glp193_rails", ROOT / "scripts/glp_affiliate_link_rails.py")
    assert spec is not None and spec.loader is not None
    rails = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = rails
    spec.loader.exec_module(rails)
    source = ROOT / "scripts/glp_wire_in_ready_queue.py"
    tree = ast.parse(source.read_text(), filename=str(source))
    names = {"repo_rel", "sha256_file", "_capture_candidates", "_scalar_identity",
             "_member_in_document", "quote_binds_capture_bytes", "load_identity_captures"}
    constants = {"CAPTURE_DIR_NAMES", "_QUOTED_MEMBER", "IDENTITY_CAPTURE_SCHEMA"}
    nodes: list[ast.stmt] = [n for n in tree.body if
             (isinstance(n, ast.FunctionDef) and n.name in names) or
             (isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in constants for t in n.targets))]
    assert {n.name for n in nodes if isinstance(n, ast.FunctionDef)} == names
    ns: dict[str, Any] = dict(Path=Path, Any=Any, hashlib=hashlib, json=json, re=re, rails=rails, BUSINESS=root)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), ns)
    return ns


def fixture_capture(tmp_path):
    q = load_queue(tmp_path)
    directory = tmp_path / "identity-captures"
    directory.mkdir()
    source = directory / "_campaign.json"
    source.write_text('{"campaign_id": 31083, "name": "Fixture"}\n')
    payload = q["rails"].bind_tracking_capture(
        "MaxBounty", "31083", "https://tracking.example/opaque", program="Fixture",
        base={"tracking_clicks": 0, "quote": '"campaign_id": 31083',
              "capture_path": str(source.relative_to(tmp_path)),
              "capture_sha256": hashlib.sha256(source.read_bytes()).hexdigest()})
    descriptor = directory / "fixture.json"
    descriptor.write_text(json.dumps(payload))
    return q, source, descriptor, payload


def test_separate_source_hash_drift_is_not_a_valid_identity(tmp_path):
    q, source, descriptor, payload = fixture_capture(tmp_path)
    assert "fixture" in q["load_identity_captures"](descriptor.parent)
    source.write_text('{"campaign_id": 99999}\n')
    loaded = q["load_identity_captures"](descriptor.parent)
    assert "fixture" not in loaded
    assert payload["tracking_link_sha256"] not in loaded.get("_by_link_sha256", {})
    assert loaded["_rejected_by_link_sha256"][payload["tracking_link_sha256"]]["reason"].startswith("identity_capture_hash_drift:")


@pytest.mark.parametrize("quote,reason", [
    ('"campaign_id": 99999', "identity_capture_quote_not_in_bytes:"),
    ('"name": "Fixture"', "identity_capture_id_not_in_quote:"),
    ("", "identity_capture_fields_incomplete"),
])
def test_separate_source_quote_must_support_identity(tmp_path, quote, reason):
    q, source, descriptor, payload = fixture_capture(tmp_path)
    payload["quote"] = quote
    descriptor.write_text(json.dumps(payload))
    loaded = q["load_identity_captures"](descriptor.parent)
    assert "fixture" not in loaded
    assert loaded["_rejected_by_link_sha256"][payload["tracking_link_sha256"]]["reason"].startswith(reason)


@pytest.mark.parametrize("case", ["missing_file", "directory", "capture_path", "capture_sha256", "quote"])
def test_missing_or_partial_separate_source_binding_refuses(tmp_path, case):
    q, source, descriptor, payload = fixture_capture(tmp_path)
    if case in {"missing_file", "directory"}:
        source.unlink()
        if case == "directory":
            source.mkdir()
        reason = "identity_capture_missing:"
    else:
        payload.pop(case)
        reason = "identity_capture_fields_incomplete"
        descriptor.write_text(json.dumps(payload))
    loaded = q["load_identity_captures"](descriptor.parent)
    assert "fixture" not in loaded
    assert loaded["_rejected_by_link_sha256"][payload["tracking_link_sha256"]]["reason"].startswith(reason)


@pytest.mark.parametrize("mode", ["relative", "absolute", "numeric_string", "self_contained", "legacy"])
def test_valid_capture_contracts_are_preserved(tmp_path, mode):
    q, source, descriptor, payload = fixture_capture(tmp_path)
    if mode == "absolute":
        payload["capture_path"] = str(source)
    elif mode == "numeric_string":
        payload["quote"] = '"campaign_id": "31083"'
    elif mode == "self_contained":
        for key in ("capture_path", "capture_sha256", "quote"):
            payload.pop(key)
    elif mode == "legacy":
        payload["schema"] = q["IDENTITY_CAPTURE_SCHEMA"]
    descriptor.write_text(json.dumps(payload))
    before = {p.name: p.read_bytes() for p in descriptor.parent.iterdir()}
    loaded = q["load_identity_captures"](tmp_path, recursive=True)
    assert loaded["fixture"]["immutable_id"] == "31083"
    assert "_rejected" not in loaded
    if mode != "legacy":
        assert loaded["fixture"] == loaded["_by_link_sha256"][payload["tracking_link_sha256"]]
    assert before == {p.name: p.read_bytes() for p in descriptor.parent.iterdir()}
