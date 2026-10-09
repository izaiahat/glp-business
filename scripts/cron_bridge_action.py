#!/usr/bin/env python3
"""Host cron entrypoint for the existing authenticated, allowlisted n8n bridge.

The bridge still owns validation, execution deadlines, admission and action
receipts. This caller owns per-action overlap and failure-streak notification.
An uncertain POST is never retried; a known job is reconciled by GET only.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import math
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from croniter import croniter

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
STATE_DIR = ROOT / "reports/ops/n8n-to-cron-20260924/runs"
ACTIONS_PATH = ROOT / "ops/n8n-bridge/actions.json"
CHANNEL = "1487300132054765629"
DECISIONS_CHANNEL = "1524254375453786294"
HEARTBEATS = "9Gevk0AdC7J6UbFn"
SHEET_QUEUE = "hJiP3eJKBvtZ6bQU"
QUEUE_FIELDS = ("update_id", "kind", "network", "program_id", "program_name",
                "evidence", "queued_by", "queued_at_utc", "note")


class TerminalRefusal(RuntimeError):
    """A conclusive machine verdict; archive this slot without replaying it."""
    def __init__(self, machine_status, reason="article_carrier_base_refused_before_consume"):
        super().__init__(reason)
        self.machine_status = machine_status


def machine_receipt(job, *, schema=None, field=None, machine=None):
    if job.get("stdout_truncated"):
        raise RuntimeError("machine_receipt_truncated_see_bridge_spill")
    for line in reversed(str(job.get("stdout_tail") or "").splitlines()):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if (isinstance(row, dict) and (schema is None or row.get("schema") == schema)
                and (field is None or field in row)
                and (machine is None or row.get("machine") == machine)):
            return row
    raise RuntimeError("machine_receipt_missing")


def heartbeat(machine, count, receipt, note, job):
    from ops.n8n_dt import insert
    if isinstance(count, float) and count.is_integer():
        count = int(count)  # Native numeric columns read integral values back as int.
    # The registered receipt key and stable terminal time make reconciliation exact.
    return insert(HEARTBEATS, {"machine": machine,
        "last_success_utc": job["finished_utc"], "output_count": count,
        "receipt": receipt, "note": note})


def post_once(content, job, state, state_path, *, channel=CHANNEL):
    # Explicit no-send registered acceptance keeps the real publication/receiving
    # path, but never reaches a messaging transport. Natural slots are unchanged.
    if (os.environ.get("CRON_BRIDGE_MANUAL_REGISTERED") == "1"
            and os.environ.get("CRON_BRIDGE_NO_SEND") == "1"):
        return {"status": "SUPPRESSED_NO_SEND", "reason": "operator_no_send_registered_acceptance", "channel": channel}
    from scripts.post_discord_message import send, readback
    if channel not in (CHANNEL, DECISIONS_CHANNEL):
        raise RuntimeError("notification_sink_invalid")
    # Retain the historical ops-alerts identity for in-flight delivery records.
    key_input = job["job_id"] + content + ("" if channel == CHANNEL else ":" + channel)
    key = hashlib.sha256(key_input.encode()).hexdigest()
    sent_content = content[:1900].rstrip("\r\n")
    delivery = state.setdefault("deliveries", {}).get(key)
    if not delivery:
        delivery = {"status": "sending", "channel": channel}
        state["deliveries"][key] = delivery
        save(state_path, state)
        sent = send(channel, sent_content)
        delivery["id"] = str(sent.get("id") or "")
        save(state_path, state)
    if delivery.get("channel", channel) != channel or not delivery.get("id"):
        raise RuntimeError("notification_ack_unknown_no_resend")
    fetched = readback(channel, delivery["id"])
    if (fetched.get("channel_id") != channel or fetched.get("content") != sent_content
            or (channel == DECISIONS_CHANNEL and str(fetched.get("id") or "") != delivery["id"])):
        raise RuntimeError("notification_exact_readback_failed")
    delivery["status"] = "verified"
    save(state_path, state)
    return delivery


def queued_sheet_batch():
    from ops import n8n_dt
    batch = []
    # The helper defaults to 100 total rows, not returnAll. Use its bounded
    # 50-page allowance; never report empty from an incomplete valid-row scan.
    candidates = n8n_dt.rows(SHEET_QUEUE, n8n_dt.eq("status", "queued"), limit=5000)
    truncated = n8n_dt.last_read_truncated
    for row in candidates:
        status = str(row.get("status") or "queued").strip().lower()
        if not row.get("update_id") or not row.get("kind") or status not in ("", "queued"):
            continue
        item = {key: str(row.get(key) or "") for key in QUEUE_FIELDS}
        number = row.get("sheet_row")
        item["sheet_row"] = None if number in (None, "") else float(number)
        item["status"] = "queued"
        batch.append(item)
        if len(batch) == 150:
            break
    if truncated and len(batch) < 150:
        raise RuntimeError("sheet_queue_scan_incomplete")
    if len({row["update_id"] for row in batch}) != len(batch):
        raise RuntimeError("sheet_queue_duplicate_identity")
    return batch


def same_sheet_operation(current, original):
    if any(str(current.get(key) or "") != str(original.get(key) or "")
           for key in QUEUE_FIELDS if key != "note"):
        return False
    if "sheet_row" in original:
        try:
            number = lambda value: None if value in (None, "") else float(value)
            if number(current.get("sheet_row")) != number(original.get("sheet_row")):
                return False
        except (TypeError, ValueError):
            return False
    return True


def resolved_sheet_row(current):
    if current.get("status") not in ("applied", "noop", "refused", "escalated", "queued", "deferred"):
        return False
    try:
        resolved = datetime.fromisoformat(str(current.get("resolved_at_utc") or "").replace("Z", "+00:00"))
        queued = datetime.fromisoformat(str(current.get("queued_at_utc") or "").replace("Z", "+00:00"))
        return resolved.tzinfo is not None and queued.tzinfo is not None and resolved >= queued
    except (TypeError, ValueError):
        return False


def finish_sheet_drain(job, state, state_path):
    from ops.n8n_dt import rows, update, eq
    if job.get("local_noop"):
        return {"machine_status": "EMPTY_QUEUE", "heartbeat": heartbeat(
            "sheet_sync", 0, job["receipt_path"], "queue empty; no sheet write attempted", job)}
    r = machine_receipt(job, schema="glp.sheet_sync_drain.v1")
    if job["rc"] == 75 and r.get("lock_contended") is True:
        return {"machine_status": "DEFERRED_LOCK_BUSY", "designed_stand_down": True,
                "heartbeat": heartbeat("sheet_sync", 0, job["receipt_path"],
                    f"DEFERRED (lock busy): {r.get('queue_rows', 0)} queue rows preserved", job)}
    if job["rc"] != 0:
        return {}
    originals = {row["update_id"]: row for row in state["pending"]["sheet_batch"]}
    results = r.get("results")
    if not isinstance(results, list):
        raise RuntimeError("sheet_drain_results_missing")
    seen = set()
    for row in results:
        identity = str(row.get("update_id") or "")
        original = originals.get(identity)
        if not original or identity in seen or row.get("kind") != original["kind"]:
            raise RuntimeError("sheet_drain_result_identity_mismatch")
        seen.add(identity)
        status = str(row.get("status") or "")
        if status not in ("applied", "noop", "refused", "escalated", "queued"):
            raise RuntimeError("sheet_drain_result_status_invalid")
        detail = str(row["reason"]) if row.get("reason") else f"{row.get('cells_written', 0)} cells written"
        data = {"status": status, "resolved_at_utc": job["finished_utc"],
                "note": (status.upper() + ": " + detail + " | " + original["note"])[:900]}
        filt = eq("update_id", identity)
        current = rows(SHEET_QUEUE, filt)
        if len(current) != 1:
            raise RuntimeError("sheet_ack_identity_not_unique")
        current = current[0]
        if not same_sheet_operation(current, original):
            raise RuntimeError("sheet_ack_queue_identity_changed")
        if all(current.get(key) == value for key, value in data.items()):
            continue  # A prior acknowledgment landed; never repeat the Sheet effect.
        # Other registered callers drain under the same Sheet lock and ACK
        # afterwards with their own time/note. Our bound result confirms the
        # same operation's outcome; retain a compatible producer ACK unchanged.
        compatible = (current.get("status") == status and status != "queued") or (
            current.get("status") in ("applied", "noop") and status in ("applied", "noop"))
        if compatible and resolved_sheet_row(current):
            continue
        if str(current.get("status") or "queued").strip().lower() != "queued" or str(current.get("note") or "") != original["note"]:
            raise RuntimeError("sheet_ack_queue_state_changed")
        # Preserve the graph's natural-key update and registered exact readback.
        # This is not atomic status-CAS; the registered drain remains sole Sheet writer.
        update(SHEET_QUEUE, filt, data)
    counts = {key: int(r.get(key) or 0) for key in ("applied", "noop", "refused", "escalated", "deferred", "cells_written")}
    note = " / ".join(f"{key} {value}" for key, value in counts.items())[:240]
    receipt = str(r.get("receipt") or job["receipt_path"])
    effects = {**counts, "heartbeat": heartbeat("sheet_sync", counts["applied"], receipt, note, job)}
    if sum(counts[key] for key in ("applied", "refused", "escalated", "deferred")):
        lines = ["GLP sheet writer: " + note, f"queue rows {r.get('queue_rows', 0)}"]
        for row in results[:20]:
            lines.append(f"{row['kind']} — {row.get('program_name') or row['update_id']} → {row['status']}: {str(row.get('reason') or '')[:200]}")
        lines.append("receipt: " + receipt)
        effects["notification"] = post_once("\n".join(lines)[:1900], job, state, state_path)
    return effects


def decisions_summary(job):
    if (job.get("status") != "done" or type(job.get("rc")) is not int
            or job["rc"] != 0 or job.get("timed_out") or job.get("interrupted")):
        raise RuntimeError("decisions_job_not_complete")
    row = machine_receipt(job, field="status")
    if row.get("status") not in ("complete", "awaiting_agent"):
        raise RuntimeError("decisions_router_noncomplete")
    if (row.get("mode") != "live" or row.get("sink") not in ("decisions", "ops-alerts")
            or not isinstance(row.get("run_id"), str)
            or not re.fullmatch(r"live-[A-Za-z0-9_-]{1,100}", row["run_id"])):
        raise RuntimeError("decisions_router_scope_invalid")
    return row


def decisions_stage(stage, payload, state, state_path, call, *, timeout_s=3300):
    """Durable bridge POST/GET for a graph continuation under the parent's flock."""
    jobs = state.setdefault("decisions_jobs", {})
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    pending = jobs.get(stage)
    if pending:
        if pending["payload_sha256"] != digest:
            raise RuntimeError("decisions_continuation_binding_changed")
        if not pending.get("job_id"):
            raise RuntimeError("decisions_submission_ack_unknown_no_replay")
        code, job = call("/job/" + pending["job_id"])
        if code != 200 or job.get("job_id") != pending["job_id"]:
            raise RuntimeError("decisions_job_readback_invalid")
    else:
        pending = {"payload_sha256": digest}
        jobs[stage] = pending
        save(state_path, state)  # Unknown POST acknowledgement is a hold, not a replay grant.
        code, job = call("/run", payload)
        if code in (400, 401, 403, 404, 413, 429):
            # Bridge rejects these before admission/execution. Preserve the
            # desired model; a later allowlist grant can retry this same route.
            jobs.pop(stage, None)
            save(state_path, state)
            raise RuntimeError(f"decisions_bridge_rejected_http_{code}")
        if code not in (200, 202) or not job.get("job_id"):
            raise RuntimeError("decisions_submission_ack_unknown_no_replay")
        pending["job_id"] = str(job["job_id"])
        save(state_path, state)
    deadline = time.monotonic() + timeout_s + 120
    while job.get("status") not in ("done", "error"):
        if time.monotonic() >= deadline:
            raise RuntimeError("decisions_job_deadline_pending_preserved")
        time.sleep(3)
        code, job = call("/job/" + pending["job_id"])
        if code != 200 or job.get("job_id") != pending["job_id"]:
            raise RuntimeError("decisions_job_readback_invalid")
    if (job.get("status") != "done" or type(job.get("rc")) is not int or job["rc"] != 0
            or job.get("stdout_truncated") or job.get("timed_out") or job.get("interrupted")
            or (job.get("termination") or {}).get("classification") == "BRIDGE_INTERRUPTED"):
        raise RuntimeError("decisions_continuation_not_complete")
    return job


def verify_decisions_confirmation(confirmation, *, run_id, sink, channel, card, mid):
    checked = confirmation.get("confirmation") or {}
    if (confirmation.get("run_id") != run_id or confirmation.get("sink") != sink
            or confirmation.get("status") != "complete" or confirmation.get("post_required") is not False
            or confirmation.get("confirmation_card") != card
            or checked.get("discord_message_id") != mid or checked.get("channel_id") != channel
            or checked.get("sink") != sink or
            any(checked.get("checks", {}).get(k) is not True for k in ("id", "channel", "content"))):
        raise RuntimeError("decisions_confirm_readback_invalid")
    from scripts.post_discord_message import readback
    fetched = readback(channel, mid)
    if (str(fetched.get("id") or "") != mid or fetched.get("channel_id") != channel
            or fetched.get("content") != card):
        raise RuntimeError("decisions_confirm_live_readback_invalid")


def decisions_host_sol(prompt, receipt_path, run_id, state, state_path):
    """Subscription-only Sol continuation; an unknown launch is never replayed."""
    from ops.decisions_router import AGENT_DIR
    import subprocess
    import signal
    if Path(receipt_path).resolve().parent != AGENT_DIR.resolve():
        raise RuntimeError("decisions_agent_receipt_outside_owned_directory")
    jobs = state.setdefault("decisions_jobs", {})
    digest = hashlib.sha256(prompt.encode()).hexdigest()
    stage = jobs.get("sol")
    if stage:
        if (stage.get("prompt_sha256") != digest or stage.get("run_id") != run_id
                or stage.get("receipt_path") != receipt_path):
            raise RuntimeError("decisions_host_sol_binding_changed")
        if stage.get("status") != "complete" or stage.get("rc") != 0:
            raise RuntimeError("decisions_host_sol_unconfirmed_no_replay")
        return {"job_id": stage["job_id"], "status": "done", "rc": 0}
    prompt_path = Path(state_path).parent / ("decisions-sol-" + run_id + ".prompt")
    # O_EXCL refuses a stale prompt file; never run an unreviewed prior query.
    fd = os.open(prompt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(prompt)
        stream.flush()
        os.fsync(stream.fileno())
    stage = {"run_id": run_id, "prompt_sha256": digest,
             "receipt_path": receipt_path, "status": "launch_unknown",
             "job_id": "host-sol-" + uuid.uuid4().hex}
    jobs["sol"] = stage
    save(state_path, state)
    # Cron's minimal PATH does not include ~/.local/bin; bind the installed CLI.
    command = ["/home/ubuntu/.local/bin/hermes", "chat", "--quiet", "--cli", "--source", "tool",
               "--provider", "openai-codex", "--model", "gpt-6.1-sol",
               "--reasoning", "xhigh", "--max-turns", "60", "--toolsets",
               "web,file,terminal,firecrawl", "--query-file", str(prompt_path)]
    try:
        proc = subprocess.Popen(command, cwd=str(ROOT), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, start_new_session=True)
        try:
            stdout, stderr = proc.communicate(timeout=3300)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
            raise RuntimeError("decisions_host_sol_timeout_no_replay")
        # No model output or exception text is persisted in public receipts.
        stage.update(status="complete" if proc.returncode == 0 else "failed",
                     rc=proc.returncode, stdout_sha256=hashlib.sha256(stdout).hexdigest(),
                     stderr_sha256=hashlib.sha256(stderr).hexdigest())
        save(state_path, state)
        if proc.returncode != 0:
            raise RuntimeError("decisions_host_sol_failed_no_replay")
        return {"job_id": stage["job_id"], "status": "done", "rc": 0}
    finally:
        prompt_path.unlink(missing_ok=True)


def finish_decisions(job, state, state_path, call):
    initial = decisions_summary(job)
    final = initial
    run_id = initial["run_id"]
    if initial["status"] == "awaiting_agent":
        prompt = initial.get("free_text_prompt")
        path = initial.get("agent_receipt_path")
        expected = initial.get("agent_expected")
        if (not isinstance(prompt, str) or not prompt.strip() or not isinstance(path, str)
                or not path or not isinstance(expected, list) or not expected
                or initial.get("post_required") is not False):
            raise RuntimeError("decisions_agent_scope_invalid")
        # The router and confirmation still use the registered bridge action.
        # Model execution is host-local on the approved subscription route;
        # never fall back to the bridge's older 5.6 model allowlist.
        from ops.decisions_router import validate_agent
        sol = decisions_host_sol(prompt, path, run_id, state, state_path)
        # Router resume converts invalid agent receipts into 'unroutable'. Do
        # not call it on an invalid/missing receipt and mistake that for success.
        validate_agent(initial)
        resumed = decisions_stage("resume", {"action": "script", "name": "decisions_router",
            "args": {"mode": "resume", "run-id": run_id, "job-id": str(sol["job_id"])}},
            state, state_path, call)
        final = decisions_summary(resumed)
    if (final["status"] != "complete" or final["run_id"] != run_id
            or final["sink"] != initial["sink"]):
        raise RuntimeError("decisions_final_binding_invalid")
    count = final.get("item_count")
    unroutable = final.get("unroutable_count")
    if (type(count) is not int or count < 0 or type(unroutable) is not int
            or not 0 <= unroutable <= count or type(final.get("post_required")) is not bool
            or not isinstance(final.get("source_message_ids"), list)):
        raise RuntimeError("decisions_final_counts_invalid")
    card = final.get("confirmation_card")
    sink = final["sink"]
    channel = CHANNEL if sink == "ops-alerts" else DECISIONS_CHANNEL
    if final["post_required"]:
        if (not isinstance(card, str) or not card or len(card) > 1900
                or card.rstrip("\r\n") != card or final.get("confirmation")):
            raise RuntimeError("decisions_confirmation_card_invalid")
        if final.get("confirmation_card_sha256") and hashlib.sha256(card.encode()).hexdigest() != final["confirmation_card_sha256"]:
            raise RuntimeError("decisions_confirmation_digest_invalid")
        delivery = post_once(card, job, state, state_path, channel=channel)
        mid = str(delivery["id"])
        confirmed = decisions_stage("confirm", {"action": "script", "name": "decisions_router",
            "args": {"mode": "confirm", "run-id": run_id, "sink": sink,
                     "discord-message-id": mid}}, state, state_path, call, timeout_s=3300)
        confirmation = decisions_summary(confirmed)
        if (confirmation.get("item_count") != count
                or confirmation.get("unroutable_count") != unroutable):
            raise RuntimeError("decisions_confirm_counts_invalid")
        verify_decisions_confirmation(confirmation, run_id=run_id, sink=sink,
                                      channel=channel, card=card, mid=mid)
        receipt = run_id + ":discord_verified"
        note = f"decisions router {final['mode']}: {count} item(s), {unroutable} unroutable"
        effects = {"machine_status": "complete", "run_id": run_id,
                   "confirmation_message_id": mid}
    else:
        checked = final.get("confirmation")
        if checked:
            mid = str(checked.get("discord_message_id") or "")
            if (not mid or not isinstance(card, str) or not card or len(card) > 1900
                    or final.get("confirmation_card_sha256") and hashlib.sha256(card.encode()).hexdigest() != final["confirmation_card_sha256"]):
                raise RuntimeError("decisions_prior_confirmation_invalid")
            verify_decisions_confirmation(final, run_id=run_id, sink=sink,
                                          channel=channel, card=card, mid=mid)
            receipt = run_id + ":discord_verified"
            note = f"decisions router {final['mode']}: {count} item(s), {unroutable} unroutable"
            effects = {"machine_status": "complete", "run_id": run_id,
                       "confirmation_message_id": mid}
        else:
            if (count != 0 or final["source_message_ids"] or card):
                raise RuntimeError("decisions_clean_binding_invalid")
            # A clean run_id names the unchanged cursor, not this observation.
            # Bind its heartbeat to the admitted dispatch so future clean
            # cycles do not collide with an older natural-key row.
            receipt = run_id + ":clean:" + str(job["job_id"])
            note = f"decisions router clean: {count} new item(s)"
            effects = {"machine_status": "complete", "run_id": run_id}
    pending = state.setdefault("pending", {})
    completed = pending.get("decisions_heartbeat")
    if completed is None:
        # All graph postconditions have completed. Persist the observed time
        # before insertion; a lost acknowledgement must retry identical bytes.
        completed = {"receipt": receipt, "finished_utc": utc_now()}
        pending["decisions_heartbeat"] = completed
        save(state_path, state)
    if completed["receipt"] != receipt:
        raise RuntimeError("decisions_heartbeat_binding_changed")
    effects["heartbeat"] = heartbeat("decisions_ack", count, receipt, note, completed)
    return effects


def finish_action(action, args, job, state, state_path, *, call=None):
    """Graph-owned postconditions for the migrated branches, not rc-only success."""
    if action == "research_autowire":
        from scripts.cron_ports.research_autowire import finish
        return finish(action, args, job, state, state_path)
    if action == "three_engine_telemetry":
        from scripts.cron_ports.three_engine_telemetry import finish
        return finish(job, state, state_path, call=call)
    if action == "glp_ads_weekly_review":
        from scripts.cron_ports.ads_weekly_review import finish
        return finish(action, args, job, state, state_path)
    if action in ("glp_ads_monitor", "glp_ads_monitor_daily_digest"):
        from scripts.cron_ports.ads_zero_spend_monitor import finish
        return finish(action, args, job, state, state_path)
    rc = job.get("rc")
    if action == "glp_seo_updater":
        r = machine_receipt(job, schema="n8n.seo_updater.v1")
        if "review-request" in args or r.get("review_only") is True or r.get("status") == "PASS_REVIEW_ONLY":
            if ("review-request" not in args or r.get("status") != "PASS_REVIEW_ONLY"
                    or r.get("review_only") is not True or r.get("published") is not False
                    or r.get("refreshed") is not False
                    or r.get("request_sha256") != args["review-request-sha256"]):
                raise RuntimeError("seo_updater_review_only_contract_invalid")
            return {"machine_status": r["status"], "review_only": True, "published": False,
                    "output_count": 0, "receipt_path": r.get("receipt_path"),
                    "operation_id": r.get("operation_id")}
        if r.get("status") not in ("PASS_REFRESHED",
                "PASS_NOTHING_DUE", "PASS_ALREADY_RUNNING", "PASS_DAILY_QUOTA_COMPLETE"):
            # The updater deliberately exits zero on domain refusals, including
            # failures after partial effects. Keep pending for GET-only readback.
            raise RuntimeError("seo_updater_nonpass_see_machine_receipt")
        published = r["status"] == "PASS_REFRESHED"
        if r.get("published") is not published or r.get("refreshed") is not published:
            raise RuntimeError("seo_updater_effect_contract_invalid")
        effects = {"machine_status": r["status"], "published": published, "output_count": int(published)}
        if published:
            import requests
            from urllib.parse import urlsplit
            url = str(r.get("url") or "")
            parsed = urlsplit(url)
            if (parsed.scheme != "https" or parsed.netloc not in ("glpclinicscout.com", "www.glpclinicscout.com")
                    or not parsed.path.startswith("/") or parsed.path.startswith("/go/")
                    or parsed.path != r.get("route") or parsed.query or parsed.fragment):
                raise RuntimeError("seo_updater_live_url_not_exact_first_party_route")
            # Do not follow an affiliate/offsite redirect while checking a page.
            response = requests.get(url, timeout=30, allow_redirects=False)
            if response.status_code != 200:
                raise RuntimeError("seo_updater_published_but_not_live")
            effects["live_status"] = response.status_code
        receipt = r.get("receipt_path") or r.get("claim_path") or job["receipt_path"]
        note = str(r.get("route") or "route unknown") if published else r["status"]
        effects["heartbeat"] = heartbeat("seo_update_carrier", int(published), receipt, note, job)
        effects["notification"] = post_once(
            f"SEO updater: {r['status']} ({'1 article live' if published else 'nothing published'})\n"
            f"{note}\n{r.get('url') or ''}\nReceipt: {receipt}", job, state, state_path)
        return effects
    if action == "decisions_router":
        return finish_decisions(job, state, state_path, call)
    if action == "sheet_sync_drain" and rc in (0, 75):
        return finish_sheet_drain(job, state, state_path)
    if (action in ("glp_weekly_seo_control", "error_fixer_daily_review")
            and isinstance(rc, int) and rc < 0
            and (rc == -15 or (job.get("termination") or {}).get("classification") == "KILLED")):
        note = ("weekly SEO control KILLED; next registered Sunday run owns recovery"
                if action == "glp_weekly_seo_control" else
                "daily review KILLED after durable checkpoint; next registered run owns recovery")
        return {"machine_status": "killed_retryable", "output_count": 0,
                "heartbeat": heartbeat(action, 0, job["receipt_path"], note, job)}
    if rc not in (None, 0):
        return {}
    if action == "sheet_vs_site_reconcile":
        from ops.n8n_dt import insert, rows, eq, WriteReconciliationError
        r = machine_receipt(job, schema="glp.sheet_vs_site_reconcile.v1")
        queue = r.get("queue_rows") or []
        for row in queue:
            intended = {key: row.get(key, "") for key in (*QUEUE_FIELDS, "status", "resolved_at_utc")}
            key = hashlib.sha256(json.dumps([job["job_id"], intended], sort_keys=True).encode()).hexdigest()
            confirmed = state.setdefault("queue_insertions", {})
            if key in confirmed:
                continue
            try:
                insert(SHEET_QUEUE, intended)
            except WriteReconciliationError:
                # Lost insert acknowledgment can race the existing consumer.
                # Prove the same immutable operation is present and advanced;
                # do not overwrite it back to queued or repeat the action.
                current = rows(SHEET_QUEUE, eq("update_id", intended["update_id"]), limit=3)
                if (len(current) != 1 or not same_sheet_operation(current[0], intended)
                        or not resolved_sheet_row(current[0])):
                    raise
            confirmed[key] = intended["update_id"]
            save(state_path, state)
        effects = {"rows_audited": int(r.get("rows_audited") or 0), "queue_rows": len(queue)}
        text = str(r.get("discord_text") or "")
        if text:
            effects["notification"] = post_once(text[:1900], job, state, state_path)
        counts = r.get("class_counts") or {}
        note = ("classes " + " ".join(f"{key}={counts[key]}" for key in sorted(counts) if counts[key] > 0)
                + f" / queued {len(queue)}")[:240]
        effects["heartbeat"] = heartbeat("sheet_reconciler", effects["rows_audited"],
                                         r.get("receipt") or job["receipt_path"], note, job)
        return effects
    if action == "topic_supply_mint":
        mint = machine_receipt(job, field="gaps")
        pool = float(mint["gaps"])
        if not math.isfinite(pool):
            raise RuntimeError("topic_supply_non_numeric_pool")
        note = (f"status={mint.get('status', '?')} gaps={pool} pillars={mint.get('pillars', '?')} "
                f"spokes={mint.get('spokes', '?')} skipped={mint.get('skipped', '?')}")
        effects = {"pool": pool, "heartbeat": heartbeat("topic_supply", pool, job["receipt_path"], note, job)}
        if pool < 6:
            effects["low_pool_notification"] = post_once(
                f"Topic supply LOW POOL: {pool} eligible gaps; target at least 6.\nReceipt: {job['receipt_path']}",
                job, state, state_path)
        # The score job belongs to this mint operation, including after a crash
        # between its completion and the parent notification/heartbeat readback.
        score_dir = state_path.parent / ("topic-score-" + job["job_id"])
        score_state_path = score_dir / "topic_data_score.state.json"
        score_state = json.loads(score_state_path.read_text()) if score_state_path.exists() else {}
        prior = score_state.get("last_receipt")
        if prior and not score_state.get("pending"):
            result = json.loads(Path(prior).read_text())
        else:
            result = run_action("topic_data_score", {}, state_dir=score_dir,
                                invocation="continuation:" + job["job_id"])
        score_job = result.get("bridge")
        if not score_job or score_job.get("status", "done") not in ("done", "error"):
            raise RuntimeError("topic_score_pending_reconcile_before_continuing")
        if score_job.get("stdout_truncated"):
            raise RuntimeError("topic_score_receipt_truncated")
        try:
            score = machine_receipt(score_job, field="multipliers_written")
        except RuntimeError:
            score = None
        scored = float(score["multipliers_written"]) if score else 0
        if not math.isfinite(scored):
            raise RuntimeError("topic_score_non_numeric_count")
        score_note = (f"scored={scored} top={score.get('top_pick', '?')} degraded={json.dumps(score.get('degraded', []))}"
                      if score else f"receipt unparseable rc={score_job.get('rc')}; selector retains sidecar/queue fallback")
        effects["score_heartbeat"] = heartbeat("topic_data_score", scored, score_job["receipt_path"], score_note, score_job)
        effects["scored"] = scored
        if scored < 1:
            effects["score_notification"] = post_once(
                f"Topic scoring DEGRADED: {score_note}\nReceipt: {score_job['receipt_path']}", job, state, state_path)
        return effects
    if action == "analytics_brain":
        r = machine_receipt(job)
        status = r.get("status")
        if not isinstance(status, str) or not status:
            raise RuntimeError("analytics_machine_status_missing")
        if status.startswith("FAIL_"):
            raise TerminalRefusal(status, "analytics_machine_failed")
        count = float(r.get("output_count") or 0)
        idle = status in ("PRELAUNCH_INACTIVE", "PASS_NO_ACTIVE_DEPLOY_WATCH")
        receipt = r.get("receipt") or job["receipt_path"]
        effects = {"machine_status": status, "phase": r.get("phase"), "output_count": count}
        if count > 0 or idle:
            effects["heartbeat"] = heartbeat("gsc_ga4_collector", count, receipt,
                f"analytics brain {status} phase={r.get('phase', 'unknown')} outputs={count}"
                + (" (designed idle: nothing to collect yet)" if idle else ""), job)
        return effects
    if action == "article_carrier_consume":
        r = machine_receipt(job, schema="n8n.article_carrier.v1")
        status = r.get("status")
        published = status == "PASS_PUBLISHED"
        replay = (status == "PASS_NOTHING_DUE" and bool(r.get("receipt_path"))
                  and "already completed; idempotent repeat consumed nothing" in str(r.get("reason", "")))
        dryrun = status == "PASS_DRYRUN" or replay
        if not published and not dryrun:
            # The adapter returns this before calling consume. Other blocked
            # statuses can follow a live effect and remain held for reconciliation.
            if status == "BLOCKED_BASE_COMPATIBILITY":
                raise TerminalRefusal(status)
            if article_failed_before_publish(r):
                raise TerminalRefusal(status, "article_carrier_failed_before_publish_no_effect")
            raise RuntimeError("article_carrier_missed_day_or_refused_see_machine_receipt")
        effects = {"machine_status": status, "output_count": int(published)}
        if published:
            from urllib.request import urlopen
            from urllib.parse import urlparse
            url = str(r.get("url") or "")
            parsed = urlparse(url)
            if parsed.scheme != "https" or parsed.hostname not in ("glpclinicscout.com", "www.glpclinicscout.com") or parsed.path.startswith("/go/"):
                raise RuntimeError("article_live_url_not_first_party_article")
            with urlopen(url, timeout=30) as response:
                if response.status != 200:
                    raise RuntimeError("article_published_but_not_live")
                effects["live_status"] = response.status
        note = str(r.get("route") or "route unknown") if published else (
            "idempotent completed-operation replay" if replay else f"dryrun ready={r.get('ready')} next={r.get('next_route') or 'none'}")
        receipt = r.get("receipt_path") or job["receipt_path"]
        effects["heartbeat"] = heartbeat("article_carrier", int(published), receipt, note, job)
        effects["notification"] = post_once(
            f"Article carrier: {status}\n{note}\n{r.get('url') or ''}\nReceipt: {receipt}", job, state, state_path)
        return effects
    if action == "glp_weekly_seo_control":
        r = machine_receipt(job, machine="glp_weekly_seo_control")
        if not isinstance(r.get("status"), str) or not r["status"]:
            raise RuntimeError("weekly_seo_status_missing")
        if r.get("status") not in ("PASS", "PASS_DEGRADED", "NOOP_ALREADY_RUNNING"):
            raise TerminalRefusal(r.get("status"), "weekly_seo_nonpass")
        note = (f"verdict={r.get('verdict')} published={r.get('published', 0)} indexed={r.get('indexed', 0)} "
                f"clicks={r.get('clicks', 0)} strategy={r.get('targeting_decision', '?')}")[:240]
        return {"heartbeat": heartbeat("glp_weekly_seo_control", float(r.get("clicks") or 0),
            str(r.get("receipt") or job["receipt_path"]), note, job), "machine_status": r["status"]}
    if action == "error_fixer_daily_review":
        r = machine_receipt(job, machine="error_fixer_daily_review")
        note = f"pt_date={r.get('pt_date')} status={r.get('status')} dispatches={r.get('dispatches', 0)}"
        return {"status": r.get("status", "unknown"), "heartbeat": heartbeat(action, r.get("dispatches", 0), r.get("receipt") or job["receipt_path"], note, job)}
    if action == "onboarding_manifest_run":
        r = machine_receipt(job, machine="onboarding_manifest")
        # Only the schedule's scan migrates here. The existing producer refuses
        # scan chaining without a scoped live webhook execution (manifest.py:
        # 4790-4850). Webhook onboarding/authoring-resume stays on n8n; no cron
        # execution ID or callback may impersonate that authority.
        if r.get("mode") != "scan" or (r.get("authoring") or {}).get("dispatched"):
            raise RuntimeError("onboarding_schedule_unexpected_authoring")
        if r.get("status") != "PASS" or not r.get("receipt") or not isinstance(r.get("scan"), dict):
            raise RuntimeError("onboarding_scan_summary_invalid")
        scan = r["scan"]
        candidates = len(r.get("onboarding_candidates") or []) + len(r.get("removal_candidates") or [])
        note = (f"scan: {candidates} candidate(s), {scan.get('transitions') or 0} changed row(s) "
                f"of {scan.get('sheet_rows') or 0}" + (" (baseline snapshot)" if scan.get("baseline") else ""))
        effects = {"machine_status": r["status"], "candidates": candidates, "mode": "scan"}
        if candidates:
            effects["notification"] = post_once(str(r.get("discord_text") or note), job, state, state_path)
        effects["heartbeat"] = heartbeat("onboarding_manifest", r.get("output_count") or 0,
                                         r["receipt"], note[:900], job)
        return effects
    if action == "pricing_refresh":
        emitted = machine_receipt(job)
        if emitted.get("schema") == "glp.price_freshness.v1":
            count = emitted.get("active_provider_count")
            completed = emitted.get("completed_provider_count")
            pages = emitted.get("successful_pages")
            if (not isinstance(count, int) or isinstance(count, bool) or count < 1
                    or completed != count or emitted.get('primary_pages_successful') != count
                    or not isinstance(pages, int) or pages < count
                    or emitted.get("failed_primary_pages") != 0 or emitted.get('failed_pages') != 0):
                raise TerminalRefusal("FAIL_INCOMPLETE_COHORT", "price_monitor_cohort_incomplete")
            coverage = emitted.get('price_coverage')
            if (emitted.get('source_monitor_status') != 'SOURCE_MONITOR_COMPLETE'
                    or not isinstance(coverage, dict)
                    or coverage.get('status') not in ('PRICE_VALIDATION_COMPLETE', 'PRICE_VALIDATION_INCOMPLETE')):
                raise TerminalRefusal('FAIL_UNATTESTED_PRICE_COVERAGE', 'price_monitor_coverage_unattested')
            note = (f"Source monitor complete: {count} providers, {pages} pages; "
                    f"price validation {coverage['status']}: "
                    f"{coverage.get('fully_validated_rows', 0)}/{coverage.get('typed_rows', 0)} unique typed rows fully validated, "
                    f"{coverage.get('cached_validated_rows', 0)} cached-supported, "
                    f"{coverage.get('unknown_rows', 0)} unknown, {coverage.get('legacy_rows_skipped', 0)} legacy rows skipped; "
                    f"{emitted.get('confirmed_changes', 0)} confirmed price fields; "
                    f"{emitted.get('review_required', 0)} price-change review rows; "
                    f"{emitted.get('evidence_unknown_rows', 0)} evidence gaps; "
                    f"{emitted.get('unsupported_extraction_rows', 0)} unsupported extraction rows. "
                    "Prices are not removed on source failures; publication is a separate F06 effect.")
            effects = {"providers_monitored": count, "pages_monitored": pages}
            if emitted.get('confirmed_changes', 0):
                effects['notification'] = post_once(note + " Registry intake: " + str(emitted.get('receipt')), job, state, state_path)
            effects['heartbeat'] = heartbeat('pricing_refresh', count, emitted['receipt'], note, job)
            return effects
        r = machine_receipt(job, schema="glp.pricing_refresh.v1")
        verdicts = r.get("row_verdicts") or {}
        checked = sum(float(verdicts.get(key) or 0) for key in ("unchanged", "evidence_lost", "capture_failed"))
        if not math.isfinite(checked) or checked < 0:
            raise RuntimeError("pricing_refresh_invalid_row_count")
        if checked == 0:
            raise TerminalRefusal("FAIL_ZERO_ROWS", "pricing_refresh_reverified_zero_rows")
        effects = {"rows_reverified": verdicts.get("unchanged", 0)}
        if float(r.get("diffs") or 0) > 0:
            text = str(r.get("discord_text") or "")
            if len(text) > 1950:
                text = text[:1940] + "…"
            effects["notification"] = post_once(text, job, state, state_path)
        effects["heartbeat"] = heartbeat(action, verdicts.get("unchanged", 0), r.get("receipt") or "", r.get("note") or "", job)
        return effects
    return {}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def make_call(action, actions):
    # Reuse the installed credential loaders and HTTP client; never return tokens.
    from scripts.glp_onboarding_bridge_scoped_cutover import bridge_call
    spec = importlib.util.spec_from_file_location("cron_existing_bridge", ROOT / "ops/n8n-bridge/bridge.py")
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)
    scope = actions[action].get("required_auth_scope")
    token = bridge.load_scoped_tokens()[scope] if scope else bridge.load_token()
    return lambda path, payload=None: bridge_call(path, token, payload)


def notify_failure(action, receipt, state, state_path):
    if (os.environ.get("CRON_BRIDGE_MANUAL_REGISTERED") == "1"
            and os.environ.get("CRON_BRIDGE_NO_SEND") == "1"):
        return {"status": "SUPPRESSED_NO_SEND", "reason": "operator_no_send_registered_acceptance"}
    from scripts.post_discord_message import send, readback
    content = f"Cron action failed: {action}\nReceipt: {receipt}\nInspect the receipt before replaying any effect."
    candidates = [*state.get("unverified_alerts", []), state.get("alert", {})]
    alert = next(item for item in candidates if item.get("receipt_path") == receipt)
    if alert.get("attempted") is False:
        alert["attempted"] = True
        save(state_path, state)
        sent = send(CHANNEL, content)
        alert["id"] = str(sent.get("id") or "")
        save(state_path, state)
    message_id = alert.get("id")
    if not message_id:
        raise RuntimeError("alert_ack_unknown")
    fetched = readback(CHANNEL, message_id)
    exact = fetched.get("channel_id") == CHANNEL and fetched.get("content") == content
    if not exact:
        raise RuntimeError("alert_readback_failed")
    return {**alert, "id": message_id, "channel_id": CHANNEL,
            "status": "verified", "exact_readback": True}

def article_current_base():
    """Read the existing accepted-base validators; never create release authority."""
    from scripts.glp_current_content_base_schema import is_prepared_pointer, validate_pointer_shape
    from scripts.glp_price_index_weekly import _read_bound_regular_file, _validate_base_evidence
    path = ROOT / "reports/glp-clinic-scout/seo-machine/current-content-base.json"
    _path, raw, identity = _read_bound_regular_file(str(path), "state")
    pointer = json.loads(raw)
    validate_pointer_shape(pointer, releases_root=None if is_prepared_pointer(pointer)
                           else ROOT / "site-system/flagship-v2/releases", require_release_dirs=True)
    _validate_base_evidence(pointer, Path(pointer["release_root"]))
    _path, after, after_identity = _read_bound_regular_file(str(path), "state")
    if after != raw or after_identity != identity:
        raise RuntimeError("carrier_current_pointer_changed")
    return {"deployment_id": pointer["deployment_id"], "pointer_sha256": hashlib.sha256(raw).hexdigest()}


def article_release_live():
    """Bind accepted current publication to the live head, not a canceled tracker."""
    current = ROOT / "reports/glp-clinic-scout/seo-machine/current-content-base.json"
    if current.exists() or current.is_symlink():
        try:
            before = article_current_base()
            return (article_production_head_id() == before["deployment_id"]
                    and article_current_base() == before)
        except (RuntimeError, OSError, ValueError, KeyError, TypeError):
            return False
    # Preserve the historical bootstrap path when no moving pointer exists.
    from scripts.linear_client import gql, OPERATIONS
    issue = gql(OPERATIONS["issue_get"], {"id": "GLP-40"}).get("issue") or {}
    if issue.get("state", {}).get("type") != "completed":
        return False
    receipt_path = ROOT / "reports/ops/orchestration-restore-20260907/PLAN/K10/EXECUTION/K10-009-C/RESULT.json"
    if not receipt_path.is_file():
        return False
    receipt = json.loads(receipt_path.read_text())
    if (receipt.get("issue") != "GLP-43" or receipt.get("status") != "COMPLETE"
            or not receipt.get("acceptance_clauses")
            or any(item.get("verdict") != "PASS" for item in receipt["acceptance_clauses"])):
        return False
    deployment_id = receipt.get("production_deployment_id")
    if not isinstance(deployment_id, str) or not deployment_id:
        return False
    return article_production_head_id() == deployment_id


def article_production_head_id():
    from scripts.glp_deploy_build6_authority_v1 import (
        CloudflareClient, read_token, production_deployment, successful_production,
    )
    head = production_deployment(CloudflareClient(read_token()).project())
    return head.get("id") if successful_production(head) else None


LIVENESS_SAFE_SOURCE_SHA256 = "a1c7c0f89374fe31fb84085021d48399bdb70f20a288e0638143509ef7008eb9"


def liveness_repair_slot(prior, due_slot, payload_sha256):
    """One distinct registered repair for an attested pre-fix tracking probe."""
    if (prior.get("status") != "PASS" or prior.get("rc") != 0
            or prior.get("run_type") != "manual_registered"
            or prior.get("due_slot") != due_slot
            or prior.get("payload_sha256") != payload_sha256):
        raise ValueError("liveness_original_receipt_mismatch")
    job = prior.get("bridge") or {}
    if job.get("status") != "done" or job.get("rc") != 0 or job.get("timed_out") or job.get("interrupted"):
        raise ValueError("liveness_original_bridge_unverified")
    rows = (((prior.get("effects") or {}).get("heartbeat") or {}).get("data") or {}).get("rows") or []
    if len(rows) != 1 or rows[0].get("machine") != "program_liveness":
        raise ValueError("liveness_original_heartbeat_unverified")
    note = str(rows[0].get("note") or "")
    count = re.search(r"(?:^| )probe_clicks=(\d+)(?: |$)", note)
    if not count or int(count.group(1)) <= 0:
        return None
    artifact = Path(str(rows[0].get("receipt") or ""))
    if not artifact.is_file() or json.loads(artifact.read_text()).get("tracking_probe_clicks") != int(count.group(1)):
        raise ValueError("liveness_original_probe_artifact_mismatch")
    source = ROOT / "scripts/glp_program_liveness_check.py"
    if hashlib.sha256(source.read_bytes()).hexdigest() != LIVENESS_SAFE_SOURCE_SHA256:
        raise ValueError("liveness_repaired_source_not_pinned")
    return due_slot + "|postrepair"


def article_materialization_failed_no_effect(receipt, child):
    """A failed remint with no plan/candidate cannot have called publication."""
    argv = child.get("argv") or []
    try:
        flags = dict(zip(argv[2::2], argv[3::2]))
        if (len(argv) != 22 or Path(argv[1]).name != "materialize_opus_run.py"
                or set(flags) != {"--packet", "--topic-id", "--route", "--run-id", "--release-root",
                                  "--publication-date", "--selection-receipt", "--topic-authority-snapshot",
                                  "--materialization-reason", "--pt-date"}
                or flags["--materialization-reason"] != "remint"):
            return False
        workspace = ROOT / "reports/glp-clinic-scout/seo-machine/article-buffer-work" / hashlib.sha256(receipt["operation_id"].encode()).hexdigest()
        if Path(flags["--packet"]) != workspace / "packet":
            return False
        prior_path = Path(receipt["accepted_failures"]["materialization"])
        prior = json.loads(prior_path.read_text())
        unsigned = dict(prior)
        sealed = unsigned.pop("materialization_sha256")
        canonical = (json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()
        run_id = f"buffer-{workspace.name[:16]}-r{prior['generation'] + 1}"
        candidate = ROOT / "site-system/flagship-v2/releases" / f"glp-buffer-{flags['--pt-date']}-{workspace.name[:16]}-r{prior['generation'] + 1}"
        if (prior_path.parent != workspace or hashlib.sha256(canonical).hexdigest() != sealed
                or prior.get("operation_id") != receipt["operation_id"]
                or flags["--run-id"] != run_id or Path(flags["--release-root"]) != candidate
                or any(p.exists() or p.is_symlink() for p in (
                    candidate, workspace / f"apply-template-{run_id}.json",
                    workspace / f"publication-{run_id}", workspace / f"materialization-r{prior['generation'] + 1}.json"))):
            return False
        base = prior["current_base_state"]
        base_path = ROOT / "reports/glp-clinic-scout/seo-machine/current-content-base.json"
        if base["path"] != str(base_path) or hashlib.sha256(base_path.read_bytes()).hexdigest() != base["sha256"]:
            return False
        # The superseded build is also demonstrably pre-publication; no older
        # possibly effectful attempt can be hidden by this failed remint.
        template_path = Path(prior["materialized"]["apply_template"]["path"])
        template_bytes = template_path.read_bytes()
        if hashlib.sha256(template_bytes).hexdigest() != prior["materialized"]["apply_template"]["sha256"]:
            return False
        report = Path(json.loads(template_bytes)["report_dir"])
        if {p.name for p in report.iterdir()} != {"release-job.json"}:
            return False
        current = article_current_base()
        heads = receipt["head_reconciliation"]
        deployment = current["deployment_id"]
        return (current["pointer_sha256"] == base["sha256"] and heads.get("aligned") is True
                and heads["pointer_head"]["deployment_id"] == deployment
                and heads["live_head"]["deployment_id"] == deployment
                and article_production_head_id() == deployment and article_current_base() == current)
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return False


def article_source_ci_failed_no_publish(receipt, failure):
    """Recognize only GitHub's empty-check race before source merge/release.

    A source PR/claim exists, so this is not zero local writes. Publication is
    excluded by the exact traceback, sealed claim/base, still-OPEN exact source
    branch, no consumed/output record and fresh unchanged production identity.
    Preserve the claim: the next attempt resumes the same accepted operation.
    """
    import subprocess
    try:
        op = receipt['operation_id']
        key = hashlib.sha256(op.encode()).hexdigest()
        root = ROOT / 'reports/glp-clinic-scout/seo-machine/article-buffer'
        claim = json.loads((root / 'claims' / (key + '.json')).read_text())
        unsigned = dict(claim)
        seal = unsigned.pop('claim_sha256')
        canonical = (json.dumps(unsigned,sort_keys=True,separators=(',',':'),ensure_ascii=False)+'\n').encode()
        entry_id = claim['entry_id']
        if (hashlib.sha256(canonical).hexdigest() != seal
                or claim.get('schema') != 'glp-seo-article-buffer-claim-v1'
                or claim.get('status') != 'CLAIMED_OLDEST_VALID_READY_ENTRY'
                or claim.get('operation_id') != op or not re.fullmatch('[a-f0-9]{24}',entry_id)):
            return False
        manifest = root / 'entries' / entry_id / 'entry.json'
        if (claim['entry_manifest']['path'] != str(manifest)
                or hashlib.sha256(manifest.read_bytes()).hexdigest() != claim['entry_manifest']['sha256']
                or os.path.lexists(root / 'consumed' / (key+'.json'))):
            return False
        payload = failure['cli_payload']
        msg = payload['reason']
        match = re.fullmatch(r"source_child_failed:\['gh', 'pr', 'checks', '(https://github.com/izaiahat/glp-site-candidate/pull/[1-9][0-9]*)', '--watch', '--interval', '10'\]:rc=1\n\nno checks reported on the 'carrier/accepted-" + entry_id + r"' branch\n", msg)
        trace = failure['stderr']
        cli = ROOT / 'systems/seo-blog-machine/scripts/article_buffer_cli.py'
        source = ROOT / 'scripts/article_source_carrier.py'
        frames = [(str(cli),'main'),(str(cli),'consume_cli'),(str(source),'consume'),
                  (str(source),'ensure_source_main'),(str(source),'run')]
        if (not match or failure.get('schema') != 'glp.article_carrier.failure.v2'
                or type(failure.get('exit_code')) is not int or failure['exit_code'] != 1
                or failure.get('operation_id') != op or payload.get('operation_id') != op
                or payload.get('command') != 'consume' or payload.get('status') != 'BLOCKED_CONSUME_FAILED'
                or payload.get('error_class') != 'RuntimeError'
                or failure.get('argv',[])[1:] != [str(cli),'consume','--operation-id',op]
                or hashlib.sha256(trace.encode()).hexdigest() != failure.get('stderr_sha256')
                or re.findall(r'  File "([^"]+)", line \d+, in ([^\n]+)\n',trace) != frames
                or not trace.endswith('RuntimeError: '+msg+'\n')
                or payload.get('exception') != {'class':'RuntimeError','message':msg,'traceback':trace}):
            return False
        base = ROOT / 'reports/glp-clinic-scout/seo-machine/current-content-base.json'
        base_sha = hashlib.sha256(base.read_bytes()).hexdigest()
        if claim['current_base_state'] != {'path':str(base),'sha256':base_sha}:
            return False
        pr = json.loads(subprocess.check_output(['gh','pr','view',match[1],
            '--json','state,headRefName'],text=True,timeout=60))
        if pr != {'state':'OPEN','headRefName':'carrier/accepted-'+entry_id}:
            return False
        current = article_current_base()
        heads = receipt['head_reconciliation']
        deployment = current['deployment_id']
        return (current.get('pointer_sha256') == base_sha and heads.get('aligned') is True
                and heads['pointer_head']['deployment_id'] == deployment
                and heads['live_head']['deployment_id'] == deployment
                and article_production_head_id() == deployment and article_current_base() == current)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return False


def article_failed_before_publish(receipt):
    """Prove a failed pre-report build never reached the publisher.

    published=false and the pre-consume head snapshot alone are insufficient.
    Require the bound build-child failure, no publication artifacts, unchanged
    pointer bytes, and a fresh production-head read. Missing evidence stays held.
    """
    if receipt.get("status") != "BLOCKED_CONSUME_FAILED" or receipt.get("published") is not False:
        return False
    try:
        failure = json.loads(Path(receipt["receipt_path"]).read_text())
        payload = failure["cli_payload"]
        if payload.get('error_class') == 'RuntimeError' and str(payload.get('reason','')).startswith("source_child_failed:['gh', 'pr', 'checks',"):
            return article_source_ci_failed_no_publish(receipt, failure)
        if payload.get('error_class') == 'ModuleNotFoundError':
            op = receipt.get('operation_id')
            trace = failure.get('stderr', '')
            cli = ROOT / 'systems/seo-blog-machine/scripts/article_buffer_cli.py'
            suffix = "    import article_source_carrier\nModuleNotFoundError: No module named 'article_source_carrier'\n"
            if (not op or failure.get('schema') != 'glp.article_carrier.failure.v2'
                    or type(failure.get('exit_code')) is not int or failure['exit_code'] != 1
                    or failure.get('operation_id') != op or payload.get('operation_id') != op
                    or payload.get('status') != 'BLOCKED_CONSUME_FAILED' or payload.get('command') != 'consume'
                    or payload.get('reason') != "No module named 'article_source_carrier'"
                    or failure.get('argv', [])[1:] != [str(cli), 'consume', '--operation-id', op]
                    or hashlib.sha256(trace.encode()).hexdigest() != failure.get('stderr_sha256')
                    or not trace.endswith(suffix)
                    or re.findall(r'  File "([^"]+)", line \d+, in ([^\n]+)\n', trace)
                        != [(str(cli), 'main'), (str(cli), 'consume_cli')]
                    or payload.get('exception') != {'class':'ModuleNotFoundError',
                        'message':"No module named 'article_source_carrier'", 'traceback':trace}):
                return False
            # This exact leaf import preceded any claim/carrier call. Preserve
            # all other missing-module/partial-effect failures as unknown.
            buffer_root = ROOT / 'reports/glp-clinic-scout/seo-machine/article-buffer'
            name = hashlib.sha256(op.encode()).hexdigest() + '.json'
            if any(os.path.lexists(buffer_root / kind / name) for kind in ('claims', 'consumed')):
                return False
            current = article_current_base()
            heads = receipt['head_reconciliation']
            deployment = current['deployment_id']
            return (heads.get('aligned') is True
                    and heads['pointer_head']['deployment_id'] == deployment
                    and heads['live_head']['deployment_id'] == deployment
                    and article_production_head_id() == deployment and article_current_base() == current)
        child = payload["child"]
        argv = child["argv"]
        if (failure.get("schema") != "glp.article_carrier.failure.v2"
                or not receipt.get("operation_id")
                or failure.get("operation_id") != receipt["operation_id"]
                or payload.get("operation_id") != receipt["operation_id"]
                or payload.get("status") != "BLOCKED_CONSUME_FAILED"
                or type(child.get("exit_code")) is not int or child["exit_code"] == 0):
            return False
        if len(argv) > 1 and Path(argv[1]).name == "materialize_opus_run.py":
            return article_materialization_failed_no_effect(receipt, child)
        if (len(argv) != 4 or Path(argv[1]).name != "apply_opus_article_release.py"
                or argv[2] != "--template"):
            return False
        template = json.loads(Path(argv[3]).read_text())
        report = Path(template["report_dir"])
        # This deliberately narrow proof covers failure before any build reports.
        # Never discard a later/possibly effectful attempt based on absence of PASS.
        if (template.get("schema") != "glp-opus-article-apply-template-v1"
                or {p.name for p in report.iterdir()} != {"release-job.json"}):
            return False
        job = json.loads((report / "release-job.json").read_text())
        for key in ("manifest_id", "base_state_path", "base_state_sha256"):
            if job.get(key) != template[key]:
                return False
        diagnostic = report.parent / "child-failures" / f"{template['manifest_id']}-build.json"
        build = json.loads(diagnostic.read_text())
        if (build.get("schema") != "glp-opus-release-child-failure-v1"
                or build.get("status") != "BLOCKED_CHILD_FAILED"
                or build.get("child") != "build_opus_article_release.py"
                or type(build.get("exit_code")) is not int or build["exit_code"] == 0
                or f"child_failed:build_opus_article_release.py:diagnostic={diagnostic}:" not in child.get("stderr", "")):
            return False
        pointer_bytes = Path(template["base_state_path"]).read_bytes()
        pointer = json.loads(pointer_bytes)
        deployment = pointer["deployment_id"]
        heads = receipt["head_reconciliation"]
        original = heads["pointer_head"]["deployment_id"]
        if not original or heads.get("aligned") is not True or heads["live_head"]["deployment_id"] != original:
            return False
        if hashlib.sha256(pointer_bytes).hexdigest() == template["base_state_sha256"]:
            if original != deployment:
                return False
        else:
            # An independently completed main release may supersede this base
            # while a pending failure awaits reconciliation. Prove its exact
            # lineage; never treat arbitrary pointer drift as a retry grant.
            base = job["base_final_receipt"]
            base_bytes = Path(base["path"]).read_bytes()
            base_final = json.loads(base_bytes)
            main_report = Path(pointer["release_root"]).parents[1]
            main = json.loads((main_report / "MAIN-RELEASE-RESULT.json").read_text())
            if (hashlib.sha256(base_bytes).hexdigest() != base["sha256"]
                    or base_final.get("schema") != "glp-opus-production-terminal-receipt-v1"
                    or base_final.get("status") != "PASS_PRODUCTION_TERMINAL"
                    or base_final["deployment"]["deployment_id"] != original
                    or pointer["supersedes"]["deployment_id"] != original
                    or main.get("status") != "PASS_PRODUCTION_TERMINAL"
                    or main.get("scope") != "operator-approved-main-release"
                    or main.get("report") != str(main_report)
                    or main.get("predecessor_deployment_id") != original
                    or main.get("deployment_id") != deployment):
                return False
        return bool(deployment) and article_production_head_id() == deployment
    except (OSError, ValueError, KeyError, TypeError):
        return False


def article_no_effect_hold(prior, observed, *, action, args, due_slot, digest, receipt_path):
    """Recognize only the carrier's durable pre-dispatch hold receipt shape."""
    required = {"action", "due_slot", "finished_utc", "invocation", "payload_sha256",
                "rc", "reason", "receipt_path", "started_utc", "status"}
    if (action != "article_carrier_consume" or args != {"mode": "consume"}
            or not observed or type(observed.get("rc")) is not int or observed["rc"] != 0
            or not required <= prior.keys()
            or not prior.keys() <= required | {"run_type", "retried_no_effect_receipt"}
            or prior.get("action") != action or prior.get("due_slot") != due_slot
            or prior.get("payload_sha256") != digest or prior.get("receipt_path") != receipt_path
            or type(prior.get("rc")) is not int or prior["rc"] != 0
            or prior.get("status") != "DECLINED_RELEASE_HOLD"
            or prior.get("reason") != "GLP-40 live release not yet complete; no publish dispatched"
            or prior.get("invocation") not in ("scheduled", "manual_registered")
            or ("run_type" in prior and prior["run_type"] != prior["invocation"])
            or ("retried_no_effect_receipt" in prior and
                (not isinstance(prior["retried_no_effect_receipt"], str)
                 or not prior["retried_no_effect_receipt"]))):
        return False
    try:
        start = datetime.fromisoformat(prior["started_utc"])
        finish = datetime.fromisoformat(prior["finished_utc"])
        return start.tzinfo is not None and finish.tzinfo is not None and finish >= start
    except (TypeError, ValueError):
        return False


def run_action(action, args, *, state_dir=STATE_DIR, actions=None, call=None,
               notify=notify_failure, invocation="manual", poll_interval=3,
               finish=finish_action, due_slot=None, _following_slot=False):
    actions = json.loads(ACTIONS_PATH.read_text()) if actions is None else actions
    if action not in actions or not re.fullmatch(r"[A-Za-z0-9_-]+", action):
        return {"status": "REFUSED_UNKNOWN_ACTION", "rc": 64}
    if action == "glp_seo_updater":
        # This successor is an explicitly invoked transport, not R2 enablement.
        if invocation != "manual" or due_slot is not None:
            return {"status": "REFUSED_SEO_UPDATER_UNSCHEDULED_ONLY", "rc": 64}
        # Port Mode's shape checks. Calendar/future, request hash, authority,
        # operation claims and all publication gates stay in the same updater.
        keys = set(args)
        refresh = (not args or keys == {"pt-date"} and isinstance(args["pt-date"], str)
                   and re.fullmatch(r"\d{4}-\d{2}-\d{2}", args["pt-date"]))
        review = (keys == {"review-request", "review-request-sha256"}
                  and isinstance(args["review-request"], str) and args["review-request"].startswith("/")
                  and isinstance(args["review-request-sha256"], str)
                  and re.fullmatch(r"[0-9a-f]{64}", args["review-request-sha256"]))
        if not (refresh or review):
            return {"status": "REFUSED_SEO_UPDATER_ARGS", "rc": 64}
    if action == "sheet_sync_drain" and args:
        return {"status": "REFUSED_SHEET_DRAIN_REQUIRES_REGISTERED_QUEUE", "rc": 64}
    if action == "decisions_router" and args:
        return {"status": "REFUSED_DECISIONS_CRON_LIVE_ONLY", "rc": 64}
    if action == "onboarding_manifest_run" and args not in ({}, {"mode": "scan"}):
        return {"status": "REFUSED_ONBOARDING_CRON_SCAN_ONLY", "rc": 64}
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    # Analytics' cadence now holds the existing installer reader lock inside
    # the bridge child. Holding it across this HTTP wait would self-deadlock;
    # retain the cron state/pending/slot serialization under a separate name.
    lock_name = f"{action}.state.lock" if action == "analytics_brain" else f"{action}.lock"
    with (state_dir / lock_name).open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "SKIPPED_OVERLAP", "rc": 0, "action": action}
        payload = {"action": "script", "name": action, "args": args}
        if action == "decisions_router":
            payload["args"] = {"mode": "live", "sink": "decisions"}
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        common_path = state_dir / f"{action}.state.json"
        common = json.loads(common_path.read_text()) if common_path.exists() else {}
        state_path = state_dir / f"{action}-{digest}.state.json" if args else common_path
        state = (json.loads(state_path.read_text()) if state_path.exists() else {}) if args else common
        slots = state.get("slots", {})
        if action == "program_liveness_check" and invocation == "manual_registered" and not state.get("pending"):
            observed = slots.get(due_slot)
            if observed and observed.get("rc") == 0:
                try:
                    prior = json.loads(Path(observed["receipt_path"]).read_text())
                    repair = liveness_repair_slot(prior, due_slot, digest)
                except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                    return {"status": "BLOCKED_LIVENESS_POSTREPAIR_PROOF", "rc": 125,
                            "action": action, "due_slot": due_slot,
                            "receipt_path": observed.get("receipt_path")}
                if repair:
                    due_slot, invocation = repair, "manual_registered_repair"
        terminal_retry = False
        no_effect_retry = None
        if invocation == "manual_registered" and not state.get("pending"):
            if not due_slot:
                return {"status": "REFUSED_MANUAL_SLOT_REQUIRED", "rc": 64, "action": action}
            observed = slots.get(due_slot)
            prior_path = (observed or {}).get("receipt_path") or state.get("last_receipt")
            if prior_path:
                try:
                    prior = json.loads(Path(prior_path).read_text())
                    # Receipts written by the initial manual implementation had
                    # no slot. Adopt a proven receipt inside this logical period,
                    # rather than executing its already-completed effects again.
                    start = datetime.fromisoformat(prior["started_utc"])
                    boundary = datetime.fromisoformat(due_slot[:16]).replace(tzinfo=ZoneInfo(due_slot[17:]))
                    if not observed and start >= boundary and prior.get("payload_sha256") == digest:
                        observed = {"rc": prior["rc"], "receipt_path": prior_path}
                        slots[due_slot] = observed
                        state["slots"] = slots
                        save(state_path, state)
                except (OSError, ValueError, KeyError, TypeError):
                    return {"status": "BLOCKED_MANUAL_SLOT_READBACK", "rc": 125,
                            "action": action, "due_slot": due_slot, "receipt_path": prior_path}
                if not common.get("pending") and article_no_effect_hold(
                        prior, observed, action=action, args=args, due_slot=due_slot,
                        digest=digest, receipt_path=prior_path):
                    no_effect_retry = prior_path
                if observed and observed["rc"] != 0:
                    job = prior.get("bridge", {})
                    terminal_job = (job.get("status") in ("done", "error") and job.get("rc") is not None
                                    and not job.get("timed_out") and not job.get("interrupted")
                                    and (job.get("termination") or {}).get("classification") != "BRIDGE_INTERRUPTED")
                    rejected = prior.get("reason") in {
                        f"bridge_rejected_http_{code}" for code in (400, 401, 403, 404, 413, 429)}
                    terminal_retry = (prior.get("payload_sha256") == digest and prior.get("status") == "FAIL"
                                      and prior.get("rc") == observed["rc"] and (terminal_job or rejected))
                    if not terminal_retry:
                        return {"status": "BLOCKED_MANUAL_SLOT_RECONCILIATION", "rc": 125,
                                "action": action, "due_slot": due_slot, "receipt_path": prior_path}
        if (due_slot and due_slot in slots and not state.get("pending")
                and not terminal_retry and not no_effect_retry):
            return {"status": "SKIPPED_OBSERVED_SLOT", "rc": slots[due_slot]["rc"],
                    "action": action, "due_slot": due_slot,
                    "receipt_path": slots[due_slot]["receipt_path"]}
        receipt_path = state_dir / f"{action}-{uuid.uuid4().hex}.json"
        result = {"action": action, "invocation": invocation, "started_utc": utc_now(),
                  "payload_sha256": digest, "receipt_path": str(receipt_path), "due_slot": due_slot}
        if invocation in ("manual_registered", "manual_registered_repair"):
            result["run_type"] = invocation
        if no_effect_retry:
            result["retried_no_effect_receipt"] = no_effect_retry
        pending = state.get("pending")
        try:
            if args and common.get("pending"):
                raise RuntimeError("legacy_pending_action_reconcile_before_new_branch")
            if action == "article_carrier_consume" and not pending and not article_release_live():
                result.update(status="DECLINED_RELEASE_HOLD", rc=0,
                              reason="GLP-40 live release not yet complete; no publish dispatched",
                              finished_utc=utc_now())
                save(receipt_path, result)
                if due_slot:
                    slots[due_slot] = {"rc": 0, "receipt_path": str(receipt_path)}
                    state["slots"] = slots
                state["last_receipt"] = str(receipt_path)
                save(state_path, state)
                return result
            if pending:
                if not pending.get("job_id"):
                    raise RuntimeError("submission_ack_unknown_reconcile_before_replay")
                if pending["payload_sha256"] != digest:
                    raise RuntimeError("different_pending_invocation_reconcile_first")
                result["reconciled_prior_receipt"] = pending["receipt_path"]
                result["due_slot"] = pending.get("due_slot") or (due_slot if invocation == "manual_registered" else None)
                result["requested_due_slot"] = due_slot
                if pending.get("local_noop"):
                    code, job = 200, pending["local_noop"]
                else:
                    call = make_call(action, actions) if call is None else call
                    code, job = call("/job/" + pending["job_id"])
                if code != 200 or job.get("job_id") != pending["job_id"]:
                    raise RuntimeError("job_readback_invalid_pending_preserved")
            else:
                batch = queued_sheet_batch() if action == "sheet_sync_drain" and not args else None
                state["pending"] = {"payload_sha256": digest, "receipt_path": str(receipt_path), "due_slot": due_slot}
                if batch is not None:
                    state["pending"]["sheet_batch"] = batch
                    payload["args"] = {"queue-json": json.dumps(batch), "apply": "true"}
                if batch == []:
                    # A locally observed empty queue is not a bridge execution.
                    # Retain its identity/time so an interrupted heartbeat resumes
                    # without fetching a different batch or minting another receipt.
                    local = {"job_id": "local-empty-" + receipt_path.stem, "local_noop": True,
                             "status": "done", "rc": 0, "finished_utc": utc_now(),
                             "receipt_path": str(receipt_path)}
                    state["pending"].update(job_id=local["job_id"], local_noop=local)
                save(state_path, state)
                if batch == []:
                    code, job = 200, local
                else:
                    call = make_call(action, actions) if call is None else call
                    dispatch_payload = payload
                    if (action == "article_carrier_consume" and due_slot
                            and invocation in ("scheduled", "manual_registered")
                            and args == {"mode": "consume"}):
                        # The exact registered due occurrence owns this publish,
                        # not a same-day historical/manual claim reconciled later.
                        # Keep payload digest/state keys and pending GET unchanged.
                        operation = "host-article-carrier-" + hashlib.sha256(due_slot.encode()).hexdigest()[:24]
                        dispatch_payload = {**payload, "args": {**args, "operation-id": operation}}
                    code, job = call("/run", dispatch_payload)
                # These HTTP rejections are before bridge execution/admission.
                if code in (400, 401, 403, 404, 413, 429):
                    state.pop("pending", None)
                    raise RuntimeError(f"bridge_rejected_http_{code}")
            if code not in (200, 202) or not job.get("job_id"):
                raise RuntimeError("bridge_ack_invalid_reconcile_before_replay")
            state["pending"]["job_id"] = job["job_id"]
            save(state_path, state)
            deadline = time.monotonic() + int(actions[action].get("timeout_s", 300)) + 120
            while job.get("status") not in ("done", "error"):
                if time.monotonic() >= deadline:
                    raise RuntimeError("job_readback_deadline_pending_preserved")
                time.sleep(poll_interval)
                code, next_job = call("/job/" + state["pending"]["job_id"])
                if code != 200 or next_job.get("job_id") != state["pending"]["job_id"]:
                    raise RuntimeError("job_readback_invalid_pending_preserved")
                job = next_job
            result["local" if job.get("local_noop") else "bridge"] = job
            if job.get("interrupted") or (job.get("termination") or {}).get("classification") == "BRIDGE_INTERRUPTED":
                raise RuntimeError("bridge_interrupted_effects_ambiguous_pending_preserved")
            if job.get("rc") is None:
                raise RuntimeError("terminal_exit_code_missing_pending_preserved")
            if action == "glp_seo_updater" and (job["status"] == "error" or job.get("timed_out")):
                # Terminal bridge failure cannot establish that the updater had
                # no partial effect. Keep the known job for GET-only readback.
                raise RuntimeError("seo_updater_terminal_effects_ambiguous_pending_preserved")
            result["rc"] = int(job["rc"]) if job.get("rc") is not None else 125
            result["status"] = "PASS" if job["status"] == "done" and result["rc"] == 0 and not job.get("timed_out") else "FAIL"
            if result["status"] == "FAIL" and result["rc"] == 0:
                result["rc"] = 125
            if (result["rc"] == 0 or action in ("glp_weekly_seo_control", "error_fixer_daily_review")
                    or (action == "sheet_sync_drain" and job["rc"] == 75 and not job.get("timed_out"))):
                result["effects"] = (finish(action, args, job, state, state_path, call=call)
                                     if action in ("decisions_router", "three_engine_telemetry") and finish is finish_action
                                     else finish(action, args, job, state, state_path))
                if (result["effects"].get("designed_stand_down") and not job.get("timed_out")
                        and action == "sheet_sync_drain" and job["rc"] == 75):
                    result.update(rc=0, status="PASS")
            if action == "decisions_router" and result["status"] == "PASS":
                # Continuation IDs bind this completed router run only. The
                # next quarter-hour dispatch must get fresh Sol/resume/confirm
                # identities; preserve them on any ambiguous/failed stage.
                state.pop("decisions_jobs", None)
            state.pop("pending", None)
        except TerminalRefusal as exc:
            result.update(rc=125, status="FAIL", machine_status=exc.machine_status,
                          reason=str(exc), error_type=type(exc).__name__)
            state.pop("pending", None)
        except Exception as exc:
            result.update(rc=125, status="FAIL", error_type=type(exc).__name__)
            # Only our own fixed diagnostic strings are safe for the public receipt.
            if type(exc) is RuntimeError:
                result["reason"] = str(exc)
        result["finished_utc"] = utc_now()
        save(receipt_path, result)
        completed_slot = result.get("due_slot")
        if completed_slot and not state.get("pending"):
            # Local wall-clock minute intentionally excludes the UTC offset: a
            # repeated DST minute is the same due slot, not another daily job.
            slots = {k: v for k, v in slots.items() if k[:10] == completed_slot[:10]}
            slots[completed_slot] = {"rc": result["rc"], "receipt_path": str(receipt_path)}
            state["slots"] = slots
        failures = common.setdefault("failed_branches", {})
        if result["rc"] == 0:
            failures.pop(digest, None)
            if not failures:
                recovered_alert = common.pop("alert", None)
                if recovered_alert and not recovered_alert.get("exact_readback"):
                    common.setdefault("unverified_alerts", []).append(recovered_alert)
        else:
            failures[digest] = str(receipt_path)
        if result["rc"] != 0 and not common.get("alert"):
            # Persist before POST: ambiguous notification delivery is not a retry grant.
            common["alert"] = {"status": "sending", "attempted": False, "receipt_path": str(receipt_path)}
            save(common_path, common)
        alerts = [*common.get("unverified_alerts", []), common.get("alert", {})]
        for alert in alerts:
            if not alert or alert.get("exact_readback"):
                continue
            try:
                alert.update(notify(action, alert["receipt_path"], common, common_path))
            except Exception as exc:
                alert.update(status="delivery_unverified", error_type=type(exc).__name__)
        common["unverified_alerts"] = [a for a in common.get("unverified_alerts", []) if not a.get("exact_readback")]
        state["last_receipt"] = str(receipt_path)
        save(state_path, state)
        if args:
            save(common_path, common)
        if invocation == "manual_registered" or not (not _following_slot and pending and due_slot and due_slot != result.get("due_slot") and not state.get("pending")):
            return result
    # Reconciliation settled an earlier slot, not this invocation's obligation.
    # Reacquire the same action lock and due-slot check: a concurrent winner is
    # observed, never duplicated. The second pass has no prior pending job.
    return run_action(action, args, state_dir=state_dir, actions=actions, call=call,
                      notify=notify, invocation=invocation, poll_interval=poll_interval,
                      finish=finish, due_slot=due_slot, _following_slot=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action")
    parser.add_argument("--arg", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--scheduled", action="store_true", help="Installed cron invocation; manual is the default")
    parser.add_argument("--cron", help="Original wall-clock expression, checked before any effects")
    parser.add_argument("--timezone", default="America/Los_Angeles")
    opts = parser.parse_args()
    # Operator decision 2026-09-24: manually execute the identical installed
    # command body outside its wall-clock slot. Locks, pending reconciliation,
    # producer gates and effect readback remain in the SAME run_action path.
    manual_registered = os.environ.get("CRON_BRIDGE_MANUAL_REGISTERED") == "1"
    if manual_registered and not (opts.scheduled and opts.cron):
        parser.error("manual_registered requires the installed --scheduled --cron command")
    local_now = datetime.now(ZoneInfo(opts.timezone))
    if opts.cron and not manual_registered and not croniter.match(opts.cron, local_now):
        return 0
    args = {}
    for value in opts.arg:
        key, separator, val = value.partition("=")
        if not separator or not key or key in args:
            parser.error("each --arg must have a unique KEY=VALUE")
        args[key] = val
    slot_time = local_now
    if manual_registered and not croniter.match(opts.cron, local_now):
        slot_time = croniter(opts.cron, local_now).get_prev(datetime)
    due_slot = (slot_time.strftime("%Y-%m-%dT%H:%M") + ":" + opts.timezone
                if opts.scheduled else None)
    if opts.action == "glp_ads_weekly_review" and opts.scheduled:
        if args:
            parser.error("weekly review identity is derived from its cron slot")
        from scripts.cron_ports.ads_weekly_review import scheduled_args
        args = scheduled_args(due_slot)
    invocation = "manual_registered" if manual_registered else ("scheduled" if opts.scheduled else "manual")
    result = run_action(opts.action, args, invocation=invocation, due_slot=due_slot)
    print(json.dumps({k: v for k, v in result.items() if k != "bridge"}, sort_keys=True))
    return 0 if result["rc"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
