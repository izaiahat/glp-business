#!/usr/bin/env python3
"""Deterministic cron entrypoint for the canonical inbox control plane."""
# CRON_DISCORD_HOUSE_STYLE_V1
# CRON_DISCORD_OUTPUT_DECLARATION_V1={"short_name":"Inbox Control Plane","output_class":"alert","composer_type":"script","privacy_level":"internal","fixture":"⚠️ **Inbox Control Plane** — canonical poll result was not safely represented\n\nblocked: **unknown** · control exit **2**\nprovider mutations **0** · outbound sends **0** · no raw mail or secret printed\n→ Inspect the private run receipt and repair the receipt/dispatch path.\n-# source: canonical read-only inbox poll · internal only","fixture_output_class":"alert","fixture_test":"/home/ubuntu/business/ops/tests/test_cron_discord_output_guard.py::test_registry_fixtures_lint","route":"1487300132054765629","detail_policy":"complete_summary_in_discord"}
from __future__ import annotations

import json
import fcntl
import subprocess
import sys
from pathlib import Path

CONTROL = Path("/home/ubuntu/business/inbox_control_plane/control_plane.py")
BEST_TERMS = Path("/home/ubuntu/business/scripts/affiliate_best_terms_machine.py")
LOCK = Path("/home/ubuntu/.hermes/state/email-monitor-cron.lock")


def run_best_terms_approval_trigger() -> dict:
    """Re-evaluate only providers whose authoritative approval receipt is new."""
    try:
        proc = subprocess.run(
            [
                sys.executable, str(BEST_TERMS), "--approval-events", "--stage",
                "--send-discord", "--stdout", "json",
            ],
            capture_output=True,
            text=True,
            timeout=240,
        )
    except subprocess.TimeoutExpired:
        return {"status": "FAIL", "reason": "approval_trigger_timeout", "rc": 124}
    try:
        payload = json.loads((proc.stdout or "{}").strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        payload = {"status": "FAIL", "reason": "approval_trigger_receipt_invalid"}
    payload["rc"] = proc.returncode
    return payload


def best_terms_failure_already_represented(payload: dict) -> bool:
    notification = payload.get("failure_notification") or {}
    return notification.get("status") in {"PASS", "PENDING_UNRECONCILED", "SUPPRESSED_SAME_SIGNATURE"} and (
        notification.get("status") != "PASS" or notification.get("readback_exact") is True
    )


def run_locked() -> int:
    health = subprocess.run(
        [sys.executable, "/home/ubuntu/business/scripts/inbox_token_health_preflight.py"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if health.returncode != 0:
        output = (health.stdout or health.stderr or "Gmail token health preflight failed").strip()
        print(output)
        return 2
    try:
        proc = subprocess.run(
            [sys.executable, str(CONTROL), "--poll", "--dispatch", "--decision-sync", "--limit", "250"],
            capture_output=True,
            text=True,
            timeout=240,
        )
    except subprocess.TimeoutExpired:
        print(
            "⚠️ **Inbox Control Plane** — canonical poll exceeded 240s\n\n"
            "provider mutations **0** · outbound sends **0** · one cycle failed closed\n"
            "→ The next run will recover the interrupted ledger row; inspect provider network health if this repeats.\n"
            "-# source: canonical read-only inbox poll · restricted"
        )
        return 2
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        print("⚠️ **Inbox Control Plane** — invalid private poll receipt\n\nprovider mutations **0** · outbound sends **0** · no raw mail or secret printed\n→ Inspect the private cron logs and repair the local receipt path.\n-# source: canonical read-only inbox poll · restricted")
        return 1
    best_terms = run_best_terms_approval_trigger()
    if best_terms.get("rc") != 0 or best_terms.get("status") not in {
        "PASS", "PASS_NO_NEW_APPROVALS", "PARTIAL",
    }:
        if not best_terms_failure_already_represented(best_terms):
            print(
                "⚠️ **Inbox Approval Trigger** — provider re-evaluation failed closed\n\n"
                "provider mutations **0** · live link changes **0** · deploys **0**\n"
                "→ Inspect the private best-terms receipt before relying on a new approval.\n"
                "-# source: affiliate best-terms approval trigger · restricted"
            )
        return 2
    if proc.returncode == 0 and payload.get("status") == "pass":
        return 0
    blocked = [row.get("mailbox_id") for row in payload.get("mailboxes", []) if row.get("status") != "pass"]
    expected_surface_block = (
        proc.returncode == 2
        and payload.get("status") == "blocked"
        and bool(blocked)
        and int(payload.get("provider_mutation_count") or 0) == 0
        and int(payload.get("outbound_send_count") or 0) == 0
        and int((payload.get("dispatch") or {}).get("failed") or 0) == 0
        and payload.get("secret_printed") is False
        and bool(payload.get("run_id"))
    )
    if expected_surface_block:
        # The control plane already emitted an hourly-deduplicated health card.
        # Treat that represented business state as handled so Hermes does not add
        # a second generic cron-failure alert every five minutes.
        return 0
    print(
        "⚠️ **Inbox Control Plane** — canonical poll result was not safely represented\n\n"
        f"blocked: **{', '.join(blocked) or 'unknown'}** · control exit **{proc.returncode}**\n"
        "provider mutations **0** · outbound sends **0** · no raw mail or secret printed\n"
        "→ Inspect the private run receipt and repair the receipt/dispatch path.\n"
        f"-# source: `{payload.get('run_id') or 'unknown'}` · restricted"
    )
    return 2


def main() -> int:
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    lock_handle = LOCK.open("a+")
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_handle.close()
        return 0
    try:
        return run_locked()
    finally:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        lock_handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
