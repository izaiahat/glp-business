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


def current_runtime(row):
    raw = runtime(row)
    for name in ("review_input", "review_runtime"):
        raw[name].update(transport_id=row["transport_id"], effort=row["effort"],
                         speed="normal", service_tier="default")
    return raw


def test_current_astra_route_is_distinct_from_historical_sol():
    transports = load("review_transports")
    current = transports.transport(transports.DEFAULT_TRANSPORT_ID)
    assert current["model"] == "gpt-6-astra-900k"
    assert current["effort"] == "high"
    assert current["speed"] == "normal"
    assert current["service_tier"] == "default"
    assert current["transport_id"] != transports.SOL_TRANSPORT["transport_id"]
    assert transports.SOL_TRANSPORT["model"] == "gpt-5.6-sol"
    assert transports.transport(None) == transports.SOL_TRANSPORT
    assert transports.validate_raw_review_runtime(runtime(transports.SOL_TRANSPORT)) == transports.SOL_TRANSPORT
    assert transports.validate_raw_review_runtime(current_runtime(current)) == current
    for block in ("review_input", "review_runtime"):
        for key, value in (("effort", "xhigh"), ("speed", "fast"), ("service_tier", "priority")):
            raw = current_runtime(current)
            raw[block][key] = value
            with pytest.raises(transports.TransportError):
                transports.validate_raw_review_runtime(raw)
    raw = current_runtime(current)
    del raw["review_runtime"]["service_tier"]
    with pytest.raises(transports.TransportError):
        transports.validate_raw_review_runtime(raw)


def load_business_script(name):
    root = Path(__file__).resolve().parents[3]
    path = root / "scripts" / (name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_native_reviewer_pins_standard_and_refuses_legacy_or_tools():
    wrapper = load_business_script("hermes_sol_article_review")
    args = wrapper.build_parser().parse_args(["--prompt", "Synthetic regression", "--disable-tools"])
    assert (args.provider, args.model, args.effort) == ("openai-codex", "gpt-6-astra-900k", "high")
    wrapper.validate_target_args(args)
    runtime_db = object()  # Native persistence is now required for result custody.
    kwargs = wrapper.hermes_agent_kwargs({}, args, "offline-fixture", runtime_db)
    assert kwargs["request_overrides"]["service_tier"] == "default"
    assert kwargs["reasoning_config"]["effort"] == "high"
    assert kwargs["fallback_model"] is None
    assert kwargs["enabled_toolsets"] == []
    assert kwargs["skip_context_files"] and kwargs["skip_memory"]
    assert kwargs["session_db"] is runtime_db and kwargs["max_iterations"] == 1
    inp = wrapper.review_input(args, "Synthetic regression", {})
    assert inp["transport_id"] == "hermes-openai-codex-astra-independent"
    assert inp["speed"] == "normal" and inp["service_tier"] == "default"
    for field, bad in (("provider", "anthropic"), ("model", "gpt-5.6-sol"),
                       ("model", "unregistered-sol"), ("effort", "xhigh"), ("disable_tools", False)):
        altered = copy.copy(args)
        setattr(altered, field, bad)
        with pytest.raises(ValueError):
            wrapper.validate_target_args(altered)


# Executable AST snapshots of the reviewed native boundary, not a replacement
# implementation. CI has no native install. On the receiving host, compare and
# execute the real extracted functions; never import cli/gateway/config loaders.
# Provenance (whole source SHA256):
# cli_chat_turn_mixin.py: 3cbe309cc3c70c438c0499a938097c94f008055bf51c68f7d4d07f9961ca513f
# run_config_loaders.py: 25f62ff386e8c4280ff2b1bdfe140fcac5abeed6a0cf92f27a171fc73efbbd50
# fallback_config.py: c19bc2b176dfab542eb3ad62ecc5c2f429782abe5836e1b8aeb4e0526cb622b8
NATIVE_FALLBACK_BOUNDARY = {
    "hermes_cli/cli_chat_turn_mixin.py": '''
def _sync_fallback_chain_with_config(self, agent) -> None:
    from cli import logger
    try:
        from gateway.run import GatewayRunner
        from hermes_cli.config_effective import load_user_config_effective
        from hermes_cli.fallback_config import get_fallback_chain
        self._fallback_model = get_fallback_chain(load_user_config_effective(fail_closed=True))
    except Exception as e:
        logger.debug('fallback chain sync skipped (keeping current chain): %s', e)
        return
    GatewayRunner._apply_fallback_chain_to_agent(agent, self._fallback_model)
''',
    "gateway/run_config_loaders.py": '''
def _apply_fallback_chain_to_agent(agent: Any, chain: list | None) -> None:
    if agent is None:
        return
    new_chain = list(chain or [])
    rate_limited_until = getattr(agent, '_rate_limited_until', 0) or 0
    if getattr(agent, '_fallback_activated', False) and rate_limited_until > time.monotonic():
        return
    old_chain = list(getattr(agent, '_fallback_chain', []) or [])
    agent._fallback_chain = new_chain
    agent._fallback_model = new_chain[0] if new_chain else None
    if not getattr(agent, '_fallback_activated', False):
        agent._fallback_index = 0
    if new_chain != old_chain:
        unavailable = getattr(agent, '_unavailable_fallback_keys', None)
        if unavailable:
            unavailable.clear()
''',
    "hermes_cli/fallback_config.py": '''
def _normalized_base_url(value: Any) -> str:
    return value.strip().rstrip('/') if isinstance(value, str) else ''
def _iter_fallback_entries(raw: Any) -> list[dict[str, Any]]:
    candidates = [raw] if isinstance(raw, dict) else raw if isinstance(raw, list) else []
    entries: list[dict[str, Any]] = []
    for entry in candidates:
        if not isinstance(entry, dict):
            continue
        provider = str(entry.get('provider') or '').strip()
        model = str(entry.get('model') or '').strip()
        if not provider or not model:
            continue
        normalized = {**entry, 'provider': provider, 'model': model}
        base_url = _normalized_base_url(entry.get('base_url'))
        if base_url:
            normalized['base_url'] = base_url
        entries.append(normalized)
    return entries
def _entry_identity(entry: dict[str, Any]) -> tuple[str, str, str]:
    return (str(entry.get('provider') or '').strip().lower(), str(entry.get('model') or '').strip().lower(), _normalized_base_url(entry.get('base_url')).lower())
def get_fallback_chain(config: dict[str, Any] | None) -> list[dict[str, Any]]:
    config = config or {}
    chain: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for key in ('fallback_providers', 'fallback_model'):
        for entry in _iter_fallback_entries(config.get(key)):
            identity = _entry_identity(entry)
            if identity not in seen:
                seen.add(identity)
                chain.append(entry)
    return chain
''',
}


def native_fallback_boundary():
    import time
    namespace = {"Any": object, "time": time}
    native = Path("/home/ubuntu/.hermes/hermes-agent")
    for relative, snapshot in NATIVE_FALLBACK_BOUNDARY.items():
        tree = ast.parse(snapshot)
        names = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
        path = native / relative
        if path.is_file():
            actual = [node for node in ast.walk(ast.parse(path.read_bytes()))
                      if isinstance(node, ast.FunctionDef) and node.name in names]
            for node in actual:
                node.decorator_list = []
                if (isinstance(node.body[0], ast.Expr)
                        and isinstance(node.body[0].value, ast.Constant)
                        and isinstance(node.body[0].value.value, str)):
                    node.body = node.body[1:]
            tree_actual = ast.Module(body=list(actual), type_ignores=[])
            assert ast.dump(tree_actual) == ast.dump(tree), "native boundary changed; re-review snapshot"
            tree = tree_actual
        exec(compile(tree, str(path), "exec"), namespace)
    return namespace


def test_repair_native_entry_overrides_config_without_persisting(monkeypatch):
    import logging
    import runpy
    import sys
    from types import SimpleNamespace
    boundary = native_fallback_boundary()
    path = SCRIPTS / "repair_opus_packet.py"
    spec = importlib.util.spec_from_file_location("repair_route_fixture", path)
    assert spec is not None and spec.loader is not None
    repair = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(repair)
    cmd = repair.worker_command("Synthetic route only; no model call.")
    assert cmd[1:4] == [str(path), "--native-repair", "chat"]
    for flag, value in (("--provider", "openai-codex"), ("--model", "gpt-6.1-sol"),
                        ("--reasoning", "high"), ("--max-turns", "60")):
        assert cmd[cmd.index(flag) + 1] == value
    persisted = {"agent": {"service_tier": "priority", "max_turns": 60},
                 "approvals": {"deny": ["fixture-protected"]},
                 "fallback_providers": [{"provider": "fixture-forbidden", "model": "fixture-model"}],
                 "fallback_model": {"provider": "fixture-legacy", "model": "fixture-legacy-model"}}
    untouched = copy.deepcopy(persisted)
    config = copy.deepcopy(persisted)
    reads = []
    torn = False

    def effective_loader(*args, **kwargs):
        reads.append((args, kwargs))
        if torn:
            raise ValueError("fixture torn config")
        # Deliberately return the fixture itself: the override must not mutate it.
        return persisted

    effective = SimpleNamespace(load_user_config_effective=effective_loader)
    monkeypatch.setitem(sys.modules, "hermes_cli", SimpleNamespace(config_effective=effective))
    monkeypatch.setitem(sys.modules, "cli", SimpleNamespace(CLI_CONFIG=config, logger=logging.getLogger("offline")))
    monkeypatch.setitem(sys.modules, "hermes_cli.config_effective", effective)
    monkeypatch.setitem(sys.modules, "gateway.run", SimpleNamespace(GatewayRunner=SimpleNamespace(
        _apply_fallback_chain_to_agent=boundary["_apply_fallback_chain_to_agent"])))
    monkeypatch.setitem(sys.modules, "hermes_cli.fallback_config", SimpleNamespace(
        get_fallback_chain=boundary["get_fallback_chain"]))
    # Prove the genuine native boundary can restore both forbidden routes.
    agent = SimpleNamespace(_fallback_chain=[], _fallback_model=None)
    cli = SimpleNamespace(_fallback_model=[])
    refresh = boundary["_sync_fallback_chain_with_config"]
    refresh(cli, agent)
    assert agent._fallback_chain == [persisted["fallback_providers"][0], persisted["fallback_model"]]
    agent._fallback_chain, agent._fallback_model, cli._fallback_model = [], None, []
    expected = copy.deepcopy(persisted)
    expected["agent"]["service_tier"] = "default"
    expected["fallback_providers"] = []
    expected.pop("fallback_model")
    observed = []

    def inference_boundary():
        nonlocal torn
        observed.append(copy.deepcopy(config))
        # Execute the actual per-turn refresh/application BEFORE the inert
        # inference observer, including a later persisted edit and a torn read.
        for mode in ("initial", "edited", "torn"):
            if mode == "edited":
                persisted["fallback_providers"].append({"provider": "fixture-added", "model": "fixture-added-model"})
            torn = mode == "torn"
            refresh(cli, agent)
            assert agent._fallback_chain == [], "native refresh restored forbidden fallback before inference"
            assert agent._fallback_model is None
            assert cli._fallback_model == []
            if not torn:
                fresh = effective.load_user_config_effective(fail_closed=True)
                assert fresh == expected  # retains approvals and other settings
        torn = False

    monkeypatch.setitem(sys.modules, "hermes_cli.main", SimpleNamespace(main=inference_boundary))
    monkeypatch.setattr(sys, "argv", cmd[1:])
    with pytest.raises(SystemExit) as exit_result:
        runpy.run_path(str(path), run_name="__main__")
    assert exit_result.value.code == 0
    assert observed == [expected]
    assert effective.load_user_config_effective is effective_loader
    assert all(kwargs == {"fail_closed": True} and not args for args, kwargs in reads)
    # Only the deliberate fixture edit above changed the persisted mapping.
    persisted["fallback_providers"].pop()
    assert persisted == untouched


def test_refill_prompt_projection_preserves_freshness_and_gate_contract():
    root = Path(__file__).resolve().parents[3]
    text = (root / "ops/hermes-cron-prompts/glp-seo-buffer-refill.md").read_text()
    assert "gpt-5.6-sol" not in text
    assert "gpt-6.1-sol at high" in text
    assert "gpt-6-astra-900k at high" in text
    assert "normal" in text and "no fallback" in text
    for clause in ("Research each selected query independently with Firecrawl first",
                   "newest same-day machine-owned", "full five normal cited repair/reseal/re-review rounds",
                   "Never replay an old review job", "Only an exact PASS_OPUS_SEO_DRAFT receipt",
                   "Never admit a HIGH packet", "zero publication/deploy/GSC/provider-write effects"):
        assert clause in text
