#!/usr/bin/env python3
"""Hermes no-agent composer for the armed LOOM-1 Guard event poller.

The rail owns internal Discord event cards. Native stdout is empty on success so
those cards are never duplicated into #ops-alerts.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, "/home/ubuntu/business/ops")
from discord_fmt import kvline, meaning, render, subtext, title

COMMAND = ["/usr/bin/python3", "/home/ubuntu/business/scripts/loom_delivery_rails.py", "poll-guard-events"]
ARTIFACT = Path("/home/ubuntu/business/reports/loom-automation-20260709/guard-poller-cron-latest.json")
ERROR_ARTIFACT = Path("/home/ubuntu/business/reports/loom-automation-20260709/guard-poller-cron-error-latest.log")


def _private_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.chmod(path, 0o600)


def main() -> int:
    env = os.environ.copy()
    env["LOOM_GUARD_EVENTS_ARMED"] = "YES"
    proc = subprocess.run(COMMAND, text=True, capture_output=True, timeout=180, env=env)
    if proc.returncode:
        _private_write(ERROR_ARTIFACT, ((proc.stderr or "") + "\n" + (proc.stdout or ""))[-12000:])
        print(render(
            title("action", "Loom Guard", "event poller failed locally"),
            meaning("inspect the private artifact; raw provider diagnostics were suppressed"),
            subtext(f"artifact: {ERROR_ARTIFACT}"),
        ))
        return proc.returncode
    try:
        result = json.loads(proc.stdout)
    except Exception:
        _private_write(ERROR_ARTIFACT, (proc.stdout or "")[-12000:])
        print(render(
            title("action", "Loom Guard", "poller returned an invalid local receipt"),
            meaning("inspect the private artifact; no raw payload was delivered to Discord"),
            subtext(f"artifact: {ERROR_ARTIFACT}"),
        ))
        return 2
    _private_write(ARTIFACT, json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    processed = result.get("processed") or []
    failures = [row for row in processed if row.get("status") == "blocked"]
    if failures:
        print(render(
            title("warn", "Loom Guard", f"{len(failures)} event(s) blocked during processing"),
            kvline(("processed", len(processed)), ("requests", result.get("requests", 0)), ("external sends", 0)),
            meaning("inspect the private receipt and repair only the blocked internal event rows"),
            subtext(f"artifact: {ARTIFACT}"),
        ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
