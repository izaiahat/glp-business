#!/usr/bin/env python3
"""Shared contract for every affiliate-marketplace adapter.

Design rules (operator build 2026-08-06):
  * ZERO provider I/O at import time and NO module-level `requests` import, so
    the pure link builders can be imported by the tracking-link resolver, which
    is contractually forbidden from ever fetching a URL.
  * Every network call goes through an injected `transport`. Tests pass a fake;
    production passes `RequestsTransport()`. There is no other way to reach a
    provider, which is what makes the test suite provider-free by construction.
  * Secrets are read in-process, never printed, never persisted into a receipt.
  * `apply()` raises `NotSupported` unless the network publishes a documented
    join/application mutation endpoint. Guessing a mutation endpoint is
    forbidden — the finding is the deliverable instead.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
import os
import stat
import urllib.parse
from pathlib import Path

BUSINESS = Path("/home/ubuntu/business")
SECRETS = Path.home() / ".hermes" / "secrets"
TERMS_DIR = BUSINESS / "reports/glp-affiliate-auto-reconciler/private-provider-terms"
UA = "GLPClinicScout-MarketplaceAdapter/1.0"

# Capability vocabulary used by every module's CAPABILITIES dict.
#   "api"           proven/implemented against a documented provider endpoint
#   "deterministic" constructed offline from a documented URL grammar
#   "portal"        provider exposes no API for this capability; portal lane owns it
#   "none"          capability does not exist for this network (with a finding)
CAPABILITY_STATES = {"api", "deterministic", "portal", "none"}


class NotSupported(Exception):
    """Raised when a capability has no documented provider API.

    Carries the citation payload that lands in the findings receipt, so a
    caller can record *why* rather than retry a guess.
    """

    def __init__(self, network: str, capability: str, reason: str, citations: list[str] | None = None):
        super().__init__(f"{network}:{capability}:{reason}")
        self.network = network
        self.capability = capability
        self.reason = reason
        self.citations = citations or []

    def finding(self) -> dict:
        return {
            "network": self.network,
            "capability": self.capability,
            "status": "no_documented_api",
            "reason": self.reason,
            "doc_citations": self.citations,
        }


class Response:
    """Transport-agnostic HTTP response."""

    def __init__(self, status: int, body: bytes = b"", headers: dict | None = None, url: str = ""):
        self.status = int(status)
        self.body = body if isinstance(body, bytes) else str(body).encode()
        self.headers = {str(k).lower(): v for k, v in (headers or {}).items()}
        self.url = url

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def json(self):
        return json.loads(self.text or "null")

    def excerpt(self, limit: int = 300) -> str:
        return self.text[:limit]


class RequestsTransport:
    """Production transport. `requests` is imported lazily on first call."""

    def __init__(self, timeout: int = 60):
        self.timeout = timeout
        self._session = None
        self.calls = 0

    def _sess(self):
        if self._session is None:
            import requests  # noqa: PLC0415 - lazy on purpose (see module docstring)

            self._session = requests.Session()
        return self._session

    def request(self, method: str, url: str, *, headers=None, params=None,
                data=None, json_body=None, auth=None, timeout=None) -> Response:
        self.calls += 1
        head = {"User-Agent": UA, "Accept": "application/json"}
        head.update(headers or {})
        resp = self._sess().request(
            method.upper(), url, headers=head, params=params, data=data,
            json=json_body, auth=auth, timeout=timeout or self.timeout,
        )
        return Response(resp.status_code, resp.content, dict(resp.headers), resp.url)


class RecordingTransport:
    """Test transport: routes (method, url-substring) -> Response, records calls."""

    def __init__(self, routes: list[tuple[str, str, Response]] | None = None):
        self.routes = list(routes or [])
        self.calls: list[dict] = []

    def add(self, method: str, url_contains: str, response: Response) -> "RecordingTransport":
        self.routes.append((method.upper(), url_contains, response))
        return self

    def request(self, method: str, url: str, *, headers=None, params=None,
                data=None, json_body=None, auth=None, timeout=None) -> Response:
        self.calls.append({"method": method.upper(), "url": url, "params": params,
                           "data": data, "json": json_body,
                           "header_names": sorted((headers or {}).keys())})
        for m, frag, resp in self.routes:
            if m == method.upper() and frag in url:
                return resp
        return Response(404, b'{"error":"no route in RecordingTransport"}')


def json_response(payload, status: int = 200) -> Response:
    return Response(status, json.dumps(payload).encode())


# ---------------------------------------------------------------- secrets/io

def secret_text(*parts: str) -> str:
    path = SECRETS.joinpath(*parts)
    return path.read_text(encoding="utf-8").strip()


def secret_json(*parts: str) -> dict:
    return json.loads(SECRETS.joinpath(*parts).read_text(encoding="utf-8"))


def env_values() -> dict:
    """Parse ~/.hermes/.env into a name->value map (values never logged)."""
    out: dict[str, str] = {}
    path = Path.home() / ".hermes" / ".env"
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" in raw and not raw.lstrip().startswith("#"):
            k, v = raw.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def utc() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d")


def write_private(path: Path, payload: dict) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n"
    path.write_text(body)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return hashlib.sha256(body.encode()).hexdigest()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ------------------------------------------------------------------- rubric

def _rubric():
    spec = importlib.util.spec_from_file_location(
        "affiliate_blitz_terms_rubric_20260802",
        BUSINESS / "scripts/affiliate_blitz_terms_rubric_20260802.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def screen(text: str) -> dict:
    """Run the canonical seven-trigger policy used at every consent boundary."""
    from scripts.affiliate_contract_policy import (  # noqa: PLC0415
        blocked_account_action_hits,
        genuine_legal_prohibition_hits,
        mechanical_hard_trigger_hits,
        normalize_text,
        sha256_text,
    )

    normalized = normalize_text(text)
    triggers = mechanical_hard_trigger_hits(normalized)
    legal = genuine_legal_prohibition_hits(normalized)
    account_actions = blocked_account_action_hits(normalized)
    return {
        "schema": "affiliate_contract_policy_screen_v2",
        "text_sha256": sha256_text(normalized),
        "normalized_chars": len(normalized),
        "hard_trigger_count": len(triggers),
        "hard_triggers": triggers,
        "legal_prohibition_hits": legal,
        "blocked_account_action_hits": account_actions,
        "verdict": "HOLD" if triggers or legal or account_actions else "PASS",
    }


def save_terms(network: str, program_id: str, text: str, sources: list[dict],
               extra: dict | None = None, terms_dir: Path | None = None) -> dict:
    """Persist a terms capture 0600 and screen it through the standing rubric.

    Path contract (operator 2026-08-06):
        reports/glp-affiliate-auto-reconciler/private-provider-terms/<net>-<id>-<date>.json
    """
    verdict = screen(text)
    payload = {
        "schema": "marketplace-adapter-terms-capture-v1",
        "network": network,
        "program_id": str(program_id),
        "captured_at_utc": utc(),
        "sources": sources,
        "terms_text": text,
        "normalized_chars": verdict["normalized_chars"],
        "text_sha256": verdict["text_sha256"],
        "rubric_screen": verdict,
        "tracking_clicks": 0,
        "provider_mutations": 0,
        "secret_printed": False,
    }
    payload.update(extra or {})
    out = (terms_dir or TERMS_DIR) / f"{network}-{program_id}-{today()}.json"
    receipt_sha = write_private(out, payload)
    return {"network": network, "program_id": str(program_id), "path": str(out),
            "receipt_sha256": receipt_sha, "text_sha256": verdict["text_sha256"],
            "verdict": verdict["verdict"], "hard_trigger_count": verdict["hard_trigger_count"],
            "hard_triggers": verdict["hard_triggers"], "normalized_chars": verdict["normalized_chars"]}


# ------------------------------------------------------------- link helpers

def qs(params: dict) -> str:
    return urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})


def quote(value: str) -> str:
    return urllib.parse.quote(str(value), safe="")


def link_resolved(url: str, method: str) -> dict:
    return {"state": "resolved", "url": url, "method": method}


def link_lookup(command: list[str], note: str) -> dict:
    return {"state": "api_lookup_required", "command": command, "note": note}


def link_portal(portal: str, note: str) -> dict:
    return {"state": "portal_required", "portal": portal, "note": note}


def link_unsupported(note: str) -> dict:
    return {"state": "unsupported", "note": note}


def readback_payload(network: str, auth_state: str, programs: list[dict],
                     extra: dict | None = None) -> dict:
    counts: dict[str, int] = {}
    for p in programs:
        rel = str(p.get("relationship") or "unknown")
        counts[rel] = counts.get(rel, 0) + 1
    payload = {
        "schema": "affiliate-network-inventory-v1",
        "network": network,
        "captured_at_utc": utc(),
        "auth_state": auth_state,
        "relationship_counts": counts,
        "program_count": len(programs),
        "programs": programs,
        "effects": {"provider_mutations": 0, "tracking_clicks": 0},
        "secret_printed": False,
    }
    payload.update(extra or {})
    return payload
