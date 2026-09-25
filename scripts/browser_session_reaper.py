#!/usr/bin/env python3
"""Reap ORPHANED headless Chrome left behind by finished browser jobs.

Why this exists (2026-09-15): 103 chrome processes had accumulated, 27 of them
orphans up to 6d21h old. Load hit 25 and WORKER-STANDARD rule 7's browser gate
(one-minute load 8) shut, idling two seats for hours. The processes were the
load; the verification suite was innocent (workers: 2).

WHAT THIS NEVER KILLS
---------------------
* Anything holding `--remote-debugging-port`. These are CDP listeners. The Awin
  drain connects to 127.0.0.1:9325 and dies without it
  (`glp_awin_portal_drain_cron.py:75`). A first pass at this filter selected 27
  PIDs of which 9 held debugging ports whose profile paths contained neither
  'katalys' nor 'awin' - a name filter alone would have broken the drain.
* Anything on a katalys or awin profile, by name, belt-and-braces.
* Anything younger than MIN_AGE_S, which is live work.

It kills by explicit PID after re-reading each one, never by pkill -f: a -f
pattern matches this script's own command line (observed exit 144).

Retire this once GLP-268 lands and portal jobs log in per run via browser_exec
instead of riding persistent sessions - then there is nothing to leak.
"""
from __future__ import annotations
import json, os, subprocess, sys, time
from pathlib import Path

MIN_AGE_S = int(os.environ.get("BROWSER_REAPER_MIN_AGE_S", "7200"))
PROTECTED = ("remote-debugging-port", "katalys", "awin")
RECEIPTS = Path("/home/ubuntu/business/reports/ops/browser-reaper")


def _live_pids() -> set[int]:
    out = subprocess.run(["ps", "-eo", "pid="], capture_output=True, text=True).stdout
    return {int(x) for x in out.split() if x.isdigit()}


def candidates() -> list[dict]:
    """Age alone is not orphanhood.

    Measured 2026-09-15: a legitimate verification run held browsers for 53
    minutes, and at another moment 22 headless processes all had LIVE parents -
    active work, not leaks. Killing on age alone would eventually kill a long
    honest run. So a process qualifies only when it is old AND genuinely
    parentless: reparented to init (ppid 1), or its parent is gone.
    """
    out = subprocess.run(["ps", "-eo", "pid,ppid,etimes,args"], capture_output=True, text=True).stdout
    live = _live_pids()
    rows = []
    for line in out.splitlines()[1:]:
        parts = line.strip().split(None, 3)
        if len(parts) < 4:
            continue
        pid, ppid, etimes, args = parts[0], parts[1], parts[2], parts[3]
        if not (pid.isdigit() and ppid.isdigit() and etimes.isdigit()):
            continue
        if "chrome" not in args.lower() or "--headless" not in args:
            continue
        if int(etimes) <= MIN_AGE_S:
            continue
        if any(token in args for token in PROTECTED):
            continue
        parentless = int(ppid) == 1 or int(ppid) not in live
        if not parentless:
            continue
        rows.append({"pid": int(pid), "ppid": int(ppid), "age_s": int(etimes), "args": args[:160]})
    return rows


def main() -> int:
    picked = candidates()
    killed, refused = [], []
    for row in picked:
        # Re-read immediately before killing: PIDs are reused and args can change.
        args = subprocess.run(["ps", "-p", str(row["pid"]), "-o", "args="],
                              capture_output=True, text=True).stdout
        if not args.strip() or "--headless" not in args or any(t in args for t in PROTECTED):
            refused.append({**row, "reason": "recheck_failed_or_protected"})
            continue
        try:
            os.kill(row["pid"], 15)
            killed.append(row)
        except (ProcessLookupError, PermissionError) as exc:
            refused.append({**row, "reason": type(exc).__name__})
    load1 = os.getloadavg()[0]
    receipt = {
        "schema": "glp-browser-reaper-v1",
        "at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "min_age_s": MIN_AGE_S,
        "selected": len(picked), "killed": len(killed), "refused": len(refused),
        "load_1m_after": load1,
        "cdp_9325_still_bound": bool(subprocess.run(
            ["bash", "-lc", "ss -ltn 2>/dev/null | grep -c 9325"],
            capture_output=True, text=True).stdout.strip() not in ("", "0")),
        "killed_pids": [r["pid"] for r in killed],
        "refused_detail": refused[:20],
    }
    RECEIPTS.mkdir(parents=True, exist_ok=True)
    (RECEIPTS / f"{receipt['at_utc'].replace(':','')}.json").write_text(json.dumps(receipt, indent=1))
    print(json.dumps(receipt))
    # A reaper that silently breaks the drain is worse than one that never runs.
    return 0 if receipt["cdp_9325_still_bound"] else 1


if __name__ == "__main__":
    sys.exit(main())
