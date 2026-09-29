#!/usr/bin/env python3
"""Admit an external codex/claude one-shot to Hermes's host-shared child pool.

Usage: python3 scripts/agent_exec.py -- codex exec ...
       python3 scripts/agent_exec.py -- claude -p ...
No queue or daemon. The lease follows this PID through exec and is reclaimed
only when the actual CLI process exits (including on signal/timeout).
"""
from __future__ import annotations

import os
import sys
import argparse
import json
import tomllib
from pathlib import Path

HERMES_SOURCE = Path.home() / ".hermes" / "hermes-agent"
sys.path.insert(0, str(HERMES_SOURCE))
from tools.delegation_admission import admission_problem, try_reserve_host_children  # noqa: E402


def codex_route(argv: list[str], task: dict) -> tuple[list[str], dict | None]:
    """Resolve the existing Hermes policy, then use Codex's native config options."""
    from tools.delegate_tool_config import resolve_worker_route

    requested = dict(task)
    config = {}
    i = 2
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            break
        if arg in ("-c", "--config", "-m", "--model"):
            i += 1
            if i == len(argv):
                raise ValueError(f"{arg} requires a value")
            value = argv[i]
        elif arg.startswith(("--config=", "--model=")):
            arg, value = arg.split("=", 1)
        else:
            i += 1
            continue
        if arg in ("-c", "--config"):
            key, separator, raw = value.partition("=")
            if not separator:
                raise ValueError("Codex config overrides require key=value")
            try:
                config[key.strip()] = tomllib.loads("value=" + raw)["value"]
            except tomllib.TOMLDecodeError:
                # Codex accepts unquoted strings when TOML value parsing fails.
                config[key.strip()] = raw
        else:
            config["model"] = value
        i += 1
    for key, value in (("model", config.get("model")),
                       ("reasoning_effort", config.get("model_reasoning_effort")),
                       ("provider", "openai-codex" if config.get("model_provider") == "openai" else config.get("model_provider"))):
        if value is not None:
            if key in requested and requested[key] != value:
                raise ValueError(f"conflicting explicit {key} values")
            requested[key] = value
    if "service_tier" in config:
        tier = config["service_tier"]
        if tier not in ("fast", "priority", "default"):
            raise ValueError("unsupported Codex service_tier; use fast or default")
        speed = "fast" if tier in ("fast", "priority") else "standard"
        if "speed" in requested and requested["speed"] != speed:
            raise ValueError("conflicting explicit speed values")
        requested["speed"] = speed
    resolved = resolve_worker_route(requested, inherited_provider="openai-codex")
    if resolved is None:
        return argv, None
    if config.get("forced_login_method", "chatgpt") != "chatgpt":
        raise ValueError("this admitted route requires included ChatGPT subscription authentication")
    # Validate the selected CLI model/effort/tier against its actual local catalog.
    home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    catalog = json.loads((home / "models_cache.json").read_text())
    models = catalog.get("models", [])
    model = next((m for m in models if m.get("slug") == resolved["model"]), None)
    if model is None:
        raise ValueError("selected model absent from Codex CLI catalog; verify access through the owning client first")
    efforts = {e["reasoning_effort"] for e in model.get("supported_reasoning_levels", [])}
    if resolved["reasoning_effort"] not in efforts:
        raise ValueError("selected reasoning effort unsupported by Codex CLI model")
    if resolved["speed"] == "fast" and not any(t.get("id") == "priority" for t in model.get("service_tiers", [])):
        raise ValueError("Fast is not advertised for this Codex CLI model")
    settings = {"model": resolved["model"], "model_provider": "openai",
                "model_reasoning_effort": resolved["reasoning_effort"],
                "service_tier": "fast" if resolved["speed"] == "fast" else "default",
                "forced_login_method": "chatgpt"}
    flags = [part for key, value in settings.items() for part in ("-c", f"{key}={json.dumps(value)}")]
    # Insert before positional input/--, so prompt text cannot consume the options.
    final = argv[:2] + flags + argv[2:]
    receipt = {"requested": requested,
               "resolved": {k: resolved[k] for k in ("model", "provider", "reasoning_effort", "speed")},
               "client": "codex", "client_options": settings,
               "effective_wire": "not observed by launcher"}
    return final, receipt


def main(argv: list[str]) -> int:
    task = {}
    if argv and argv[0].startswith("--") and argv[0] != "--":
        parser = argparse.ArgumentParser(description=__doc__)
        for flag in ("task-kind", "model", "provider", "reasoning-effort", "speed"):
            parser.add_argument("--" + flag)
        split = argv.index("--") if "--" in argv else len(argv)
        task = {k: v for k, v in vars(parser.parse_args(argv[:split])).items() if v is not None}
        argv = argv[split:]
    if argv and argv[0] == "--":
        argv = argv[1:]
    hermes_oneshot = (len(argv) >= 2 and Path(argv[0]).name == "hermes"
                      and argv[1] == "chat" and "--oneshot" in argv
                      and ("--query-file" in argv or "-q" in argv)
                      and "--run-budget" in argv)
    if len(argv) < 2 or not ((argv[0] == "codex" and argv[1] == "exec") or
                              (argv[0] == "claude" and argv[1] == "-p") or hermes_oneshot):
        print("usage: agent_exec.py -- codex exec ... | claude -p ... | hermes chat --oneshot --query-file FILE --run-budget SECONDS ...", file=sys.stderr)
        return 2
    if argv[:2] == ["codex", "exec"]:
        try:
            argv, receipt = codex_route(argv, task)
        except (ValueError, OSError, ImportError) as exc:
            print(f"agent_exec: routing refused: {exc}", file=sys.stderr)
            return 2
        if receipt:
            print("WORKER_ROUTE " + json.dumps(receipt, sort_keys=True), file=sys.stderr, flush=True)
    problem = admission_problem()
    if problem:
        print(f"agent_exec: admission refused: {problem}", file=sys.stderr)
        return 75
    try:
        leases, active, limit = try_reserve_host_children(1)
    except (OSError, RuntimeError) as exc:
        print(f"agent_exec: admission refused: {exc}", file=sys.stderr)
        return 75
    if leases is None:
        print(f"agent_exec: admission refused: host slots {active}/{limit}", file=sys.stderr)
        return 75
    try:
        # An exec retains pid and /proc start tick. No parent can release early
        # while the external process is still running; next admission reaps it.
        os.execvp(argv[0], argv)
    except OSError as exc:
        leases[0].release()
        print(f"agent_exec: launch failed: {exc}", file=sys.stderr)
        return 127
    return 0  # unreachable after a successful exec


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
