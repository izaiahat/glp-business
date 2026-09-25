#!/usr/bin/env python3
"""Live Gmail token-health preflight for the canonical multi-account inbox cron."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path("/home/ubuntu/business")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from inbox_control_plane.gmail_client import (
    GmailClient,
    GmailClientError,
    alert_token_health_failure,
    load_registry,
)

REPORT_DIR = Path("/home/ubuntu/business/reports/email-monitor-hardening-2026-08-04/token-health")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def main() -> int:
    captured = utc_now()
    rows = []
    failed = []
    for mailbox in load_registry():
        try:
            client = GmailClient.from_mailbox(mailbox, mode="read")
            rows.append(client.preflight())
        except GmailClientError as exc:
            reason = str(exc)
            alert = alert_token_health_failure(mailbox, reason)
            failed.append({
                "mailbox_id": mailbox.mailbox_id,
                "account": mailbox.canonical_address,
                "reason": reason,
                "alert": alert,
            })
    receipt = {
        "schema": "gmail-token-health-preflight-v1",
        "captured_at_utc": captured,
        "status": "PASS" if not failed else "BLOCKED",
        "mailboxes": rows,
        "failures": failed,
        "secret_printed": False,
        "provider_mutations": 0,
        "outbound_sends": 0,
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"{captured.replace(':', '').replace('-', '')}-preflight.json"
    path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    path.chmod(0o600)
    if failed:
        print(f"⚠️ Gmail token health BLOCKED — {len(failed)} mailbox(es) failed; #decisions alerted")
        print(f"artifact: {path}")
        return 2
    print(f"PASS Gmail token health — {len(rows)} API mailbox(es); profile probes passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
