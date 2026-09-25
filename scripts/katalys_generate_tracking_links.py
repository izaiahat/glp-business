#!/usr/bin/env python3
"""Generate and verify default Katalys tracking links for approved programs."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright

ORG = "098e5455-ca72-4099-9ff9-a9dd014148e7"
TRAFFIC_SOURCE = "GLP Clinic Scout Site"
AFF_ID = 13031
SUCCESS = {"SUBMITTED_VERIFIED", "SUBMITTED_ACTION_REMOVED", "ALREADY_RELATIONSHIP"}


def approved(program: dict[str, Any], app: dict[str, Any] | None) -> bool:
    if program.get("decision") == "ALREADY_ACTIVE":
        return True
    if not app or app.get("status") not in SUCCESS:
        return False
    text = str(app.get("after_excerpt") or "") + "\n" + str(app.get("before_excerpt") or "")
    return "Approved" in text or program.get("status_before") == "preapproved_join_available"


async def generate_one(page, program: dict[str, Any]) -> dict[str, Any]:
    offer = int(program["offer_id"])
    url = f"https://app.katalys.com/pub/{ORG}/explore/{offer}"
    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(2700)
    create = page.get_by_role("button", name="Create Link", exact=True)
    if await create.count() < 1:
        return {"offer_id": offer, "name": program.get("name"), "status": "BLOCKED_NO_CREATE_LINK"}
    create = create.last
    if not await create.is_visible():
        return {"offer_id": offer, "name": program.get("name"), "status": "BLOCKED_CREATE_LINK_HIDDEN"}
    await create.click()
    await page.wait_for_timeout(900)
    traffic = page.locator('input[name="traffic-source"]:visible')
    if await traffic.count() != 1:
        return {"offer_id": offer, "name": program.get("name"), "status": "BLOCKED_TRAFFIC_SOURCE_INPUT", "count": await traffic.count()}
    await traffic.click()
    await page.wait_for_timeout(450)
    option = page.get_by_role("option").filter(has_text=TRAFFIC_SOURCE)
    if await option.count() != 1:
        return {"offer_id": offer, "name": program.get("name"), "status": "BLOCKED_TRAFFIC_SOURCE_OPTION", "count": await option.count()}
    await option.click()
    await page.wait_for_timeout(1000)
    dialog = page.locator('[role="dialog"]:visible')
    if await dialog.count() != 1:
        return {"offer_id": offer, "name": program.get("name"), "status": "BLOCKED_LINK_DIALOG", "count": await dialog.count()}
    text = await dialog.inner_text()
    links = re.findall(r"https://track\.revoffers\.com/[^\s]+", text)
    valid = [link.rstrip(".,)") for link in links if f"offer_id={offer}" in link and f"aff_id={AFF_ID}" in link]
    copy_button = dialog.get_by_role("button", name="Copy Link", exact=True)
    if len(valid) != 1 or await copy_button.count() != 1 or await copy_button.is_disabled():
        return {
            "offer_id": offer, "name": program.get("name"), "status": "BLOCKED_GENERATED_LINK",
            "matching_links": len(valid), "copy_button_count": await copy_button.count(),
        }
    return {
        "offer_id": offer,
        "name": program.get("name"),
        "status": "GENERATED_VERIFIED",
        "tracking_link": valid[0],
        "traffic_source_id": AFF_ID,
        "landing_page": "Default landing page",
    }


def write_progress(out: Path, records: list[dict[str, Any]], status: str = "RUNNING") -> None:
    payload = {
        "status": status,
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "traffic_source": TRAFFIC_SOURCE,
        "traffic_source_id": AFF_ID,
        "programs": records,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n")
    os.chmod(out, 0o600)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--classification", required=True)
    ap.add_argument("--applications", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--cdp", default="http://127.0.0.1:9324")
    args = ap.parse_args()
    classification = json.loads(Path(args.classification).read_text())
    applications = json.loads(Path(args.applications).read_text())
    app_by_id = {int(row["offer_id"]): row for row in applications.get("programs", []) if row.get("offer_id") is not None}
    targets = [row for row in classification.get("programs", []) if row.get("decision") in {"APPLY", "ALREADY_ACTIVE"}]
    out = Path(args.output)
    previous = json.loads(out.read_text()) if out.exists() else {}
    prior = {int(row["offer_id"]): row for row in previous.get("programs", []) if row.get("offer_id") is not None}

    by_offer: dict[int, dict[str, Any]] = {}
    pending: list[dict[str, Any]] = []
    for program in targets:
        offer = int(program["offer_id"])
        if not approved(program, app_by_id.get(offer)):
            by_offer[offer] = {"offer_id": offer, "name": program.get("name"), "status": "NOT_APPROVED_NO_LINK"}
            continue
        old = prior.get(offer)
        if old and old.get("status") == "GENERATED_VERIFIED" and old.get("tracking_link"):
            by_offer[offer] = old
            continue
        pending.append(program)

    # A verified provider-issued link is durable. Do not start Playwright on
    # idempotent retries when every target already has a verified receipt.
    if pending:
        async with async_playwright() as pw:
            browser = None
            for program in pending:
                offer = int(program["offer_id"])
                page = None
                try:
                    if browser is None or not browser.is_connected():
                        browser = await pw.chromium.connect_over_cdp(args.cdp, timeout=30000)
                        if not browser.contexts:
                            raise RuntimeError("cdp_has_no_context")
                    page = await browser.contexts[0].new_page()
                    by_offer[offer] = await generate_one(page, program)
                except Exception as exc:
                    by_offer[offer] = {
                        "offer_id": offer,
                        "name": program.get("name"),
                        "status": "FAILED",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                    browser = None
                finally:
                    if page is not None:
                        with contextlib.suppress(Exception):
                            await page.close()
                write_progress(out, [by_offer[int(row["offer_id"])] for row in targets if int(row["offer_id"]) in by_offer])

    records = [by_offer[int(row["offer_id"])] for row in targets]
    blockers = [row for row in records if row["status"].startswith("BLOCKED") or row["status"] == "FAILED"]
    payload = {
        "schema": "glp.katalys_generated_tracking_links.v1",
        "status": "PASS" if records and not blockers else "BLOCKED",
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "traffic_source": TRAFFIC_SOURCE,
        "traffic_source_id": AFF_ID,
        "approved_link_count": sum(1 for row in records if row["status"] == "GENERATED_VERIFIED"),
        "pending_no_link_count": sum(1 for row in records if row["status"] == "NOT_APPROVED_NO_LINK"),
        "blocker_count": len(blockers),
        "programs": records,
        "effects": {"tracking_clicks": 0, "deploys": 0, "site_writes": 0},
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n")
    os.chmod(out, 0o600)
    print(json.dumps({k: payload[k] for k in ("status", "approved_link_count", "pending_no_link_count", "blocker_count")}, indent=2))
    return 0 if payload["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
