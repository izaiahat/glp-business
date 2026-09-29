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
from pathlib import Path

HERMES_SOURCE = Path.home() / ".hermes" / "hermes-agent"
sys.path.insert(0, str(HERMES_SOURCE))
from tools.delegation_admission import admission_problem, try_reserve_host_children  # noqa: E402


def main(argv: list[str]) -> int:
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
