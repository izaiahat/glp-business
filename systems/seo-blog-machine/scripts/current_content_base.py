#!/usr/bin/env python3
"""Fail-closed resolution of the active GLP content release.

The checked-in vertical config names only a mutable current-pointer and
release-relative catalog paths.  This module resolves those names to one
verified release under flagship-v2 and returns immutable hashes for every
input.  Call ``verify_resolution`` after consuming the files when a caller
needs to detect a same-run pointer or catalog race.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
from pathlib import Path, PurePosixPath
from typing import Any

BUSINESS_ROOT = Path("/home/ubuntu/business")
if str(BUSINESS_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(BUSINESS_ROOT / "scripts"))
from glp_current_content_base_schema import validate_pointer_shape  # noqa: E402
from glp_price_index_weekly import _validate_base_evidence  # noqa: E402
DEFAULT_FLAGSHIP_ROOT = BUSINESS_ROOT / "site-system/flagship-v2"
POINTER_SCHEMA = "glp-opus-current-content-base-v1"
POINTER_STATUS = "PASS"
REQUIRED_CATALOG_COUNT = 3


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{label}_invalid:{path}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{label}_not_object:{path}")
    return payload


def _contained(path: Path, root: Path, label: str) -> Path:
    resolved = path.resolve(strict=True)
    root_resolved = root.resolve(strict=True)
    if resolved == root_resolved or root_resolved not in resolved.parents:
        raise RuntimeError(f"{label}_root_escape:{resolved}")
    return resolved


def _release_relative_path(value: Any, label: str) -> PurePosixPath:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{label}_release_relative_path_missing")
    pure = PurePosixPath(value)
    if pure.is_absolute() or ".." in pure.parts or "." in pure.parts:
        raise RuntimeError(f"{label}_release_relative_path_invalid:{value}")
    if str(pure) != value or not pure.parts:
        raise RuntimeError(f"{label}_release_relative_path_invalid:{value}")
    return pure


def _resolved_release_file(release_root: Path, relative: Any, label: str) -> Path:
    pure = _release_relative_path(relative, label)
    candidate = _contained(release_root.joinpath(*pure.parts), release_root, label)
    if not candidate.is_file() or candidate.is_symlink():
        raise RuntimeError(f"{label}_file_invalid:{candidate}")
    return candidate


def resolve_current_content_base(
    config: dict[str, Any],
    *,
    flagship_root: Path = DEFAULT_FLAGSHIP_ROOT,
) -> dict[str, Any]:
    """Resolve and hash the current content base named by ``config``.

    Fail closed on pointer schema/status/root drift, root escape, missing or
    duplicate required catalogs, concrete catalog paths, and malformed JSON.
    """
    paths = config.get("paths")
    if not isinstance(paths, dict):
        raise RuntimeError("current_content_base_paths_invalid")
    state_value = paths.get("current_content_base_state")
    if not isinstance(state_value, str) or not state_value:
        raise RuntimeError("current_content_base_state_missing")
    state_path = Path(state_value)
    if not state_path.is_absolute():
        state_path = BUSINESS_ROOT / state_path
    state_path = state_path.resolve(strict=True)
    if not state_path.is_file() or state_path.is_symlink():
        raise RuntimeError(f"current_content_base_state_invalid:{state_path}")

    pointer = _json_object(state_path, "current_content_base_state")
    try:
        validate_pointer_shape(
            pointer,
            releases_root=flagship_root / "releases",
            require_release_dirs=True,
        )
    except RuntimeError as exc:
        raise RuntimeError(f"current_content_base_pointer_invalid:{exc}") from exc
    root_value = pointer.get("release_root")
    if not isinstance(root_value, str) or not os.path.isabs(root_value):
        raise RuntimeError("current_content_base_release_root_invalid")
    release_root = _contained(Path(root_value), flagship_root, "current_content_base")
    if not release_root.is_dir() or release_root.is_symlink():
        raise RuntimeError("current_content_base_release_root_invalid")
    evidence = _validate_base_evidence(pointer, release_root)

    catalogs = paths.get("content_catalogs")
    if not isinstance(catalogs, list) or len(catalogs) != REQUIRED_CATALOG_COUNT:
        raise RuntimeError("current_content_base_catalog_count_invalid")
    catalog_ids = [row.get("catalog_id") for row in catalogs if isinstance(row, dict)]
    if len(catalog_ids) != len(catalogs) or any(not isinstance(value, str) or not value for value in catalog_ids):
        raise RuntimeError("current_content_base_catalog_ids_invalid")
    if len(catalog_ids) != len(set(catalog_ids)):
        raise RuntimeError("current_content_base_catalog_ids_duplicate")

    resolved_catalogs: list[dict[str, Any]] = []
    for catalog in catalogs:
        catalog_id = str(catalog["catalog_id"])
        kind = str(catalog["kind"])
        if "path" in catalog or "registry_path" in catalog:
            raise RuntimeError(f"current_content_base_concrete_catalog_path_forbidden:{kind}")
        path = _resolved_release_file(
            release_root,
            catalog.get("release_relative_path"),
            f"content_catalog:{catalog_id}",
        )
        _json_object(path, f"content_catalog:{catalog_id}")
        resolved_catalogs.append(
            {
                "catalog_id": catalog_id,
                "kind": kind,
                "release_relative_path": catalog["release_relative_path"],
                "path": str(path),
                "sha256": sha256_file(path),
            }
        )

    program_set = config.get("program_set")
    if not isinstance(program_set, dict) or "registry_path" in program_set:
        raise RuntimeError("current_content_base_program_set_config_invalid")
    registry_path = _resolved_release_file(
        release_root,
        program_set.get("release_relative_path"),
        "program_registry",
    )
    _json_object(registry_path, "program_registry")

    return {
        "schema": "glp-current-content-base-resolution-v1",
        "status": "PASS_CURRENT_CONTENT_BASE_RESOLVED",
        "state": {"path": str(state_path), "sha256": sha256_file(state_path)},
        "pointer": {
            "schema": pointer["schema"],
            "status": pointer["status"],
            "deployment_id": pointer["deployment_id"],
            "release_root": str(release_root),
            "release_tree_sha256": evidence["release_tree_sha256"],
            "public_tree_sha256": evidence["public_tree_sha256"],
            "manifest": dict(pointer["manifest"]),
            "final_receipt": dict(pointer["final_receipt"]),
            "supersedes": dict(pointer["supersedes"]),
        },
        "flagship_root": str(flagship_root.resolve(strict=True)),
        "catalogs": resolved_catalogs,
        "program_registry": {
            "release_relative_path": program_set["release_relative_path"],
            "path": str(registry_path),
            "sha256": sha256_file(registry_path),
        },
    }


def bind_current_content_base(
    config: dict[str, Any],
    *,
    flagship_root: Path = DEFAULT_FLAGSHIP_ROOT,
) -> dict[str, Any]:
    """Return a copy with runtime-only resolved paths and resolution evidence."""
    bound = copy.deepcopy(config)
    resolution = resolve_current_content_base(bound, flagship_root=flagship_root)
    by_id = {row["catalog_id"]: row for row in resolution["catalogs"]}
    for catalog in bound["paths"]["content_catalogs"]:
        catalog["_resolved_path"] = by_id[catalog["catalog_id"]]["path"]
        catalog["_resolved_sha256"] = by_id[catalog["catalog_id"]]["sha256"]
    bound["program_set"]["_resolved_registry_path"] = resolution["program_registry"]["path"]
    bound["program_set"]["_resolved_registry_sha256"] = resolution["program_registry"]["sha256"]
    bound["_current_content_base_resolution"] = resolution
    return bound


def verify_resolution(resolution: dict[str, Any]) -> None:
    """Re-hash a prior resolution and reject any pointer/catalog drift."""
    if resolution.get("schema") != "glp-current-content-base-resolution-v1":
        raise RuntimeError("current_content_base_resolution_schema_invalid")
    state = resolution.get("state") or {}
    state_path = Path(str(state.get("path") or ""))
    if not state_path.is_file() or sha256_file(state_path) != state.get("sha256"):
        raise RuntimeError("current_content_base_state_hash_drift")
    pointer = _json_object(state_path, "current_content_base_state")
    if pointer.get("schema") != POINTER_SCHEMA or pointer.get("status") != POINTER_STATUS:
        raise RuntimeError("current_content_base_pointer_drift")
    if Path(str(pointer.get("release_root") or "")).resolve() != Path(
        resolution["pointer"]["release_root"]
    ).resolve():
        raise RuntimeError("current_content_base_release_root_drift")
    rows = list(resolution.get("catalogs") or []) + [resolution.get("program_registry") or {}]
    for row in rows:
        path = Path(str(row.get("path") or ""))
        if not path.is_file() or path.is_symlink() or sha256_file(path) != row.get("sha256"):
            raise RuntimeError(f"current_content_base_file_hash_drift:{path}")


def resolved_catalog_path(config: dict[str, Any], kind: str) -> Path:
    catalog = next(
        (row for row in config.get("paths", {}).get("content_catalogs", []) if row.get("kind") == kind),
        None,
    )
    if not catalog or not catalog.get("_resolved_path"):
        raise RuntimeError(f"content_catalog_not_resolved:{kind}")
    return Path(catalog["_resolved_path"])


def resolved_program_registry_path(config: dict[str, Any]) -> Path:
    path_value = (config.get("program_set") or {}).get("_resolved_registry_path")
    if not path_value:
        raise RuntimeError("program_registry_resolved_path_missing")
    return Path(str(path_value))


def current_content_counts(config: dict[str, Any]) -> dict[str, int]:
    """Return the shared current-release counts used by feeds and reports."""
    catalog_records = 0
    for catalog in config.get("paths", {}).get("content_catalogs") or []:
        payload = _json_object(Path(str(catalog.get("_resolved_path") or "")),
                               f"catalog:{catalog.get('catalog_id')}")
        records = payload.get(catalog.get("records_key"))
        if not isinstance(records, list):
            raise RuntimeError(f"catalog_records_invalid:{catalog.get('catalog_id')}")
        catalog_records += len(records)
    program_payload = _json_object(resolved_program_registry_path(config), "program_registry")
    programs = program_payload.get("programs")
    if not isinstance(programs, list):
        raise RuntimeError("program_registry_records_invalid")
    return {"catalog_records": catalog_records, "programs": len(programs),
            "total": catalog_records + len(programs)}
