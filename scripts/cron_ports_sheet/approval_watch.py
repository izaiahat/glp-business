#!/usr/bin/env python3
"""Cron port of the published glp/approval-watch scheduled graph.

The registered producer owns approval diffs and inserts into the governed Sheet
queue via --queue. This wrapper never writes a Sheet or invents a second queue
producer. Dry-run exercises the allowlisted coverage-only branch without queue,
heartbeat, notification, portal login, or snapshot changes.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from croniter import croniter
from scripts import cron_bridge_action as bridge

ACTION = "approval_watch"
CRON = "15 8,13,18 * * *"
TIMEZONE = "America/Los_Angeles"


def finish(action, args, job, state, state_path):
    """Published Resolve Watch -> heartbeat -> conditional ops-alerts report."""
    if action != ACTION:
        raise RuntimeError("approval_watch_action_mismatch")
    summary = bridge.machine_receipt(job, schema="glp.approval_watch.v1")
    if args.get("coverage-only") == "1":
        if args.get("queue") != "0":
            raise RuntimeError("approval_watch_dry_run_queue_enabled")
        if summary.get("mode") != "coverage_only":
            raise RuntimeError("approval_watch_dry_run_mode_mismatch")
        return {"machine_status": summary.get("status"), "mode": "coverage_only",
                "coverage": summary.get("coverage", {}), "queue_rows_inserted": 0,
                "no_effects": True}
    if args.get("queue") != "1" or summary.get("mode") != "full_watch":
        raise RuntimeError("approval_watch_live_mode_mismatch")
    if summary.get("status") not in ("PASS", "GAPS_DECLARED"):
        raise RuntimeError("approval_watch_status_invalid")
    coverage = summary.get("coverage")
    if not isinstance(coverage, dict) or not isinstance(coverage.get("rows"), list):
        raise RuntimeError("approval_watch_coverage_missing")
    events = (summary.get("approval_events") or []) + (summary.get("katalys_approval_events") or [])
    unbound = summary.get("unbound_approvals") or []
    unauth = summary.get("tenants_unauthenticated") or []
    not_yet = summary.get("tenants_no_account_yet") or []
    if not all(isinstance(x, list) for x in (events, unbound, unauth, not_yet)):
        raise RuntimeError("approval_watch_event_shape_invalid")
    receipt = summary.get("receipt")
    if not isinstance(receipt, str) or not receipt:
        raise RuntimeError("approval_watch_receipt_missing")
    # The producer inserts its proposed approval rows through n8n_dt using its
    # stable update_id before printing this receipt. Never insert them again.
    count = summary.get("effects", {}).get("queue_rows_inserted")
    if type(count) is not int or count < 0:
        raise RuntimeError("approval_watch_queue_outcome_missing")
    watched, total = coverage.get("networks_watched"), coverage.get("networks_total")
    if type(watched) is not int or type(total) is not int:
        raise RuntimeError("approval_watch_network_counts_missing")
    note = f"coverage {watched}/{total} · unbound {len(unbound)}"
    effects = {"machine_status": summary["status"], "events": len(events),
               "queue_rows_inserted": count,
               "heartbeat": bridge.heartbeat(ACTION, len(events), receipt, note, job)}
    # Published Report? is true for events, unbound approvals, or blind tenants.
    # No notification for merely unwatched networks or accounts not yet opened.
    if events or unbound or unauth:
        lines = []
        if events:
            lines.append(f"🟢 **GLP approval watch — {len(events)} NEW approval(s) on a surface no email was needed for**")
            for event in events[:12]:
                binding = event.get("binding") or {}
                lines.append("• " + str(event.get("tenant_slug") or event.get("network") or "?")
                             + " offer " + str(event.get("offer_id") or event.get("program_id") or "undefined")
                             + " — " + str(event.get("offer_name") or "") + " → " + str(event.get("to", "undefined"))
                             + (" · Sheet row " + str(binding.get("sheet_row")) if binding.get("bound")
                                else " · UNBOUND: " + str(binding.get("reason") or "no row")))
        else:
            lines.append("⚠️ **GLP approval watch — attention**")
        if unauth:
            lines.append("⚠️ tenants BLIND (need a sign-in): " + ", ".join(map(str, unauth)))
        if not_yet:
            lines.append("ℹ️ no account yet (they have not approved us; nothing to do): " + ", ".join(map(str, not_yet)))
        bad = [row for row in coverage["rows"] if row.get("watched") is False]
        if bad:
            lines.append(f"⚪ unwatched networks ({len(bad)}): " + ", ".join(str(row.get("network")) for row in bad))
        lines.append(note.split(" · unbound")[0] + " · tenants "
                     + str(len(coverage.get("everflow_tenants_derived") or [])) + " · receipt " + receipt)
        effects["notification"] = bridge.post_once("\n".join(lines)[:1900], job, state, state_path)
    return effects


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--scheduled", action="store_true")
    parser.add_argument("--cron", default=CRON)
    opts = parser.parse_args(argv)
    manual_registered = os.environ.get("CRON_BRIDGE_MANUAL_REGISTERED") == "1"
    if manual_registered and not opts.scheduled:
        parser.error("manual_registered requires --scheduled")
    now = datetime.now(ZoneInfo(TIMEZONE))
    if opts.scheduled and not manual_registered and not croniter.match(opts.cron, now):
        print(json.dumps({"status": "NOT_DUE", "cron": opts.cron}))
        return 0
    slot = croniter(opts.cron, now).get_prev(datetime) if (manual_registered and not croniter.match(opts.cron, now)) else now
    due_slot = slot.strftime("%Y-%m-%dT%H:%M") + ":" + TIMEZONE if opts.scheduled else None
    # A bounded, stable run identity for each scheduled slot; run_action keeps
    # the same pending bridge job across interrupted invocations.
    run_id = slot.strftime("cron-%Y%m%dT%H%M") if opts.scheduled else datetime.now(ZoneInfo("UTC")).strftime("manual-%Y%m%dT%H%M%S%f")
    args = ({"coverage-only": "1", "queue": "0"} if opts.dry_run
            else {"run-id": run_id, "queue": "1"})
    extras = ({"state_dir": bridge.STATE_DIR / "approval_watch_dry_run",
               "notify": lambda *unused: {"status": "dry_run_alert_suppressed"}}
              if opts.dry_run else {})
    result = bridge.run_action(ACTION, args, finish=finish, due_slot=due_slot,
                               invocation=("manual_registered" if manual_registered else
                                           "scheduled" if opts.scheduled else "manual"), **extras)
    print(json.dumps({k: v for k, v in result.items() if k != "bridge"}, sort_keys=True))
    return 0 if result.get("rc") == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
