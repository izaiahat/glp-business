#!/usr/bin/env python3
"""Exact schema helpers for the canonical GLP moving content-base pointer.

This module is deliberately provider-free.  Every active writer and consumer
must use the same outer pointer and ``supersedes`` contract before trusting any
release or deployment identity.
"""
from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import Any

POINTER_SCHEMA = "glp-opus-current-content-base-v1"
POINTER_STATUS = "PASS"
DEPLOYMENT_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SUPERSEDES_KEYS = frozenset({"deployment_id", "release_root"})
EVIDENCE_SPEC_KEYS = frozenset({"path", "sha256"})
COUNT_KEYS = frozenset({"release_files", "release_bytes", "public_files", "public_bytes"})
POINTER_REQUIRED_KEYS = frozenset(
    {
        "schema",
        "status",
        "deployment_id",
        "release_root",
        "release_tree_sha256",
        "public_tree_sha256",
        "manifest",
        "final_receipt",
        "supersedes",
    }
)
POINTER_OPTIONAL_KEYS = frozenset({"reminted_at_utc"}) | COUNT_KEYS
POINTER_ALLOWED_KEYS = POINTER_REQUIRED_KEYS | POINTER_OPTIONAL_KEYS


def require_deployment_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or DEPLOYMENT_ID_RE.fullmatch(value) is None:
        raise RuntimeError(f"{label}_deployment_id_invalid")
    return value


def require_sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise RuntimeError(f"{label}_sha256_invalid")
    return value


def require_release_root(
    value: Any,
    label: str,
    *,
    releases_root: Path | None = None,
    require_exists: bool = True,
) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or not os.path.isabs(value)
    ):
        raise RuntimeError(f"{label}_release_root_invalid")
    path = Path(value)
    if Path(os.path.normpath(value)) != path:
        raise RuntimeError(f"{label}_release_root_invalid")
    try:
        resolved = path.resolve(strict=require_exists)
        if str(resolved) != value:
            raise RuntimeError
        if require_exists:
            info = os.lstat(path)
            if path.is_symlink() or not stat.S_ISDIR(info.st_mode):
                raise RuntimeError
        if releases_root is not None:
            root = releases_root.resolve(strict=require_exists)
            if resolved.parent != root:
                raise RuntimeError
    except (OSError, RuntimeError):
        raise RuntimeError(f"{label}_release_root_invalid") from None
    return value


def validate_evidence_spec(value: Any, label: str) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != EVIDENCE_SPEC_KEYS:
        raise RuntimeError(f"{label}_spec_invalid")
    path = value.get("path")
    if (
        not isinstance(path, str)
        or not path
        or "\x00" in path
        or not os.path.isabs(path)
        or Path(os.path.normpath(path)) != Path(path)
    ):
        raise RuntimeError(f"{label}_path_invalid")
    return {"path": path, "sha256": require_sha256(value.get("sha256"), label)}


def validate_supersedes(
    value: Any,
    *,
    releases_root: Path | None = None,
    current_deployment_id: str | None = None,
    current_release_root: str | None = None,
    require_exists: bool = True,
    label: str = "current_content_base_supersedes",
) -> dict[str, str]:
    """Validate the one exact predecessor identity schema.

    Both identity components are mandatory and no extension keys are accepted.
    A pointer may never claim that its current identity supersedes itself.
    """
    if not isinstance(value, dict) or set(value) != SUPERSEDES_KEYS:
        raise RuntimeError(f"{label}_fields_invalid")
    deployment_id = require_deployment_id(value.get("deployment_id"), label)
    release_root = require_release_root(
        value.get("release_root"),
        label,
        releases_root=releases_root,
        require_exists=require_exists,
    )
    if current_deployment_id is not None and deployment_id == current_deployment_id:
        raise RuntimeError(f"{label}_deployment_id_not_predecessor")
    if current_release_root is not None and release_root == current_release_root:
        raise RuntimeError(f"{label}_release_root_not_predecessor")
    return {"deployment_id": deployment_id, "release_root": release_root}


def validate_pointer_shape(
    pointer: Any,
    *,
    releases_root: Path | None = None,
    require_release_dirs: bool = True,
    allow_missing: frozenset[str] = frozenset(),
    label: str = "current_content_base",
) -> dict[str, Any]:
    """Validate the exact versioned outer pointer and predecessor schema.

    ``allow_missing`` exists only for a writer's bounded same-deployment repair
    path.  Consumers must use the default and therefore require every field.
    """
    if not isinstance(pointer, dict):
        raise RuntimeError(f"{label}_not_object")
    missing = POINTER_REQUIRED_KEYS - set(pointer) - set(allow_missing)
    unexpected = set(pointer) - POINTER_ALLOWED_KEYS
    if missing or unexpected:
        raise RuntimeError(f"{label}_fields_invalid")
    if "schema" in pointer and pointer.get("schema") != POINTER_SCHEMA:
        raise RuntimeError(f"{label}_schema_invalid")
    if "status" in pointer and pointer.get("status") != POINTER_STATUS:
        raise RuntimeError(f"{label}_status_invalid")

    deployment_id = None
    release_root = None
    if "deployment_id" in pointer:
        deployment_id = require_deployment_id(pointer.get("deployment_id"), label)
    if "release_root" in pointer:
        release_root = require_release_root(
            pointer.get("release_root"),
            label,
            releases_root=releases_root,
            require_exists=require_release_dirs,
        )
    for field in ("release_tree_sha256", "public_tree_sha256"):
        if field in pointer:
            require_sha256(pointer.get(field), f"{label}_{field.removesuffix('_sha256')}")
    for field in ("manifest", "final_receipt"):
        if field in pointer:
            validate_evidence_spec(pointer.get(field), f"{label}_{field}")
    if "supersedes" in pointer:
        validate_supersedes(
            pointer.get("supersedes"),
            releases_root=releases_root,
            current_deployment_id=deployment_id,
            current_release_root=release_root,
            require_exists=require_release_dirs,
            label=f"{label}_supersedes",
        )
    if "reminted_at_utc" in pointer and (
        not isinstance(pointer["reminted_at_utc"], str) or not pointer["reminted_at_utc"]
    ):
        raise RuntimeError(f"{label}_reminted_at_utc_invalid")
    for field in COUNT_KEYS:
        if field in pointer and (type(pointer[field]) is not int or pointer[field] < 0):
            raise RuntimeError(f"{label}_{field}_invalid")
    return pointer
