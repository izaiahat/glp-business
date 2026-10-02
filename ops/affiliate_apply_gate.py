#!/usr/bin/env python3
"""The one control that decides whether we may submit an affiliate application.

Operator, 2026-08-26: "i thought we paused affiliate applications for now until
site is deployed and we run maxbounty and xentraffic and any other backlogged
queued applications".

Applications had in fact stopped -- but only because the apply queue happened to
be empty. `glp/applications` was still active and polling every 15 minutes, so
anything that enqueued a row would have been submitted. That is not a pause, it
is a coincidence, and a coincidence is not a control. This module makes the
pause a fact the machine has to read.

Design rules, in order of importance:

1. **Fail closed.** Missing file, unreadable file, bad JSON, unknown schema,
   unexpected type -- every one of those means PAUSED. An apply gate that opens
   when its own config breaks is worse than no gate, because it is trusted.
2. **Say which state it is in.** A gated run must NOT look like an idle run.
   "Nothing to do" and "forbidden to act" are different facts and conflating
   them is exactly how this pause hid in the first place.
3. **Per-network prerequisites.** Some rails cannot be applied to until an asset
   exists -- MaxBounty applications carry an advertising image the operator has
   not approved yet. A rail whose prerequisite is unmet stays blocked even when
   the global gate is open.
4. **Only the operator opens it.** Nothing in the fleet may flip this to open;
   machines may only read it. Reversing that is the whole point.

The state file is `ops/control/affiliate-apply-gate.json`.
"""
from __future__ import annotations

import hashlib
import json
import re
import struct
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BUSINESS = Path("/home/ubuntu/business")
GATE_PATH = BUSINESS / "ops/control/affiliate-apply-gate.json"
ATTEMPT_ROOT = BUSINESS / "reports/glp-clinic-scout/affiliate-machine/applications"
SCHEMA = "affiliate-apply-gate-v1"

#: Rails that additionally require a named asset to exist and be operator
#: approved before a single application may be submitted through them.
#: MaxBounty's five-question application carries our advertising creative, so we
#: cannot apply there until the operator approves the image.
NETWORK_PREREQUISITES: dict[str, str] = {
    "maxbounty": "maxbounty_advertising_image",
}


class ApplyPaused(RuntimeError):
    """Raised instead of submitting. Carries a reason a human can act on."""


def _fail_closed(reason: str) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "paused": True,
        "reason": reason,
        "approved_assets": [],
        "approved_asset_records": [],
        "scope": {"networks_allowed": [], "max_applications": 0},
        "fail_closed": True,
    }


def _utc(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def application_attempts_consumed(path: Path | None = None,
                                  attempt_root: Path | None = None) -> int:
    """Count durable provider requests observed under the current gate opening.

    Operator decision 2026-08-29: form preparation and submit-control clicks do
    not consume an attempt unless the exact provider request leaves the browser.
    The provider-attempt count is therefore authoritative; a pre-click journal or
    legacy ``gate_slot_consumed`` reservation with zero provider attempts is free.
    """
    state = gate_state(path)
    if state["fail_closed"]:
        return 0
    opened_at = _utc(state.get("set_at_utc"))
    if opened_at is None:
        return int(state["scope"]["max_applications"])
    root = Path(attempt_root or ATTEMPT_ROOT)
    # Re-open decisions can carry already-consumed attempts from an immediately
    # preceding closed gate. Those remain part of the operator's absolute cap
    # even though their journals predate this opening.
    consumed = int(state["scope"].get("attempts_consumed_before_this_decision") or 0)
    for receipt in root.glob("**/provider-attempt-*.json") if root.is_dir() else ():
        try:
            data = json.loads(receipt.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # An unreadable provider journal may hide an attempt. Fail closed by
            # consuming the entire opening rather than licensing a replay.
            return int(state["scope"]["max_applications"])
        provider_attempts = int(data.get("provider_application_attempts") or 0)
        if provider_attempts < 1:
            continue
        reserved_at = _utc(data.get("reserved_at_utc") or data.get("attempted_at_utc"))
        if reserved_at is None:
            return int(state["scope"]["max_applications"])
        if reserved_at >= opened_at:
            consumed += 1
    return consumed


def _asset_dimensions(path: Path) -> str:
    """Read PNG dimensions without adding a Pillow/runtime dependency."""
    with path.open("rb") as handle:
        header = handle.read(24)
    if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise ValueError("approved asset is not a PNG with an IHDR header")
    width, height = struct.unpack(">II", header[16:24])
    return f"{width}x{height}"


def _verified_asset_record(value: Any) -> tuple[str, dict[str, Any] | None]:
    """Return the asset name and a hash/dimension-verified structured record.

    Legacy string assets remain readable for gates that do not require a file.
    A network prerequisite is satisfied only by a structured record, so a stale
    name-only MaxBounty opening cannot authorize an upload.
    """
    if isinstance(value, str):
        name = value.strip()
        if not name:
            raise ValueError("approved asset name is empty")
        return name, None
    if not isinstance(value, dict):
        raise ValueError("approved_assets entries must be strings or objects")

    required = ("asset", "path", "sha256", "dimensions", "approved_by")
    missing = [key for key in required if not str(value.get(key) or "").strip()]
    if missing:
        raise ValueError("approved asset fields missing: " + ",".join(missing))
    name = str(value["asset"]).strip()
    relative = Path(str(value["path"]).strip())
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"approved asset path is not repo-relative: {name}")
    path = (BUSINESS / relative).resolve()
    try:
        path.relative_to(BUSINESS.resolve())
    except ValueError as exc:
        raise ValueError(f"approved asset escapes business root: {name}") from exc
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"approved asset is missing or not a regular file: {name}")
    expected_sha = str(value["sha256"]).strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
        raise ValueError(f"approved asset sha256 is invalid: {name}")
    actual_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual_sha != expected_sha:
        raise ValueError(f"approved asset sha256 drift: {name}")
    expected_dimensions = str(value["dimensions"]).strip().lower()
    actual_dimensions = _asset_dimensions(path)
    if actual_dimensions.lower() != expected_dimensions:
        raise ValueError(f"approved asset dimensions drift: {name}")
    if not str(value["approved_by"]).strip().lower().startswith("operator"):
        raise ValueError(f"approved asset lacks operator approval: {name}")
    return name, {
        **value,
        "asset": name,
        "path": str(relative),
        "sha256": actual_sha,
        "dimensions": actual_dimensions,
        "absolute_path": str(path),
        "verified": True,
    }


def gate_state(path: Path | None = None) -> dict[str, Any]:
    """Read the gate. Any doubt whatsoever resolves to paused."""
    target = path or GATE_PATH
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as exc:
        return _fail_closed(f"apply gate unreadable at {target}: {exc}")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return _fail_closed(f"apply gate is not valid JSON at {target}: {exc}")

    if not isinstance(data, dict):
        return _fail_closed(f"apply gate must be a JSON object at {target}")
    if data.get("schema") != SCHEMA:
        return _fail_closed(
            f"apply gate schema is {data.get('schema')!r}, expected {SCHEMA!r}")
    if not isinstance(data.get("paused"), bool):
        return _fail_closed("apply gate 'paused' must be a boolean")

    assets = data.get("approved_assets")
    if assets is None:
        assets = []
    if not isinstance(assets, list):
        return _fail_closed("apply gate 'approved_assets' must be a list")
    try:
        normalized_assets = [_verified_asset_record(asset) for asset in assets]
    except (OSError, ValueError) as exc:
        return _fail_closed(f"apply gate approved asset invalid: {exc}")
    asset_names = [name for name, _record in normalized_assets]
    if len(asset_names) != len(set(asset_names)):
        return _fail_closed("apply gate approved asset names must be unique")

    scope = data.get("scope")
    if not isinstance(scope, dict):
        return _fail_closed("apply gate 'scope' must be an object")
    networks = scope.get("networks_allowed")
    if not isinstance(networks, list) or not networks:
        return _fail_closed("apply gate scope.networks_allowed must be a non-empty list")
    normalized_networks = [str(network or "").strip().lower() for network in networks]
    if (any(not network for network in normalized_networks)
            or len(normalized_networks) != len(set(normalized_networks))):
        return _fail_closed("apply gate scope.networks_allowed is empty or duplicated")
    raw_limit = scope.get("max_applications")
    if isinstance(raw_limit, bool):
        return _fail_closed("apply gate scope.max_applications must be a positive integer")
    try:
        max_applications = int(raw_limit)
    except (TypeError, ValueError):
        return _fail_closed("apply gate scope.max_applications must be a positive integer")
    if max_applications < 1 or str(raw_limit).strip() != str(max_applications):
        return _fail_closed("apply gate scope.max_applications must be a positive integer")
    raw_consumed_before = scope.get("attempts_consumed_before_this_decision", 0)
    if isinstance(raw_consumed_before, bool):
        return _fail_closed(
            "apply gate scope.attempts_consumed_before_this_decision must be a non-negative integer")
    try:
        attempts_consumed_before = int(raw_consumed_before)
    except (TypeError, ValueError):
        return _fail_closed(
            "apply gate scope.attempts_consumed_before_this_decision must be a non-negative integer")
    if (attempts_consumed_before < 0
            or str(raw_consumed_before).strip() != str(attempts_consumed_before)
            or attempts_consumed_before > max_applications):
        return _fail_closed(
            "apply gate scope.attempts_consumed_before_this_decision must be within the application cap")

    raw_queue_ids = scope.get("queue_ids_allowed")
    queue_ids_allowed: list[str] | None = None
    if raw_queue_ids is not None:
        if not isinstance(raw_queue_ids, list) or not raw_queue_ids:
            return _fail_closed(
                "apply gate scope.queue_ids_allowed must be a non-empty list when present")
        queue_ids_allowed = [str(queue_id or "").strip() for queue_id in raw_queue_ids]
        if (any(not queue_id for queue_id in queue_ids_allowed)
                or len(queue_ids_allowed) != len(set(queue_ids_allowed))):
            return _fail_closed(
                "apply gate scope.queue_ids_allowed contains an empty or duplicate identity")

    normalized_scope: dict[str, Any] = {
        "networks_allowed": normalized_networks,
        "max_applications": max_applications,
        "attempts_consumed_before_this_decision": attempts_consumed_before,
    }
    if queue_ids_allowed is not None:
        normalized_scope["queue_ids_allowed"] = queue_ids_allowed

    return {
        "schema": SCHEMA,
        "paused": bool(data["paused"]),
        "reason": str(data.get("reason") or ""),
        "approved_assets": asset_names,
        "approved_asset_records": [record for _name, record in normalized_assets if record],
        "scope": normalized_scope,
        "set_by": data.get("set_by"),
        "set_at_utc": data.get("set_at_utc"),
        "fail_closed": False,
    }


def blocking_reason(network: str | None = None,
                    path: Path | None = None,
                    application_root: Path | None = None) -> str | None:
    """Return why applying is forbidden, or None when it is allowed.

    `network` is optional so a caller can ask the global question first and the
    per-rail question per candidate.
    """
    state = gate_state(path)
    if state["paused"]:
        detail = state["reason"] or "no reason recorded"
        prefix = "apply gate FAILED CLOSED" if state["fail_closed"] else "apply gate is PAUSED"
        return f"{prefix}: {detail}"

    consumed = application_attempts_consumed(path, application_root or ATTEMPT_ROOT)
    limit = int(state["scope"]["max_applications"])
    if consumed >= limit:
        return (f"operator-approved application limit exhausted: "
                f"{consumed}/{limit} provider attempts consumed under this gate opening")

    if network:
        normalized_network = str(network).strip().lower()
        if normalized_network not in state["scope"]["networks_allowed"]:
            return f"network '{network}' is outside the operator-approved application scope"
        required = NETWORK_PREREQUISITES.get(normalized_network)
        records = {str(record.get("asset")): record
                   for record in state.get("approved_asset_records") or []}
        if required and required not in records:
            return (f"network '{network}' requires the asset '{required}', which the "
                    "operator has not approved and hash-verified")
    return None


def approved_asset_record(asset: str, path: Path | None = None) -> dict[str, Any] | None:
    """Return one verified structured asset record from the current gate."""
    state = gate_state(path)
    if state["fail_closed"]:
        return None
    wanted = str(asset or "").strip()
    return next((record for record in state.get("approved_asset_records") or []
                 if str(record.get("asset") or "") == wanted), None)


def assert_apply_allowed(network: str | None = None,
                         path: Path | None = None,
                         application_root: Path | None = None) -> None:
    """Raise ApplyPaused unless submitting is currently permitted."""
    reason = blocking_reason(network, path, application_root)
    if reason:
        raise ApplyPaused(reason)


def apply_allowed(network: str | None = None, path: Path | None = None,
                  application_root: Path | None = None) -> bool:
    return blocking_reason(network, path, application_root) is None
