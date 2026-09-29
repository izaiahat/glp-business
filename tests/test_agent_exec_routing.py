"""CLI translation only; Hermes's focused tests exercise the shared policy itself."""
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest


def test_explicit_codex_dispatch_and_refusals(tmp_path, monkeypatch):
    admission = types.ModuleType("tools.delegation_admission")
    setattr(admission, "admission_problem", lambda: None)
    setattr(admission, "try_reserve_host_children", lambda n: ([object()], 0, 8))
    policy = types.ModuleType("tools.delegate_tool_config")
    seen = []

    def resolve(route, **kwargs):
        seen.append(route.copy())
        model = route.get("model", "gpt-6.1-sol")
        return dict(provider="openai-codex", model=model,
                    reasoning_effort=route.get("reasoning_effort", "high"),
                    speed=route.get("speed", "standard" if "astra" in model else "fast"))

    setattr(policy, "resolve_worker_route", resolve)
    monkeypatch.setitem(sys.modules, "tools.delegation_admission", admission)
    monkeypatch.setitem(sys.modules, "tools.delegate_tool_config", policy)
    spec = importlib.util.spec_from_file_location("agent_exec_candidate", Path(__file__).parents[1] / "scripts/agent_exec.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "models_cache.json").write_text(json.dumps({"models": [
        {"slug": name, "supported_reasoning_levels": [{"reasoning_effort": level} for level in ("low", "medium", "high", "max")],
         "service_tiers": [{"id": "priority"}] if "sol" in name else []}
        for name in ("gpt-6.1-sol", "gpt-6-astra-900k")]}))
    for model, tier in (("gpt-6.1-sol", "fast"), ("gpt-6-astra-900k", "default")):
        command, receipt = module.codex_route(["codex", "exec", "--sandbox", "read-only", "a bounded task"],
                                             {"model": model, "task_kind": "independent_review"})
        assert f'model="{model}"' in command
        assert 'model_provider="openai"' in command
        assert 'model_reasoning_effort="high"' in command
        assert f'service_tier="{tier}"' in command
        assert 'forced_login_method="chatgpt"' in command
        assert command[-1] == "a bounded task"
        assert receipt["effective_wire"] == "not observed by launcher"
        assert seen[-1]["task_kind"] == "independent_review"
    command, receipt = module.codex_route(["codex", "exec", "-m", "gpt-6.1-sol", "-c", 'model_reasoning_effort="max"'], {})
    assert receipt["resolved"]["reasoning_effort"] == "max"
    with pytest.raises(ValueError, match="conflicting explicit"):
        module.codex_route(["codex", "exec", "-m", "gpt-6.1-sol"], {"model": "gpt-6-astra-900k"})
    with pytest.raises(ValueError, match="unsupported"):
        module.codex_route(["codex", "exec"], {"reasoning_effort": "ultra"})
    with pytest.raises(ValueError, match="absent"):
        module.codex_route(["codex", "exec"], {"model": "unknown"})
    with pytest.raises(ValueError, match="Fast"):
        module.codex_route(["codex", "exec"], {"model": "gpt-6-astra-900k", "speed": "fast"})


def test_disabled_policy_preserves_codex_authentication(tmp_path, monkeypatch):
    admission = types.ModuleType("tools.delegation_admission")
    setattr(admission, "admission_problem", lambda: None)
    setattr(admission, "try_reserve_host_children", lambda n: ([object()], 0, 8))
    policy = types.ModuleType("tools.delegate_tool_config")
    setattr(policy, "resolve_worker_route", lambda *args, **kwargs: None)
    monkeypatch.setitem(sys.modules, "tools.delegation_admission", admission)
    monkeypatch.setitem(sys.modules, "tools.delegate_tool_config", policy)
    spec = importlib.util.spec_from_file_location("agent_exec_auth", Path(__file__).parents[1] / "scripts/agent_exec.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    command = ["codex", "exec", "-c", 'forced_login_method="api"', "offline"]
    assert module.codex_route(command, {}) == (command, None)


def test_codex_native_bare_config_values(tmp_path, monkeypatch):
    admission = types.ModuleType("tools.delegation_admission")
    setattr(admission, "admission_problem", lambda: None)
    setattr(admission, "try_reserve_host_children", lambda n: ([object()], 0, 8))
    policy = types.ModuleType("tools.delegate_tool_config")
    seen = []
    def resolve(route, **kwargs):
        seen.append(route.copy())
        return None
    setattr(policy, "resolve_worker_route", resolve)
    monkeypatch.setitem(sys.modules, "tools.delegation_admission", admission)
    monkeypatch.setitem(sys.modules, "tools.delegate_tool_config", policy)
    spec = importlib.util.spec_from_file_location("agent_exec_bare", Path(__file__).parents[1] / "scripts/agent_exec.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for value in ("high", '"high"'):
        command = ["codex", "exec", "-c", f"model_reasoning_effort={value}", "offline"]
        assert module.codex_route(command, {}) == (command, None)
        assert seen[-1]["reasoning_effort"] == "high"
