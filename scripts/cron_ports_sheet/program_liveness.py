#!/usr/bin/env python3
"""Cron port of published glp/program-liveness (BViwBYueM0kLpfJW).

Only the Monday 07:00 PT schedule migrates; the authenticated n8n webhook is
separate. The bridge audits, while n8n_dt queues dispositions for the sole
Sheet writer. --dry-run refuses until the liveness action has a registered
no-effect mode; a bridge selftest cannot prove this action executes safely.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from croniter import croniter

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts import cron_bridge_action as runner

ACTION = "program_liveness_check"
CRON = "0 7 * * 1"
TZ = "America/Los_Angeles"
SNAPSHOT = "5caYqx3gfFPlp9R5"
QUEUE = runner.SHEET_QUEUE


def snapshot_args(due_slot):
    """Freeze advisory input per slot so runner's action digest survives retry."""
    from ops import n8n_dt
    cache = runner.STATE_DIR / "program_liveness_snapshots"
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / (due_slot[:16] + ".json")
    with (cache / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.exists():
            saved = json.loads(path.read_text())
            if saved.get("due_slot") != due_slot or not isinstance(saved.get("args"), dict):
                raise RuntimeError("liveness_snapshot_slot_binding_invalid")
            return saved["args"]
        try:
            source = n8n_dt.rows(SNAPSHOT, limit=5000)
            if n8n_dt.last_read_truncated:
                source = []
        except n8n_dt.MCPError:
            source = []  # Published Read Snapshot continues on transport error.
        rows = []
        for row in source:
            network = str(row.get("network") or "").strip()
            program = str(row.get("program_id") or "").strip()
            if not network or not program:
                continue
            rows.append({"network": network, "program_id": program,
                         "status": str(row.get("status") or ""),
                         "relationship": str(row.get("relationship") or ""),
                         "last_seen_utc": str(row.get("last_seen_utc") or "")})
            if len(rows) >= 800:
                break
        compact = json.dumps(rows, separators=(",", ":"), ensure_ascii=False)
        args = {"snapshot-json": compact} if len(compact) <= 180000 else {}
        runner.save(path, {"due_slot": due_slot, "args": args})
        return args


def finish(action, args, job, state, state_path):
    """Published parse -> queue -> heartbeat -> attention notification."""
    from ops import n8n_dt
    if job.get("status") != "done" or type(job.get("rc")) is not int or job["rc"] != 0:
        raise RuntimeError("liveness_job_not_complete")
    summary = runner.machine_receipt(job, schema="glp.program_liveness_summary.v1")
    try:
        checked = float(summary["checked"])
    except (KeyError, TypeError, ValueError):
        raise RuntimeError("liveness_checked_missing") from None
    if not math.isfinite(checked) or checked < 0:
        raise RuntimeError("liveness_checked_invalid")
    if checked.is_integer():
        checked = int(checked)
    attention = summary.get("attention")
    attention = attention if isinstance(attention, list) else []
    if any(not isinstance(a, dict) for a in attention):
        raise RuntimeError("liveness_attention_invalid")
    stamp = str(job.get("finished_utc") or "")
    if not stamp:
        raise RuntimeError("liveness_finished_time_missing")
    day = stamp[:10]
    queue_path = str(summary.get("queue") or "")
    citable = queue_path.startswith("reports/") or "/business/reports/" in queue_path
    removal = [a for a in attention if str(a.get("verdict") or "").lower() in ("paused", "gone")]
    rows = []
    for a in removal:
        network = str(a.get("network") or "")
        pid = str(a.get("network_program_id") or "")
        program = str(a.get("program") or "")
        name = str(a.get("display_name") or program)
        verdict = str(a["verdict"]).lower()
        evidence = {"message_id": f"liveness:{network}:{pid or program}@{stamp}",
                    "path": queue_path, "source": "weekly liveness audit",
                    "verdict": verdict,
                    "probe_evidence": str(a.get("evidence") or "")[:400],
                    "observed_at_utc": stamp, "cron_job": str(job.get("job_id") or "")}
        rows.append({"update_id": f"liveness-network_paused-{network}-{pid or program}-{day}"[:120],
                     "kind": "network_paused", "network": network,
                     "program_id": pid, "program_name": name,
                     "evidence": json.dumps(evidence, separators=(",", ":")),
                     "status": "queued", "queued_by": "program_liveness",
                     "queued_at_utc": stamp, "resolved_at_utc": "",
                     "note": (f"Weekly liveness audit verdict {verdict.upper()} for {name} "
                              f"[{network}]: {str(a.get('evidence') or '')[:300]}")[:900]})
    if len({r["update_id"] for r in rows}) != len(rows):
        raise RuntimeError("liveness_duplicate_queue_identity")
    for row in rows if citable else []:
        # The sole writer may resolve an earlier row before this finalizer
        # resumes. Reconcile its original operation, then continue the batch.
        current = n8n_dt.rows(QUEUE, n8n_dt.eq("update_id", row["update_id"]), limit=3)
        if n8n_dt.last_read_truncated:
            raise RuntimeError("liveness_queue_read_truncated")
        if not current:
            try:
                n8n_dt.insert(QUEUE, row)
                continue
            except n8n_dt.WriteReconciliationError:
                current = n8n_dt.rows(QUEUE, n8n_dt.eq("update_id", row["update_id"]), limit=3)
                if n8n_dt.last_read_truncated:
                    raise RuntimeError("liveness_queue_read_truncated") from None
        if len(current) != 1 or not runner.same_sheet_operation(current[0], row):
            raise RuntimeError("liveness_queue_operation_drift")
        if current[0].get("status") == "queued":
            if current[0].get("note") != row["note"]:
                raise RuntimeError("liveness_queue_note_drift")
        elif not runner.resolved_sheet_row(current[0]):
            raise RuntimeError("liveness_queue_resolution_unverified")
    note = (f"live={summary.get('live')} paused={summary.get('paused')} gone={summary.get('gone')} "
            f"unknown={summary.get('unknown')} queue={summary.get('removal_queue_count')} "
            f"probe_clicks={summary.get('tracking_probe_clicks')} "
            f"live_manifest_matches_release={summary.get('live_manifest_matches_release')}")
    effects = {"checked": checked, "queue_rows": len(rows) if citable else 0,
               "queue_suppressed": 0 if citable else len(rows),
               "heartbeat": runner.heartbeat("program_liveness", checked,
                   summary.get("receipt") or job["receipt_path"], note, job)}
    if attention:
        headline = (f"🚨 GLP program liveness: {len(removal)} program(s) are NO LONGER RUNNING and must come off the site"
                    if removal else "✅ GLP program liveness: no paused/gone programs on the site")
        lines = [headline,
                 f"checked {checked} — live {summary.get('live')} | paused {summary.get('paused')} | gone {summary.get('gone')} | unknown {summary.get('unknown')}",
                 f"live /go manifest matches the release build: {summary.get('live_manifest_matches_release')}"]
        for a in attention:
            lines.append(f"• {a.get('program')} [{a.get('network')}] → {str(a.get('verdict') or '').upper()} — {str(a.get('evidence') or '')[:180]}")
        if removal:
            lines.append(f"Removal queue: {queue_path} — run scripts/glp_site_paused_program_removal.py against a fresh release.")
        lines.append(f"receipt: {summary.get('receipt')}")
        text = "\n".join(lines)
        if len(text) > 1900:
            text = text[:1890] + "…"
        effects["notification"] = runner.post_once(text, job, state, state_path)
    return effects


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--scheduled", action="store_true")
    parser.add_argument("--cron", default=CRON)
    parser.add_argument("--timezone", default=TZ)
    opts = parser.parse_args(argv)
    if opts.dry_run:
        result = runner.run_action(ACTION, {"dry-run": "1"},
                                   invocation="dry_run", finish=lambda *a: {})
    else:
        if not opts.scheduled or opts.cron != CRON or opts.timezone != TZ:
            parser.error("live invocation requires --scheduled and the published cron/timezone")
        now = datetime.now(ZoneInfo(TZ))
        manual = os.environ.get("CRON_BRIDGE_MANUAL_REGISTERED") == "1"
        if not manual and not croniter.match(CRON, now):
            print(json.dumps({"status": "SKIPPED_NOT_DUE", "rc": 0}))
            return 0
        slot = (croniter(CRON, now).get_prev(datetime) if manual and not croniter.match(CRON, now)
                else now)
        due_slot = slot.strftime("%Y-%m-%dT%H:%M") + ":" + TZ
        result = runner.run_action(ACTION, snapshot_args(due_slot), finish=finish,
                                   invocation="manual_registered" if manual else "scheduled",
                                   due_slot=due_slot)
    print(json.dumps({k: v for k, v in result.items() if k != "bridge"}, sort_keys=True))
    return 0 if result["rc"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
