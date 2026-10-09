#!/usr/bin/env python3
"""Trustworthy first-party price verification for GLP Clinic Scout.

The checker uses a desktop Playwright browser first, follows redirects, performs the
provider-specific Weight Loss tab interaction for Synergy RX, then falls back to a
desktop requests fetch and Firecrawl only when needed. Every review page gets a row in
the report, including pages with no numeric price claim.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import html
import json
import os
import re
import stat
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

try:
    from playwright.sync_api import sync_playwright
except Exception:  # pragma: no cover - environment fallback is exercised at runtime
    sync_playwright = None

DEFAULT_RELEASE = Path(
    os.environ.get(
        "GLP_PRICE_RELEASE",
        "/home/ubuntu/business/site-system/flagship-v2/candidates/glp-price-corrected-20260804",
    )
)
RELEASE = DEFAULT_RELEASE
LEDGER = RELEASE / "data/program-content-ledger.json"
CONTRACTS = RELEASE / "data/provider-page-contracts.json"
REVIEWS = RELEASE / "public/reviews"
REPORT_ROOT = Path("/home/ubuntu/business/reports/glp-clinic-scout/finish-line-20260804/price-verification")
STATE = REPORT_ROOT / "decision-card-state.json"
ENV = Path("/home/ubuntu/.hermes/.env")
DISCORD_CHANNEL = "1487300132054765629"
FIRECRAWL_URL = "https://api.firecrawl.dev/v2/scrape"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

# The previous published Synergy mapping is retained only as a correction audit.
# It is never used as the current expected value after the ledger correction.
PRIOR_PUBLISHED_CLAIMS = {
    "synergy-rx": {
        "Compounded Semaglutide": 199,
        "Compounded Tirzepatide": 349,
        "Compounded Semaglutide Oral Dissolving Tablet": 299,
        "Compounded Tirzepatide Oral Dissolving Tablet": 399,
        "Wegovy": 947,
        "Mounjaro": 947,
        "Ozempic": 499,
    }
}


def utc() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha(value: bytes | str | dict[str, Any]) -> str:
    if isinstance(value, dict):
        value = json.dumps(value, sort_keys=True, separators=(",", ":"))
    if isinstance(value, str):
        value = value.encode()
    return hashlib.sha256(value).hexdigest()


def env_value(name: str) -> str:
    for raw in ENV.read_text().splitlines():
        if raw.startswith(name + "="):
            value = raw.split("=", 1)[1].strip().strip('"').strip("'")
            if value:
                return value
    raise RuntimeError(f"missing_env:{name}")


def read_discord_token() -> str:
    info = ENV.stat()
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
        raise RuntimeError("insecure_env_file")
    return env_value("DISCORD_BOT_TOKEN")


def price_numbers(text: str) -> list[int]:
    return sorted({int(value.replace(",", "")) for value in re.findall(r"\$\s*([0-9]{2,4}(?:,[0-9]{3})?)", text)})


def visible_text(raw_html: str) -> str:
    soup = BeautifulSoup(raw_html, "html.parser")
    for node in soup(["script", "style", "template", "noscript"]):
        node.decompose()
    return "\n".join(line.strip() for line in soup.get_text("\n").splitlines() if line.strip())


def normalize_label(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("®", "")).strip()


def rendered_weight_loss_claims(text: str) -> dict[str, int]:
    lines = [normalize_label(line) for line in text.splitlines() if normalize_label(line)]
    claims: dict[str, int] = {}
    for index, line in enumerate(lines[:-1]):
        match = re.fullmatch(r"Starting at \$([0-9]{2,4}(?:,[0-9]{3})?)", line, re.I)
        if not match:
            continue
        label = normalize_label(lines[index + 1])
        if label and label not in {"GET STARTED", "Get Started"}:
            claims[label] = int(match.group(1).replace(",", ""))
    return claims


def source_urls_for_contract(contract: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    for field, values in (contract.get("field_sources") or {}).items():
        if not any(token in field.lower() for token in ("price", "pricing", "inclusion", "lab_cost")):
            continue
        for value in values:
            if value not in urls:
                urls.append(value)
    return urls or ([contract.get("official_url")] if contract.get("official_url") else [])


def expected_prices(text: str) -> list[int]:
    return price_numbers(text)


def review_pages() -> dict[str, dict[str, Any]]:
    pages: dict[str, dict[str, Any]] = {}
    for path in sorted(REVIEWS.glob("*/index.html")):
        raw = path.read_text(errors="replace")
        soup = BeautifulSoup(raw, "html.parser")
        heading = soup.find("h1")
        display_name = normalize_label(heading.get_text(" ", strip=True)) if heading else path.parent.name
        pages[path.parent.name] = {
            "review_slug": path.parent.name,
            "review_path": f"/reviews/{path.parent.name}/",
            "display_name": display_name,
            "html_sha256": sha(raw),
        }
    return pages


def build_inventory() -> dict[str, dict[str, Any]]:
    inventory: dict[str, dict[str, Any]] = {}
    ledger = json.loads(LEDGER.read_text())
    for program in ledger.get("programs", []):
        program_id = program.get("program_id")
        if not program_id:
            continue
        inventory[program_id] = {
            "program_id": program_id,
            "display_name": program.get("display_name"),
            "published_claim": program.get("starting_price") or "",
            "source_urls": [program["source_url"]] if program.get("source_url") else [],
            "published_price_claims": {normalize_label(k): int(v) for k, v in (program.get("price_claims") or {}).items()},
            "published_prices": expected_prices(str(program.get("starting_price") or "")),
        }
    contracts = json.loads(CONTRACTS.read_text())
    for contract in contracts.get("providers", []):
        program_id = contract.get("program_id")
        if not program_id:
            continue
        row = contract.get("row_contract") or {}
        claim = str(row.get("starting_price") or "")
        inventory[program_id] = {
            "program_id": program_id,
            "display_name": contract.get("display_name"),
            "published_claim": claim,
            "source_urls": source_urls_for_contract(contract),
            "published_price_claims": {},
            "published_prices": expected_prices(claim),
        }
    return inventory


def browser_fetch(browser: Any, url: str, program_id: str) -> dict[str, Any]:
    page = browser.new_page(
        user_agent=UA,
        viewport={"width": 1440, "height": 1000},
        locale="en-US",
        timezone_id="America/Los_Angeles",
    )
    try:
        response = page.goto(url, wait_until="domcontentloaded", timeout=90000)
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass
        if program_id == "synergy-rx":
            button = page.locator("button").filter(has_text="WEIGHT LOSS").first
            button.click(timeout=30000)
            page.wait_for_timeout(1200)
        text = page.locator("body").inner_text(timeout=30000)
        return {
            "method": "playwright_desktop",
            "requested_url": url,
            "final_url": page.url,
            "http_status": response.status if response else None,
            "bytes": len(text.encode()),
            "text_sha256": sha(text),
            "text": text,
            "interaction": "click Weight Loss tab" if program_id == "synergy-rx" else None,
        }
    finally:
        page.close()


def requests_fetch(url: str) -> dict[str, Any]:
    response = requests.get(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
        allow_redirects=True,
        timeout=60,
    )
    text = visible_text(response.text)
    return {
        "method": "requests_desktop_follow_redirects",
        "requested_url": url,
        "final_url": response.url,
        "http_status": response.status_code,
        "bytes": len(response.content),
        "text_sha256": sha(text),
        "text": text,
        "interaction": None,
    }


def firecrawl_fetch(url: str, api_key: str, proxy: str = 'basic') -> dict[str, Any]:
    from firecrawl_client import firecrawl_post
    response = firecrawl_post(
        FIRECRAWL_URL,
        caller='glp_price_verification',
        headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
        json={"url": url, "formats": ["markdown"], "onlyMainContent": False, "proxy": proxy},
        timeout=180,
    )
    try:
        body = response.json()
    except Exception:
        body = {}
    result = body.get("data") or body.get("result") or body
    text = result.get("markdown") if isinstance(result, dict) else ""
    text = text if isinstance(text, str) else ""
    return {
        "method": f"firecrawl_rendered_{proxy}_fallback",
        "requested_url": url,
        "final_url": (result.get("metadata") or {}).get("url", url) if isinstance(result, dict) else url,
        "http_status": response.status_code,
        "bytes": len(response.content),
        "text_sha256": sha(text),
        "text": text,
        "interaction": None,
    }


def fetch_source(browser: Any, url: str, program_id: str, firecrawl_key: str | None) -> dict[str, Any]:
    errors: list[str] = []
    for attempt, fetcher in enumerate((
        lambda: browser_fetch(browser, url, program_id) if browser is not None else (_ for _ in ()).throw(RuntimeError("playwright_unavailable")),
        lambda: requests_fetch(url),
        lambda: firecrawl_fetch(url, firecrawl_key, 'basic') if firecrawl_key else (_ for _ in ()).throw(RuntimeError("firecrawl_key_unavailable")),
        lambda: firecrawl_fetch(url, firecrawl_key, 'enhanced') if firecrawl_key else (_ for _ in ()).throw(RuntimeError("firecrawl_key_unavailable")),
    )):
        try:
            result = fetcher()
            if result.get("http_status") and 200 <= int(result["http_status"]) < 400 and result.get("text"):
                return result
            errors.append(f"{result.get('method')}:{result.get('http_status')}")
        except Exception as exc:
            errors.append(type(exc).__name__)
            # A dispatched Firecrawl effect with no result is uncertain. Only
            # an explicit completed response permits basic-to-enhanced fallback.
            if attempt >= 2:
                break
    return {
        "method": "all_fetch_methods_failed",
        "requested_url": url,
        "final_url": None,
        "http_status": None,
        "bytes": 0,
        "text_sha256": None,
        "text": "",
        "interaction": None,
        "errors": errors,
    }


def discord_request(method: str, url: str, token: str, body: dict[str, Any] | None = None) -> requests.Response:
    headers = {"Authorization": "Bot " + token, "User-Agent": "GLPClinicScout-PriceVerification/2.0", "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    return requests.request(method, url, headers=headers, json=body, timeout=30)


def upsert_decision_card(rows: list[dict[str, Any]], receipt_path: Path) -> dict[str, Any]:
    unresolved = [row for row in rows if row.get("verdict") in {"confirmed-drift", "source-unreachable"} and not row.get("corrected_in_current_publish")]
    historical = [row for row in rows if row.get("verdict") == "confirmed-drift" and row.get("corrected_in_current_publish")]
    false_positive = [row for row in rows if row.get("verdict") == "false-positive"]
    status = "🔴 UNRESOLVED GLP PRICE DRIFT" if unresolved else "🟢 GLP PRICE CHECK RESOLVED"
    lines = [status, f"Review pages checked: {len(rows)}", f"Unresolved: {len(unresolved)} | corrected historical drift: {len(historical)} | false positives corrected: {len(false_positive)}", ""]
    for row in unresolved + historical + false_positive:
        if row.get("verdict") == "confirmed-drift":
            detail = f"published {row.get('published_prices')} / found {row.get('found_prices')}"
        elif row.get("verdict") == "false-positive":
            detail = f"browser found {row.get('found_prices')}"
        else:
            detail = row.get("verdict", "")
        lines.append(f"• **{row['display_name']}** — {row['verdict']}: {detail}; source {row.get('source_urls_fetched', [None])[0]}")
    lines += ["", f"Receipt: `{receipt_path}`", "secret_printed=false"]
    content = "\n".join(lines)[:1900]
    token = read_discord_token()
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    if state.get("channel_id") != DISCORD_CHANNEL:
        state = {}
    base = "https://discord.com/api/v10/channels/" + DISCORD_CHANNEL + "/messages"
    payload = {"content": content}
    if state.get("message_id"):
        response = discord_request("PATCH", base + "/" + str(state["message_id"]), token, payload)
        action = "patched"
    else:
        response = discord_request("POST", base, token, payload)
        action = "created"
    if response.status_code not in (200, 201):
        return {"status": "BLOCKED_DISCORD_CARD_WRITE", "http_status": response.status_code, "action": action}
    body = response.json()
    message_id = str(body.get("id") or state.get("message_id") or "")
    if not message_id:
        return {"status": "BLOCKED_DISCORD_CARD_READBACK", "http_status": response.status_code, "action": action}
    REPORT_ROOT.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"message_id": message_id, "channel_id": DISCORD_CHANNEL, "content_sha256": sha(content)}, indent=2) + "\n")
    STATE.chmod(0o600)
    readback = discord_request("GET", base + "/" + message_id, token)
    return {
        "status": "PASS" if readback.status_code == 200 else "BLOCKED_DISCORD_CARD_READBACK",
        "action": action,
        "http_status": response.status_code,
        "message_id": message_id,
        "readback_http_status": readback.status_code,
        "channel_id": DISCORD_CHANNEL,
        "content_sha256": sha(content),
    }


def verify_record(record: dict[str, Any], browser: Any, firecrawl_key: str | None) -> dict[str, Any]:
    expected = set(record.get("published_prices") or [])
    expected_claims = record.get("published_price_claims") or {}
    evidence: list[dict[str, Any]] = []
    combined_text: list[str] = []
    for url in record.get("source_urls") or []:
        fetched = fetch_source(browser, url, record["program_id"], firecrawl_key)
        text = fetched.pop("text", "")
        fetched["text_present"] = bool(text)
        fetched["found_prices"] = price_numbers(text)
        fetched["found_price_claims"] = rendered_weight_loss_claims(text) if record["program_id"] == "synergy-rx" else {}
        evidence.append(fetched)
        if fetched.get("http_status") and text is not None:
            combined_text.append(text)
    reachable = [item for item in evidence if item.get("http_status") and 200 <= int(item["http_status"]) < 400 and item.get("text_present")]
    if record["program_id"] == "synergy-rx":
        reachable = [item for item in reachable if item.get("interaction") == "click Weight Loss tab"]
    found = sorted(set(price for item in evidence for price in item.get("found_prices", [])))
    found_claims: dict[str, int] = {}
    for item in evidence:
        found_claims.update(item.get("found_price_claims") or {})
    missing_prices = sorted(expected - set(found))
    missing_claims = {key: value for key, value in expected_claims.items() if found_claims.get(key) != value}
    if record["program_id"] == "synergy-rx":
        # A label-to-price comparison is authoritative for a dynamic product tab;
        # numeric set presence alone would miss a product-price swap.
        missing_prices = []
    if not evidence or not reachable:
        verdict = "source-unreachable"
    elif missing_prices or missing_claims:
        verdict = "confirmed-drift"
    elif record["program_id"] == "enhance-md":
        verdict = "false-positive"
    else:
        verdict = "no-drift"
    corrected_current = False
    if record["program_id"] == "synergy-rx" and PRIOR_PUBLISHED_CLAIMS["synergy-rx"] != expected_claims:
        prior_missing = {key: value for key, value in PRIOR_PUBLISHED_CLAIMS["synergy-rx"].items() if found_claims.get(key) != value}
        corrected_current = bool(prior_missing) and not missing_claims
        if corrected_current:
            verdict = "confirmed-drift"
    return {
        "program_id": record["program_id"],
        "display_name": record["display_name"],
        "published_claim": record.get("published_claim"),
        "published_prices": sorted(expected),
        "published_price_claims": expected_claims,
        "found_prices": found,
        "found_price_claims": found_claims,
        "missing_prices": missing_prices,
        "missing_price_claims": missing_claims,
        "source_urls_fetched": record.get("source_urls") or [],
        "requested_source_urls": [item.get("requested_url") for item in evidence],
        "final_source_urls": [item.get("final_url") for item in evidence],
        "evidence": evidence,
        "verdict": verdict,
        "corrected_in_current_publish": corrected_current,
        "prior_published_claims": PRIOR_PUBLISHED_CLAIMS.get(record["program_id"]),
        "checked_at_utc": utc(),
    }


def main() -> int:
    if not LEDGER.is_file() or not CONTRACTS.is_file() or not REVIEWS.is_dir():
        raise RuntimeError("price_verification_inputs_missing")
    pages = review_pages()
    inventory = build_inventory()
    records: list[dict[str, Any]] = []
    browser_context = sync_playwright() if sync_playwright is not None else None
    playwright_runtime = None
    browser = None
    firecrawl_key = None
    try:
        if browser_context is not None:
            playwright_runtime = browser_context.start()
            browser = playwright_runtime.chromium.launch(headless=True)
        try:
            firecrawl_key = env_value("FIRECRAWL_API_KEY")
        except Exception:
            firecrawl_key = None
        for slug, page in pages.items():
            match = next((item for item in inventory.values() if item["program_id"] == slug), None)
            if match is None:
                records.append({
                    "program_id": slug,
                    "display_name": page["display_name"],
                    "published_claim": None,
                    "published_prices": [],
                    "published_price_claims": {},
                    "found_prices": [],
                    "found_price_claims": {},
                    "missing_prices": [],
                    "missing_price_claims": {},
                    "source_urls_fetched": [],
                    "requested_source_urls": [],
                    "final_source_urls": [],
                    "evidence": [],
                    "verdict": "not-applicable-no-numeric-claim",
                    "corrected_in_current_publish": False,
                    "prior_published_claims": None,
                    "checked_at_utc": utc(),
                    "review_path": page["review_path"],
                    "review_html_sha256": page["html_sha256"],
                })
                continue
            result = verify_record(match, browser, firecrawl_key)
            result.update({"review_path": page["review_path"], "review_html_sha256": page["html_sha256"]})
            records.append(result)
    finally:
        if browser is not None:
            browser.close()
        if playwright_runtime is not None:
            playwright_runtime.stop()
    enhance_rows = [row for row in records if row.get("program_id") == "enhance-md"]
    if len(enhance_rows) != 1 or not {112, 212}.issubset(set(enhance_rows[0].get("found_prices") or [])) or enhance_rows[0].get("verdict") != "false-positive":
        raise RuntimeError("enhance_md_regression_failed")
    unresolved = [row for row in records if row["verdict"] in {"confirmed-drift", "source-unreachable"} and not row.get("corrected_in_current_publish")]
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    REPORT_ROOT.mkdir(parents=True, exist_ok=True)
    receipt_path = REPORT_ROOT / f"{stamp}.json"
    payload: dict[str, Any] = {
        "schema": "glp-price-verification-v2",
        "captured_at_utc": utc(),
        "release_root": str(RELEASE),
        "status": "PASS" if not unresolved else "DRIFT",
        "review_pages_checked": len(records),
        "numeric_price_reviews": sum(bool(row.get("published_prices")) for row in records),
        "non_numeric_price_reviews": sum(row["verdict"] == "not-applicable-no-numeric-claim" for row in records),
        "unresolved_count": len(unresolved),
        "rows": records,
        "decision_card": None,
        "secret_printed": False,
    }
    receipt_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    receipt_path.chmod(0o600)
    try:
        card = upsert_decision_card(records, receipt_path)
    except Exception as exc:
        card = {"status": "BLOCKED_DISCORD_CARD_WRITE", "error": type(exc).__name__}
    payload["decision_card"] = card
    receipt_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    receipt_path.chmod(0o600)
    output = {
        "status": payload["status"] if card.get("status") == "PASS" else "CARD_WRITE_BLOCKED",
        "review_pages_checked": len(records),
        "numeric_price_reviews": payload["numeric_price_reviews"],
        "unresolved_count": len(unresolved),
        "receipt": str(receipt_path),
        "decision_message_id": card.get("message_id"),
    }
    print(json.dumps(output, sort_keys=True))
    return 0 if payload["status"] == "PASS" and card.get("status") == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
