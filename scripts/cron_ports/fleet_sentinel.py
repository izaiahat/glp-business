#!/usr/bin/env python3
"""Scheduled, graph-native ops/fleet-sentinel port (published aIth7wbz2LCLLu40).

Candidate only: no clock is installed here. The n8n Manual Run webhook remains
untouched. The only bridge action is the existing allowlisted ``buffer_status``;
registry comparison, health, cases and notifications are local continuations.

Integration: install this script ONLY after published Schedule disable/readback and
invoker census. Use the exact CLI shown by ``--help``. State is keyed by Pacific
wall-clock minute, so a repeated DST minute cannot duplicate a sweep. A pending
notification without a known ID is deliberately held, never resent.
"""
from __future__ import annotations

import argparse
import fcntl
import importlib.util
import json
import math
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from croniter import croniter

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
STATE_DIR = ROOT / "reports/ops/n8n-to-cron-20260924/runs/fleet_sentinel"
REGISTRY = "ReulkaY4kEwCh9RY"
HEARTBEATS = "9Gevk0AdC7J6UbFn"
CASES = "jwrG1kHGYUIcOPWG"
CHANNEL = "1487300132054765629"
WORKFLOW = "aIth7wbz2LCLLu40"
CARRIER = "uabEqV8bzEYanLVJ"
HEALTH_URL = "https://bridge.allocant.ai/health"
CRON = "10 8,18 * * *"
ZONE = "America/Los_Angeles"
MISS_CLASS = "machine has not produced verified output on schedule"
BRIDGE_CLASS = "bridge health check failed"
SUPPLY_CLASS = "article buffer ready count is below the pool minimum"
SAFE_REFUSALS = frozenset({
    "machine_registry_empty_unknown_not_healthy", "registry_cadence_unknown",
    "buffer_status_stdout_truncated_reconcile_spill", "datatable_pagination_incomplete",
    "datatable_insert_unverified", "discord_ack_unknown_no_resend",
    "discord_exact_readback_failed", "bridge_health_http_not_200",
    "bridge_health_bad_response", "slot_state_identity_mismatch",
})


def fnv1a64(raw: str) -> str:
    """Exact UTF-8 FNV-1a 64 used by published Compare and fixer_runtime."""
    value = 0xCBF29CE484222325
    for byte in raw.encode("utf-8"):
        value = ((value ^ byte) * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return f"{value:016x}"


def iso(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def case_row(workflow: str, source: str, error_class: str, text: str,
             now: datetime, *, execution_id: str = "", receipt: str = "") -> dict:
    stamp = iso(now)
    signature = fnv1a64("|".join((workflow, source, error_class)))
    return {"case_id": "ec-" + now.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S")
            + "-" + signature[:8], "signature": signature, "source": source,
            "workflow_id": workflow, "execution_id": execution_id,
            "error_text": text[:1800], "status": "new", "attempts_today": 0,
            "first_seen_utc": stamp, "last_update_utc": stamp, "receipt": receipt}


def _parse_utc(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc) if dt.tzinfo else None
    except (TypeError, ValueError, OverflowError):
        return None


def compare(registry: list[dict], heartbeats: list[dict], now: datetime) -> dict:
    """Published Compare semantics, refusing incomplete/invalid source evidence."""
    if not registry:
        raise ValueError("machine_registry_empty_unknown_not_healthy")
    latest: dict[str, datetime] = {}
    for row in heartbeats:
        machine = row.get("machine")
        observed = _parse_utc(row.get("last_success_utc"))
        if machine and observed and (machine not in latest or observed > latest[machine]):
            latest[machine] = observed
    day = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")[(now.weekday() + 1) % 7]
    misses, cases = [], []
    for row in registry:
        machine = str(row.get("machine") or "")
        if not machine:
            continue
        active = str(row.get("active_days") or "all")
        if active != "all" and day not in active:
            continue
        try:
            cadence = float(row.get("cadence_hours") or 24)
            grace = float(row.get("grace_hours") or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("registry_cadence_unknown") from exc
        if not math.isfinite(cadence) or not math.isfinite(grace) or cadence <= 0 or grace < 0:
            raise ValueError("registry_cadence_unknown")
        seen = latest.get(machine)
        age = (now - seen).total_seconds() / 3600 if seen else None
        if age is not None and age <= cadence + grace:
            continue
        detail = (f"**{machine}** — no heartbeat EVER recorded" if age is None else
                  f"**{machine}** — last success {age:.1f}h ago (expected every {row.get('cadence_hours')}h)")
        misses.append(detail)
        text = (detail.replace("**", "") + " | owner=" + str(row.get("owner") or "?")
                + " cadence=" + str(row.get("cadence_hours") or "?") + "h grace="
                + str(row.get("grace_hours") or 0) + "h")
        cases.append(case_row(machine, "sentinel_miss", MISS_CLASS, text, now,
                              receipt="ops/fleet-sentinel sweep " + iso(now)))
    alert = (f"🔴 **Fleet Sentinel** — {len(misses)} machine(s) not producing\n\n"
             + "\n".join(misses)
             + "\n→ every line is a machine that has not produced verified output on schedule"
             if misses else "")
    return {"misses": len(misses), "text": alert, "miss_rows": cases}


def bridge_down(error: str, now: datetime, slot: str) -> dict:
    msg = error[:600] or "no detail"
    text = ("🔴 **Fleet Sentinel — bridge health check FAILED**\n"
            "GET authenticated public /health did not answer OK.\n"
            + "error: " + msg + "\n"
            "No bridge_health heartbeat was written for this sweep — every machine that runs "
            "through the bridge is at risk until this clears.\n"
            "host cron slot " + slot)
    row = case_row("bridge_health", "sentinel_miss", BRIDGE_CLASS,
                   "bridge_health — GET /health failed: " + msg, now,
                   receipt="ops/fleet-sentinel bridge health " + iso(now))
    return {"text": text[:1900], "case_row": row}


def supply_floor(job: dict, now: datetime) -> dict:
    """Published Supply Floor; transport truncation/rc != 0 cannot claim healthy."""
    if job.get("stdout_truncated"):
        raise ValueError("buffer_status_stdout_truncated_reconcile_spill")
    receipt = None
    for line in reversed(str(job.get("stdout_tail") or "").splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                parsed = json.loads(line)
            except ValueError:
                continue
            if isinstance(parsed, dict) and isinstance(parsed.get("pool"), dict):
                receipt = parsed
                break
    valid = (job.get("status") == "done" and job.get("rc") == 0 and receipt is not None)
    pool = receipt.get("pool", {}) if valid else {}
    try:
        ready, minimum = float(pool["ready"]), float(pool["minimum"])
        valid = valid and all(math.isfinite(v) and v >= 0 for v in (ready, minimum))
    except (KeyError, ValueError, TypeError):
        valid = False
    if not valid:
        text = ("🔴 **article buffer supply** — SUPPLY UNKNOWN\n"
                "the bridge buffer_status call returned no readable receipt "
                f"(rc={job.get('rc', '?')}, status={job.get('status') or '?'})\n"
                "→ the article machine could be dry and nobody would know; "
                "check the bridge action buffer_status on the box")
        error_text = text.replace("**", "")
        result = {"below": True, "unknown": True, "ready": None,
                  "minimum": None, "deficit": None}
    else:
        deficit = max(0.0, minimum - ready)
        ready = int(ready) if ready.is_integer() else ready
        minimum = int(minimum) if minimum.is_integer() else minimum
        deficit = int(deficit) if deficit.is_integer() else deficit
        entries = receipt.get("entries") or []
        next_route = entries[0].get("target_route") if entries and isinstance(entries[0], dict) else None
        stale = receipt.get("stale_claim_count") or 0
        text = ("🔴 **article buffer supply** — " + str(ready) + " ready vs minimum "
                + str(minimum) + " (deficit " + str(deficit) + ")\n"
                "every publish drains one; at this depth the M/W/F carrier runs dry\n"
                + ("next in line: " + str(next_route) + "\n" if next_route else "")
                + ("held by stale claims: " + str(stale)
                   + " (auto-recycled on the next carrier run)\n" if stale else "")
                + "→ refill runs through the standard review-only admission lane; the floor is not lowered") if deficit else ""
        error_text = ("article buffer supply floor: " + str(ready)
                      + " review-passed articles ready against a minimum of " + str(minimum)
                      + " (deficit " + str(deficit) + "). Every publish drains one. "
                      "Refill runs through the standard review-only admission lane; "
                      "the floor is never lowered.")
        result = {"below": deficit > 0, "unknown": False, "ready": ready,
                  "minimum": minimum, "deficit": deficit}
    result["text"] = text
    result["case_row"] = case_row(CARRIER, "carrier_supply_floor", SUPPLY_CLASS,
                                  error_text, now,
                                  receipt="ops/fleet-sentinel supply sweep " + iso(now))
    return result


def _save(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".new")
    with temp.open("w") as fh:
        json.dump(value, fh, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(temp, path)


def _read_table(table: str) -> list[dict]:
    from ops import n8n_dt
    # The heartbeat table exceeds one 5,000-row client window. Exhaust its
    # native cursor rather than silently comparing a recent subset.
    data = []
    cursor = None
    seen = set()
    while True:
        response = n8n_dt._native_datatable_call(
            {"action": "getRows", "tableId": table, "limit": 5000, "cursor": cursor})
        body = response.get("data") or {}
        data.extend(body.get("rows") or [])
        cursor = body.get("nextCursor")
        if not cursor:
            return data
        if cursor in seen:
            raise ValueError("datatable_pagination_incomplete")
        seen.add(cursor)


def _insert(table: str, row: dict) -> None:
    from ops.n8n_dt import insert
    result = insert(table, row)
    # n8n_dt verifies the natural key and returns the exact row, including for
    # ambiguous-but-landed writes. A transport ACK alone is not effect proof.
    observed = ((result.get("data") or {}).get("rows") or [])
    if (result.get("success") is not True or len(observed) != 1
            or any(observed[0].get(key) != value for key, value in row.items())):
        raise ValueError("datatable_insert_unverified")

def _case_once(row: dict, state: dict, state_path: Path, insert) -> None:
    """Journal before insertion; never blindly repeat an uncertain case write."""
    cases = state.setdefault("cases", {})
    key = row["case_id"]
    prior = cases.get(key)
    if prior is None:
        cases[key] = {"status": "inserting", "row": row}
        _save(state_path, state)
        insert(CASES, row)
        cases[key]["status"] = "verified"
        _save(state_path, state)
    elif prior.get("row") != row or prior.get("status") != "verified":
        # An ACK/crash gap is unknown, not permission to create a second row.
        raise ValueError("datatable_insert_unverified")


def _post(key: str, content: str, state: dict, state_path: Path) -> None:
    from scripts.post_discord_message import send, readback
    body = content[:1900]
    deliveries = state.setdefault("deliveries", {})
    delivery = deliveries.get(key)
    if delivery is None:
        delivery = {"status": "sending", "content": body}
        deliveries[key] = delivery
        _save(state_path, state)
        sent = send(CHANNEL, body)
        delivery["id"] = str(sent.get("id") or "")
        _save(state_path, state)
    if delivery.get("content") != body or not delivery.get("id"):
        raise ValueError("discord_ack_unknown_no_resend")
    fetched = readback(CHANNEL, delivery["id"])
    if (str(fetched.get("id") or "") != delivery["id"]
            or fetched.get("channel_id") != CHANNEL or fetched.get("content") != body):
        raise ValueError("discord_exact_readback_failed")
    delivery["status"] = "verified"
    _save(state_path, state)


def _bridge_heartbeat_once(row: dict, state: dict, state_path: Path, insert) -> None:
    """Reconcile a receipt-keyed heartbeat before any repeated insert."""
    from ops import n8n_dt
    def observed():
        found = n8n_dt.rows(HEARTBEATS, n8n_dt.eq("receipt", row["receipt"]), limit=2)
        if n8n_dt.last_read_truncated or len(found) > 1:
            raise ValueError("bridge_heartbeat_readback_ambiguous")
        return found[0] if found else None

    saved = state.get("bridge_heartbeat")
    if saved is None and (state.get("errors") or {}).get("bridge") == "NativeSemanticRefusal":
        prior = observed()
        if prior is None or any(prior.get(k) != row[k] for k in
                                ("machine", "last_success_utc", "output_count", "receipt")) \
                or not str(prior.get("note") or "").startswith("uptime_s "):
            raise ValueError("bridge_heartbeat_prior_effect_unverified")
        saved = {k: prior[k] for k in row}
        state["bridge_heartbeat"] = saved
        state["bridge_heartbeat_status"] = "verified_prior_effect"
        _save(state_path, state)
        return
    if saved is not None:
        if any(saved.get(k) != row[k] for k in ("machine", "last_success_utc", "output_count", "receipt")):
            raise ValueError("bridge_heartbeat_identity_changed")
        if state.get("bridge_heartbeat_status") in ("verified", "verified_prior_effect"):
            return
        prior = observed()
        if prior is None or any(prior.get(k) != saved[k] for k in saved):
            raise ValueError("bridge_heartbeat_insert_unverified")
        state["bridge_heartbeat_status"] = "verified"
        _save(state_path, state)
        return
    state["bridge_heartbeat"] = row
    state["bridge_heartbeat_status"] = "inserting"
    _save(state_path, state)
    try:
        insert(HEARTBEATS, row)
    except Exception:
        prior = observed()
        if prior is None or any(prior.get(k) != row[k] for k in row):
            raise
    state["bridge_heartbeat_status"] = "verified"
    _save(state_path, state)


def _buffer_job(slot: str, *, invocation="scheduled") -> dict:
    from scripts.cron_bridge_action import run_action
    # The existing runner owns scoped credentials, action allowlisting, pending
    # POST acknowledgement and known-job GET reconciliation. No invented action.
    result = run_action("buffer_status", {}, due_slot=slot, invocation=invocation,
                        finish=lambda *_: {"sensor_only": True})
    if result.get("status") == "SKIPPED_OBSERVED_SLOT" and result.get("receipt_path"):
        result = json.loads(Path(result["receipt_path"]).read_text())
    job = result.get("bridge")
    if not isinstance(job, dict):
        return {"status": "unknown", "rc": result.get("rc"), "stdout_tail": ""}
    return job


def _bridge_health() -> dict:
    # Reuse the bridge's strict 0600 global credential loader (not an action's
    # scoped bearer). Cloudflare blocks Python-urllib's User-Agent with 403;
    # the published n8n HTTP Request uses axios against this public path.
    spec = importlib.util.spec_from_file_location("sentinel_existing_bridge", ROOT / "ops/n8n-bridge/bridge.py")
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)
    req = urllib.request.Request(HEALTH_URL, headers={
        "Authorization": "Bearer " + bridge.load_token(),
        "User-Agent": "axios/1.6.8", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as response:
        if response.status != 200:
            raise ValueError("bridge_health_http_not_200")
        raw = response.read(8192)
    body = json.loads(raw)
    if not isinstance(body, dict) or body.get("status") in ("error", "down", "unhealthy"):
        raise ValueError("bridge_health_bad_response")
    return body


def run(slot: str, *, now: datetime | None = None, state_dir: Path = STATE_DIR,
        read_table=_read_table, insert=_insert, post=_post,
        health=_bridge_health, buffer_job=_buffer_job, run_type="scheduled",
        repair_prior=None) -> dict:
    """Run three independent graph lanes. Dependencies are injectable for tests."""
    if not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:America/Los_Angeles", slot):
        raise ValueError("invalid_pacific_slot")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("naive_time_refused")
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / "fleet-sentinel.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "SKIPPED_OVERLAP", "rc": 0, "slot": slot}
        state_path = state_dir / (slot.replace(":", "-") + ".json")
        state = json.loads(state_path.read_text()) if state_path.exists() else {"slot": slot, "lanes": {}}
        if state.get("slot") != slot:
            raise ValueError("slot_state_identity_mismatch")
        if state.get("run_type", run_type) != run_type:
            raise ValueError("slot_run_type_mismatch")
        state.setdefault("run_type", run_type)
        prior_signatures = set()
        if run_type == "manual_registered_repair":
            if not isinstance(repair_prior, dict) or repair_prior.get("status") != "PASS":
                raise ValueError("repair_prior_missing")
            prior_cases = [v for v in (repair_prior.get("cases") or {}).values()
                           if "last success" in str(v.get("row", {}).get("error_text") or "")
                           or "no heartbeat EVER" in str(v.get("row", {}).get("error_text") or "")]
            if (len(prior_cases) != int(repair_prior.get("misses") or 0)
                    or any(v.get("status") != "verified" for v in prior_cases)):
                raise ValueError("repair_prior_miss_cases_unverified")
            prior_delivery = (repair_prior.get("deliveries") or {}).get("misses") or {}
            if prior_cases and (prior_delivery.get("status") != "verified" or not prior_delivery.get("id")):
                raise ValueError("repair_prior_miss_delivery_unverified")
            prior_signatures = {v["row"]["signature"] for v in prior_cases}
            bound = {"slot": repair_prior["slot"], "signatures": sorted(prior_signatures),
                     "message_id": prior_delivery.get("id")}
            if state.get("repair_of", bound) != bound:
                raise ValueError("repair_prior_identity_changed")
            state.setdefault("repair_of", bound)
        # First timestamp is durable across interrupted continuations. Case IDs
        # are stable on retry; signatures remain constant across all sweeps.
        state.setdefault("started_utc", iso(now))
        _save(state_path, state)
        stamp = datetime.fromisoformat(state["started_utc"].replace("Z", "+00:00"))
        errors = {}
        lanes = state["lanes"]
        def lane(name, worker):
            if lanes.get(name) == "done":
                return
            try:
                worker()
                lanes[name] = "done"
                _save(state_path, state)
            except Exception as exc:
                # Provider exception text may contain URLs/headers. Keep only
                # local refusal codes in this private receipt, never raw errors.
                reason = str(exc) if type(exc) is ValueError and str(exc) in SAFE_REFUSALS else ""
                errors[name] = type(exc).__name__ + (":" + reason if reason else "")
        def comparator():
            result = compare(read_table(REGISTRY), read_table(HEARTBEATS), stamp)
            if result["misses"]:
                new_rows = [row for row in result["miss_rows"]
                            if row["signature"] not in prior_signatures]
                for row in new_rows:
                    _case_once(row, state, state_path, insert)
                if new_rows:
                    text = (result["text"] if not repair_prior else
                            "🔴 **Fleet Sentinel manual repair — new misses only**\n" +
                            "\n".join(str(row["error_text"]) for row in new_rows))
                    post("misses", text, state, state_path)
                state["prior_misses_suppressed"] = result["misses"] - len(new_rows)
            state["misses"] = result["misses"]
        def bridge():
            try:
                body = health()
            except Exception as exc:
                # No heartbeat on failure, matching Bridge Down branch.
                failed = bridge_down(type(exc).__name__, stamp, slot)
                _case_once(failed["case_row"], state, state_path, insert)
                post("bridge_down", failed["text"], state, state_path)
                state["bridge"] = "down"
            else:
                _bridge_heartbeat_once({"machine": "bridge_health", "last_success_utc": iso(stamp),
                                        "output_count": 1, "receipt": HEALTH_URL + " " + slot,
                                        "note": "uptime_s " + str(body.get("uptime_s", "?"))},
                                       state, state_path, insert)
                state["bridge"] = "healthy"
        def supply():
            result = supply_floor(buffer_job(slot), stamp)
            if result["below"]:
                _case_once(result["case_row"], state, state_path, insert)
                post("supply", result["text"], state, state_path)
            state["supply"] = {k: result[k] for k in ("below", "unknown", "ready", "minimum", "deficit")}
        # Original schedule fans out to all three; failures do not suppress peers.
        lane("compare", comparator)
        lane("bridge", bridge)
        lane("supply", supply)
        state["errors"] = errors
        state["status"] = "PASS" if not errors else "FAIL"
        _save(state_path, state)
        return {"status": state["status"], "rc": 0 if not errors else 1,
                "slot": slot, "lanes": dict(lanes), "errors": errors,
                "run_type": state["run_type"], "misses": state.get("misses"), "bridge": state.get("bridge"),
                "supply": state.get("supply"), "receipt_path": str(state_path)}


def repair_slot(original: str, correction_id: str, *, state_dir=STATE_DIR) -> str:
    """Bind one manual re-sweep to a corrected, effectful prior slot."""
    if not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:America/Los_Angeles", original):
        raise ValueError("repair_original_slot_invalid")
    start = datetime.strptime(original[:16], "%Y-%m-%dT%H:%M").replace(tzinfo=ZoneInfo(ZONE))
    if not croniter.match(CRON, start):
        raise ValueError("repair_original_not_registered_slot")
    prior_path = Path(state_dir) / (original.replace(":", "-") + ".json")
    prior = json.loads(prior_path.read_text())
    if prior.get("slot") != original or prior.get("status") != "PASS" or prior.get("bridge") != "down":
        raise ValueError("repair_prior_effect_unverified")
    old_message = (prior.get("deliveries", {}).get("bridge_down") or {}).get("id")
    case_ids = [k for k, v in (prior.get("cases") or {}).items()
                if v.get("status") == "verified" and v.get("row", {}).get("workflow_id") == "bridge_health"
                and "GET /health failed" in str(v.get("row", {}).get("error_text") or "")]
    if not old_message or len(case_ids) != 1 or not correction_id:
        raise ValueError("repair_correction_binding_missing")
    from scripts.post_discord_message import readback
    correction = readback(CHANNEL, correction_id)
    text = correction.get("content") or ""
    if (str(correction.get("id")) != correction_id or correction.get("channel_id") != CHANNEL
            or old_message not in text or case_ids[0] not in text or "403" not in text):
        raise ValueError("repair_correction_readback_mismatch")
    return (start + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M") + ":" + ZONE


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scheduled", action="store_true", required=True)
    parser.add_argument("--cron", default=CRON)
    parser.add_argument("--timezone", default=ZONE)
    parser.add_argument("--manual-repair-of", help="Prior registered slot with corrected false bridge alert")
    parser.add_argument("--correction-id", help="Verified Discord correction bound to the prior case")
    args = parser.parse_args(argv)
    if args.cron != CRON or args.timezone != ZONE:
        parser.error("only the published Pacific schedule is supported")
    local = datetime.now(ZoneInfo(ZONE))
    manual = os.environ.get("CRON_BRIDGE_MANUAL_REGISTERED") == "1"
    if args.manual_repair_of:
        if not manual or not args.correction_id:
            parser.error("manual repair requires registered authorization and correction readback")
        slot = repair_slot(args.manual_repair_of, args.correction_id)
        prior_path = STATE_DIR / (args.manual_repair_of.replace(":", "-") + ".json")
        prior = json.loads(prior_path.read_text())
        result = run(slot, run_type="manual_registered_repair",
                     buffer_job=lambda s: _buffer_job(s, invocation="manual_registered_repair"),
                     repair_prior=prior)
        print(json.dumps(result, sort_keys=True))
        return result["rc"]
    if args.correction_id:
        parser.error("correction-id requires manual-repair-of")
    if not manual and not croniter.match(CRON, local):
        return 0
    slot_time = local if not manual or croniter.match(CRON, local) else croniter(CRON, local).get_prev(datetime)
    slot = slot_time.strftime("%Y-%m-%dT%H:%M") + ":" + ZONE
    result = run(slot, run_type="manual_registered" if manual else "scheduled")
    print(json.dumps(result, sort_keys=True))
    return result["rc"]


if __name__ == "__main__":
    sys.exit(main())
