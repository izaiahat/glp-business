#!/usr/bin/env python3
"""Cron carrier for the provider-free GLP Growth Build 2 weekly gate.
CRON_DISCORD_HOUSE_STYLE_V1
CRON_DISCORD_OUTPUT_DECLARATION_V1={"short_name":"GLP Growth Build 2","output_class":"report","composer_type":"no_agent_script_discord_fmt","privacy_level":"internal","fixture":"✅ **GLP Growth Build 2** — weekly digital PR gate completed with no newsworthy movement\n\nstatus: **NO_NEWS_NO_PITCH** · drafts: **0** · sends: **0**\n→ Price Index movement did not clear the evidence threshold; no journalist pitch was drafted or sent\n-# receipt: /home/ubuntu/business/reports/glp-clinic-scout/growth/build-2/weekly-runs/2026-08-10/receipt.json","fixture_output_class":"report","fixture_test":"python3 -m pytest -q tests/test_glp_digital_pr_weekly.py tests/test_approved_inbox_send.py tests/test_inbox_control_plane.py","route":"1487300132054765629","detail_policy":"complete_summary_in_discord","success_silence":false,"schedule":"5 7 * * 1 America/Los_Angeles","standard_chain":"current content pointer -> exact Price Index -> newsworthiness gate -> approval-held draft"}
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

BUSINESS = Path("/home/ubuntu/business")
sys.path.insert(0, str(BUSINESS))
sys.path.insert(0, str(BUSINESS / "ops"))
from scripts.glp_digital_pr_weekly import run_weekly  # noqa: E402
import discord_fmt as fmt  # noqa: E402


def render(result: dict) -> str:
    status = str(result.get("status") or "UNKNOWN")
    receipt = str(result.get("receipt_path") or "unknown")
    drafts = int(result.get("pitch_drafts_created") or result.get("effect_counts", {}).get("pitch_drafts_created") or 0)
    sends = int(result.get("outreach_sends") or result.get("effect_counts", {}).get("outreach_sends") or 0)
    if status == "NO_NEWS_NO_PITCH":
        return fmt.render(
            fmt.title("ok", "GLP Growth Build 2", "weekly digital PR gate completed with no newsworthy movement"),
            fmt.kvline(("status", status), ("drafts", drafts), ("sends", sends)),
            fmt.meaning("Price Index movement did not clear the evidence threshold; no journalist pitch was drafted or sent"),
            fmt.subtext(f"receipt: {receipt}"),
            output_class="report",
        )
    return fmt.render(
        fmt.title("action", "GLP Growth Build 2", "weekly digital PR gate needs review"),
        fmt.kvline(("status", status), ("drafts", drafts), ("sends", sends)),
        fmt.meaning("any draft remains approval-held; this cron never authorizes or sends journalist outreach"),
        fmt.subtext(f"receipt: {receipt}"),
        output_class="alert" if status in {"BLOCKED", "FAIL", "UNKNOWN"} else "report",
    )


def main() -> int:
    now = datetime.now(ZoneInfo("America/Los_Angeles"))
    receipt = (
        BUSINESS
        / "reports/glp-clinic-scout/growth/build-2/weekly-runs"
        / now.date().isoformat()
        / "receipt.json"
    )
    try:
        result = run_weekly(now=now, receipt_path=receipt)
        result["receipt_path"] = str(receipt)
        print(render(result))
        return 0
    except Exception as exc:
        result = {
            "status": "BLOCKED",
            "receipt_path": str(receipt),
            "blocker": f"{type(exc).__name__}: {exc}",
            "pitch_drafts_created": 0,
            "outreach_sends": 0,
        }
        print(render(result))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
