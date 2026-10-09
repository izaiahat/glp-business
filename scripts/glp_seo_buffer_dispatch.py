#!/usr/bin/env python3
"""Bounded n8n entry adapter for the canonical Hermes SEO-buffer refill job."""
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

HERMES = Path("/home/ubuntu/.local/bin/hermes")
REFILL_JOB_ID = "d70cda5f4416"
EXECUTIONS_DB = Path.home() / ".hermes/cron/executions.db"
BUSINESS_ROOT = Path("/home/ubuntu/business")
SYSTEMD_RUN = Path("/usr/bin/systemd-run")
READBACK_TIMEOUT_S = 30.0
READBACK_INTERVAL_S = 0.5


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pid_starttime(pid: int) -> int | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        tail = stat.rsplit(")", 1)[1].split()
        return int(tail[19])
    except (OSError, ValueError, IndexError):
        return None


def execution_is_live(
    execution_id: str,
    db_path: Path = EXECUTIONS_DB,
    starttime_reader: Callable[[int], int | None] = _pid_starttime,
) -> bool:
    """Fail closed for concurrency unless an exact running row is provably stale."""
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as connection:
            row = connection.execute(
                """
                SELECT job_id, status, pid, process_started_at
                FROM executions
                WHERE id = ?
                """,
                (execution_id,),
            ).fetchone()
    except (OSError, sqlite3.Error):
        return True
    if not row or row[0] != REFILL_JOB_ID or row[1] != "running":
        return True
    try:
        pid = int(row[2])
        expected_starttime = int(row[3])
    except (TypeError, ValueError):
        return False
    observed_starttime = starttime_reader(pid)
    return observed_starttime is not None and observed_starttime == expected_starttime


def cron_runs() -> tuple[list[str], list[str], str]:
    result = subprocess.run(
        [str(HERMES), "cron", "runs", REFILL_JOB_ID, "--limit", "20"],
        text=True,
        capture_output=True,
        timeout=45,
    )
    if result.returncode:
        raise RuntimeError(f"cron_runs_failed:{result.returncode}:{result.stderr[-1000:]}")
    candidates = [
        line.split()[0]
        for line in result.stdout.splitlines()
        if " running " in f" {line} " and f"job={REFILL_JOB_ID}" in line
    ]
    running = []
    stale = []
    for execution_id in candidates:
        (running if execution_is_live(execution_id) else stale).append(execution_id)
    return running, stale, result.stdout


def launch_refill() -> str:
    """Start the synchronous Hermes command under a durable user-systemd owner."""
    unit = f"glp-seo-buffer-refill-{uuid.uuid4().hex[:12]}"
    result = subprocess.run(
        [
            str(SYSTEMD_RUN),
            "--user",
            "--quiet",
            "--collect",
            f"--unit={unit}",
            "--property=Type=exec",
            f"--property=WorkingDirectory={BUSINESS_ROOT}",
            "--property=KillMode=control-group",
            "--property=UMask=0077",
            "--property=Environment=HOME=/home/ubuntu",
            "--property=Environment=PATH=/home/ubuntu/.local/bin:/home/ubuntu/.npm-global/bin:/usr/local/bin:/usr/bin:/bin",
            "--",
            str(HERMES),
            "cron",
            "run",
            REFILL_JOB_ID,
        ],
        text=True,
        capture_output=True,
        timeout=30,
    )
    if result.returncode:
        raise RuntimeError(
            f"cron_dispatch_failed:{result.returncode}:{result.stderr[-1000:]}"
        )
    return unit


def execute(mode: str) -> dict:
    running, stale, _ = cron_runs()
    base = {
        "schema": "glp.seo_buffer.dispatch.v1",
        "observed_at_utc": now(),
        "job_id": REFILL_JOB_ID,
        "running_execution_ids": running,
        "ignored_stale_execution_ids": stale,
    }
    if mode == "status":
        return {**base, "status": "PASS_RUNNING" if running else "PASS_IDLE", "dispatched": False}
    if running:
        return {**base, "status": "PASS_ALREADY_RUNNING", "dispatched": False}
    unit = launch_refill()
    deadline = time.monotonic() + READBACK_TIMEOUT_S
    while True:
        after, after_stale, _ = cron_runs()
        if after:
            return {
                **base,
                "status": "PASS_DISPATCHED",
                "dispatched": True,
                "dispatch_unit": unit,
                "running_execution_ids": after,
                "ignored_stale_execution_ids": sorted(set(stale + after_stale)),
            }
        if time.monotonic() >= deadline:
            raise RuntimeError("cron_dispatch_unconfirmed")
        time.sleep(READBACK_INTERVAL_S)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("dispatch", "status"), default="dispatch")
    args = parser.parse_args()
    print(json.dumps(execute(args.mode), sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
