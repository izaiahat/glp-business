#!/usr/bin/env python3
"""Cron port of published glp/inbox-mirror (bQWtmjut2vnR6MQN).

The bridge runs the two allowlisted machines; the n8n Data Table queue remains
transport for the sole Sheet writer. This module never writes Sheet cells.
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

CRON = "20 7 * * *"
TZ = "America/Los_Angeles"
QUEUE_COLUMNS = (*bridge.QUEUE_FIELDS, "status", "resolved_at_utc", "sheet_row")


def finish(action, args, job, state, state_path):
    """Complete the published graph's postconditions with durable readbacks."""
    from ops import n8n_dt

    if action != "inbox_mirror" or job.get("rc") != 0 or job.get("status") != "done":
        raise RuntimeError("inbox_mirror_job_not_successful")
    summary = bridge.machine_receipt(job, schema="glp.inbox_mirror.v1")
    source_rows = summary.get("rows")
    if not isinstance(source_rows, list):
        raise RuntimeError("inbox_mirror_rows_missing")
    rows = []
    for raw in source_rows:
        if not isinstance(raw, dict) or not raw.get("update_id") or not raw.get("kind"):
            raise RuntimeError("inbox_mirror_queue_identity_missing")
        rows.append({key: raw.get(key, "") for key in QUEUE_COLUMNS})
    if len({r["update_id"] for r in rows}) != len(rows):
        raise RuntimeError("inbox_mirror_duplicate_update_id")
    receipt = str(summary.get("receipt") or job["receipt_path"])
    inserted = state.setdefault("mirror_insertions", {})
    for row in rows:
        key = str(row["update_id"])
        if key in inserted:
            if inserted[key] != row:
                raise RuntimeError("inbox_mirror_queue_binding_changed")
            continue
        try:
            n8n_dt.insert(bridge.SHEET_QUEUE, row)
        except n8n_dt.WriteReconciliationError:
            # An acknowledgment can be lost after insertion, or the sole writer
            # can already have consumed it. Never issue a second blind INSERT.
            current = n8n_dt.rows(bridge.SHEET_QUEUE, n8n_dt.eq("update_id", key), limit=3)
            if len(current) != 1 or not bridge.same_sheet_operation(current[0], row):
                raise  # The natural key alone does not bind the original operation.
            observed = current[0]
            if observed.get("status") == "queued":
                # Before the sole writer resolves it, the source note is part
                # of the exact queued payload, not an optional annotation.
                if str(observed.get("note") or "") != str(row["note"] or ""):
                    raise
            elif not bridge.resolved_sheet_row(observed):
                # A bound, resolved row may carry the sole writer's new note.
                raise
        inserted[key] = row
        bridge.save(state_path, state)

    scanned = summary.get("scanned", 0)
    attention = summary.get("attention", 0)
    unclassified = summary.get("unclassified", 0)
    try:
        scanned, attention, unclassified = (int(x) for x in (scanned, attention, unclassified))
        if min(scanned, attention, unclassified) < 0:
            raise ValueError()
    except (TypeError, ValueError):
        raise RuntimeError("inbox_mirror_counts_invalid") from None
    note = (f"scanned {scanned} / queued {len(rows)} / attention {attention} / "
            f"unclassified {unclassified}")[:240]
    effects = {"scanned": scanned, "queue_rows": len(rows), "receipt": receipt}
    if len(rows) + attention > 0:
        text = str(summary.get("discord_text") or
                   f"GLP affiliate inbox mirror: {len(rows)} queue row(s) from {scanned} scanned.")
        effects["notification"] = bridge.post_once(text[:1890] + "…" if len(text) > 1900 else text,
                                                   job, state, state_path)
    effects["heartbeat"] = bridge.heartbeat("inbox_mirror", len(rows), receipt, note, job)

    parts = summary.get("decisions_parts")
    if not isinstance(parts, list) or not any(parts):
        text = str(summary.get("decisions_text") or "")
        parts = [text[:1990]] if text else []
    decisions = []
    for part in parts:
        if not str(part or "").strip():
            continue
        # The graph clips each pre-chunked card part at 1990; post_once's
        # conservative 1900 limit must not silently drop the remaining text.
        content = str(part)[:1990]
        for start in range(0, len(content), 1900):
            decisions.append(bridge.post_once(content[start:start + 1900], job,
                                              state, state_path, channel=bridge.DECISIONS_CHANNEL))
    effects["decision_messages"] = len(decisions)

    if int(summary.get("lane_handoff_count") or 0) > 0:
        if not summary.get("receipt"):
            raise RuntimeError("commission_handoff_source_receipt_missing")
        # This stable slot binds the continuation to this mirror job. A crash
        # after commission success cannot re-run an effectful self-serve action.
        continuation = bridge.run_action(
            "commission_terms_selfserve", {"from-receipt": str(summary["receipt"])},
            invocation="continuation:" + str(job["job_id"]),
            due_slot="mirror-job:" + str(job["job_id"]), finish=finish_commission)
        if continuation.get("rc") != 0 or continuation.get("status") not in ("PASS", "SKIPPED_OBSERVED_SLOT"):
            raise RuntimeError("commission_terms_selfserve_not_complete_see_bridge_receipt")
        effects["commission"] = {"status": continuation["status"],
                                 "receipt_path": continuation["receipt_path"]}
    return effects


def finish_commission(action, args, job, state, state_path):
    if (action != "commission_terms_selfserve" or job.get("status") != "done"
            or job.get("rc") != 0):
        raise RuntimeError("commission_job_not_successful")
    summary = bridge.machine_receipt(job, schema="glp.commission_terms_selfserve.v1")
    try:
        queued = int(summary["queued"])
        note = (f"notices {summary['notices']} / queued {queued} / already "
                f"{int(summary['already_queued']) + int(summary['already_correct'])} / "
                f"site-rate-checks {summary['site_rate_checks']} / "
                f"operator cards {summary['operator_cards']}")[:240]
        if queued < 0:
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise RuntimeError("commission_receipt_counts_invalid") from None
    # The ledger is shared across runs; the terminal bridge receipt is this
    # invocation's stable Data Table natural key, including GET-only recovery.
    receipt = str(job["receipt_path"])
    return {"queued": queued, "heartbeat": bridge.heartbeat(
        "commission_terms_selfserve", queued, receipt, note, job)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="registered scan-only action; no queue, cursor, heartbeat or notification effects")
    parser.add_argument("--scheduled", action="store_true")
    parser.add_argument("--cron", default=CRON)
    opts = parser.parse_args(argv)
    manual_registered = os.environ.get("CRON_BRIDGE_MANUAL_REGISTERED") == "1"
    if manual_registered and not opts.scheduled:
        parser.error("manual_registered requires --scheduled")
    now = datetime.now(ZoneInfo(TZ))
    if opts.scheduled and not opts.dry_run and not manual_registered and not croniter.match(opts.cron, now):
        print(json.dumps({"status": "SKIPPED_NOT_DUE", "cron": opts.cron, "timezone": TZ}))
        return 0
    slot_time = now
    if opts.scheduled and manual_registered and not croniter.match(opts.cron, now):
        slot_time = croniter(opts.cron, now).get_prev(datetime)
    due_slot = slot_time.strftime("%Y-%m-%dT%H:%M") + ":" + TZ if opts.scheduled and not opts.dry_run else None
    if opts.dry_run:
        result = bridge.run_action("inbox_mirror_scan_only", {"max-messages": "1"},
                                   invocation="dry_run", finish=lambda *a: {})
    else:
        invocation = "manual_registered" if manual_registered else ("scheduled" if opts.scheduled else "manual")
        result = bridge.run_action("inbox_mirror", {"max-messages": "120"},
                                   invocation=invocation, due_slot=due_slot, finish=finish)
    print(json.dumps({k: v for k, v in result.items() if k != "bridge"}, sort_keys=True))
    return 0 if result["rc"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
