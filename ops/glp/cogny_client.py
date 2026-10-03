#!/usr/bin/env python3
"""Cogny MCP client — the fleet's single entry point to https://app.cogny.com/mcp.

Why a client module and not inline HTTP in every lane: Cogny speaks JSON-RPC over
a Streamable-HTTP MCP endpoint that answers with either ``application/json`` or an
SSE (``text/event-stream``) body depending on the tool, and it may or may not hand
back an ``Mcp-Session-Id``. Every lane that talks to Cogny needs identical parsing,
identical retry, and identical secret handling, so it lives here once.

Secret: ``MCP_COGNY_API_KEY`` in ``~/.hermes/.env`` (mirror: ``~/.hermes/secrets/cogny/api_key``).
The value is never printed, never logged, never written to a receipt.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ENDPOINT = "https://app.cogny.com/mcp"
_ENV = Path.home() / ".hermes/.env"
_KEY_FILE = Path.home() / ".hermes/secrets/cogny/api_key"


class CognyError(RuntimeError):
    """Raised when Cogny answers with a transport or JSON-RPC error."""


def load_key() -> str:
    key = os.environ.get("MCP_COGNY_API_KEY", "").strip()
    if key:
        return key
    if _KEY_FILE.exists():
        key = _KEY_FILE.read_text(encoding="utf-8").strip()
        if key:
            return key
    if _ENV.exists():
        for line in _ENV.read_text(encoding="utf-8").splitlines():
            if line.startswith("MCP_COGNY_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise CognyError("cogny_api_key_missing")


def _parse(body: str) -> dict[str, Any]:
    """Cogny answers as raw JSON or as SSE frames; both carry one JSON-RPC object."""
    body = body.strip()
    if body.startswith("{"):
        return json.loads(body)
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            chunk = line[5:].strip()
            if chunk.startswith("{"):
                parsed = json.loads(chunk)
                if "result" in parsed or "error" in parsed:
                    return parsed
    raise CognyError("cogny_unparseable_response")


class CognyClient:
    """One Cogny session. Reused across many tool calls inside a single lane run."""

    def __init__(self, *, timeout: int = 180, retries: int = 3) -> None:
        self._key = load_key()
        self._timeout = timeout
        self._retries = retries
        self._session: str | None = None
        self._id = 0
        self.calls: list[dict[str, Any]] = []

    # -- transport ---------------------------------------------------------
    def _post(self, payload: dict[str, Any]) -> tuple[Any, str]:
        headers = {
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session:
            headers["Mcp-Session-Id"] = self._session
        request = urllib.request.Request(
            ENDPOINT, data=json.dumps(payload).encode("utf-8"), headers=headers)
        with urllib.request.urlopen(request, timeout=self._timeout) as response:
            return response.headers, response.read().decode("utf-8")

    def initialize(self) -> None:
        self._id += 1
        headers, _ = self._post({
            "jsonrpc": "2.0", "id": self._id, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "glp-seo-machine", "version": "1"}},
        })
        session = headers.get("Mcp-Session-Id") or headers.get("mcp-session-id")
        if session:
            self._session = session
        try:
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        except Exception:
            # Notification delivery is best-effort; Cogny serves tools/call without it.
            pass

    # -- tools -------------------------------------------------------------
    def list_tools(self) -> list[dict[str, Any]]:
        self._id += 1
        _, body = self._post({"jsonrpc": "2.0", "id": self._id, "method": "tools/list",
                              "params": {}})
        parsed = _parse(body)
        if "error" in parsed:
            raise CognyError(json.dumps(parsed["error"])[:300])
        return (parsed.get("result") or {}).get("tools") or []

    def call(self, tool: str, arguments: dict[str, Any] | None = None,
             *, allow_error: bool = False) -> Any:
        """Call one Cogny tool. Returns the decoded payload (JSON when Cogny sends JSON text).

        ``allow_error=True`` returns ``{"cogny_error": ...}`` instead of raising, so a
        lane can record an honest per-probe failure without losing the whole run.
        """
        last: Exception | None = None
        for attempt in range(self._retries):
            try:
                self._id += 1
                _, body = self._post({
                    "jsonrpc": "2.0", "id": self._id, "method": "tools/call",
                    "params": {"name": tool, "arguments": arguments or {}},
                })
                parsed = _parse(body)
                if "error" in parsed:
                    raise CognyError(f"{tool}: {json.dumps(parsed['error'])[:300]}")
                result = parsed.get("result") or {}
                content = result.get("content") or []
                text = content[0].get("text") if content else None
                payload: Any
                if text is None:
                    payload = result.get("structuredContent", result)
                else:
                    try:
                        payload = json.loads(text)
                    except Exception:
                        payload = text
                self.calls.append({"tool": tool, "ok": True})
                if isinstance(payload, dict) and payload.get("isError"):
                    if not allow_error:
                        raise CognyError(f"{tool}: {json.dumps(payload)[:300]}")
                return payload
            except Exception as exc:  # transport / rpc / parse
                last = exc
                if attempt + 1 < self._retries:
                    time.sleep(2 * (attempt + 1))
                    if isinstance(exc, urllib.error.HTTPError) and exc.code in (400, 401, 404):
                        # Session may have been invalidated; re-init once.
                        self._session = None
                        try:
                            self.initialize()
                        except Exception:
                            pass
        self.calls.append({"tool": tool, "ok": False, "error": str(last)[:200]})
        if allow_error:
            return {"cogny_error": str(last)[:300]}
        raise CognyError(f"{tool}: {last}")


def connect(**kwargs: Any) -> CognyClient:
    client = CognyClient(**kwargs)
    client.initialize()
    return client


# --------------------------------------------------------------------------
# Tool names
# --------------------------------------------------------------------------
# Cogny namespaces every integration suite as ``mcp__<Integration>__tool_<name>``
# while its own core tools are bare (``create_finding``, ``write_context_node``…).
# Calling the bare integration name returns JSON-RPC -32601 "Unknown tool", which
# a lane can easily mistake for "the integration is not connected". These
# constants exist so no caller ever has to guess, and so a Cogny-side rename is a
# one-line fix here instead of a hunt through every machine.

GA4 = "mcp__Google_Analytics_4__tool_"
GSC = "mcp__Google_Search_Console__tool_"
BING = "mcp__Bing_Webmaster_Tools__tool_"
ADS = "mcp__Google_Ads__tool_"
GTM = "mcp__Google_Tag_Manager__tool_"
META = "mcp__Meta_Ads__tool_"

TOOL = {
    # GSC
    "gsc_search_analytics": GSC + "get_search_analytics",
    "gsc_inspect_url": GSC + "inspect_url",
    "gsc_submit_sitemap": GSC + "submit_sitemap",
    "gsc_list_properties": GSC + "list_properties",
    # GA4
    "ga4_run_report": GA4 + "run_report",
    "ga4_realtime": GA4 + "run_realtime_report",
    # Bing
    "bing_query_stats": BING + "get_query_stats",
    "bing_page_stats": BING + "get_page_stats",
    "bing_keyword": BING + "get_keyword",
    "bing_keyword_stats": BING + "get_keyword_stats",
    "bing_related_keywords": BING + "get_related_keywords",
    "bing_submit_url": BING + "submit_url",
    "bing_submit_url_batch": BING + "submit_url_batch",
    "bing_submit_content": BING + "submit_content",
    "bing_quota": BING + "get_url_submission_quota",
    "bing_url_info": BING + "get_url_info",
    # Google Ads
    "ads_keyword_ideas": ADS + "generate_keyword_ideas",
    "ads_gaql": ADS + "execute_gaql",
    # GTM
    "gtm_privacy_audit": GTM + "audit_container_privacy",
}
