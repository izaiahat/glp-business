#!/usr/bin/env python3
"""G6-R1a Stage 5 — reusable source fetcher for program-deep-research-v1.

Fetches the public provider pages that carry the six research fields
(first_month_price, states_served, consultation_fee, cancellation,
pharmacy_type, legitscript) and stores the raw markdown under a per-program
capture directory. It NEVER touches a tracking/affiliate redirect URL, submits
nothing, and creates no accounts.

TWO ROUTES, IN ORDER
--------------------
1. **Firecrawl** — fast, cheap, and the default.
2. **A real headful browser** (`scripts/glp_headful_capture.py`) — the fallback,
   used per-URL whenever Firecrawl cannot answer: no key, HTTP 402 (credits
   exhausted), any other HTTP error, a connection error, or a body so thin it
   is really a bot wall.

The fallback exists because a single route is a single point of failure and
this one failed twice in one week: Firecrawl sat at 402 while `emed.com`
answered every non-browser fetch with HTTP 429 behind a Vercel Security
Checkpoint. A GLP-fit approved provider went un-researched for weeks over a bot
check on a public marketing page. Every capture now records `capture_route`, so
which route produced a quote is itself evidence.

The authoring step (turning captures into program-deep-research-v1 artifacts)
is deliberately separate: values must be quoted from the captured text, so the
runner produces evidence, not claims.

PER-CYCLE FETCH RECEIPTS (2026-08-20, Blocker B)
------------------------------------------------
Every fetch now also writes a receipt — cycle id, timestamp, route, HTTP status,
a hash of the request we made (header NAMES only, plus a per-request nonce) and
a hash of the response evidence the network returned. They are collected in
`<out>/fetch-receipts.json`.

This exists because the clean-room guard could not tell *reused a prior
artifact* from *re-fetched an unchanged page*: an honest second capture of a
stable marketing page is byte-identical to the first, and the guard read that
identity as reuse. Bytes cannot separate those two; a receipt of the fetch can.
The receipt asserts only that the fetch happened in this cycle — it says
nothing about whether the bytes are new, because unchanged pages are the
expected steady state. See `scripts/glp_capture_fetch_receipts.py`.

Usage:
  python3 scripts/g6_r1a_stage5_research_fetch.py --spec <spec.json> --out <dir>
                                                  [--cycle-id <id>]

spec.json = {"programs": [{"program_id": "...", "display_name": "...",
                           "urls": ["https://...", ...]}]}
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

BUSINESS = Path("/home/ubuntu/business")
sys.path.insert(0, str(BUSINESS / "scripts"))
from glp_price_verification import env_value  # noqa: E402
import glp_headful_capture as headful  # noqa: E402
import glp_capture_fetch_receipts as fetch_receipts  # noqa: E402

FIRECRAWL_URL = "https://api.firecrawl.dev/v2/scrape"
FORBIDDEN_URL_MARKERS = ("awin1.com", "awclick", "cread.php", "anrdoezrs",
                         "dpbolvw", "jdoqocy", "tkqlhce", "track.revoffers.com",
                         "aff_c", "/go/")
NOT_FOUND_MARKERS = ("this page could not be found", "just not this way",
                     "404 not found", "page not found")
MIN_CHARS = 200


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def guard_url(url: str) -> None:
    low = url.lower()
    for bad in FORBIDDEN_URL_MARKERS:
        if bad in low:
            raise RuntimeError(f"refused_tracking_url:{bad}")


def scrape(url: str, key: str, attempts: int = 3, cycle_id: str = "") -> dict[str, Any]:
    guard_url(url)
    # Firecrawl intermittently refuses the connection outright. Without a retry a
    # single blip aborts the whole run and loses every capture already fetched.
    response = None
    last_error = None
    body = {"url": url, "formats": ["markdown"], "onlyMainContent": True}
    # Minted BEFORE the request: a receipt that can be rebuilt afterwards from
    # the URL alone proves nothing.
    nonce = fetch_receipts.new_nonce()
    descriptor = fetch_receipts.request_descriptor(
        route="firecrawl", method="POST", endpoint=FIRECRAWL_URL, url=url,
        header_names=("authorization", "content-type"),   # NAMES only, never values
        body_keys=sorted(body), nonce=nonce, attempts=attempts)
    for attempt in range(attempts):
        try:
            from firecrawl_client import firecrawl_post
            response = firecrawl_post(
                FIRECRAWL_URL,
                caller='g6_r1a_stage5_research_fetch',
                headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
                json=body,
                timeout=180,
            )
            break
        except requests.exceptions.RequestException as exc:
            last_error = exc
            if attempt == attempts - 1:
                return {"url": url, "status": "connection_error", "fetched_at_utc": now(),
                        "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                        "attempts": attempts}
            time.sleep(5 * (attempt + 1))
    if response is None:  # pragma: no cover - defensive
        raise RuntimeError(f"scrape_failed:{url}:{last_error}")
    if response.status_code != 200:
        return {"url": url, "status": "http_error", "http_status": response.status_code,
                "fetched_at_utc": now()}
    markdown = ((response.json().get("data") or {}).get("markdown") or "").strip()
    low = markdown[:400].lower()
    if any(marker in low for marker in NOT_FOUND_MARKERS):
        return {"url": url, "status": "not_found", "fetched_at_utc": now(), "chars": len(markdown)}
    if len(markdown) < MIN_CHARS:
        return {"url": url, "status": "too_thin", "fetched_at_utc": now(), "chars": len(markdown)}
    fetched_at = now()
    sha = hashlib.sha256(markdown.encode()).hexdigest()
    # RESPONSE evidence: bytes the network returned during THIS fetch. Firecrawl's
    # own response headers carry per-request values (date, request id, cache
    # markers) and the envelope hash covers metadata the markdown does not — none
    # of it reconstructible from a file already on disk, which is the whole point.
    receipt = fetch_receipts.make_receipt(
        cycle_id=cycle_id, url=url, fetched_at_utc=fetched_at, content_sha256=sha,
        route="firecrawl", http_status=response.status_code, descriptor=descriptor,
        chars=len(markdown),
        response_evidence={
            "http_status": response.status_code,
            "headers": {str(k).lower(): str(v) for k, v in
                        dict(getattr(response, "headers", {}) or {}).items()},
            "envelope_sha256": hashlib.sha256(
                (response.text or "").encode("utf-8", "replace")).hexdigest(),
        })
    return {"url": url, "status": "ok", "fetched_at_utc": fetched_at, "chars": len(markdown),
            "sha256": sha, "markdown": markdown,
            "capture_route": "firecrawl", "fetch_receipt": receipt}


#: A Firecrawl outcome that the browser route may legitimately be able to beat.
#: `not_found` is NOT here: a real 404 is an answer, and re-fetching it in a
#: browser would only launder the same 404 into a different-looking receipt.
FALLBACK_STATUSES = ("http_error", "connection_error", "too_thin")


def _headful_receipt(cap: dict[str, Any], cycle_id: str) -> dict[str, Any] | None:
    """A browser capture's own fetch receipt.

    Its response evidence is the navigation status plus the RAW HTML hash, which
    is a different digest from the extracted text the capture is cited by — so
    it is evidence of the fetch, not a restatement of the file."""
    if cap.get("status") != "ok" or not cap.get("sha256"):
        return None
    nonce = fetch_receipts.new_nonce()
    descriptor = fetch_receipts.request_descriptor(
        route="headful_chrome", method="GET", endpoint="chrome://navigate",
        url=str(cap.get("url") or ""), header_names=(), body_keys=(), nonce=nonce)
    return fetch_receipts.make_receipt(
        cycle_id=cycle_id, url=str(cap.get("url") or ""),
        fetched_at_utc=str(cap.get("fetched_at_utc") or now()),
        content_sha256=str(cap.get("sha256") or ""), route="headful_chrome",
        http_status=cap.get("initial_http_status"), descriptor=descriptor,
        chars=cap.get("chars"), final_url=str(cap.get("final_url") or ""),
        response_evidence={
            "initial_http_status": cap.get("initial_http_status"),
            "html_sha256": cap.get("html_sha256"),
            "final_url": cap.get("final_url") or "",
            "chars": cap.get("chars"),
        })


def capture_program(urls: list[str], key: str | None,
                    cycle_id: str = "") -> list[dict[str, Any]]:
    """Capture every URL, escalating to the real browser only where needed.

    Firecrawl runs first when a key exists. Each URL it could not answer is
    retried through the headful browser; the browser's verdict replaces the
    Firecrawl one only when the browser actually got the page, so a fallback can
    never turn a real failure into a fake success.
    """
    if key:
        primary = [scrape(url, key, cycle_id=cycle_id) for url in urls]
    else:
        primary = [{"url": url, "status": "http_error", "fetched_at_utc": now(),
                    "error": "firecrawl_key_missing", "capture_route": "firecrawl"}
                   for url in urls]
    retry_idx = [i for i, cap in enumerate(primary)
                 if cap["status"] in FALLBACK_STATUSES]
    if not retry_idx:
        return primary
    try:
        fallback = headful.capture([primary[i]["url"] for i in retry_idx])
    except Exception as exc:  # the browser lane itself is unavailable
        for i in retry_idx:
            primary[i]["fallback"] = {
                "capture_route": "headful_chrome",
                "status": "unavailable",
                "error": f"{type(exc).__name__}: {str(exc)[:200]}",
            }
        return primary
    for i, cap in zip(retry_idx, fallback):
        first = dict(primary[i])
        first.pop("markdown", None)
        cap["firecrawl_attempt"] = first
        if cap["status"] == "ok":
            receipt = _headful_receipt(cap, cycle_id)
            if receipt:
                cap["fetch_receipt"] = receipt
            primary[i] = cap
        else:
            primary[i]["fallback"] = {k: v for k, v in cap.items()
                                      if k not in ("markdown", "html")}
    return primary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--cycle-id", default="",
                    help="authoring cycle these fetches belong to; derived from "
                         "--out when omitted, which is what the machine's own "
                         "capture root already encodes")
    ns = ap.parse_args()

    # A missing key is no longer fatal: the browser route can carry the run on
    # its own. It IS recorded, so a silently key-less run is never mistaken for
    # a healthy Firecrawl run.
    key = env_value("FIRECRAWL_API_KEY")
    spec = json.loads(Path(ns.spec).read_text())
    out = Path(ns.out)
    out.mkdir(parents=True, exist_ok=True)
    subprocess.run([sys.executable, str(BUSINESS / 'scripts/research_glp_batch2_fda_warning_provenance.py'),
                    '--spec', ns.spec, '--output', str(out / 'government-index')], check=True)

    cycle_id = (ns.cycle_id
                or os.environ.get("GLP_AUTHORING_CYCLE_ID", "")
                or fetch_receipts.cycle_id_for_capture_root(out))

    summary = []
    all_receipts: list[dict[str, Any]] = []
    for program in spec["programs"]:
        captures = capture_program(list(program["urls"]), key, cycle_id=cycle_id)
        all_receipts.extend(c["fetch_receipt"] for c in captures
                            if isinstance(c.get("fetch_receipt"), dict))
        payload = {
            "schema": "g6-r1a-stage5-capture-v1",
            "program_id": program["program_id"],
            "display_name": program["display_name"],
            "captured_at_utc": now(),
            "cycle_id": cycle_id,
            "firecrawl_key_present": bool(key),
            "capture_routes": sorted({str(c.get("capture_route") or "unknown")
                                      for c in captures}),
            "captures": captures,
            "tracking_clicks": 0,
            "forms_submitted": 0,
            "accounts_created": 0,
        }
        path = out / f"{program['program_id']}-captures.json"
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        os.chmod(path, 0o600)
        summary.append({"program_id": program["program_id"], "path": str(path),
                        "ok": sum(1 for c in captures if c["status"] == "ok"),
                        "by_route": {
                            route: sum(1 for c in captures
                                       if c["status"] == "ok"
                                       and c.get("capture_route") == route)
                            for route in ("firecrawl", "headful_chrome")},
                        "failed": [c["url"] for c in captures if c["status"] != "ok"]})
    # The clean-room guard reads this file to tell "re-fetched an unchanged page"
    # from "reused a prior artifact". Merged, not clobbered: a cycle may call this
    # step more than once, and a half-empty index re-creates the false positive.
    receipts_path = fetch_receipts.write_index(out, cycle_id, all_receipts)
    print(json.dumps({"status": "PASS", "programs": summary, "tracking_clicks": 0,
                      "cycle_id": cycle_id,
                      "fetch_receipts": {"path": str(receipts_path),
                                         "count": len(all_receipts)}},
                     indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
