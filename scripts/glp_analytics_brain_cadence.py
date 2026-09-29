#!/usr/bin/env python3
"""Cadence and fail-loud wrapper for the GLP analytics brain.

Every failed collector run is posted to Discord ops-alerts with webhook readback.
Tests pass ``--alert-sink`` so no live message is reachable. Pre-launch inactive
runs are silent and provider-free.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import importlib.util
import json
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any

BUSINESS = Path(__file__).resolve().parent.parent
COLLECTOR = BUSINESS / "systems/analytics-brain/collector.py"
OBSERVABILITY_COLLECTOR = Path.home() / ".hermes/scripts/glp_observability_collect.sh"
RUN_ROOT = BUSINESS / "reports/glp-clinic-scout/analytics/runs"
# This is the existing installer/cron reader fence, not a new lock authority.
# The cron caller holds its distinct state lock while awaiting this child.
READER_LOCK = BUSINESS / "reports/ops/n8n-to-cron-20260924/runs/analytics_brain.lock"
DEPLOY_TRIGGER = BUSINESS / "ops/glp/analytics-brain-state/deploy-trigger.json"
ADS_STATE = BUSINESS / "ops/glp/analytics-brain-state/ads-digest-state.json"
APPROVAL_LOOP = BUSINESS / "scripts/ads_approval_loop.py"
# Google Ads' official client is intentionally isolated in the ads control-plane
# venv.  The bridge invokes this cadence with its generic `python3`, so using
# sys.executable here silently turns a real Google readback into a false
# provider-blocked result.  Fall back only for installations that do not carry
# the canonical venv; the approval loop still fails closed in that case.
APPROVAL_LOOP_PYTHON = BUSINESS / "ventures/glp-clinic-scout/ads-control-plane/.venv/bin/python"
OPS_WEBHOOK = Path.home() / ".hermes/secrets/n8n/ops-alerts-webhook.txt"

# One standing defect must not page hourly. The operator's own rule is one
# consolidated daily pass, and "no machine errors twice on the same thing"; a
# stop-loss that re-fires every hour for the same unchanged condition is exactly
# the noise that rule was written against. A CHANGED signature always pages.
IMMEDIATE_REALERT_HOURS = 12


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def read_last_json(stdout: str) -> dict[str, Any]:
    for line in reversed(stdout.splitlines()):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return {"status": "FAIL_COLLECTOR_NO_JSON_RECEIPT", "output_count": 0}


def alert_content(run: dict[str, Any]) -> str:
    status = str(run.get("status") or "FAIL_UNKNOWN")
    phase = str(run.get("phase") or "unknown")
    return (
        f"🔴 **GLP analytics brain** — {status}\n\n"
        f"Phase: **{phase}** · outputs: **{run.get('output_count', 0)}** · "
        f"exit: **{run.get('exit_code')}**\n"
        f"→ Inspect `{run.get('receipt_path')}`; search and revenue decisions are fail-closed.\n"
        "-# source: analytics brain collector and exact-host reconciliation guard"
    )


def send_alert(content: str, sink: Path | None) -> dict[str, Any]:
    if sink is not None:
        sink.parent.mkdir(parents=True, exist_ok=True)
        # Several posts can happen in one ads run (digest + stop-loss). Appending
        # keeps a test able to assert on every one of them, not just the last.
        with sink.open("a", encoding="utf-8") as handle:
            handle.write(content + "\n")
        return {"status": "PASS_LOCAL_TEST_SINK", "path": str(sink), "live_send": False}
    if not OPS_WEBHOOK.is_file():
        raise RuntimeError("ops_alerts_webhook_missing")
    url = OPS_WEBHOOK.read_text(encoding="utf-8").strip()
    separator = "&" if "?" in url else "?"
    body = json.dumps({"content": content}).encode("utf-8")
    request = urllib.request.Request(
        url + separator + "wait=true", data=body, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "glp-analytics-brain/1.0"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        reply = json.loads(response.read().decode("utf-8"))
    message_id = str(reply.get("id") or "")
    if not message_id:
        raise RuntimeError("ops_alerts_webhook_readback_missing_message_id")
    return {"status": "PASS_DISCORD_READBACK", "message_id": message_id,
            "channel": "ops-alerts", "channel_id": "1487300132054765629", "live_send": True}


def deploy_watch_active(now: dt.datetime | None = None) -> bool:
    if not DEPLOY_TRIGGER.is_file():
        return False
    payload = json.loads(DEPLOY_TRIGGER.read_text(encoding="utf-8"))
    if payload.get("schema") != "glp-analytics-deploy-trigger-v1":
        raise RuntimeError("deploy_trigger_schema_invalid")
    triggered = dt.datetime.fromisoformat(str(payload["triggered_at_utc"]).replace("Z", "+00:00"))
    current = now or dt.datetime.now(dt.timezone.utc)
    return dt.timedelta(0) <= current - triggered <= dt.timedelta(hours=24)


def trigger_deploy() -> dict[str, Any]:
    payload = {
        "schema": "glp-analytics-deploy-trigger-v1",
        "triggered_at_utc": utc_now(),
        "hourly_watch_until_utc": (
            dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=24)
        ).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }
    payload["receipt_sha256"] = digest(payload)
    DEPLOY_TRIGGER.parent.mkdir(parents=True, exist_ok=True)
    temporary = DEPLOY_TRIGGER.with_name(f".{DEPLOY_TRIGGER.name}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(DEPLOY_TRIGGER)
    return payload


def _ads_module():
    """Import the ads leg lazily so the other five cadences never pay for it."""
    path = BUSINESS / "systems/analytics-brain/ads_monitor.py"
    spec = importlib.util.spec_from_file_location("glp_analytics_brain_ads_monitor", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("ads_monitor_import_failed")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _read_ads_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_ads_state(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _pt_now(now: dt.datetime | None = None) -> dt.datetime:
    """Pacific wall time without a tz database dependency in the cron shell."""
    moment = now or dt.datetime.now(dt.timezone.utc)
    try:
        from zoneinfo import ZoneInfo

        return moment.astimezone(ZoneInfo("America/Los_Angeles"))
    except Exception:  # noqa: BLE001
        return moment - dt.timedelta(hours=7)


def ads_dispatch(payload: dict[str, Any], *, state_path: Path, alert_sink: Path | None,
                 now: dt.datetime | None = None, force_digest: bool = False) -> dict[str, Any]:
    """Decide what leaves the machine: immediate stop-loss, daily digest, or silence.

    The split is the operator's, not an invention: stop-loss conditions page the
    moment they appear, everything else waits for one consolidated digest per PT
    day. A run with no findings and no digest due posts nothing at all — which is
    what makes an hourly cadence acceptable on a paused account.
    """
    ads = _ads_module()
    state = _read_ads_state(state_path)
    moment = now or dt.datetime.now(dt.timezone.utc)
    local = _pt_now(moment)
    control = payload.get("control") or {}
    findings = payload["findings"]

    posts: list[dict[str, Any]] = []

    signature = digest(sorted(item["id"] + "|" + str(item.get("platform"))
                              for item in findings["immediate"]))
    last_signature = state.get("last_immediate_signature")
    last_at = state.get("last_immediate_at_utc")
    age = None
    if last_at:
        try:
            age = (moment - dt.datetime.fromisoformat(str(last_at).replace("Z", "+00:00"))
                   ).total_seconds() / 3600.0
        except ValueError:
            age = None
    repeat_suppressed = (findings["immediate"]
                         and signature == last_signature
                         and age is not None
                         and age < IMMEDIATE_REALERT_HOURS)
    if findings["immediate"] and not repeat_suppressed:
        posts.append({"kind": "immediate",
                      **send_alert(ads.alert_content(payload), alert_sink)})
        state["last_immediate_signature"] = signature
        state["last_immediate_at_utc"] = utc_now()

    digest_hour = int(control.get("digest_hour_pt") or 9)
    digest_due = force_digest or (local.strftime("%Y-%m-%d") != state.get("last_digest_pt_date")
                                  and local.hour >= digest_hour)
    if digest_due:
        posts.append({"kind": "digest", **send_alert(ads.digest_content(payload), alert_sink)})
        state["last_digest_pt_date"] = local.strftime("%Y-%m-%d")
        state["last_digest_at_utc"] = utc_now()

    _write_ads_state(state_path, state)
    return {
        "posts": posts,
        "digest_due": bool(digest_due),
        "immediate_count": len(findings["immediate"]),
        "immediate_repeat_suppressed": bool(repeat_suppressed),
        "immediate_signature": signature,
        "pt_now": local.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def run_ads(*, fixture: Path | None = None, alert_sink: Path | None = None,
            output_dir: Path | None = None, state_path: Path | None = None,
            force_digest: bool = False) -> tuple[dict[str, Any], int]:
    ads = _ads_module()
    payload = ads.collect(output_dir=output_dir, fixture=fixture)
    # A Discord transport failure must fail LOUD but must not destroy the readback.
    # On 2026-09-01 the first live ads run raised HTTP 400 (a 2494-character digest
    # against Discord's 2000 limit) out of the dispatch and took the whole receipt
    # with it: the machine had successfully read all four sources and left no
    # evidence of it. The readback is written before dispatch is attempted, and a
    # dispatch failure becomes a recorded FAIL status instead of a traceback.
    status = payload["status"]
    try:
        dispatch = ads_dispatch(payload, state_path=state_path or ADS_STATE,
                                alert_sink=alert_sink, force_digest=force_digest)
    except Exception as exc:  # noqa: BLE001
        dispatch = {"posts": [], "error": f"{type(exc).__name__}: {exc}"[:300],
                    "digest_due": None, "immediate_count": len(payload["findings"]["immediate"]),
                    "immediate_repeat_suppressed": False}
        status = "FAIL_ADS_DISPATCH"
    receipt = {
        "schema": "glp-analytics-brain-cadence-receipt-v1",
        "generated_at_utc": utc_now(),
        "phase": "ads",
        "collector_phase": "ads",
        "status": status,
        "exit_code": 0 if status.startswith("PASS_") else 2,
        # Sources read, never dollars: a paused account is a real readback.
        "output_count": payload["output_count"],
        "collector_receipt": {
            "status": payload["status"],
            "phase": "ads",
            "output_count": payload["output_count"],
            "receipt": payload.get("receipt"),
            "sources_read": payload["sources_read"],
            "bing_measured": payload["bing_measured"],
            "total_window_spend_usd": payload["findings"]["total_window_spend_usd"],
            "immediate": payload["findings"]["immediate"],
            "digest": payload["findings"]["digest"],
        },
        "stderr": "",
        "observability_compatibility": {"required": False, "status": "NOT_REQUIRED",
                                        "exit_code": None, "stderr": "", "writer": None},
        "deploy_trigger": None,
        "ads_dispatch": dispatch,
        "message_targets": ([{"platform": "discord", "channel": "ops-alerts",
                              "channel_id": "1487300132054765629"}] if dispatch["posts"] else []),
        "mutations_attempted": payload["mutations_attempted"],
        "credentials_printed": False,
        "candidate_tree_reads": 0,
        "candidate_tree_writes": 0,
    }
    stamp_value = utc_now().replace(":", "").replace("-", "")
    path = RUN_ROOT / f"{stamp_value}-ads.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    receipt["receipt_path"] = str(path)
    receipt["receipt_sha256"] = digest(receipt)
    path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return receipt, receipt["exit_code"]


def run_ads_approval_loop(*, fixture: Path | None = None,
                          runner: Any = subprocess.run) -> tuple[dict[str, Any], int]:
    """The approval-iteration phase: readback, then at most one ladder step per ad.

    It is a phase of this existing cadence rather than a new n8n workflow or cron
    because the ads cadence already runs on a schedule the operator approved, and
    a second always-on schedule for the same account would double the review
    surface it is trying to measure. `--max-steps-per-ad 1` is the whole reason
    this is safe to run on a timer: a run can never walk an ad up two rungs, so a
    disapproval loop cannot burn a ladder (or a Google appeal budget) unattended.

    Writes stay double-gated: the loop needs `--apply` *and* an operator-placed
    gate file, and this phase passes neither. Scheduled runs are pure readback.
    """
    approval_python = (str(APPROVAL_LOOP_PYTHON)
                       if APPROVAL_LOOP_PYTHON.is_file() else sys.executable)
    command = [approval_python, str(APPROVAL_LOOP), "--max-steps-per-ad", "1"]
    if fixture:
        command.extend(["--fixture", str(fixture)])
    proc = runner(command, cwd=BUSINESS, text=True, capture_output=True, timeout=1200)
    loop = read_last_json(proc.stdout or "")
    status = loop.get("status") or ("FAIL_APPROVAL_LOOP_EXIT" if proc.returncode else "PASS")
    if proc.returncode:
        status = "FAIL_APPROVAL_LOOP_EXIT"
    receipt = {
        "schema": "glp-analytics-brain-cadence-receipt-v1",
        "generated_at_utc": utc_now(),
        "phase": "ads_approval_loop",
        "collector_phase": "ads_approval_loop",
        "status": status,
        "exit_code": proc.returncode,
        "output_count": sum((loop.get("classification_counts") or {}).values()),
        "collector_receipt": loop,
        "stderr": proc.stderr[-1000:] if proc.stderr else "",
        "observability_compatibility": {"required": False, "status": "NOT_REQUIRED",
                                        "exit_code": None, "stderr": "", "writer": None},
        "deploy_trigger": None,
        "message_targets": [],
        "mutations_attempted": 0,
        "ads_enabled": 0,
        "appeals_submitted": 0,
        "spend_caused_usd": 0.0,
        "credentials_printed": False,
        "candidate_tree_reads": 0,
        "candidate_tree_writes": 0,
    }
    stamp = utc_now().replace(":", "").replace("-", "")
    path = RUN_ROOT / f"{stamp}-ads_approval_loop.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    receipt["receipt_path"] = str(path)
    receipt["receipt_sha256"] = digest(receipt)
    path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return receipt, proc.returncode


def run(phase: str, *, fixture: Path | None = None, alert_sink: Path | None = None,
        output_dir: Path | None = None, database: Path | None = None,
        runner: Any = subprocess.run) -> tuple[dict[str, Any], int]:
    if phase == "ads":
        return run_ads(fixture=fixture, alert_sink=alert_sink, output_dir=output_dir)
    if phase == "ads_approval_loop":
        return run_ads_approval_loop(fixture=fixture, runner=runner)
    if phase == "deploy-trigger":
        trigger = trigger_deploy()
        phase = "deploy"
    else:
        trigger = None
    if phase == "deploy-watch" and not deploy_watch_active():
        receipt = {
            "schema": "glp-analytics-brain-cadence-receipt-v1",
            "status": "PASS_NO_ACTIVE_DEPLOY_WATCH",
            "phase": phase,
            "output_count": 0,
            "provider_calls": 0,
            "candidate_tree_reads": 0,
            "candidate_tree_writes": 0,
        }
        return receipt, 0
    # During the 24-hour deploy watch, the normal hourly cadence also repeats
    # the live route census. One hourly trigger therefore covers both duties;
    # a second always-running n8n schedule would waste executions.
    watch_routes = phase == "deploy-watch" or (phase == "hourly" and deploy_watch_active())
    collector_phase = "deploy" if watch_routes else phase
    # The analytics-brain replacement paused the former daily observability cron,
    # but the SEO topic queue still reads its page/engagement tables. Preserve that
    # registered writer as the daily compatibility leg until those consumers are
    # migrated. Test/fixture runs must never reach live providers.
    compatibility_required = (
        phase == "daily" and fixture is None and output_dir is None and database is None
    )
    compatibility_proc = None
    if compatibility_required:
        compatibility_proc = runner(
            ["bash", str(OBSERVABILITY_COLLECTOR)], cwd=BUSINESS, text=True,
            capture_output=True, timeout=900,
        )

    if compatibility_proc is not None and compatibility_proc.returncode != 0:
        proc = compatibility_proc
        collector = {"status": "FAIL_OBSERVABILITY_COMPATIBILITY", "output_count": 0}
    else:
        command = [sys.executable, str(COLLECTOR), "--phase", collector_phase]
        if fixture:
            command.extend(["--fixture", str(fixture)])
        if output_dir:
            command.extend(["--output-dir", str(output_dir)])
        if database:
            command.extend(["--database", str(database)])
        proc = runner(
            command, cwd=BUSINESS, text=True, capture_output=True,
            timeout=240 if compatibility_required else 1200,
        )
        collector = read_last_json(proc.stdout)
    stamp = utc_now().replace(":", "").replace("-", "")
    path = RUN_ROOT / f"{stamp}-{phase}.json"
    run_receipt = {
        "schema": "glp-analytics-brain-cadence-receipt-v1",
        "generated_at_utc": utc_now(),
        "phase": phase,
        "collector_phase": collector_phase,
        "status": collector.get("status") or ("FAIL_COLLECTOR_EXIT" if proc.returncode else "PASS"),
        "exit_code": proc.returncode,
        "output_count": int(collector.get("output_count") or 0),
        "collector_receipt": collector,
        "stderr": proc.stderr[-1000:] if proc.stderr else "",
        "observability_compatibility": {
            "required": compatibility_required,
            "status": (
                "PASS" if compatibility_proc is not None and compatibility_proc.returncode == 0
                else "FAIL" if compatibility_proc is not None
                else "NOT_REQUIRED"
            ),
            "exit_code": (compatibility_proc.returncode
                          if compatibility_proc is not None else None),
            "stderr": (compatibility_proc.stderr[-1000:]
                       if compatibility_proc is not None and compatibility_proc.stderr else ""),
            "writer": str(OBSERVABILITY_COLLECTOR) if compatibility_required else None,
        },
        "deploy_trigger": trigger,
        "message_targets": ([{"platform": "discord", "channel": "ops-alerts",
                              "channel_id": "1487300132054765629"}]
                            if proc.returncode != 0 or str(collector.get("status", "")).startswith("FAIL_")
                            else []),
        "candidate_tree_reads": 0,
        "candidate_tree_writes": 0,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    run_receipt["receipt_path"] = str(path)
    run_receipt["receipt_sha256"] = digest({k: v for k, v in run_receipt.items()
                                             if k != "receipt_sha256"})
    path.write_text(json.dumps(run_receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    failed = proc.returncode != 0 or str(run_receipt["status"]).startswith("FAIL_")
    if failed:
        try:
            run_receipt["alert"] = send_alert(alert_content(run_receipt), alert_sink)
        except Exception as alert_error:
            # Delivery might have occurred before its acknowledgement failed.
            # Preserve the primary collector failure; never repost here.
            run_receipt["alert"] = {
                "status": "UNKNOWN_ACKNOWLEDGEMENT",
                "error_type": type(alert_error).__name__[:80],
                "automatic_retry": False,
            }
        run_receipt["receipt_sha256"] = digest({k: v for k, v in run_receipt.items()
                                                if k != "receipt_sha256"})
        path.write_text(json.dumps(run_receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return run_receipt, proc.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=["deploy-trigger", "deploy-watch", "hourly", "daily",
                                                   "route-census", "weekly", "monthly", "ads",
                                                   "ads_approval_loop"],
                        required=True)
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--alert-sink", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--database", type=Path)
    args = parser.parse_args(argv)
    # The bridge /run invokes this CLI directly, bypassing cron_bridge_action.
    # Acquire before deploy-trigger or collector reads/writes, and keep the
    # descriptor open through every child and receipt/alert completion.
    READER_LOCK.parent.mkdir(parents=True, exist_ok=True)
    with READER_LOCK.open("a") as reader_lock:
        try:
            fcntl.flock(reader_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"status": "REFUSED_READER_EXCLUSION", "phase": args.phase,
                              "output_count": 0}, sort_keys=True))
            return 75
        receipt, code = run(args.phase, fixture=args.fixture, alert_sink=args.alert_sink,
                            output_dir=args.output_dir, database=args.database)
    print(json.dumps({
        "status": receipt["status"], "phase": receipt["phase"],
        "output_count": receipt["output_count"], "receipt": receipt.get("receipt_path"),
        "alert": receipt.get("alert"),
        "ads_dispatch": receipt.get("ads_dispatch"),
    }, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
