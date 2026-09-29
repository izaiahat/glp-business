"""Three source-only GLP-73 boundaries; synthetic receipts, no build or provider call."""
import ast
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path

import pytest

SCRIPTS = Path(os.environ.get("GLP73_PREDECESSOR_SCRIPTS",
             str(Path(__file__).resolve().parents[1] / "scripts")))


def load(name):
    path = SCRIPTS / (name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    return path


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def runtime(row):
    return {
        "review_input": {"transport_id": row["transport_id"], "context_isolated": True,
                         "provider": row["provider"], "model": row["model"],
                         "effort": row["effort"], "api_mode": row["api_mode"],
                         "tools_disabled": True, "tool_count": 0, "fallback_provider_count": 0},
        "review_runtime": {"provider": row["provider"], "model": row["model"],
                           "api_mode": row["api_mode"], "model_turns": 1,
                           "tool_count": 0, "tool_calls": 0,
                           "fallback_provider_count": 0, "context_isolated": True},
        "permission_denials": [], "usage": {"tool_calls": 0, "server_tool_use": {}},
        "modelUsage": {row["model"]: {"canonicalModel": row.get("canonical_model", row["model"]),
                                     "outputTokens": 50}},
    }


def test_b01_builder_joins_pinned_raw_transport_and_preserves_operator_branch(tmp_path):
    transports = load("review_transports")
    # Execute the maintained builder's own pure functions without importing its
    # release/build dependencies or starting any side-effecting entry point.
    path = SCRIPTS / "build_opus_article_release.py"
    tree = ast.parse(path.read_text())
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name in {"sha", "checked", "require_opus"}]
    assert {node.name for node in functions} == {"sha", "checked", "require_opus"}
    namespace = {"Path": Path, "hashlib": hashlib, "json": json,
                 "review_transports": transports, "Any": object}
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(path), "exec"), namespace)
    require_opus = namespace["require_opus"]
    sol, opus = transports.SOL_TRANSPORT, transports.POLICY_OPUS55_TRANSPORT
    raw_path = write(tmp_path / "raw.json", runtime(sol))
    manifest = write(tmp_path / "manifest.json", {"synthetic": True})
    summary = {"status": "PASS_OPUS_SEO_DRAFT", "tools_disabled": True,
               "review_transport": opus["transport_id"], "provider": opus["provider"],
               "model": opus["model"], "effort": opus["effort"],
               "raw_review_path": str(raw_path), "raw_review_sha256": digest(raw_path),
               "subject_manifest_sha256": digest(manifest), "findings": [],
               "gates": [{"gate": gate, "status": "PASS"} for gate in "ABCDE"]}
    receipt = write(tmp_path / "summary.json", summary)
    job = {"opus_receipt": {"path": str(receipt), "sha256": digest(receipt)},
           "subject_manifest": {"path": str(manifest), "sha256": digest(manifest)}}
    with pytest.raises(RuntimeError, match="opus_review_transport_mismatch"):
        require_opus(job)
    matching = {**summary, "review_transport": sol["transport_id"],
                "provider": sol["provider"], "model": sol["model"], "effort": sol["effort"]}
    receipt = write(tmp_path / "matching.json", matching)
    good = {**job, "opus_receipt": {"path": str(receipt), "sha256": digest(receipt)}}
    assert require_opus(good)[1]["review_transport"] == sol["transport_id"]
    bad = copy.deepcopy(good)
    bad["opus_receipt"]["sha256"] = "0" * 64
    with pytest.raises(RuntimeError, match="opus_receipt_drift"):
        require_opus(bad)
    bad = copy.deepcopy(good)
    write(raw_path, {**runtime(sol), "review_runtime": {**runtime(sol)["review_runtime"], "tool_calls": 1}})
    with pytest.raises(RuntimeError, match="opus_raw_review_drift"):
        require_opus(bad)
    # The separately governed operator-remediation branch remains present and separate.
    assert "defect_remediation.directive(" in (ast.get_source_segment(path.read_text(),
        next(node for node in functions if node.name == "require_opus")) or "")


def test_f01_taxonomy_requires_producer_capture_for_single_and_multi_source(tmp_path):
    fixtures = load("carrier_fixture_contracts")
    base, business = tmp_path / "base", tmp_path / "business"
    pid, stamp = "synthetic-program", "2026-09-28"
    tags = {field: (["semaglutide"] if field == "molecule_tags" else [])
            for field in fixtures.TAXONOMY_TAG_FIELDS}
    url = "https://example.org/program"
    write(base / "data/program-registry.json", {"therapeutic_taxonomy": {"programs": {
        pid: {**tags, "verified_as_of": "2026-07-01", "source_urls": [url]}}}})
    path = business / "reports/synthetic/verification.json"
    registry_path = business / "site-system/flagship-v2/candidates/synthetic/data/program-registry.json"
    manifest = {"program_id": pid, "verified_as_of": stamp, "tags_unchanged": tags}

    def bind(payload):
        write(path, payload)
        source = payload.get("sources", [payload])[0]
        write(registry_path, {"therapeutic_taxonomy": {
            "authorities": {"a": {"path": "reports/synthetic/verification.json", "sha256": digest(path)}},
            "programs": {pid: {"evidence_bindings": [{"authority": "a",
                "authority_sha256": digest(path), "verified_as_of": stamp,
                "source_url": url, "evidence_sha256": source.get("capture_sha256")}]}}}})
        return fixtures.taxonomy_reverification_binding(base, business)["stamps"]

    assert bind(manifest) == {}  # formerly accepted: three-field, no producer evidence
    capture = write(business / "reports/synthetic/capture.json", {
        "request": {"url": url, "formats": ["markdown", "links"], "maxAge": 0},
        "started_at": "2026-09-28T12:00:00+00:00",
        "received_at": "2026-09-28T12:00:01+00:00",
        "response": {"success": True, "data": {"markdown": "Semaglutide evidence.",
            "metadata": {"statusCode": 200, "sourceURL": url, "cacheState": "miss"}}}})
    assertions = {field: ({"semaglutide": {"quote": "Semaglutide", "context": "semaglutide evidence."}}
                          if field == "molecule_tags" else {}) for field in tags}
    proof = {"source_url": url, "verified_as_of": stamp,
             "captured_at": "2026-09-28T12:00:01+00:00", "capture_path": "reports/synthetic/capture.json",
             "capture_sha256": digest(capture), "assertions": assertions}
    single = {**manifest, **proof, "assertions": assertions}
    assert bind(single) == {pid: stamp}
    multi = {**manifest, "sources": [proof], "assertions": assertions}
    assert bind(multi) == {pid: stamp}
    for mutation in (
        {**multi, "sources": [{**proof, "capture_sha256": "0" * 64}]},
        {**multi, "sources": [{**proof, "captured_at": "2026-09-29T12:00:01+00:00"}]},
        {**multi, "sources": [{**proof, "assertions": {**assertions, "molecule_tags": {
            "semaglutide": {"quote": "unobserved", "context": ""}}}}]},
    ):
        assert bind(mutation) == {}
    assert json.loads((base / "data/program-registry.json").read_text())["therapeutic_taxonomy"]["programs"][pid]["verified_as_of"] == "2026-07-01"


def test_t01_runtime_isolation_denial_overrides_input_but_absence_replays():
    transports = load("review_transports")
    for row in (transports.SOL_TRANSPORT, transports.POLICY_OPUS55_TRANSPORT):
        valid = runtime(row)
        assert transports.validate_raw_review_runtime(valid)["transport_id"] == row["transport_id"]
        historical = copy.deepcopy(valid)
        del historical["review_runtime"]["context_isolated"]
        assert transports.validate_raw_review_runtime(historical)["transport_id"] == row["transport_id"]
        contradiction = copy.deepcopy(valid)
        contradiction["review_runtime"]["context_isolated"] = False
        with pytest.raises(transports.TransportError, match="review_runtime_unproven"):
            transports.validate_raw_review_runtime(contradiction)
        bad_input = copy.deepcopy(valid)
        bad_input["review_input"]["context_isolated"] = False
        with pytest.raises(transports.TransportError, match="review_execution_pin_invalid"):
            transports.validate_raw_review_runtime(bad_input)
