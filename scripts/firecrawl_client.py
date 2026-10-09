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
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise ValueError("firecrawl_search_failed_not_empty")
    data = payload.get("data")
    if not isinstance(data, dict) or not sources or sources[0] not in data:
        raise ValueError("firecrawl_search_grouped_data_missing")
    rows = []
    for source in sources:
        if source not in data:
            continue
        group = data[source]
        if not isinstance(group, list) or any(not isinstance(row, dict) for row in group):
            raise ValueError("firecrawl_search_grouped_data_invalid")
        rows.extend(row for row in group if row.get("url"))
    return rows


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
    credit = payload.get("creditsUsed", payload.get("credits_used", meta.get("creditsUsed", meta.get("credits_used")))) if isinstance(payload, dict) else None
    if not isinstance(credit, (int, float)) or isinstance(credit, bool):
        credit = None
    ident = (payload.get("id") or payload.get("jobId") or meta.get("scrapeId") or meta.get("scrape_id")) if isinstance(payload, dict) else None
    # Provider identifiers are opaque untrusted data, not a credential-safe
    # alphabet. Hash every non-null identifier and retain correlation only.
    if ident is not None:
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
    response.firecrawl_operation_id = op
    return response


class FirecrawlClient:
    def __init__(self, caller, run_id=None, api_key=None, ledger_path=None):
        self.caller, self.run_id = caller, run_id or RUN_ID
        self.key, self.ledger_path = api_key, ledger_path

    def request(self, endpoint, payload=None, method="POST", timeout=120):
        url = endpoint if endpoint.startswith("https://") else API + endpoint.lstrip("/")
        route = _endpoint(url)
        if route == '/v2/agent' and method == 'POST':
            cap = (payload or {}).get('maxCredits')
            if not isinstance(cap, int) or isinstance(cap, bool) or cap <= 0:
                raise ValueError('agent_maxCredits_required')
        request = urllib.request.Request(url, data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Authorization": "Bearer " + (self.key or api_key()), "Content-Type": "application/json"}, method=method)
        with firecrawl_urlopen(request, caller=self.caller, run_id=self.run_id, timeout=timeout,
                              ledger_path=self.ledger_path) as response:
            return json.load(response)

    def scrape(self, payload, **kwargs):
        return self.request("scrape", payload, **kwargs)

    def search(self, payload, **kwargs):
        return self.request("search", payload, **kwargs)


class LedgerSDKClient:
    """Preserve SDK typed results while attributing each logical operation."""
    def __init__(self, client, caller, run_id=None):
        self.client, self.caller, self.run_id = client, caller, run_id or RUN_ID

    def _call(self, name, value, **options):
        endpoint = {'search':'/v2/search', 'scrape':'/v2/scrape', 'gov_search':'/v2/search/gov'}[name]
        body = {**options, ('url' if name == 'scrape' else 'query'): value}
        operation = str(uuid.uuid4())
        record_operation(self.caller, endpoint, body, outcome='started', operation_id=operation, run_id=self.run_id)
        try:
            result = getattr(self.client, name)(value, **options)
        except BaseException:
            record_operation(self.caller, endpoint, body, outcome='ambiguous_transport_error', operation_id=operation, run_id=self.run_id)
            raise
        payload = result.model_dump(by_alias=True) if hasattr(result, 'model_dump') else result if isinstance(result, dict) else {}
        if 'metadata' in payload and 'data' not in payload:
            payload = {'success':True, 'data':payload}
        record_operation(self.caller, endpoint, body, payload=payload,
                         outcome='provider_error' if payload.get('success') is False else 'success',
                         operation_id=operation, run_id=self.run_id)
        return result

    def search(self, query, **options):
        return self._call('search', query, **options)

    def scrape(self, url, **options):
        return self._call('scrape', url, **options)

    def gov_search(self, query, **options):
        return self._call('gov_search', query, **options)
