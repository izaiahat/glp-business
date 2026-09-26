#!/usr/bin/env python3
"""Validation and binding helpers for immutable Opus topic-selection receipts."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

SCHEMA = "glp-opus-content-selection-receipt-v2"
STATUS = "PASS_STRATEGY_SAFE_SELECTION"
UPDATE_SCHEMA = "glp-opus-content-update-selection-receipt-v1"
UPDATE_STATUS = "PASS_R2_UPDATE_SELECTION"


def canonical(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def payload_hash(payload: dict[str, Any], field: str = "receipt_sha256") -> str:
    body = dict(payload)
    body.pop(field, None)
    return hashlib.sha256(canonical(body)).hexdigest()


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{label}_invalid:{path}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{label}_not_object:{path}")
    return value


def validate_file_spec(spec: Any, label: str, *, require_current_hash: bool = True) -> Path:
    if not isinstance(spec, dict) or not {"path", "sha256"} <= set(spec):
        raise RuntimeError(f"{label}_spec_invalid")
    path = Path(str(spec.get("path") or ""))
    digest = spec.get("sha256")
    if not path.is_absolute() or not isinstance(digest, str) or len(digest) != 64:
        raise RuntimeError(f"{label}_spec_invalid")
    if require_current_hash and (not path.is_file() or sha256_file(path) != digest):
        raise RuntimeError(f"{label}_hash_invalid")
    return path


def validate_selection_payload(payload: dict[str, Any]) -> dict[str, Any]:
    operation = payload.get("operation", "new_article")
    expected_contract = {
        "new_article": (SCHEMA, STATUS),
        "r2_refresh": (UPDATE_SCHEMA, UPDATE_STATUS),
    }.get(operation)
    if expected_contract is None or (payload.get("schema"), payload.get("status")) != expected_contract:
        raise RuntimeError("selection_receipt_contract_invalid")
    if payload.get("receipt_sha256") != payload_hash(payload):
        raise RuntimeError("selection_receipt_self_hash_invalid")
    selected = payload.get("selected")
    if not isinstance(selected, list) or len(selected) != 1 or not isinstance(selected[0], dict):
        raise RuntimeError("selection_receipt_selected_invalid")
    selection = selected[0]
    required = (
        "topic_id", "target_query", "target_route", "operator_directive_id",
        "queue_position", "cluster_id", "depth_stage", "queue_sha256",
    )
    if any(selection.get(key) in (None, "") for key in required):
        raise RuntimeError("selection_receipt_binding_incomplete")
    if selection.get("strategy_safe") is not True:
        raise RuntimeError("selection_receipt_strategy_proof_invalid")
    predecessor = selection.get("predecessor_proof")
    if not isinstance(predecessor, dict) or predecessor.get("satisfied") is not True:
        raise RuntimeError("selection_receipt_predecessor_proof_invalid")
    inputs = payload.get("inputs")
    if not isinstance(inputs, dict):
        raise RuntimeError("selection_receipt_inputs_invalid")
    required_inputs = [
        "topic_queue", "strategy_queue", "strategy_map", "published_ledger",
        "hold_ledger", "current_content_base_state", "sitemap_readback",
    ]
    if operation == "r2_refresh":
        required_inputs.append("update_authority")
        if selection.get("refresh_tier") != "R2" or selection.get("article_id") in (None, ""):
            raise RuntimeError("update_selection_binding_incomplete")
    for key in required_inputs:
        spec = inputs.get(key)
        if not isinstance(spec, dict) or not isinstance(spec.get("path"), str) or not isinstance(spec.get("sha256"), str) or len(spec["sha256"]) != 64:
            raise RuntimeError(f"selection_receipt_input_spec_invalid:{key}")
    resolution = inputs.get("current_content_base_resolution")
    if not isinstance(resolution, dict) or len(resolution.get("catalogs") or []) != 3:
        raise RuntimeError("selection_receipt_content_base_resolution_invalid")
    if any(not isinstance(row.get("sha256"), str) or len(row["sha256"]) != 64 for row in resolution["catalogs"]):
        raise RuntimeError("selection_receipt_catalog_hashes_invalid")
    return selection


def load_selection_receipt(
    path: str | Path,
    *,
    expected_sha256: str | None = None,
    verify_receipt_file: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    source = Path(path).resolve()
    if not source.is_file():
        raise RuntimeError("selection_receipt_missing")
    if expected_sha256 is not None and sha256_file(source) != expected_sha256:
        raise RuntimeError("selection_receipt_file_hash_invalid")
    payload = _read_object(source, "selection_receipt")
    selection = validate_selection_payload(payload)
    if verify_receipt_file and stat.S_IMODE(source.stat().st_mode) & 0o222:
        raise RuntimeError("selection_receipt_not_immutable")
    return payload, selection


def selection_spec(path: str | Path) -> dict[str, str]:
    source = Path(path).resolve()
    payload, _ = load_selection_receipt(source)
    return {
        "path": str(source),
        "sha256": sha256_file(source),
        "receipt_sha256": payload["receipt_sha256"],
    }


def selection_binding(payload: dict[str, Any]) -> dict[str, Any]:
    selection = validate_selection_payload(payload)
    return {
        "topic_id": selection["topic_id"],
        "target_query": selection["target_query"],
        "target_route": selection["target_route"],
        "operator_directive_id": selection["operator_directive_id"],
        "queue_position": selection["queue_position"],
        "queue_sha256": selection["queue_sha256"],
        "strategy_queue_sha256": payload["inputs"]["strategy_queue"]["sha256"],
        "strategy_map_sha256": payload["inputs"]["strategy_map"]["sha256"],
        "selection_receipt_sha256": payload["receipt_sha256"],
    }


def write_immutable_receipt(path: str | Path, payload: dict[str, Any]) -> Path:
    target = Path(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    sealed = dict(payload)
    sealed["receipt_sha256"] = payload_hash(sealed)
    validate_selection_payload(sealed)
    rendered = canonical(sealed)
    if target.exists():
        existing = target.read_bytes()
        if existing != rendered:
            raise RuntimeError("immutable_selection_receipt_conflict")
        os.chmod(target, 0o444)
        return target
    fd, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o444)
        try:
            os.link(temporary, target)
        except FileExistsError:
            if target.read_bytes() != rendered:
                raise RuntimeError("immutable_selection_receipt_conflict")
        finally:
            temporary.unlink(missing_ok=True)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    directory_fd = os.open(target.parent, os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return target


def validate_authority_selection_binding(
    authority: dict[str, Any],
    receipt_path: str | Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
    spec = authority.get("selection_receipt")
    if not isinstance(spec, dict):
        raise RuntimeError("packet_selection_receipt_missing")
    path = Path(receipt_path).resolve() if receipt_path is not None else validate_file_spec(spec, "packet_selection_receipt")
    if str(path) != spec.get("path"):
        raise RuntimeError("packet_selection_receipt_path_mismatch")
    payload, selection = load_selection_receipt(path, expected_sha256=spec.get("sha256"))
    if spec.get("receipt_sha256") != payload["receipt_sha256"]:
        raise RuntimeError("packet_selection_receipt_payload_hash_mismatch")
    expected_binding = selection_binding(payload)
    if authority.get("selection_binding") != expected_binding:
        raise RuntimeError("packet_selection_binding_mismatch")
    return payload, selection, spec
