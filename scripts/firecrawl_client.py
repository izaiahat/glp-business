#!/usr/bin/env python3
"""Shared v2 transport and secret-free, append-only operation attribution.

No retry is automatic: a timeout is an ambiguous effect. creditsUsed=None means
unknown, never zero. maxAge is not a credit-saving cache. Agents/async work must
use bounded options and log subsequent status receipts with the same run_id.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from typing import Any
from pathlib import Path
import urllib.error
import urllib.request
from urllib.parse import urlsplit
import uuid

API = "https://api.firecrawl.dev/v2/"
LEDGER = Path(os.environ.get("FIRECRAWL_OPERATION_LEDGER", "/home/ubuntu/business/data/firecrawl/operations.jsonl"))
RUN_ID = os.environ.get("FIRECRAWL_RUN_ID") or str(uuid.uuid4())


def api_key():
    value = os.environ.get("FIRECRAWL_API_KEY", "").strip()
    if not value:
        home = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
        for line in (home / ".env").read_text().splitlines():
            if line.startswith("FIRECRAWL_API_KEY="):
                value = line.split("=", 1)[1].strip().strip('"\'')
                break
    if not value:
        raise RuntimeError("firecrawl_api_key_missing")
    return value


def search_rows(payload, sources=("web", "news")):
    """v2 groups results in data.web/news; don't iterate dict keys as rows."""
    if not isinstance(payload, dict) or payload.get("success") is False:
        return []
    data = payload.get("data", {})
    if not isinstance(data, dict):
        return []
    return [row for source in sources for row in (data.get(source) or [])
            if isinstance(row, dict) and row.get("url")]


def _hash(value):
    return hashlib.sha256(str(value).encode()).hexdigest()


def _endpoint(url):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != "api.firecrawl.dev" or not parsed.path.startswith("/v2/"):
        raise ValueError("firecrawl_v2_endpoint_required")
    return parsed.path


def _options(body):
    # Never retain query, prompt, schema, URLs, headers, cookies, people, or
    # arbitrary Alexandria options. Stable hashes give correlation without PII.
    result: dict[str, Any] = {"payload_sha256": _hash(json.dumps(body, sort_keys=True, default=str))}
    for name in ("limit", "timeout", "maxAge", "waitFor", "maxCredits", "onlyMainContent"):
        value = body.get(name)
        if isinstance(value, (int, float, bool)):
            result[name] = value
    if body.get("proxy") in ("basic", "enhanced", "auto"):
        result["proxy"] = body["proxy"]
    if isinstance(body.get("formats"), list):
        result["formats"] = [v if isinstance(v, str) and v in {"markdown", "html", "rawHtml", "links", "screenshot", "json", "changeTracking"}
                             else v.get("type") if isinstance(v, dict) and v.get("type") in {"json", "changeTracking", "screenshot"} else "other"
                             for v in body["formats"]]
    return result


def _receipt(payload):
    data = payload.get("data") if isinstance(payload, dict) else None
    meta = data.get("metadata", {}) if isinstance(data, dict) else {}
    if not isinstance(meta, dict):
        meta = {}
    credit = payload.get("creditsUsed", meta.get("creditsUsed")) if isinstance(payload, dict) else None
    if not isinstance(credit, (int, float)) or isinstance(credit, bool):
        credit = None
    ident = (payload.get("id") or payload.get("jobId") or meta.get("scrapeId")) if isinstance(payload, dict) else None
    # Provider receipts may contain emails or tokens; identifiers have a bounded
    # restricted shape, otherwise hash instead of writing their raw values.
    if ident is not None:
        ident = str(ident)
        if len(ident) > 100 or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in ident):
            ident = "sha256:" + _hash(ident)
    return ident, credit


def record_operation(caller, endpoint, body=None, *, payload=None, outcome, run_id=None,
                     operation_id=None, http_status=None, ledger_path=None):
    body = body or {}
    ident, credits = _receipt(payload or {})
    urls = body.get("urls") or ([body["url"]] if body.get("url") else [])
    row = {"schema": "firecrawl-operation-v1", "at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
           "caller": caller, "run": run_id or RUN_ID, "operation_id": operation_id or str(uuid.uuid4()),
           "endpoint": endpoint, "options": _options(body), "url_hash": [_hash(url) for url in urls],
           "scrape_search_id": ident, "creditsUsed": credits, "outcome": outcome, "http_status": http_status}
    path = Path(ledger_path or LEDGER)
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.fchmod(fd, 0o600)
        encoded = (json.dumps(row, sort_keys=True) + "\n").encode()
        while encoded:
            written = os.write(fd, encoded)
            encoded = encoded[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    return row


@contextlib.contextmanager
def firecrawl_urlopen(request, *, caller, timeout=120, run_id=None, ledger_path=None):
    """Preserve urllib response headers/bytes, logging exactly one network effect."""
    endpoint = _endpoint(request.full_url)
    body = json.loads(request.data or b"{}")
    op = str(uuid.uuid4())
    kwargs = dict(run_id=run_id, operation_id=op, ledger_path=ledger_path)
    record_operation(caller, endpoint, body, outcome="started", **kwargs)
    try:
        response = urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        record_operation(caller, endpoint, body, outcome="http_error", http_status=exc.code, **kwargs)
        raise
    except Exception:
        record_operation(caller, endpoint, body, outcome="ambiguous_transport_error", **kwargs)
        raise
    try:
        raw = response.read()
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeError):
            payload = {}
        outcome = "provider_error" if payload.get("success") is False else "success" if payload else "invalid_json"
        record_operation(caller, endpoint, body, payload=payload, outcome=outcome,
                         http_status=getattr(response, 'status', 200), **kwargs)
        # Buffer with original HTTP metadata for existing provenance readers.
        import io
        buffered = io.BytesIO(raw)
        buffered.headers = getattr(response, 'headers', {})
        buffered.status = getattr(response, 'status', 200)
        yield buffered
    finally:
        if hasattr(response, 'close'):
            response.close()


def firecrawl_post(url, *, caller, **kwargs):
    """requests compatibility for maintained provenance producers."""
    import requests
    endpoint = _endpoint(url)
    body = kwargs.get("json") or {}
    op = str(uuid.uuid4())
    record_operation(caller, endpoint, body, outcome="started", operation_id=op)
    try:
        response = requests.post(url, **kwargs)
    except requests.RequestException:
        record_operation(caller, endpoint, body, outcome="ambiguous_transport_error", operation_id=op)
        raise
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    outcome = "http_error" if response.status_code >= 400 else "provider_error" if payload.get("success") is False else "success" if payload else "invalid_json"
    record_operation(caller, endpoint, body, payload=payload, outcome=outcome,
                     http_status=response.status_code, operation_id=op)
    return response


class FirecrawlClient:
    def __init__(self, caller, run_id=None, api_key=None, ledger_path=None):
        self.caller, self.run_id = caller, run_id or RUN_ID
        self.key, self.ledger_path = api_key, ledger_path

    def request(self, endpoint, payload=None, method="POST", timeout=120):
        url = endpoint if endpoint.startswith("https://") else API + endpoint.lstrip("/")
        _endpoint(url)
        if endpoint.strip("/") == "agent" and method == "POST" and not (payload or {}).get("maxCredits"):
            raise ValueError("agent_maxCredits_required")
        request = urllib.request.Request(url, data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Authorization": "Bearer " + (self.key or api_key()), "Content-Type": "application/json"}, method=method)
        with firecrawl_urlopen(request, caller=self.caller, run_id=self.run_id, timeout=timeout,
                              ledger_path=self.ledger_path) as response:
            return json.load(response)

    def scrape(self, payload, **kwargs):
        return self.request("scrape", payload, **kwargs)

    def search(self, payload, **kwargs):
        return self.request("search", payload, **kwargs)
