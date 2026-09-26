#!/usr/bin/env python3
"""Read-only API stage for the agent/vault Awin liveness candidate.

API readiness is not portal readiness. The agent then uses the maintained
``glp_awin_portal_login.prepare/finish`` caller with a fresh AI browser and
an explicit origin-bound vault tool call between stages. No application,
password, browser profile, scheduler, or success-receipt input exists here.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

PUBLISHER_ID = "2979387"
API_URL = f"https://api.awin.com/publishers/{PUBLISHER_ID}/programmes"
SOURCE_FILES = (
    "scripts/awin_fresh_preflight.py", "scripts/glp_awin_portal_login.py",
    "scripts/marketplace_adapters/__init__.py", "scripts/marketplace_adapters/awin.py",
    "scripts/marketplace_adapters/_base.py", "ops/awin-fresh-preflight.md",
)


def verify_source(root: Path | None = None) -> None:
    """Refuse drift; the candidate itself needs digest-bound adoption in the job prompt."""
    root = root or Path(__file__).resolve().parents[1]
    pins = json.loads((root / "ops/awin-fresh-preflight.candidate.json").read_text())["source_sha256"]
    if set(pins) != set(SOURCE_FILES):
        raise ValueError("source_pin_set_mismatch")
    for name in SOURCE_FILES:
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != pins[name]:
            raise ValueError("source_pin_mismatch")


class ReadOnlyTransport:
    """One fixed GET, no redirects; validate before the legacy adapter consumes it."""
    def __init__(self, transport=None):
        self.transport = transport
        self.responses = 0

    def request(self, method, url, *, headers=None, params=None):
        if method != "GET" or url != API_URL or params != {"relationship": "joined"}:
            raise ValueError("unexpected_api_request")
        if self.responses:
            raise ValueError("api_request_budget_exceeded")
        if self.transport is not None:
            response = self.transport.request(method, url, headers=headers, params=params)
        else:
            import requests
            from scripts.marketplace_adapters._base import Response
            # Credentials are supplied only by the maintained Awin adapter.
            raw = requests.get(url, headers=headers, params=params, timeout=30,
                               allow_redirects=False)
            response = Response(raw.status_code, raw.content, {}, raw.url)
        final = urlsplit(response.url or API_URL)
        expected = urlsplit(API_URL)
        if (final.scheme, final.netloc, final.path) != (expected.scheme, expected.netloc, expected.path):
            raise ValueError("unexpected_api_response_origin")
        if response.status != 200:
            raise ValueError("api_http_not_ok")
        rows = response.json()
        if not isinstance(rows, list) or any(
                not isinstance(row, dict) or not str(row.get("id", "")).isdigit()
                or not isinstance(row.get("name"), str) or not row["name"].strip()
                for row in rows):
            raise ValueError("api_schema_not_ready")
        self.responses += 1
        return response


def api_probe(transport=None) -> dict:
    """Use the maintained official adapter; emit only an allowlisted summary."""
    result = {"publisher_id": PUBLISHER_ID, "api_state": "UNAVAILABLE",
              "portal_state": "NOT_CHECKED", "next_stage": "fresh_ai_browser_vault",
              "provider_writes": 0, "tracking_clicks": 0}
    try:
        from scripts.marketplace_adapters import awin
        guarded = ReadOnlyTransport(transport)
        payload = awin.readback(guarded, relationships=("joined",))
        if guarded.responses == 1 and payload.get("auth_state") == "PASS":
            result["api_state"] = "PASS"
    except Exception:
        # Never serialize API bodies, token-bearing exceptions, headers or URLs.
        result["api_state"] = "UNAVAILABLE"
    return result


if __name__ == "__main__":
    try:
        verify_source()
    except Exception:
        print(json.dumps({"blocked": "reviewed_source_required"}))
        raise SystemExit(66)
    print(json.dumps(api_probe(), sort_keys=True))
