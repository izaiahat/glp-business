#!/usr/bin/env python3
"""09:40 PT applications queue schedule, migrated from n8n tToLBS845swwpXe4.

The producer remains the allowlisted application_machine action. Only its
operator-queued drain may submit; Sheet changes are queued through n8n_dt.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from croniter import croniter

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts import cron_bridge_action as bridge

ACTION = "application_machine"
CRON = "40 9 * * *"
TIMEZONE = "America/Los_Angeles"
SCHEMA = "glp-application-queue-drain-summary-v1"
ROW_FIELDS = ("update_id", "kind", "network", "program_id", "program_name",
              "evidence", "status", "queued_by", "queued_at_utc", "note")
RESOLVED_STATUSES = ("applied", "noop", "refused", "escalated", "deferred")


def same_queue_note_or_resolved(current, original):
    """Only the sole writer's resolved ACK may change an operation's note."""
    return current.get("note") == original["note"] or (
        current.get("status") in RESOLVED_STATUSES and bridge.resolved_sheet_row(current))


def finish(action, args, job, state, state_path):
    if action != ACTION or args.get("mode") not in ("drain", "plan"):
        raise RuntimeError("applications_action_binding_invalid")
    if job.get("status") != "done" or type(job.get("rc")) is not int or job["rc"] != 0:
        raise RuntimeError("applications_job_not_complete")
    summary = bridge.machine_receipt(job, schema=SCHEMA)
    if not isinstance(summary.get("status"), str) or not summary["status"]:
        raise RuntimeError("applications_summary_status_missing")
    rows = summary.get("queue_rows")
    if not isinstance(rows, list):
        raise RuntimeError("applications_queue_rows_missing")
    submitted = summary.get("submitted", 0)
    if type(submitted) is not int or submitted < 0:
        raise RuntimeError("applications_submitted_invalid")
    if args["mode"] == "plan":
        if rows or submitted or summary["status"] not in (
                "PLAN_ONLY", "HEALTHY_EMPTY", "PAUSED_BY_APPLY_GATE",
                "NO_ITEMS_IN_OPERATOR_SCOPE", "LOCK_CONTENDED_NO_EFFECT"):
            raise RuntimeError("applications_plan_had_effects_or_unknown_status")
        return {"machine_status": summary["status"], "mode": "plan",
                "queue_items": summary.get("queue_items"), "queue_rows_confirmed": 0,
                "notification": "suppressed", "heartbeat": "suppressed"}
    from ops import n8n_dt
    confirmed = 0
    seen = set()
    for raw in rows:
        if not isinstance(raw, dict) or any(not isinstance(raw.get(k), str) for k in ROW_FIELDS):
            raise RuntimeError("applications_queue_row_shape_invalid")
        row = {key: raw[key] for key in ROW_FIELDS}
        if not row["update_id"] or not row["kind"] or not row["queued_at_utc"] or row["status"] != "queued":
            raise RuntimeError("applications_queue_row_identity_invalid")
        if row["update_id"] in seen:
            raise RuntimeError("applications_queue_duplicate_update_id")
        seen.add(row["update_id"])
        if raw.get("sheet_row") not in (None, ""):
            row["sheet_row"] = raw["sheet_row"]
        key = row["update_id"]
        existing = n8n_dt.rows(bridge.SHEET_QUEUE, n8n_dt.eq("update_id", key), limit=3)
        if existing:
            if len(existing) != 1 or not bridge.same_sheet_operation(existing[0], row):
                raise RuntimeError("applications_queue_existing_identity_drift")
            if existing[0].get("status") not in ("queued", *RESOLVED_STATUSES):
                raise RuntimeError("applications_queue_existing_status_invalid")
            if not same_queue_note_or_resolved(existing[0], row):
                raise RuntimeError("applications_queue_existing_note_drift")
        else:
            try:
                n8n_dt.insert(bridge.SHEET_QUEUE, row)
            except n8n_dt.WriteReconciliationError:
                # An ambiguous acknowledgment may race the sole Sheet consumer.
                # Only a matching operation is safe to recognize; never reinsert.
                landed = n8n_dt.rows(bridge.SHEET_QUEUE, n8n_dt.eq("update_id", key), limit=3)
                if (len(landed) != 1 or not bridge.same_sheet_operation(landed[0], row)
                        or landed[0].get("status") not in ("queued", *RESOLVED_STATUSES)
                        or not same_queue_note_or_resolved(landed[0], row)):
                    raise
        confirmed += 1
    text = str(summary.get("discord_text") or "")[:1900]
    effects = {"machine_status": summary["status"], "submitted": submitted,
               "queue_rows_confirmed": confirmed, "run_id": str(summary.get("run_id") or "")}
    if text:
        suffix = "\nqueue rows confirmed: " + str(confirmed)
        effects["notification"] = bridge.post_once(
            text[:1900 - len(suffix)] + suffix, job, state, state_path)
    note = (f"status={summary['status']} queued={int(summary.get('queue_items') or 0)} "
            f"processed={int(summary.get('processed') or 0)} submitted={submitted}")[:240]
    effects["heartbeat"] = bridge.heartbeat(
        "applications", submitted, str(summary.get("receipt") or job["receipt_path"]), note, job)
    return effects


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="plan mode; no queue, heartbeat or notification")
    parser.add_argument("--scheduled", action="store_true")
    opts = parser.parse_args(argv)
    now = datetime.now(ZoneInfo(TIMEZONE))
    manual_registered = os.environ.get("CRON_BRIDGE_MANUAL_REGISTERED") == "1"
    if manual_registered and (not opts.scheduled or opts.dry_run):
        parser.error("manual registered requires --scheduled without --dry-run")
    if opts.scheduled and not manual_registered and not croniter.match(CRON, now):
        print(json.dumps({"status": "SKIPPED_NOT_DUE", "action": ACTION}))
        return 0

    slot_time = now
    if manual_registered and not croniter.match(CRON, now):
        slot_time = croniter(CRON, now).get_prev(datetime)
    slot = (slot_time.strftime("%Y-%m-%dT%H:%M") + ":" + TIMEZONE) if opts.scheduled else None
    result = bridge.run_action(ACTION, {"mode": "plan" if opts.dry_run else "drain"},
                               finish=finish, invocation="manual_registered" if manual_registered
                               else "scheduled" if opts.scheduled else "manual", due_slot=slot)
    print(json.dumps({k: v for k, v in result.items() if k != "bridge"}, sort_keys=True))
    return 0 if result["rc"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
