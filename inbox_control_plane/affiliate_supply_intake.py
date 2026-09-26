#!/usr/bin/env python3
"""Private inbox-control-plane newsletter artifacts for supply discovery.

This module deliberately imports no provider clients. The canonical inbox
control plane calls it only after exact GLP-mailbox and affiliate-network
classification, then writes one deterministic local artifact. It never changes
mailbox, notification, label, application, Sheet, or queue state.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import stat
from typing import Any
from pathlib import Path

INTAKE_ROOT = Path(
    "/home/ubuntu/business/reports/glp-clinic-scout/affiliate-machine/"
    "newsletter-supply-intake"
)
GLP_MAILBOX_IDS = frozenset({"glp_ops_gmail", "glp_izaiah_gmail"})
EVENT_ID_RE = re.compile(r"\Aevt_[0-9a-f]{28}\Z")

SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [^-\n]*PRIVATE KEY-----.*?-----END [^-\n]*PRIVATE KEY-----", re.I | re.S),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"(?i)\b(?:authorization|api[_ -]?key|client[_ -]?secret|access[_ -]?token|"
               r"refresh[_ -]?token|password)\b\s*(?:=|:)\s*[^\s,;]+"),
)


def _utc() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _redact(value: Any) -> str:
    text = str(value or "")
    for pattern in SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


def build_artifact(
    parsed: dict[str, Any],
    classification: dict[str, Any],
    *,
    mailbox_id: str,
    provider_message_id: str,
    event_id: str,
    affiliate_network_root: str,
    captured_at_utc: str | None = None,
) -> dict[str, Any] | None:
    """Return a bounded canonical newsletter artifact, or None for other mail."""
    category = str(classification.get("category") or "").lower()
    if category not in {"newsletter", "affiliate_network_response"}:
        return None
    message_id = str(provider_message_id or "").strip()
    mailbox = str(mailbox_id or "").strip()
    event = str(event_id or "").strip()
    network_root = str(affiliate_network_root or "").strip().lower().rstrip(".")
    captured = str(captured_at_utc or _utc()).strip()
    if mailbox not in GLP_MAILBOX_IDS or not message_id or not EVENT_ID_RE.fullmatch(event):
        raise ValueError("affiliate_supply_newsletter_identity_missing")
    if not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,63}", network_root):
        raise ValueError("affiliate_supply_newsletter_network_identity_missing")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", captured):
        raise ValueError("affiliate_supply_newsletter_timestamp_invalid")
    return {
        "schema": "inbox-control-plane-affiliate-supply-intake-v1",
        "canonical_inbox_artifact": True,
        "captured_at_utc": captured,
        "event_id": event,
        "mailbox_id": mailbox,
        "affiliate_network_root": network_root,
        "provider_message_id_sha256": hashlib.sha256(message_id.encode()).hexdigest(),
        "category": category,
        "reason_code": str(classification.get("reason_code") or ""),
        "sender": _redact(parsed.get("sender") or "")[:320],
        "subject": _redact(parsed.get("subject") or "")[:500],
        "body_text": _redact(parsed.get("body_text") or "")[:400_000],
        "normal_notification_route": str(classification.get("route") or "silent"),
        "normal_notification_behavior_unchanged": True,
        "application_state_unchanged": True,
        "effects": {
            "provider_reads": 0,
            "provider_writes": 0,
            "emails_sent": 0,
            "message_labels_changed": 0,
            "applications_submitted": 0,
            "sheet_writes": 0,
        },
        "secret_printed": False,
    }


def _private_root(root: Path) -> None:
    root = Path(root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    current = Path(root.anchor)
    for component in root.parts[1:]:
        current /= component
        current_stat = os.lstat(current)
        if stat.S_ISLNK(current_stat.st_mode) or not stat.S_ISDIR(current_stat.st_mode):
            raise RuntimeError("affiliate_supply_intake_root_unsafe")
    os.chmod(root, 0o700)


def _validate_existing(path: Path, expected: bytes) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise RuntimeError("affiliate_supply_intake_artifact_unsafe") from exc
    with os.fdopen(descriptor, "rb", closefd=True) as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeError("affiliate_supply_intake_artifact_unsafe")
        if info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600:
            raise RuntimeError("affiliate_supply_intake_artifact_permissions_invalid")
        if handle.read() != expected:
            raise RuntimeError("affiliate_supply_intake_artifact_conflict")


def write_artifact(artifact: dict[str, Any], *, root: Path | None = None) -> Path:
    """Create one immutable-by-content private artifact without replacing bytes."""
    event_id = str(artifact.get("event_id") or "")
    if artifact.get("schema") != "inbox-control-plane-affiliate-supply-intake-v1" \
            or not EVENT_ID_RE.fullmatch(event_id):
        raise ValueError("affiliate_supply_intake_artifact_invalid")
    target_root = Path(root or INTAKE_ROOT)
    _private_root(target_root)
    target = target_root / f"{event_id}.json"
    payload = (json.dumps(artifact, indent=2, sort_keys=True) + "\n").encode()
    if target.exists() or target.is_symlink():
        _validate_existing(target, payload)
        return target

    temporary = target_root / f".{event_id}.{os.getpid()}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, target, follow_symlinks=False)
        temporary.unlink()
        directory_descriptor = os.open(target_root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except FileExistsError:
        temporary.unlink(missing_ok=True)
        _validate_existing(target, payload)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    _validate_existing(target, payload)
    return target
