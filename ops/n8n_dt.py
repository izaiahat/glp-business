#!/usr/bin/env python3
"""Minimal n8n-cloud datatable client (pure Python, no shell).

Every machine on this box that has to read or write an n8n datatable used to
shell out to a per-directory `mcp.sh`. That worked, but it meant each caller
re-implemented SSE parsing and error handling, and a caller without its own copy
of the script simply could not touch a table. This module is the one client.

Transport: Data Tables default to the instance's native REST API; fixed data
loops no longer spend the AI-facing n8n-MCP quota. The n8n-mcp relay remains
the workflow/execution transport and an explicit Data Table rollback mode.
Both transports preserve semantic refusals, exact natural-key reconciliation,
and 100-row cursor pagination. Credentials are read only from the approved
local stores and are never printed.


DEADLINES — defect 2026-08-20 #5, and why a socket timeout was not enough
-------------------------------------------------------------------------
Nu Image exec 330's manifest script slept **2h20m** on one dead socket inside
the per-row insert loop, with a `timeout=120` already passed to `urlopen`.

That timeout was never a bound on the call. `urlopen(timeout=)` sets the
*socket* timeout: it fires when a single `recv` waits that long with nothing
arriving. A relay that holds an SSE stream open — or a middlebox that dribbles
a byte, or a half-open connection the kernel never reaps — resets that clock on
every scrap of traffic, so `resp.read()` can block for hours without a single
socket operation ever timing out. A per-operation timeout answers "is this
socket dead?"; it never answers "has this call taken too long?"

So every call now carries THREE bounds, and the transport is driven through
`http.client` phase by phase so the client always knows whether the request was
actually delivered:

  * `CONNECT_TIMEOUT_S` — TCP+TLS establishment (≤60s, clamped).
  * `READ_TIMEOUT_S`    — one socket operation (≤60s, clamped).
  * `CALL_DEADLINE_S`   — **wall clock for the whole attempt**, re-checked
    before every chunk of the body. This is the bound the incident needed.

A call that blows its deadline RAISES `MCPTimeout`. It never sleeps forever and
it never returns as though it had succeeded.


RETRIES — bounded, and never a silent duplicate write
------------------------------------------------------
`MAX_ATTEMPTS` (3) with `BACKOFF_S` between them. Retry safety is decided by
which phase failed, which is why the phases are explicit:

  * `MCPConnectError` — the connection was never established, so the relay
    cannot have seen the request. **Always** retryable.
  * `MCPTransportError` / `MCPTimeout` — the request was (or may have been)
    delivered. Only reads retry in the transport. Every write is reconciled by
    stable natural key before any native fallback mutation is attempted.
  * HTTP 429 — the relay refused before processing, so retrying is safe for
    every action. HTTP 5xx — the relay may have processed it; idempotent only.


A REFUSAL IS AN ERROR — `{"success": false}` RAISES
----------------------------------------------------
2026-08-20 (defect #4): `route_to_error_handler` posted a case row carrying a
column the table does not have. The relay answered

    {"success": false, "error": "unknown column name 'node'", ...}

and this module returned it as an ordinary dict. The caller saw a dict, called
it queued, and the machine's red card was a lie for four hours.

`success: false` now raises `MCPRefused`. The escape hatch is explicit and
per-call: `allow_refusal=True` returns the refusal payload for the small
allowlist of callers that genuinely inspect and report it (see
`REFUSAL_INSPECTORS`). Silence is no longer reachable by accident — a caller
that wants the refusal has to say so in the call.
"""
from __future__ import annotations

import http.client
import copy
import json
import os
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

MCP_URL = os.environ.get("N8N_MCP_URL", "https://api.n8n-mcp.com/mcp")
KEY_FILE = os.path.expanduser(
    os.environ.get("N8N_MCP_KEY_FILE", "~/.hermes/secrets/n8n/mcp-key.txt"))
NATIVE_BASE = os.environ.get("N8N_NATIVE_BASE", "https://hermesops.app.n8n.cloud")
NATIVE_KEY_FILE = os.path.expanduser(os.environ.get(
    "N8N_NATIVE_KEY_FILE", "~/.hermes/secrets/n8n/native-api-key.txt"))
RATE_LIMIT_STATE_FILE = Path(os.path.expanduser(os.environ.get(
    "N8N_DT_RATE_LIMIT_STATE",
    "/home/ubuntu/business/reports/ops/n8n-mcp-rate-limit-state.json")))
RATE_LIMIT_DAILY_REMAINING_HEADER = "X-RateLimit-Daily-Remaining"
RATE_LIMIT_DAILY_RESET_HEADER = "X-RateLimit-Daily-Reset"
PAGE_LIMIT = 100

# Transport default flipped to MCP on 2026-09-15 (operator). The earlier
# "native first" default existed to conserve the AI-facing n8n-MCP quota, but
# the quota economics are the other way round: the paid n8n-MCP plan allows
# 5,000 calls/day (~150k/month) and was running under 50/day (~1%), while the
# instance's native REST allowance is 10,000/MONTH and had reached 8,000 (80%)
# with a reset on the 1st. Conserving the abundant resource by exhausting the
# scarce one is backwards, and it contradicted the standing rule: n8n-MCP is
# first-class transport, native REST is the fallback.
#
# The failover path is unchanged and still strict: only transport/provider
# unavailability falls back to native (`_mcp_error_allows_native_fallback`);
# a semantic refusal is NEVER bypassable, and an ambiguous mutation still
# reconciles on its natural key before any native retry.
#
# Rollback is this one value: set "native" here, or export
# N8N_DT_DATATABLE_TRANSPORT=native. Read per call so a rollback needs no
# re-import.
DATATABLE_TRANSPORT_DEFAULT = "mcp"

#: Hard ceiling on every configurable timeout. The defect was an unbounded wait,
#: so an env var must not be able to reintroduce one — every knob below is
#: clamped through `_bound()`, not merely defaulted.
MAX_TIMEOUT_S = 60.0


def _bound(env: str, default: float) -> float:
    try:
        value = float(os.environ.get(env, "") or default)
    except ValueError:
        value = default
    return max(1.0, min(MAX_TIMEOUT_S, value))


CONNECT_TIMEOUT_S = _bound("N8N_DT_CONNECT_TIMEOUT_S", 15.0)
READ_TIMEOUT_S = _bound("N8N_DT_READ_TIMEOUT_S", 45.0)
CALL_DEADLINE_S = _bound("N8N_DT_CALL_DEADLINE_S", 60.0)

MAX_ATTEMPTS = max(1, min(5, int(os.environ.get("N8N_DT_MAX_ATTEMPTS", "3") or 3)))
#: Backoff before attempt 2 and attempt 3. Deterministic on purpose: a machine
#: that retries has to be reasoned about from a receipt, not from a dice roll.
BACKOFF_S = (2.0, 5.0)
#: A relay answering an endless body is the same defect wearing a different hat.
MAX_RESPONSE_BYTES = 8 * 1024 * 1024

#: Actions that may be re-sent by the MCP transport after an AMBIGUOUS failure.
#: Reads are free. Writes are deliberately absent: even a convergent update must
#: reconcile the target row before another transport is allowed to touch it.
IDEMPOTENT_ACTIONS = frozenset({"getRows", "getRow", "getColumns", "listTables",
                                })

# Stable row identities for every live data table on the account. Native write
# fallback is refused for unregistered tables, missing key values, broad filters,
# and duplicate/conflicting rows. This is the safety boundary that lets every
# existing n8n_dt writer gain the fallback without caller-specific retry code.
TABLE_NATURAL_KEYS = {
    "9Gevk0AdC7J6UbFn": ("receipt",),                   # heartbeats
    "jwrG1kHGYUIcOPWG": ("case_id",),                 # error_cases
    "hJiP3eJKBvtZ6bQU": ("update_id",),               # sheet_update_queue
    "VgYinuqV8Edhn7DS": ("thread_id",),               # reddit_suggested
    "5caYqx3gfFPlp9R5": ("network", "program_id"),   # marketplace_snapshot
    "QLfr4xDk5T3aBg7f": ("item_id",),                 # action_items
    "RwYCOtvVAGiRMGo5": ("run_id", "provider", "step"),  # onboarding_manifest
    "wuDGU2Xg1LvwXuk6": ("queue_id",),                # affiliate_application_queue
    "ReulkaY4kEwCh9RY": ("machine",),                 # machine_registry
}

# Canonical field types for live tables whose REST representation is stricter
# than the legacy producer payloads.  Canonicalization happens before preflight,
# mutation, and exact readback, so this does not relax reconciliation: it makes
# the intended state match the registered table schema before any write occurs.
TABLE_FIELD_TYPES = {
    "hJiP3eJKBvtZ6bQU": {"sheet_row": "integer"},     # sheet_update_queue
}

#: The allowlist for `allow_refusal=True`: callers that read a `success: false`
#: and REPORT it rather than swallowing it. Documentation, not enforcement —
#: the flag is per-call — but a new name here is a decision somebody made on
#: purpose, and `tests/test_n8n_dt_timeouts.py` pins that these two still do
#: the reporting they claim.
REFUSAL_INSPECTORS = (
    "scripts/glp_onboarding_authoring_dispatch.py::route_to_error_handler",
    "scripts/glp_everflow_apply_queue.py::main",
)

RETRYABLE_STATUS_ANY = frozenset({429})
RETRYABLE_STATUS_IDEMPOTENT = frozenset({500, 502, 503, 504})


class MCPError(RuntimeError):
    """Anything that stopped this call from returning a relay result."""


class MCPConnectError(MCPError):
    """The connection was never established — the relay did not see this."""


class MCPTransportError(MCPError):
    """The request was, or may have been, delivered. Re-sending is a decision."""


class MCPTimeout(MCPTransportError):
    """A bound was exceeded: the per-op read timeout or the call deadline."""


class MCPRateLimitHold(MCPTransportError):
    """The shared relay quota is zero until its authoritative reset instant."""

    def __init__(self, reset_epoch: float, reset_raw: str = ""):
        self.reset_epoch = float(reset_epoch)
        self.reset_raw = str(reset_raw or "")
        super().__init__(
            "n8n MCP daily quota is zero; transport held until %s"
            % datetime.fromtimestamp(self.reset_epoch, timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"))


class MCPRefused(MCPError):
    """The relay answered `{"success": false, ...}` — the write did NOT happen."""

    def __init__(self, tool: str, payload: dict):
        self.tool = tool
        self.payload = payload
        super().__init__("MCP refused %s: %s"
                         % (tool, str(payload.get("error")
                                      or json.dumps(payload, sort_keys=True))[:300]))


class NativeSemanticRefusal(MCPError):
    """Native REST conclusively rejected a request; never switch transport."""

    def __init__(self, status: int, method: str, path: str, detail: str):
        self.status = int(status)
        self.method = str(method)
        self.path = str(path)
        super().__init__(
            "native n8n REST semantic refusal HTTP %s for %s %s: %s"
            % (self.status, self.method, self.path.split("?", 1)[0], detail[:300]))


MCP_UNAVAILABLE_CODES = frozenset({
    "AUTHENTICATION_ERROR", "UNAUTHORIZED", "RATE_LIMITED", "QUOTA_EXCEEDED",
    "SERVICE_UNAVAILABLE", "TIMEOUT",
})
MCP_UNAVAILABLE_MARKERS = (
    "failed to authenticate", "authentication error", "unauthorized", "api key",
    "quota exceeded", "daily quota", "rate limit", "service unavailable", "timeout",
)


def _refusal_allows_native_fallback(error: MCPRefused) -> bool:
    """Separate provider unavailability from conclusive semantic rejection.

    Native REST is an availability fallback, never a way around n8n's schema or
    validation rules. The current live incident is AUTHENTICATION_ERROR after n8n
    invalidated the MCP credential server-side; when MCP auth works again, normal
    MCP-first execution resumes automatically.
    """
    payload = error.payload if isinstance(error.payload, dict) else {}
    code = str(payload.get("code") or "").strip().upper()
    text = " ".join(str(payload.get(field) or "")
                    for field in ("error", "message", "code")).lower()
    if code:
        return code in MCP_UNAVAILABLE_CODES
    # Uncoded mixed/ambiguous diagnostics cannot confer fallback permission.
    # A schema rejection mentioning an "api key" column is still a rejection.
    if any(marker in text for marker in (
            "schema", "validation", "invalid", "not allowed", "not an allowed",
            "unknown column", "unknown field", "constraint", "duplicate", "conflict")):
        return False
    return text.strip(" .:") in MCP_UNAVAILABLE_MARKERS


def _mcp_error_allows_native_fallback(error: MCPError) -> bool:
    """Allow native REST only for transport/provider unavailability.

    Correlation mismatches, unparseable responses, and semantic/schema refusals
    are integrity failures. Native REST must not reinterpret those failures.
    """
    if isinstance(error, MCPRefused):
        return _refusal_allows_native_fallback(error)
    return isinstance(error, (MCPConnectError, MCPTransportError))


class WriteReconciliationError(MCPError):
    """A write could not be proven unique and exact; no blind replay is allowed."""


def _native_key() -> str:
    with open(os.path.expanduser(os.environ.get("N8N_NATIVE_KEY_FILE")
                                 or NATIVE_KEY_FILE), "r", encoding="utf-8") as fh:
        return fh.read().strip()


def _native_http(method: str, path: str, body: dict | None = None,
                 timeout_s: float | None = None) -> dict | list:
    """One bounded native n8n REST request. Writes are never retried here."""
    timeout_s = CALL_DEADLINE_S if timeout_s is None else min(CALL_DEADLINE_S, float(timeout_s))
    if not 0 < timeout_s <= CALL_DEADLINE_S:
        raise MCPTimeout("native n8n REST deadline unavailable or exhausted")
    deadline = time.monotonic() + timeout_s
    payload = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"X-N8N-API-KEY": _native_key(), "Accept": "application/json"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        NATIVE_BASE.rstrip("/") + "/api/v1" + path,
        data=payload, method=method, headers=headers)
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise MCPTimeout("native n8n REST deadline exhausted before connect")
        with urllib.request.urlopen(request, timeout=remaining) as response:
            raw = _read_bounded(response, deadline, "native_rest", timeout_s, decode=False)
    except urllib.error.HTTPError as exc:
        # Keep the known HTTP status even if its bounded diagnostic body fails.
        # A semantic 4xx must not become permission to fall back or replay a write.
        try:
            detail = _read_bounded(exc, deadline, "native_rest_error", timeout_s)[:1000]
        except MCPError as body_error:
            detail = "error body unavailable: " + type(body_error).__name__
        finally:
            exc.close()
        if 400 <= int(exc.code) < 500 and int(exc.code) not in {408, 429}:
            raise NativeSemanticRefusal(
                int(exc.code), method, path, detail) from exc
        raise MCPTransportError(
            "native n8n REST HTTP %s for %s %s: %s"
            % (exc.code, method, path.split("?", 1)[0], detail[:300])) from exc
    except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
        raise MCPTransportError(
            "native n8n REST failed for %s %s: %s: %s"
            % (method, path.split("?", 1)[0], type(exc).__name__, str(exc)[:200])) from exc
    if len(raw) > MAX_RESPONSE_BYTES:
        raise MCPTransportError("native n8n REST response exceeded %d bytes"
                                % MAX_RESPONSE_BYTES)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MCPTransportError("native n8n REST returned invalid JSON") from exc
    if not isinstance(parsed, (dict, list)):
        raise MCPTransportError("native n8n REST returned a non-container response")
    return parsed


def _datatable_native_first() -> bool:
    configured = os.environ.get("N8N_DT_DATATABLE_TRANSPORT")
    # Existing transport unit tests stand up a local MCP relay and deliberately
    # patch MCP seams. Keep that harness hermetic unless a native-route test opts
    # in explicitly; production has no PYTEST_CURRENT_TEST and defaults native.
    if configured is None and os.environ.get("PYTEST_CURRENT_TEST"):
        configured = "mcp"
    return str(configured or DATATABLE_TRANSPORT_DEFAULT).strip().casefold() != "mcp"


def _native_row_matches(row: dict, filt: dict) -> bool:
    clauses = filt.get("filters") or []
    if not clauses:
        return True
    results: list[bool] = []
    for clause in clauses:
        column = clause.get("columnName")
        condition = clause.get("condition") or "eq"
        wanted, actual = clause.get("value"), row.get(column)
        if condition == "eq":
            results.append(actual == wanted)
        elif condition == "neq":
            results.append(actual != wanted)
        elif condition == "like":
            results.append(wanted is not None and str(wanted) in str(actual or ""))
        else:
            raise MCPError("native n8n REST cannot express filter condition %r on %r"
                           % (condition, column))
    return all(results) if (filt.get("type") or "and") == "and" else any(results)


def _native_datatable_call(args: dict) -> dict:
    """MCP-shaped datatable result over explicitly selected native REST transport."""
    action = str(args.get("action") or "")
    table_id = urllib.parse.quote(str(args.get("tableId") or ""), safe="")
    if not table_id:
        raise MCPError("native n8n REST tableId missing")
    if action == "getRows":
        deadline = time.monotonic() + CALL_DEADLINE_S
        limit = max(1, int(args.get("limit") or PAGE_LIMIT))
        collected: list[dict] = []
        cursor = args.get("cursor")
        next_cursor = None
        for _ in range(50):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MCPTimeout(
                    "native data-table pagination exceeded %.0fs shared deadline"
                    % CALL_DEADLINE_S)
            page_limit = min(PAGE_LIMIT, max(1, limit - len(collected)))
            query = {"limit": str(page_limit)}
            if cursor:
                query["cursor"] = str(cursor)
            if args.get("filter"):
                query["filter"] = json.dumps(
                    args["filter"], sort_keys=True, separators=(",", ":"))
            if args.get("sortBy"):
                query["sortBy"] = str(args["sortBy"])
            if args.get("search"):
                query["search"] = str(args["search"])
            path = "/data-tables/%s/rows?%s" % (
                table_id, urllib.parse.urlencode(query))
            page = _native_http("GET", path, timeout_s=remaining)
            if not isinstance(page, dict):
                raise MCPTransportError("native n8n REST row read returned a non-object")
            for row in page.get("data") or []:
                if not args.get("filter") or _native_row_matches(row, args["filter"]):
                    collected.append(row)
            next_cursor = page.get("nextCursor")
            if not next_cursor or len(collected) >= limit:
                break
            cursor = next_cursor
        else:
            raise MCPError("native Data Table pagination exceeded 50 pages; result incomplete")
        return {"success": True, "data": {
            "rows": collected[:limit],
            "nextCursor": next_cursor,
        }}
    if action == "insertRows":
        response = _native_http(
            "POST", "/data-tables/%s/rows" % table_id,
            {"data": args.get("data") or [], "returnType": "all"})
        returned = response if isinstance(response, list) else response.get("data") or []
        return {"success": True, "data": {"rows": returned}}
    if action == "updateRows":
        response = _native_http(
            "PATCH", "/data-tables/%s/rows/update" % table_id,
            {"filter": args.get("filter") or {}, "data": args.get("data") or {},
             "returnData": True})
        returned = response if isinstance(response, list) else response.get("data") or []
        return {"success": True, "data": {"rows": returned}}
    if action == "upsertRows":
        response = _native_http(
            "POST", "/data-tables/%s/rows/upsert" % table_id,
            {"filter": args.get("filter") or {}, "data": args.get("data") or {},
             "returnData": True})
        returned = response if isinstance(response, list) else response.get("data") or []
        return {"success": True, "data": {"rows": returned}}
    raise MCPError("native n8n REST action unsupported: %r" % action)


def _response_rows(response: dict | list) -> list[dict]:
    if isinstance(response, list):
        return [row for row in response if isinstance(row, dict)]
    data = response.get("data") if isinstance(response, dict) else None
    if isinstance(data, dict):
        data = data.get("rows") or data.get("data")
    return [row for row in (data or []) if isinstance(row, dict)]


def _key_fields(table_id: str, row: dict, override=None) -> tuple[str, ...]:
    registered = tuple(TABLE_NATURAL_KEYS.get(str(table_id)) or ())
    if not registered:
        raise WriteReconciliationError(
            "native write fallback refused: table %s has no registered natural key"
            % table_id)
    if override is not None and tuple(override) != registered:
        raise WriteReconciliationError(
            "native write fallback refused: caller natural key %s does not match "
            "the registered natural key %s for table %s"
            % (tuple(override), registered, table_id))
    fields = registered
    missing = [field for field in fields
               if field not in row or row.get(field) is None or row.get(field) == ""]
    if missing:
        raise WriteReconciliationError(
            "native write fallback refused: table %s row is missing natural key field(s) %s"
            % (table_id, ",".join(missing)))
    return fields


def _key_filter(row: dict, fields: tuple[str, ...]) -> dict:
    return {"type": "and", "filters": [
        {"columnName": field, "condition": "eq", "value": row[field]}
        for field in fields
    ]}


def _canonicalize_table_row(table_id: str, row: dict) -> dict:
    """Return the schema-canonical intended row or fail before mutation.

    n8n's native REST API coerces a numeric string in a number column.  Exact
    readback must compare against that canonical type, not the producer's
    historical JSON spelling. Empty optional values remain untouched because
    they carry separate writer semantics; only present numeric values convert.
    """
    canonical = dict(row)
    for field, target in TABLE_FIELD_TYPES.get(str(table_id), {}).items():
        if field not in canonical or canonical[field] in (None, ""):
            continue
        value = canonical[field]
        if target == "integer":
            if isinstance(value, bool):
                raise WriteReconciliationError(
                    "table %s field %s requires an integer, not bool"
                    % (table_id, field))
            if isinstance(value, int):
                continue
            if isinstance(value, float) and value.is_integer():
                canonical[field] = int(value)
                continue
            if isinstance(value, str):
                try:
                    canonical[field] = int(value.strip(), 10)
                    continue
                except ValueError:
                    pass
            raise WriteReconciliationError(
                "table %s field %s requires an integer-compatible value"
                % (table_id, field))
        raise WriteReconciliationError(
            "table %s field %s has unsupported registered type %s"
            % (table_id, field, target))
    return canonical


def _filter_key_fields(table_id: str, filt: dict) -> tuple[str, ...]:
    clauses = filt.get("filters") if isinstance(filt, dict) else None
    if not clauses or (filt.get("type") or "and") != "and":
        raise WriteReconciliationError(
            "native update fallback refused: filter must be a nonempty AND filter")
    if any(clause.get("condition") != "eq" or clause.get("value") in (None, "")
           for clause in clauses):
        raise WriteReconciliationError(
            "native update fallback refused: every filter clause must be exact equality")
    fields = tuple(str(clause.get("columnName") or "") for clause in clauses)
    registered = tuple(TABLE_NATURAL_KEYS.get(str(table_id)) or ())
    if set(fields) != {"id"} and (not registered or set(fields) != set(registered)):
        raise WriteReconciliationError(
            "native update fallback refused: filter fields %s are not the registered "
            "natural key %s (or server row id) for table %s"
            % (fields, registered or "<none>", table_id))
    return fields


def _normalised_cell(value):
    """The comparable form of one datatable cell.

    The Data Table coerces on write, and a comparison that ignores that reports
    a perfectly good write as a failure. The case that bit us on 2026-09-03: a
    queue row carrying `sheet_row: ""` is stored in an integer column as `0`, so
    a strict `==` saw `0 != ""` and raised WriteReconciliationError AFTER the
    insert had landed. The caller retried and inserted a second row — a guard
    that exists to prevent duplicates causing one.

    Only representational differences are folded here. A genuinely different
    value still compares unequal.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        # An empty cell in a numeric column reads back as 0.
        return "" if value == 0 else str(value)
    text = str(value).strip()
    if text in ("0",):
        return ""
    return text


def _row_has_state(row: dict, intended: dict) -> bool:
    return all(_normalised_cell(row.get(key)) == _normalised_cell(value)
               for key, value in intended.items())


def _mcp_rows_page(table_id: str, filt: dict | None, limit: int,
                   cursor=None, sort_by=None, search=None) -> tuple[list[dict], object]:
    args = {"action": "getRows", "tableId": table_id,
            "limit": min(PAGE_LIMIT, max(1, limit))}
    if filt:
        args["filter"] = filt
    if cursor:
        args["cursor"] = cursor
    if sort_by:
        args["sortBy"] = sort_by
    if search:
        args["search"] = search
    result = call("n8n_manage_datatable", args, idempotent=True)
    data = result.get("data") if isinstance(result, dict) else {}
    next_cursor = data.get("nextCursor") if isinstance(data, dict) else None
    return _response_rows(result), next_cursor


def _mcp_rows_once(table_id: str, filt: dict, limit: int = 3) -> list[dict]:
    rows_found, _cursor = _mcp_rows_page(table_id, filt, limit)
    return rows_found


def _native_rows(table_id: str, filt: dict, limit: int = 3) -> list[dict]:
    global last_read_truncated
    args = {"action": "getRows", "tableId": table_id,
            "limit": max(1, limit)}
    if filt:
        args["filter"] = filt
    result = _native_datatable_call(args)
    last_read_truncated = bool((result.get("data") or {}).get("nextCursor"))
    return _response_rows(result)


def native_rows_for_keys(table_id: str, key_field: str, values) -> dict:
    """Read a finite natural-key set over native REST in one paginated pass.

    This is the fleet migration seam for fixed producer loops: callers prefetch
    once, dedupe locally, then issue one ``insert_many`` mutation.  The entire
    table is scanned (bounded to 50 native pages) so a duplicate natural key is
    detected rather than hidden after the first match.
    """
    table_id = str(table_id or "")
    key_field = str(key_field or "")
    wanted = set(values or ())
    if not wanted:
        return {}
    registered = tuple(TABLE_NATURAL_KEYS.get(table_id) or ())
    if registered != (key_field,):
        raise WriteReconciliationError(
            "native key prefetch requires the table's sole registered natural key; "
            "table %s has %s, requested %r" % (table_id, registered or "<none>", key_field))
    quoted = urllib.parse.quote(table_id, safe="")
    found: dict = {}
    cursor = None
    exhausted = False
    deadline = time.monotonic() + CALL_DEADLINE_S
    for _ in range(50):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise MCPTimeout(
                "native key prefetch exceeded %.0fs shared deadline for table %s"
                % (CALL_DEADLINE_S, table_id))
        path = "/data-tables/%s/rows?limit=200" % quoted
        if cursor:
            path += "&cursor=" + urllib.parse.quote(str(cursor), safe="")
        page = _native_http("GET", path, timeout_s=remaining)
        if not isinstance(page, dict):
            raise MCPTransportError("native key prefetch returned a non-object")
        for row in page.get("data") or []:
            if not isinstance(row, dict) or row.get(key_field) not in wanted:
                continue
            key = row[key_field]
            if key in found:
                raise WriteReconciliationError(
                    "native key prefetch found duplicate %s=%r in table %s"
                    % (key_field, key, table_id))
            found[key] = row
        cursor = page.get("nextCursor")
        if not cursor:
            exhausted = True
            break
    if not exhausted:
        raise WriteReconciliationError(
            "native key prefetch exceeded 50 pages for table %s" % table_id)
    return found


def native_rows_for_key_tuples(table_id: str, rows_or_keys) -> dict:
    """Read a finite set of registered natural-key tuples in one paginated scan."""
    table_id = str(table_id or "")
    fields = tuple(TABLE_NATURAL_KEYS.get(table_id) or ())
    if not fields:
        raise WriteReconciliationError(
            "native key-tuple prefetch requires a registered natural key for table %s"
            % table_id)
    wanted = set()
    for item in rows_or_keys or ():
        if isinstance(item, dict):
            _key_fields(table_id, item)
            wanted.add(tuple(item[field] for field in fields))
        else:
            value = tuple(item)
            if len(value) != len(fields):
                raise WriteReconciliationError(
                    "native key tuple length does not match %s" % (fields,))
            wanted.add(value)
    if not wanted:
        return {}
    quoted = urllib.parse.quote(table_id, safe="")
    found: dict[tuple, dict] = {}
    cursor = None
    exhausted = False
    deadline = time.monotonic() + CALL_DEADLINE_S
    for _ in range(50):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise MCPTimeout(
                "native key-tuple prefetch exceeded %.0fs for table %s"
                % (CALL_DEADLINE_S, table_id))
        path = "/data-tables/%s/rows?limit=200" % quoted
        if cursor:
            path += "&cursor=" + urllib.parse.quote(str(cursor), safe="")
        page = _native_http("GET", path, timeout_s=remaining)
        if not isinstance(page, dict):
            raise MCPTransportError("native key-tuple prefetch returned a non-object")
        for row in page.get("data") or []:
            if not isinstance(row, dict):
                continue
            key = tuple(row.get(field) for field in fields)
            if key not in wanted:
                continue
            if key in found:
                raise WriteReconciliationError(
                    "native key-tuple prefetch found duplicate %r in table %s"
                    % (key, table_id))
            found[key] = row
        cursor = page.get("nextCursor")
        if not cursor:
            exhausted = True
            break
    if not exhausted:
        raise WriteReconciliationError(
            "native key-tuple prefetch exceeded 50 pages for table %s" % table_id)
    return found


def _preflight_rows(table_id: str, filt: dict, limit: int = 3) -> list[dict]:
    """MCP-first exact read, native only when MCP is unavailable."""
    if _datatable_native_first():
        return _native_rows(table_id, filt, limit)
    try:
        return _mcp_rows_once(table_id, filt, limit)
    except MCPError as mcp_error:
        if not _mcp_error_allows_native_fallback(mcp_error):
            raise
        try:
            return _native_rows(table_id, filt, limit)
        except Exception as native_error:  # noqa: BLE001
            raise WriteReconciliationError(
                "MCP preflight failed and native readback was unavailable: %s / %s"
                % (type(mcp_error).__name__, type(native_error).__name__)) from mcp_error


def _one_exact(rows_found: list[dict], intended: dict, operation: str) -> dict:
    if len(rows_found) != 1:
        raise WriteReconciliationError(
            "%s readback expected exactly one row, found %d"
            % (operation, len(rows_found)))
    if not _row_has_state(rows_found[0], intended):
        raise WriteReconciliationError(
            "%s readback found the natural key but intended state does not match"
            % operation)
    return rows_found[0]


def _result(status: str, row: dict | None = None, *, transport: str,
            extra: dict | None = None) -> dict:
    result = {"success": True, "write_status": status, "transport": transport,
              "data": {"rows": [row] if row else []}}
    if extra:
        result.update(extra)
    return result


def _native_insert_after_reconciliation(table_id: str, row: dict,
                                        filt: dict) -> dict:
    try:
        _native_datatable_call(
            {"action": "insertRows", "tableId": table_id, "data": [row]})
    except NativeSemanticRefusal:
        raise
    except Exception as native_error:  # noqa: BLE001
        # A native timeout may also be timeout-but-landed. Read once; never replay.
        try:
            landed = _native_rows(table_id, filt, 3)
            exact = _one_exact(landed, row, "native insert timeout")
        except Exception as readback_error:  # noqa: BLE001
            raise WriteReconciliationError(
                "native insert failed and exact readback did not prove it landed: %s / %s"
                % (type(native_error).__name__, type(readback_error).__name__)) \
                from native_error
        return _result("native_timeout_landed", exact, transport="native-rest")
    exact = _one_exact(_native_rows(table_id, filt, 3), row,
                       "native insert post-write")
    return _result("native_fallback_confirmed", exact, transport="native-rest")


def _native_update_after_reconciliation(table_id: str, filt: dict,
                                        data: dict) -> dict:
    try:
        _native_datatable_call({"action": "updateRows", "tableId": table_id,
                                "filter": filt, "data": data})
    except NativeSemanticRefusal:
        raise
    except Exception as native_error:  # noqa: BLE001
        try:
            exact = _one_exact(_native_rows(table_id, filt, 3), data,
                               "native update timeout")
        except Exception as readback_error:  # noqa: BLE001
            raise WriteReconciliationError(
                "native update failed and exact readback did not prove it landed: %s / %s"
                % (type(native_error).__name__, type(readback_error).__name__)) \
                from native_error
        return _result("native_timeout_landed", exact, transport="native-rest")
    exact = _one_exact(_native_rows(table_id, filt, 3), data,
                       "native update post-write")
    return _result("native_fallback_confirmed", exact, transport="native-rest")


def _native_upsert_after_reconciliation(table_id: str, filt: dict,
                                        data: dict) -> dict:
    try:
        _native_datatable_call({"action": "upsertRows", "tableId": table_id,
                                "filter": filt, "data": data})
    except NativeSemanticRefusal:
        raise
    except Exception as native_error:  # noqa: BLE001
        try:
            exact = _one_exact(_native_rows(table_id, filt, 3), data,
                               "native upsert timeout")
        except Exception as readback_error:  # noqa: BLE001
            raise WriteReconciliationError(
                "native upsert failed and exact readback did not prove it landed: %s / %s"
                % (type(native_error).__name__, type(readback_error).__name__)) \
                from native_error
        return _result("native_timeout_landed", exact, transport="native-rest")
    exact = _one_exact(_native_rows(table_id, filt, 3), data,
                       "native upsert post-write")
    return _result("native_fallback_confirmed", exact, transport="native-rest")


def _url() -> str:
    """Read per call so a test can point this at a local server after import."""
    return os.environ.get("N8N_MCP_URL") or MCP_URL


def _key() -> str:
    with open(os.path.expanduser(os.environ.get("N8N_MCP_KEY_FILE") or KEY_FILE),
              "r", encoding="utf-8") as fh:
        return fh.read().strip()


def _atomic_rate_state(payload: dict) -> None:
    RATE_LIMIT_STATE_FILE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = RATE_LIMIT_STATE_FILE.with_name(
        RATE_LIMIT_STATE_FILE.name + f".{os.getpid()}.{uuid.uuid4().hex}.tmp")
    raw = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(raw)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, RATE_LIMIT_STATE_FILE)


def _reset_epoch(value: str, now: float | None = None) -> float:
    now = time.time() if now is None else float(now)
    raw = str(value or "").strip()
    if not raw:
        return now + 60.0
    try:
        numeric = float(raw)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return max(now + 1.0, parsed.timestamp())
        except ValueError:
            return now + 60.0
    # Providers use either an epoch or seconds-until-reset. Distinguish them
    # without guessing a timezone.
    return max(now + 1.0, numeric if numeric > 1_000_000_000 else now + numeric)


def _load_rate_state() -> dict:
    try:
        value = json.loads(RATE_LIMIT_STATE_FILE.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _active_rate_hold(now: float | None = None) -> dict:
    now = time.time() if now is None else float(now)
    state = _load_rate_state()
    try:
        reset = float(state.get("reset_epoch") or 0)
    except (TypeError, ValueError):
        return {}
    if int(state.get("remaining", -1)) == 0 and reset > now:
        return state
    return {}


def _record_rate_limit_headers(headers, now: float | None = None) -> dict:
    """Persist the authoritative daily quota window without logging secrets."""
    if hasattr(headers, "getheaders"):
        pairs = headers.getheaders()
    elif isinstance(headers, dict):
        pairs = headers.items()
    else:
        pairs = headers or []
    values = {str(key).casefold(): str(value).strip() for key, value in pairs}
    remaining_raw = values.get(RATE_LIMIT_DAILY_REMAINING_HEADER.casefold())
    reset_raw = values.get(RATE_LIMIT_DAILY_RESET_HEADER.casefold())
    if remaining_raw is None:
        return {}
    try:
        remaining = int(float(remaining_raw))
    except ValueError:
        return {}
    state = {
        "schema": "ops.n8n-mcp-rate-limit-state.v1",
        "remaining": remaining,
        "reset_raw": reset_raw or "",
        "observed_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if remaining <= 0:
        state["remaining"] = 0
        state["reset_epoch"] = _reset_epoch(reset_raw or "", now=now)
        _atomic_rate_state(state)
    elif RATE_LIMIT_STATE_FILE.exists():
        # A positive authoritative read clears a stale hold from an earlier
        # process/day. Keep the last positive count for auditability.
        state["reset_epoch"] = 0
        _atomic_rate_state(state)
    return state


def _connect(url: str):
    parts = urllib.parse.urlsplit(url)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    if parts.scheme == "https":
        conn = http.client.HTTPSConnection(
            parts.hostname, parts.port, timeout=CONNECT_TIMEOUT_S,
            context=ssl.create_default_context())
    else:
        conn = http.client.HTTPConnection(parts.hostname, parts.port,
                                          timeout=CONNECT_TIMEOUT_S)
    try:
        conn.connect()
    except Exception as exc:  # noqa: BLE001
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        raise MCPConnectError("could not reach the MCP relay at %s://%s within %.0fs: "
                              "%s: %s" % (parts.scheme, parts.netloc, CONNECT_TIMEOUT_S,
                                          type(exc).__name__, str(exc)[:200])) from exc
    # The connect timeout has done its job; every socket operation from here is
    # bounded by the READ timeout, and the whole body by the wall-clock deadline.
    if conn.sock is not None:
        conn.sock.settimeout(READ_TIMEOUT_S)
    return conn, path


def _read_bounded(resp, deadline: float, tool: str, budget_s: float, *,
                  decode: bool = True) -> str | bytes:
    """Read the body with a WALL-CLOCK bound, not just a per-recv one.

    The deadline is re-checked before every chunk, so a relay that dribbles
    bytes forever (which is exactly what a per-socket timeout cannot catch)
    fails loud here instead of holding the caller for hours."""
    # `read(n)` is NOT usable here: it blocks until it has n bytes or EOF, so a
    # peer dribbling one byte every 200ms keeps the interpreter inside a single
    # call and the deadline below is never re-checked — the exact hang this
    # function exists to stop. `read1()` returns whatever one raw read produced,
    # which is what makes the deadline a real bound.
    reader = getattr(resp, "read1", None) or resp.read
    chunks: list[bytes] = []
    total = 0
    while True:
        if time.monotonic() >= deadline:
            raise MCPTimeout("MCP call %s exceeded its %.0fs deadline while reading the "
                             "response (%d bytes so far) — aborted, not waited on"
                             % (tool, budget_s, total))
        try:
            # urllib/http.client expose the socket through the buffered raw file.
            # Bound each blocking read by the remaining overall budget as well.
            sock = getattr(getattr(getattr(resp, "fp", None), "raw", None), "_sock", None)
            if sock is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise MCPTimeout("response deadline exhausted before read")
                sock.settimeout(min(READ_TIMEOUT_S, remaining))
            chunk = reader(65536)
        except (socket.timeout, TimeoutError) as exc:
            if time.monotonic() >= deadline:
                raise MCPTimeout("MCP call %s exceeded its %.0fs wall-clock deadline "
                                 "while reading the response" % (tool, budget_s)) from exc
            raise MCPTimeout("MCP call %s: no data for %.0fs while reading the response "
                             "(%d bytes so far)" % (tool, READ_TIMEOUT_S, total)) from exc
        except (ssl.SSLError, OSError, http.client.HTTPException) as exc:
            raise MCPTransportError("MCP call %s: the connection failed mid-response "
                                    "(%d bytes so far): %s: %s"
                                    % (tool, total, type(exc).__name__,
                                       str(exc)[:200])) from exc
        # Even a final EOF or complete JSON body arriving late is not success.
        if time.monotonic() >= deadline:
            raise MCPTimeout("response deadline exhausted after read")
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            raise MCPTransportError("MCP call %s returned more than %d bytes — refusing "
                                    "to buffer an unbounded response"
                                    % (tool, MAX_RESPONSE_BYTES))
        chunks.append(chunk)
    raw = b"".join(chunks)
    return raw.decode("utf-8", "replace") if decode else raw


def _attempt(tool: str, payload: bytes, headers: dict,
             budget_s: float) -> tuple[int, str]:
    """One bounded round trip. Raises a phase-tagged MCPError, never blocks."""
    url = _url()
    conn, path = _connect(url)
    deadline = time.monotonic() + budget_s
    try:
        try:
            conn.request("POST", path, body=payload, headers=headers)
            resp = conn.getresponse()
        except (socket.timeout, TimeoutError) as exc:
            raise MCPTimeout("MCP call %s: the relay sent no response headers within "
                             "%.0fs" % (tool, READ_TIMEOUT_S)) from exc
        except (ssl.SSLError, OSError, http.client.HTTPException) as exc:
            raise MCPTransportError("MCP call %s: the request failed in flight: %s: %s"
                                    % (tool, type(exc).__name__, str(exc)[:200])) from exc
        _record_rate_limit_headers(resp)
        return resp.status, _read_bounded(resp, deadline, tool, budget_s)
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def _retryable(exc: MCPError | None, status: int | None, idempotent: bool) -> bool:
    if isinstance(exc, MCPConnectError):
        return True                                  # never delivered
    if isinstance(exc, MCPTransportError):
        return idempotent                            # may have been delivered
    if status in RETRYABLE_STATUS_ANY:
        return True                                  # refused before processing
    if status in RETRYABLE_STATUS_IDEMPOTENT:
        return idempotent
    return False


def call(tool: str, args: dict, timeout: int | None = None, *,
         allow_refusal: bool = False, idempotent: bool | None = None,
         sleep=time.sleep) -> dict:
    """One MCP tool call: bounded, retried, and loud when it fails.

    `timeout` is kept for callers that already pass it, but it is a CEILING on
    the per-attempt deadline, never a way to widen one past `MAX_TIMEOUT_S`.

    `allow_refusal=True` returns a `{"success": false}` payload instead of
    raising — the allowlist escape (see `REFUSAL_INSPECTORS`).
    """
    hold = _active_rate_hold()
    if hold:
        raise MCPRateLimitHold(hold["reset_epoch"], hold.get("reset_raw") or "")
    if idempotent is None:
        idempotent = str(args.get("action") or "") in IDEMPOTENT_ACTIONS
    deadline_s = CALL_DEADLINE_S if timeout is None else min(CALL_DEADLINE_S,
                                                             max(1.0, float(timeout)))
    # The relay correlates concurrent calls by JSON-RPC id.  A process-wide
    # constant let two machines sharing this credential receive the same result:
    # an error_cases read and an application-queue read both used id=1, and the
    # held application rows were returned to the error fixer.  A fresh id per
    # logical call keeps simultaneous table reads isolated; retries intentionally
    # retain that id so one call still has one correlation identity.
    request_id = uuid.uuid4().hex
    payload = json.dumps({"jsonrpc": "2.0", "id": request_id,
                          "method": "tools/call",
                          "params": {"name": tool, "arguments": args}}).encode("utf-8")
    headers = {"Authorization": "Bearer " + _key(),
               "Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}

    for attempt in range(1, MAX_ATTEMPTS + 1):
        status: int | None = None
        error: MCPError | None = None
        raw = ""
        try:
            status, raw = _attempt(tool, payload, headers, deadline_s)
        except MCPError as exc:
            error = exc
        if error is None and status == 200:
            return _parse(raw, tool, allow_refusal=allow_refusal,
                          expected_id=request_id)
        hold = _active_rate_hold()
        if hold:
            # Do not retry a known zero quota. Callers either take the automatic
            # native fallback or leave a reset-bound durable hold for a scheduler.
            raise MCPRateLimitHold(hold["reset_epoch"], hold.get("reset_raw") or "")
        if error is None:
            error = MCPTransportError("MCP HTTP %s for %s: %s"
                                      % (status, tool, raw[:300]))
        if attempt >= MAX_ATTEMPTS or not _retryable(
                error if status is None else None, status, idempotent):
            raise type(error)(
                "%s [%s after %d of %d attempt(s); action=%r idempotent=%s]"
                % (str(error),
                   "gave up" if attempt >= MAX_ATTEMPTS else "not safe to retry",
                   attempt, MAX_ATTEMPTS, args.get("action"), idempotent)) from error
        sleep(BACKOFF_S[min(attempt - 1, len(BACKOFF_S) - 1)])
    raise MCPTransportError("MCP call %s exhausted %d attempts" % (tool, MAX_ATTEMPTS))


_WORKFLOW_DEFINITION_CACHE: dict[tuple[str, str], dict] = {}


def _read_workflow_definition(workflow_id: str, mode: str) -> dict:
    wid = str(workflow_id or "").strip()
    if not wid:
        raise MCPError("workflow id required")
    try:
        response = call(
            "n8n_get_workflow", {"id": wid, "mode": mode}, idempotent=True)
        workflow = response.get("data") or response
    except MCPError as error:
        if not _mcp_error_allows_native_fallback(error):
            raise
        try:
            workflow = _native_http(
                "GET", "/workflows/%s" % urllib.parse.quote(wid, safe=""))
        except Exception as native_error:  # noqa: BLE001
            raise error from native_error
    if not isinstance(workflow, dict) or workflow.get("id") not in (None, wid):
        raise MCPError("workflow definition readback invalid for %s" % wid)
    if not isinstance(workflow.get("nodes"), list):
        raise MCPError("workflow definition omitted nodes for %s" % wid)
    return workflow


def workflow_definition_once(workflow_id: str, mode: str = "full") -> dict:
    """Fetch one workflow definition per process and return an isolated copy."""
    key = (str(workflow_id or "").strip(), str(mode or "full"))
    if key not in _WORKFLOW_DEFINITION_CACHE:
        _WORKFLOW_DEFINITION_CACHE[key] = copy.deepcopy(
            _read_workflow_definition(key[0], key[1]))
    return copy.deepcopy(_WORKFLOW_DEFINITION_CACHE[key])


def workflow_definition_fresh(workflow_id: str, mode: str = "full") -> dict:
    """Required post-write readback; refresh and advance the per-run cache."""
    key = (str(workflow_id or "").strip(), str(mode or "full"))
    workflow = _read_workflow_definition(key[0], key[1])
    _WORKFLOW_DEFINITION_CACHE[key] = copy.deepcopy(workflow)
    return copy.deepcopy(workflow)


def workflow_definition_executable(workflow_id: str, *, definition: dict | None = None) -> dict:
    """Bind a saved definition to the separately returned MCP active executable.

    mode=full omits activeVersion on this server. mode=active identifies its
    executable with activeVersionId. Preserve that exact ID through an explicit
    alias for owner validators, never substitute saved nodes for published ones.
    An unavailable published selector stays UNKNOWN; no guessed native endpoint.
    """
    if not isinstance(workflow_id, str) or not workflow_id or workflow_id.strip() != workflow_id:
        raise MCPError("literal workflow identity invalid")
    before = copy.deepcopy(definition) if definition is not None else workflow_definition_fresh(workflow_id)
    if not isinstance(before, dict) or before.get("id") != workflow_id:
        raise MCPError("saved workflow identity mismatch")
    if before.get("active") is not True:
        return before
    expected = before.get("activeVersionId")
    if not isinstance(expected, str) or not expected:
        raise MCPError("active version identity missing")
    response = call("n8n_get_workflow", {"id": workflow_id, "mode": "active"}, idempotent=True)
    if not isinstance(response, dict):
        raise MCPError("published response envelope invalid")
    published = response.get("data", response)
    if (response.get("success") is False or not isinstance(published, dict)
            or published.get("id") != workflow_id or published.get("active") is not True
            or published.get("activeVersionId") != expected
            or not isinstance(published.get("nodes"), list)
            or not isinstance(published.get("connections"), dict)):
        raise MCPError("published executable identity or body unavailable")
    after = workflow_definition_fresh(workflow_id)
    keys = ("id", "active", "versionId", "activeVersionId", "nodes", "connections", "settings")
    if any(after.get(key) != before.get(key) for key in keys):
        raise MCPError("workflow changed during published binding read")
    bound = copy.deepcopy(after)
    bound["activeVersion"] = {"versionId": expected,
                              "nodes": copy.deepcopy(published["nodes"]),
                              "connections": copy.deepcopy(published["connections"])}
    bound["_active_version_evidence"] = {"selector": "n8n_get_workflow:mode=active",
        "activeVersionId": expected, "savedVersionId": after.get("versionId"),
        "versionId_alias_source": "published.activeVersionId"}
    return bound


def _parse(raw: str, tool: str, allow_refusal: bool = False,
           expected_id: str | None = None) -> dict:
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("data: "):
            line = line[6:]
        if not line.startswith("{"):
            continue
        try:
            doc = json.loads(line)
        except ValueError:
            continue
        if ("result" in doc or "error" in doc) and expected_id is not None:
            response_id = doc.get("id")
            if response_id != expected_id:
                raise MCPError(
                    "MCP correlation mismatch for %s: expected response id %r, got %r"
                    % (tool, expected_id, response_id))
        if "error" in doc:
            raise MCPError("MCP error for %s: %s" % (tool, json.dumps(doc["error"])[:300]))
        if "result" in doc:
            content = (doc.get("result") or {}).get("content") or []
            text = content[0].get("text") if content else ""
            try:
                parsed = json.loads(text)
            except (ValueError, TypeError):
                lowered = str(text or "").casefold()
                unavailable_code = None
                if "quota exceeded" in lowered or "daily quota" in lowered:
                    unavailable_code = "QUOTA_EXCEEDED"
                elif "rate limit" in lowered:
                    unavailable_code = "RATE_LIMITED"
                elif ("failed to authenticate" in lowered
                      or "authentication error" in lowered
                      or "unauthorized" in lowered):
                    unavailable_code = "AUTHENTICATION_ERROR"
                elif "service unavailable" in lowered:
                    unavailable_code = "SERVICE_UNAVAILABLE"
                elif "timeout" in lowered:
                    unavailable_code = "TIMEOUT"
                if unavailable_code:
                    raise MCPRefused(tool, {
                        "success": False,
                        "code": unavailable_code,
                        "error": str(text)[:300],
                    })
                return {"success": True, "raw": text}
            if isinstance(parsed, dict) and parsed.get("success") is False:
                # The write did NOT happen. A caller that treats this as a
                # result reports a row it never wrote (defect 2026-08-20 #4).
                if not allow_refusal:
                    raise MCPRefused(tool, parsed)
            return parsed
    raise MCPError("unparseable MCP response for %s: %s" % (tool, raw[:300]))


def eq(column: str, value) -> dict:
    return {"type": "and",
            "filters": [{"columnName": column, "condition": "eq", "value": value}]}


def rows(table_id: str, filt=None, limit: int = PAGE_LIMIT, *,
         allow_refusal: bool = False, sort_by=None, search=None) -> list:
    """Return matching rows with 100-row pages and cursor exhaustion.

    Data Tables use native REST by default. Explicit MCP rollback mode still
    uses the relay's `limit`/`filter`/`sortBy`/`search` arguments and follows the
    returned cursor instead of attempting the provider's ignored legacy offset.
    """
    global last_read_truncated
    requested = max(1, int(limit or PAGE_LIMIT))
    args = {"action": "getRows", "tableId": table_id, "limit": requested}
    if filt:
        args["filter"] = filt
    if sort_by:
        args["sortBy"] = sort_by
    if search:
        args["search"] = search
    if _datatable_native_first():
        result = _native_datatable_call(args)
        data = result.get("data") or {}
        page = data.get("rows") or []
        last_read_truncated = bool(data.get("nextCursor"))
        return page
    collected: list[dict] = []
    cursor = None
    try:
        for _ in range(50):
            page, cursor = _mcp_rows_page(
                table_id, filt, requested - len(collected), cursor,
                sort_by=sort_by, search=search)
            collected.extend(page)
            if not cursor or len(collected) >= requested:
                break
        else:
            raise MCPError("MCP Data Table pagination exceeded 50 pages")
    except MCPError as error:
        if not _mcp_error_allows_native_fallback(error):
            raise
        last_read_truncated = False
        try:
            page = _native_rows(table_id, filt, requested)
        except Exception as native_error:  # noqa: BLE001
            # Native REST is an availability fallback, not a replacement error.
            # Preserve the primary MCP failure so timeout/correlation diagnostics
            # stay truthful when both transports are unavailable.
            raise error from native_error
        return page
    last_read_truncated = bool(cursor and len(collected) >= requested)
    return collected[:requested]


last_read_truncated = False


def insert(table_id: str, row: dict, *, allow_refusal: bool = False,
           key_fields=None) -> dict:
    """Insert one row with MCP-first, read-modify-verify fallback semantics."""
    if _datatable_native_first():
        result = insert_many(
            table_id, [row], allow_refusal=allow_refusal,
            key_fields=key_fields)
        outcomes = result.get("rows") or []
        if len(outcomes) != 1:
            raise WriteReconciliationError(
                "single insert batch expected one outcome, found %d" % len(outcomes))
        return outcomes[0]
    row = _canonicalize_table_row(table_id, row)
    fields = _key_fields(table_id, row, key_fields)
    filt = _key_filter(row, fields)
    before = _preflight_rows(table_id, filt, 3)
    if before:
        exact = _one_exact(before, row, "insert preflight")
        return _result("already_present", exact, transport="readback")
    try:
        response = call(
            "n8n_manage_datatable",
            {"action": "insertRows", "tableId": table_id, "data": [row]},
            allow_refusal=allow_refusal, idempotent=False)
        if allow_refusal and response.get("success") is False:
            refusal = MCPRefused("n8n_manage_datatable", response)
            if not _refusal_allows_native_fallback(refusal):
                return response
            raise refusal
    except MCPError as mcp_error:
        if not _mcp_error_allows_native_fallback(mcp_error):
            # Semantic/schema refusals and malformed/correlation-invalid answers
            # are conclusive. Native REST must never be used to bypass them.
            raise
        landed = _native_rows(table_id, filt, 3)
        if landed:
            exact = _one_exact(landed, row, "MCP insert failure")
            return _result("mcp_failure_landed", exact, transport="readback",
                           extra={"mcp_error": type(mcp_error).__name__})
        return _native_insert_after_reconciliation(table_id, row, filt)
    landed = _native_rows(table_id, filt, 3)
    if landed:
        exact = _one_exact(landed, row, "MCP insert post-write")
        return _result("mcp_confirmed", exact, transport="mcp")
    # A success envelope without the intended row is an acknowledgement, not a
    # write receipt. Exact native readback proved the natural key is absent, so
    # perform the same single native mutation used after an ambiguous timeout.
    return _native_insert_after_reconciliation(table_id, row, filt)


def insert_many(table_id: str, rows_to_insert: list[dict], *,
                allow_refusal: bool = False, key_fields=None) -> dict:
    """Batch insert without blind replay; deterministic preflights use native REST.

    The single MCP mutation carries the entire pending array. Natural-key
    preflight and readback are fixed data loops, so they deliberately stay off
    the AI-facing MCP quota and use the native transport.
    """
    if not rows_to_insert:
        return {"success": True, "write_status": "empty", "inserted": 0,
                "skipped": 0, "rows": []}
    rows_to_insert = [_canonicalize_table_row(table_id, row)
                      for row in rows_to_insert]
    if _datatable_native_first():
        fields = tuple(TABLE_NATURAL_KEYS.get(str(table_id)) or ())
        seen_keys: set[tuple] = set()
        for row in rows_to_insert:
            checked = _key_fields(table_id, row, key_fields)
            if checked != fields:
                raise WriteReconciliationError("registered natural key changed mid-batch")
            key = tuple(row[field] for field in fields)
            if key in seen_keys:
                raise WriteReconciliationError(
                    "insert_many input repeats natural key %r" % (key,))
            seen_keys.add(key)
        before = native_rows_for_key_tuples(table_id, rows_to_insert)
        outcome_by_key: dict[tuple, dict] = {}
        pending: list[dict] = []
        for row in rows_to_insert:
            key = tuple(row[field] for field in fields)
            if key in before:
                exact = _one_exact([before[key]], row, "insert_many preflight")
                outcome_by_key[key] = _result(
                    "already_present", exact, transport="readback")
            else:
                pending.append(row)
        if pending:
            try:
                _native_datatable_call({
                    "action": "insertRows", "tableId": table_id,
                    "data": pending,
                })
            except NativeSemanticRefusal:
                raise
            except MCPError as native_error:
                landed = native_rows_for_key_tuples(table_id, pending)
                if len(landed) != len(pending):
                    raise WriteReconciliationError(
                        "native batch insert failed ambiguously; %d of %d natural keys landed; "
                        "no replay attempted" % (len(landed), len(pending))) from native_error
                for row in pending:
                    key = tuple(row[field] for field in fields)
                    exact = _one_exact([landed[key]], row, "native batch insert timeout")
                    outcome_by_key[key] = _result(
                        "native_timeout_landed", exact, transport="native-rest")
            else:
                landed = native_rows_for_key_tuples(table_id, pending)
                for row in pending:
                    key = tuple(row[field] for field in fields)
                    found = [landed[key]] if key in landed else []
                    # A POST-WRITE READBACK THAT FINDS THE KEY MEANS THE WRITE
                    # LANDED. Raising here told the caller the insert had failed;
                    # the caller retried and created a SECOND row under a fresh
                    # update_id, which is exactly the duplicate this
                    # reconciliation exists to prevent (Awin 129557, 2026-09-03).
                    # The key being present is the idempotency fact. If the
                    # stored state differs from what we intended, that is worth
                    # SAYING - it is returned as a distinct write_status with the
                    # differing fields named - but it is never worth re-inserting.
                    if len(found) == 1:
                        stored = found[0]
                        if _row_has_state(stored, row):
                            outcome_by_key[key] = _result(
                                "native_confirmed", stored, transport="native-rest")
                        else:
                            drift = sorted(
                                field for field, value in row.items()
                                if _normalised_cell(stored.get(field))
                                != _normalised_cell(value))
                            outcome_by_key[key] = _result(
                                "native_confirmed_state_drift", stored,
                                transport="native-rest",
                                extra={"state_drift_fields": drift})
                    else:
                        raise WriteReconciliationError(
                            "native batch insert post-write readback expected exactly "
                            "one row for the natural key, found %d" % len(found))
        outcomes = [outcome_by_key[tuple(row[field] for field in fields)]
                    for row in rows_to_insert]
        inserted = sum(item.get("write_status") != "already_present"
                       for item in outcomes)
        return {"success": True, "write_status": "reconciled",
                "inserted": inserted, "skipped": len(outcomes) - inserted,
                "rows": outcomes}
    pending: list[tuple[dict, dict]] = []
    outcomes: list[dict] = []
    seen_keys: set[tuple] = set()
    for row in rows_to_insert:
        fields = _key_fields(table_id, row, key_fields)
        key = tuple((field, row[field]) for field in fields)
        if key in seen_keys:
            raise WriteReconciliationError(
                "insert_many input repeats natural key %r" % (key,))
        seen_keys.add(key)
        filt = _key_filter(row, fields)
        before = _native_rows(table_id, filt, 3)
        if before:
            exact = _one_exact(before, row, "insert_many preflight")
            outcomes.append(_result("already_present", exact, transport="readback"))
        else:
            pending.append((row, filt))
    if not pending:
        return {"success": True, "write_status": "already_present",
                "inserted": 0, "skipped": len(outcomes), "rows": outcomes}
    try:
        response = call(
            "n8n_manage_datatable",
            {"action": "insertRows", "tableId": table_id,
             "data": [row for row, _filt in pending]},
            allow_refusal=allow_refusal, idempotent=False)
        if allow_refusal and response.get("success") is False:
            refusal = MCPRefused("n8n_manage_datatable", response)
            if not _refusal_allows_native_fallback(refusal):
                return response
            raise refusal
    except MCPError as mcp_error:
        if not _mcp_error_allows_native_fallback(mcp_error):
            raise
        for row, filt in pending:
            landed = _native_rows(table_id, filt, 3)
            if landed:
                exact = _one_exact(landed, row, "MCP insert_many failure")
                outcomes.append(_result(
                    "mcp_failure_landed", exact, transport="readback",
                    extra={"mcp_error": type(mcp_error).__name__}))
            else:
                outcomes.append(_native_insert_after_reconciliation(table_id, row, filt))
    else:
        for row, filt in pending:
            landed = _native_rows(table_id, filt, 3)
            if landed:
                exact = _one_exact(landed, row, "MCP insert_many post-write")
                outcomes.append(_result("mcp_confirmed", exact, transport="mcp"))
            else:
                outcomes.append(_native_insert_after_reconciliation(table_id, row, filt))
    inserted = sum(item.get("write_status") not in {"already_present"}
                   for item in outcomes)
    return {"success": True, "write_status": "reconciled",
            "inserted": inserted, "skipped": len(outcomes) - inserted,
            "rows": outcomes}


def update(table_id: str, filt: dict, data: dict, *,
           allow_refusal: bool = False) -> dict:
    _filter_key_fields(table_id, filt)
    data = _canonicalize_table_row(table_id, data)
    before = _preflight_rows(table_id, filt, 3)
    if len(before) != 1:
        raise WriteReconciliationError(
            "update preflight expected exactly one row, found %d" % len(before))
    if _row_has_state(before[0], data):
        return _result("already_present", before[0], transport="readback")
    if _datatable_native_first():
        return _native_update_after_reconciliation(table_id, filt, data)
    try:
        response = call(
            "n8n_manage_datatable",
            {"action": "updateRows", "tableId": table_id,
             "filter": filt, "data": data},
            allow_refusal=allow_refusal, idempotent=False)
        if allow_refusal and response.get("success") is False:
            refusal = MCPRefused("n8n_manage_datatable", response)
            if not _refusal_allows_native_fallback(refusal):
                return response
            raise refusal
    except MCPError as mcp_error:
        if not _mcp_error_allows_native_fallback(mcp_error):
            raise
        landed = _native_rows(table_id, filt, 3)
        if len(landed) != 1:
            raise WriteReconciliationError(
                "MCP update failed and native readback found %d target rows"
                % len(landed)) from mcp_error
        if _row_has_state(landed[0], data):
            return _result("mcp_failure_landed", landed[0], transport="readback",
                           extra={"mcp_error": type(mcp_error).__name__})
        return _native_update_after_reconciliation(table_id, filt, data)
    landed = _native_rows(table_id, filt, 3)
    if len(landed) != 1:
        raise WriteReconciliationError(
            "MCP update post-write expected exactly one target row, found %d"
            % len(landed))
    if _row_has_state(landed[0], data):
        return _result("mcp_confirmed", landed[0], transport="mcp")
    # The key still identifies exactly one row, but acknowledged state did not
    # land. Reconcile the old state, then issue one native update + readback.
    return _native_update_after_reconciliation(table_id, filt, data)


def upsert(table_id: str, filt: dict, data: dict, *,
           allow_refusal: bool = False) -> dict:
    """Update one natural-key row or insert it in one provider mutation.

    This is the replacement for caller-level ``rows()`` followed by
    ``update()``/``insert()``. The equality filter is merged into the intended
    row so an insert branch cannot omit its natural key. One exact readback
    proves the resulting state; an ambiguous write is never replayed.
    """
    fields = _filter_key_fields(table_id, filt)
    clauses = filt.get("filters") or []
    filter_values = {str(clause["columnName"]): clause.get("value")
                     for clause in clauses}
    intended = dict(data)
    for field in fields:
        if field == "id":
            continue
        if field in intended and intended[field] != filter_values[field]:
            raise WriteReconciliationError(
                "upsert data conflicts with natural-key filter field %s" % field)
        intended[field] = filter_values[field]
    intended = _canonicalize_table_row(table_id, intended)
    if _datatable_native_first():
        try:
            _native_datatable_call({
                "action": "upsertRows", "tableId": table_id,
                "filter": filt, "data": intended,
            })
        except NativeSemanticRefusal:
            raise
        except MCPError as native_error:
            landed = _native_rows(table_id, filt, 3)
            exact = _one_exact(landed, intended, "native upsert timeout")
            return _result(
                "native_timeout_landed", exact, transport="native-rest",
                extra={"native_error": type(native_error).__name__})
        exact = _one_exact(
            _native_rows(table_id, filt, 3), intended,
            "native upsert post-write")
        return _result("native_upsert_confirmed", exact, transport="native-rest")
    try:
        response = call(
            "n8n_manage_datatable",
            {"action": "upsertRows", "tableId": table_id,
             "filter": filt, "data": intended},
            allow_refusal=allow_refusal, idempotent=False)
        if allow_refusal and response.get("success") is False:
            refusal = MCPRefused("n8n_manage_datatable", response)
            if not _refusal_allows_native_fallback(refusal):
                return response
            raise refusal
    except MCPError as mcp_error:
        if not _mcp_error_allows_native_fallback(mcp_error):
            raise
        landed = _native_rows(table_id, filt, 3)
        if landed and _row_has_state(landed[0], intended):
            exact = _one_exact(landed, intended, "MCP upsert failure")
            return _result(
                "mcp_failure_landed", exact, transport="readback",
                extra={"mcp_error": type(mcp_error).__name__})
        return _native_upsert_after_reconciliation(table_id, filt, intended)
    exact = _one_exact(
        _native_rows(table_id, filt, 3), intended,
        "MCP upsert post-write")
    return _result("mcp_confirmed", exact, transport="mcp")
