#!/usr/bin/env python3
"""Webhook-bound authority and receipt attestation for GLP postback portal writes.

Only the active n8n onboarding webhook execution may mint a short-lived child
capability. The capability crosses into a registered provider worker through an
inherited anonymous pipe, never argv, environment, stdout, or a receipt.
"""
from __future__ import annotations

import base64
import datetime as dt
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

N8N_BASE = "https://hermesops.app.n8n.cloud"
N8N_API_KEY_PATH = Path("/home/ubuntu/.hermes/secrets/n8n/native-api-key.txt")
AUTHORITY_KEY_PATH = Path(
    "/home/ubuntu/.hermes/secrets/glp-affiliate-postback/portal-authority-key.txt"
)
CAPABILITY_PRIVATE_KEY_PATH = Path(
    "/home/ubuntu/.hermes/secrets/glp-affiliate-postback/portal-capability-ed25519-private.pem"
)
CAPABILITY_PUBLIC_KEY_PATH = Path(
    "/home/ubuntu/.hermes/secrets/glp-affiliate-postback/portal-capability-ed25519-public.pem"
)
BRIDGE_ENTRY_PUBLIC_KEY_PATH = Path(
    "/home/ubuntu/.hermes/secrets/n8n-bridge/onboarding-entry-ed25519-public.pem"
)
BRIDGE_ENTRY_NONCE_STATE_PATH = Path(
    "/home/ubuntu/.hermes/state/n8n-bridge/onboarding-entry-consumed-nonces.json"
)
ONBOARDING_WORKFLOW_ID = "8lnjTRUBrG7zCgPn"
BRIDGE_ENTRY_SCHEMA = "n8n_bridge.entry_authority.v1"
BRIDGE_ENTRY_SCOPE = "onboarding_manifest"
BRIDGE_ENTRY_TTL_S = 90
CAPABILITY_SCHEMA = "glp.postback_portal_authority.v3"
ATTESTATION_SCHEMA = "glp.postback_receipt_attestation.v1"
LEGACY_OPERATION_SCHEMA = "glp.postback_portal_operation.v1"
OPERATION_SCHEMA = "glp.postback_portal_operation.v3"
CAPABILITY_TTL_S = 900
_MAX_CAPABILITY_BYTES = 16 * 1024
TRANSPORT_EXECUTION_ID_PARAMETER = "n8n_execution_id"
TRANSPORT_RUN_ID_PREFIX = "n8n_"
_EXECUTION_ID = re.compile(r"^[0-9]{1,24}$")
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_PROVIDER = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
_WORKER = re.compile(r"^[A-Za-z0-9_.:-]{3,160}$")
_PROGRAM_ID = re.compile(r"^[0-9]{1,24}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_BASE64 = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")


class AuthorityError(RuntimeError):
    """Fail-closed execution, capability, or receipt authority failure."""


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_value(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def extract_transport_execution_id(callback_url: str) -> tuple[str, str]:
    """Read the bridge transport binding and return a clean callback URL."""
    value = str(callback_url or "").strip()
    if not value:
        return "", ""
    parsed = urllib.parse.urlsplit(value)
    pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    execution_ids = [
        item_value
        for item_key, item_value in pairs
        if item_key == TRANSPORT_EXECUTION_ID_PARAMETER
    ]
    if not execution_ids:
        return "", value
    if len(execution_ids) != 1 or not _EXECUTION_ID.fullmatch(execution_ids[0]):
        raise AuthorityError("entry_transport_execution_id_invalid")
    if parsed.scheme != "https" or parsed.hostname != "hermesops.app.n8n.cloud":
        raise AuthorityError("entry_transport_callback_origin_invalid")
    if not parsed.path.startswith("/webhook-waiting/"):
        raise AuthorityError("entry_transport_callback_path_invalid")
    clean_query = urllib.parse.urlencode(
        [
            (item_key, item_value)
            for item_key, item_value in pairs
            if item_key != TRANSPORT_EXECUTION_ID_PARAMETER
        ]
    )
    clean_url = urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, clean_query, parsed.fragment)
    )
    return execution_ids[0], clean_url


def extract_transport_run_id(value: str) -> tuple[str, str]:
    """Extract the n8n execution binding and preserve any requested run ID."""
    raw = str(value or "").strip()
    if not raw.startswith(TRANSPORT_RUN_ID_PREFIX):
        return "", raw
    match = re.fullmatch(r"n8n_([0-9]{1,24})(?:__(.*))?", raw)
    if not match:
        raise AuthorityError("entry_transport_run_id_invalid")
    execution_id = match.group(1)
    clean_run_id = str(match.group(2) or "").strip()
    if clean_run_id and not _RUN_ID.fullmatch(clean_run_id):
        raise AuthorityError("entry_transport_clean_run_id_invalid")
    return execution_id, clean_run_id


def _load_private_key(path: Path = AUTHORITY_KEY_PATH) -> bytes:
    if not path.is_file():
        raise AuthorityError("postback_authority_key_missing")
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise AuthorityError("postback_authority_key_permissions_invalid")
    value = path.read_bytes().strip()
    if len(value) < 43:
        raise AuthorityError("postback_authority_key_entropy_invalid")
    return value


def ensure_private_key(path: Path = AUTHORITY_KEY_PATH) -> tuple[bytes, bool]:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    created = False
    if not path.exists():
        value = secrets.token_urlsafe(48).encode("ascii")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(value + b"\n")
        except Exception:
            path.unlink(missing_ok=True)
            raise
        created = True
    return _load_private_key(path), created


def _mac(value: Any, key: bytes) -> str:
    return hmac.new(key, canonical_bytes(value), hashlib.sha256).hexdigest()


def _load_ed25519_private(path: Path) -> Ed25519PrivateKey:
    if not path.is_file() or path.is_symlink():
        raise AuthorityError("postback_capability_private_key_missing_or_invalid")
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise AuthorityError("postback_capability_private_key_permissions_invalid")
    try:
        value = serialization.load_pem_private_key(path.read_bytes(), password=None)
    except (TypeError, ValueError) as exc:
        raise AuthorityError("postback_capability_private_key_invalid") from exc
    if not isinstance(value, Ed25519PrivateKey):
        raise AuthorityError("postback_capability_private_key_type_invalid")
    return value


def _load_ed25519_public(path: Path, *, reason_prefix: str) -> Ed25519PublicKey:
    if not path.is_file() or path.is_symlink():
        raise AuthorityError(reason_prefix + "_public_key_missing_or_invalid")
    try:
        value = serialization.load_pem_public_key(path.read_bytes())
    except (TypeError, ValueError) as exc:
        raise AuthorityError(reason_prefix + "_public_key_invalid") from exc
    if not isinstance(value, Ed25519PublicKey):
        raise AuthorityError(reason_prefix + "_public_key_type_invalid")
    return value


def ensure_capability_keypair(
    private_path: Path = CAPABILITY_PRIVATE_KEY_PATH,
    public_path: Path = CAPABILITY_PUBLIC_KEY_PATH,
    *,
    allow_create: bool = True,
) -> bool:
    """Create one capability signing pair; the child receives only the public key."""
    if allow_create:
        private_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(private_path.parent, 0o700)
    elif not private_path.parent.is_dir():
        raise AuthorityError("reviewed_capability_keypair_directory_missing")
    if private_path.exists() or public_path.exists():
        _load_ed25519_private(private_path)
        _load_ed25519_public(public_path, reason_prefix="postback_capability")
        return False
    if not allow_create:
        raise AuthorityError("reviewed_capability_keypair_missing")
    private = Ed25519PrivateKey.generate()
    private_bytes = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_bytes = private.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    private_fd = os.open(private_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(private_fd, "wb") as handle:
            handle.write(private_bytes)
        public_fd = os.open(public_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with os.fdopen(public_fd, "wb") as handle:
            handle.write(public_bytes)
    except Exception:
        private_path.unlink(missing_ok=True)
        public_path.unlink(missing_ok=True)
        raise
    return True


def _read_anonymous_pipe(fd: int, *, max_bytes: int, reason_prefix: str) -> bytes:
    if not isinstance(fd, int) or fd < 3:
        raise AuthorityError(reason_prefix + "_fd_invalid")
    try:
        info = os.fstat(fd)
        target = os.readlink(f"/proc/self/fd/{fd}")
    except OSError as exc:
        raise AuthorityError(reason_prefix + "_fd_unavailable") from exc
    if not stat.S_ISFIFO(info.st_mode) or not target.startswith("pipe:["):
        raise AuthorityError(reason_prefix + "_fd_not_anonymous_pipe")
    chunks: list[bytes] = []
    total = 0
    try:
        while True:
            chunk = os.read(fd, min(4096, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise AuthorityError(reason_prefix + "_payload_too_large")
    finally:
        os.close(fd)
    return b"".join(chunks)


def _consume_bridge_entry_nonce(
    admission: dict[str, Any], *, state_path: Path, now_epoch: int
) -> None:
    """Atomically reject replay of a valid scoped bridge admission."""
    state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(state_path.parent, 0o700)
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(state_path, flags, 0o600)
    except OSError as exc:
        raise AuthorityError("bridge_entry_nonce_state_unavailable") from exc
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise AuthorityError("bridge_entry_nonce_state_invalid")
        os.lseek(fd, 0, os.SEEK_SET)
        raw = os.read(fd, 64 * 1024)
        try:
            stored = json.loads(raw.decode("utf-8")) if raw.strip() else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AuthorityError("bridge_entry_nonce_state_invalid") from exc
        if not isinstance(stored, dict):
            raise AuthorityError("bridge_entry_nonce_state_invalid")
        active = {
            str(nonce): int(expires)
            for nonce, expires in stored.items()
            if str(expires).isdigit() and int(expires) >= now_epoch
        }
        nonce = str(admission["nonce"])
        if nonce in active:
            raise AuthorityError("bridge_entry_admission_replayed")
        active[nonce] = int(admission["expires_at_epoch"])
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, canonical_bytes(active) + b"\n")
        os.fsync(fd)
        os.fchmod(fd, 0o600)
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def read_bridge_entry_authority_fd(
    fd: int,
    *,
    argv: list[str],
    public_key_path: Path = BRIDGE_ENTRY_PUBLIC_KEY_PATH,
    nonce_state_path: Path = BRIDGE_ENTRY_NONCE_STATE_PATH,
    now_epoch: int | None = None,
) -> dict[str, Any]:
    """Verify the scoped bridge issuer and exact manifest argv before effects."""
    raw = _read_anonymous_pipe(
        fd, max_bytes=_MAX_CAPABILITY_BYTES, reason_prefix="bridge_entry_authority"
    )
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise AuthorityError("bridge_entry_authority_payload_invalid_json") from exc
    if not isinstance(value, dict):
        raise AuthorityError("bridge_entry_authority_payload_not_object")
    expected = {
        "schema", "scope", "action_name", "argv_sha256", "issued_at_epoch",
        "expires_at_epoch", "nonce", "ed25519_signature_b64",
    }
    if set(value) != expected:
        raise AuthorityError("bridge_entry_authority_shape_invalid")
    signature_text = str(value.get("ed25519_signature_b64") or "")
    if not _BASE64.fullmatch(signature_text):
        raise AuthorityError("bridge_entry_authority_signature_invalid")
    try:
        signature = base64.b64decode(signature_text, validate=True)
    except ValueError as exc:
        raise AuthorityError("bridge_entry_authority_signature_invalid") from exc
    unsigned = {key: item for key, item in value.items() if key != "ed25519_signature_b64"}
    try:
        _load_ed25519_public(
            public_key_path, reason_prefix="bridge_entry_authority"
        ).verify(signature, canonical_bytes(unsigned))
    except InvalidSignature as exc:
        raise AuthorityError("bridge_entry_authority_signature_invalid") from exc
    if value.get("schema") != BRIDGE_ENTRY_SCHEMA:
        raise AuthorityError("bridge_entry_authority_schema_invalid")
    if value.get("scope") != BRIDGE_ENTRY_SCOPE:
        raise AuthorityError("bridge_entry_authority_scope_invalid")
    if value.get("action_name") != "onboarding_manifest_run":
        raise AuthorityError("bridge_entry_authority_action_invalid")
    clean_argv = list(argv)
    indexes = [index for index, item in enumerate(clean_argv) if item == "--entry-authority-fd"]
    if len(indexes) != 1:
        raise AuthorityError("bridge_entry_authority_argv_fd_shape_invalid")
    index = indexes[0]
    if index + 1 >= len(clean_argv) or clean_argv[index + 1] != str(fd):
        raise AuthorityError("bridge_entry_authority_argv_fd_mismatch")
    del clean_argv[index:index + 2]
    if not clean_argv or not clean_argv[0].endswith("scripts/glp_onboarding_manifest.py"):
        raise AuthorityError("bridge_entry_authority_argv_target_invalid")
    argv_sha = hashlib.sha256(canonical_bytes(clean_argv)).hexdigest()
    if not hmac.compare_digest(str(value.get("argv_sha256") or ""), argv_sha):
        raise AuthorityError("bridge_entry_authority_argv_mismatch")
    now = int(dt.datetime.now(dt.timezone.utc).timestamp()) if now_epoch is None else int(now_epoch)
    issued = int(value.get("issued_at_epoch") or 0)
    expires = int(value.get("expires_at_epoch") or 0)
    if issued > now + 5 or expires <= now or expires - issued != BRIDGE_ENTRY_TTL_S:
        raise AuthorityError("bridge_entry_authority_expired_or_invalid")
    _consume_bridge_entry_nonce(
        value,
        state_path=nonce_state_path,
        now_epoch=now,
    )
    return {
        "schema": BRIDGE_ENTRY_SCHEMA,
        "scope": BRIDGE_ENTRY_SCOPE,
        "action_name": "onboarding_manifest_run",
        "issued_at_epoch": issued,
        "expires_at_epoch": expires,
        "admission_sha256": sha256_value(value),
    }


def _fetch_execution(execution_id: str, *, api_key_path: Path = N8N_API_KEY_PATH) -> dict[str, Any]:
    api_key = api_key_path.read_text(encoding="utf-8").strip()
    if not api_key:
        raise AuthorityError("n8n_api_key_missing")
    url = (
        N8N_BASE
        + "/api/v1/executions/"
        + urllib.parse.quote(execution_id, safe="")
        + "?includeData=false"
    )
    request = urllib.request.Request(
        url,
        headers={"X-N8N-API-KEY": api_key, "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
    except (OSError, urllib.error.URLError) as exc:
        raise AuthorityError("n8n_execution_readback_unavailable") from exc
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise AuthorityError("n8n_execution_readback_invalid_json") from exc
    if not isinstance(value, dict):
        raise AuthorityError("n8n_execution_readback_not_object")
    return value


def verify_entry_execution(
    execution_id: str,
    *,
    fetch: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Prove the caller is the still-open canonical onboarding webhook execution."""
    execution_id = str(execution_id or "").strip()
    if not _EXECUTION_ID.fullmatch(execution_id):
        raise AuthorityError("onboarding_webhook_execution_id_invalid")
    value = (fetch or _fetch_execution)(execution_id)
    if str(value.get("id") or "") != execution_id:
        raise AuthorityError("onboarding_webhook_execution_identity_mismatch")
    if str(value.get("workflowId") or "") != ONBOARDING_WORKFLOW_ID:
        raise AuthorityError("onboarding_webhook_workflow_identity_mismatch")
    if str(value.get("mode") or "") != "webhook":
        raise AuthorityError("onboarding_entry_source_not_webhook")
    status = str(value.get("status") or "")
    if value.get("finished") is not False:
        raise AuthorityError("onboarding_webhook_execution_not_open")
    if status not in {"new", "running", "waiting"}:
        raise AuthorityError("onboarding_webhook_execution_status_invalid")
    # n8n sets stoppedAt when it suspends an execution at a Wait node even though
    # finished remains false. That suspended waiting execution is precisely what
    # the queued bridge job must resume; reject stoppedAt only outside waiting.
    if status != "waiting" and value.get("stoppedAt") not in (None, ""):
        raise AuthorityError("onboarding_webhook_execution_not_open")
    safe = {
        "execution_id": execution_id,
        "workflow_id": ONBOARDING_WORKFLOW_ID,
        "mode": "webhook",
        "status": status,
        "started_at_utc": str(value.get("startedAt") or ""),
        "finished": False,
        "readback_sha256": sha256_value({
            key: value.get(key)
            for key in (
                "id", "workflowId", "mode", "status", "startedAt", "stoppedAt", "finished"
            )
        }),
    }
    return safe


def validate_program_registration(
    value: Any, *, provider: str, network: str | None = None
) -> dict[str, Any]:
    """Validate the governed-row program identity bound into a child capability.

    The onboarding manifest is the sole registrar: it copies these fields from
    the exact governed Sheet row it bound to the site provider. Child workers
    consume this signed registration instead of carrying a provider program ID
    in source, argv, or environment state.
    """
    expected = {"provider", "network", "program_name", "program_id", "sheet_row"}
    if not isinstance(value, dict) or set(value) != expected:
        raise AuthorityError("postback_program_registration_shape_invalid")
    registered_provider = str(value.get("provider") or "").strip()
    registered_network = str(value.get("network") or "").strip()
    program_name = str(value.get("program_name") or "").strip()
    program_id = str(value.get("program_id") or "").strip()
    sheet_row = value.get("sheet_row")
    if registered_provider != str(provider or "").strip() or not _PROVIDER.fullmatch(
        registered_provider
    ):
        raise AuthorityError("postback_program_registration_provider_invalid")
    if not registered_network or any(char in registered_network for char in "\r\n\x00"):
        raise AuthorityError("postback_program_registration_network_invalid")
    if network is not None and registered_network.casefold() != str(network).strip().casefold():
        raise AuthorityError("postback_program_registration_network_mismatch")
    if not program_name or len(program_name) > 160 or any(
        char in program_name for char in "\r\n\x00"
    ):
        raise AuthorityError("postback_program_registration_name_invalid")
    if not _PROGRAM_ID.fullmatch(program_id):
        raise AuthorityError("postback_program_registration_id_invalid")
    if not isinstance(sheet_row, int) or isinstance(sheet_row, bool) or sheet_row < 2:
        raise AuthorityError("postback_program_registration_sheet_row_invalid")
    return {
        "provider": registered_provider,
        "network": registered_network,
        "program_name": program_name,
        "program_id": program_id,
        "sheet_row": sheet_row,
    }


def mint_capability(
    entry: dict[str, Any],
    *,
    run_id: str,
    provider: str,
    worker_id: str,
    program: dict[str, Any],
    private_key_path: Path = CAPABILITY_PRIVATE_KEY_PATH,
    public_key_path: Path = CAPABILITY_PUBLIC_KEY_PATH,
    now_epoch: int | None = None,
) -> dict[str, Any]:
    run_id = str(run_id or "").strip()
    provider = str(provider or "").strip()
    worker_id = str(worker_id or "").strip()
    if not _RUN_ID.fullmatch(run_id):
        raise AuthorityError("manifest_run_id_invalid")
    if not _PROVIDER.fullmatch(provider):
        raise AuthorityError("postback_provider_invalid")
    if not _WORKER.fullmatch(worker_id):
        raise AuthorityError("postback_worker_identity_invalid")
    registered_program = validate_program_registration(program, provider=provider)
    if entry.get("workflow_id") != ONBOARDING_WORKFLOW_ID or entry.get("mode") != "webhook":
        raise AuthorityError("onboarding_entry_authority_invalid")
    execution_id = str(entry.get("execution_id") or "")
    if not _EXECUTION_ID.fullmatch(execution_id):
        raise AuthorityError("onboarding_entry_execution_id_invalid")
    bridge_admission_sha = str(entry.get("bridge_entry_admission_sha256") or "")
    if not _SHA256.fullmatch(bridge_admission_sha):
        raise AuthorityError("bridge_entry_admission_binding_missing")
    ensure_capability_keypair(private_key_path, public_key_path)
    issued = int(dt.datetime.now(dt.timezone.utc).timestamp()) if now_epoch is None else int(now_epoch)
    unsigned = {
        "schema": CAPABILITY_SCHEMA,
        "operation_id": secrets.token_hex(16),
        "onboarding_execution_id": execution_id,
        "onboarding_workflow_id": ONBOARDING_WORKFLOW_ID,
        "execution_mode": "webhook",
        "manifest_run_id": run_id,
        "provider": provider,
        "worker_id": worker_id,
        "program": registered_program,
        "bridge_entry_admission_sha256": bridge_admission_sha,
        "issued_at_epoch": issued,
        "expires_at_epoch": issued + CAPABILITY_TTL_S,
        "nonce": secrets.token_hex(16),
    }
    signature = _load_ed25519_private(private_key_path).sign(canonical_bytes(unsigned))
    return {
        **unsigned,
        "ed25519_signature_b64": base64.b64encode(signature).decode("ascii"),
    }


def _validate_capability(
    value: dict[str, Any],
    *,
    provider: str,
    run_id: str,
    worker_id: str,
    public_key_path: Path = CAPABILITY_PUBLIC_KEY_PATH,
    now_epoch: int | None = None,
) -> dict[str, Any]:
    expected_keys = {
        "schema", "operation_id", "onboarding_execution_id", "onboarding_workflow_id",
        "execution_mode", "manifest_run_id", "provider", "worker_id", "program",
        "issued_at_epoch",
        "expires_at_epoch", "nonce", "bridge_entry_admission_sha256",
        "ed25519_signature_b64",
    }
    if set(value) != expected_keys:
        raise AuthorityError("postback_authority_shape_invalid")
    signature_text = str(value.get("ed25519_signature_b64") or "")
    if not _BASE64.fullmatch(signature_text):
        raise AuthorityError("postback_authority_signature_invalid")
    try:
        signature = base64.b64decode(signature_text, validate=True)
    except ValueError as exc:
        raise AuthorityError("postback_authority_signature_invalid") from exc
    unsigned = {
        key: item for key, item in value.items() if key != "ed25519_signature_b64"
    }
    try:
        _load_ed25519_public(
            public_key_path, reason_prefix="postback_capability"
        ).verify(signature, canonical_bytes(unsigned))
    except InvalidSignature as exc:
        raise AuthorityError("postback_authority_signature_invalid") from exc
    if value.get("schema") != CAPABILITY_SCHEMA:
        raise AuthorityError("postback_authority_schema_invalid")
    if value.get("onboarding_workflow_id") != ONBOARDING_WORKFLOW_ID:
        raise AuthorityError("postback_authority_workflow_invalid")
    if value.get("execution_mode") != "webhook":
        raise AuthorityError("postback_authority_source_invalid")
    if str(value.get("provider") or "") != provider:
        raise AuthorityError("postback_authority_provider_mismatch")
    if str(value.get("manifest_run_id") or "") != run_id:
        raise AuthorityError("postback_authority_run_mismatch")
    if str(value.get("worker_id") or "") != worker_id:
        raise AuthorityError("postback_authority_worker_mismatch")
    validate_program_registration(value.get("program"), provider=provider)
    if not _SHA256.fullmatch(str(value.get("bridge_entry_admission_sha256") or "")):
        raise AuthorityError("postback_authority_bridge_admission_invalid")
    now = int(dt.datetime.now(dt.timezone.utc).timestamp()) if now_epoch is None else int(now_epoch)
    issued = int(value.get("issued_at_epoch") or 0)
    expires = int(value.get("expires_at_epoch") or 0)
    if issued > now + 30 or expires <= now or expires - issued != CAPABILITY_TTL_S:
        raise AuthorityError("postback_authority_expired_or_invalid")
    return value


def read_capability_fd(
    fd: int,
    *,
    provider: str,
    run_id: str,
    worker_id: str,
    public_key_path: Path = CAPABILITY_PUBLIC_KEY_PATH,
    now_epoch: int | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Consume one capability from an inherited anonymous pipe and close it."""
    raw = _read_anonymous_pipe(
        fd, max_bytes=_MAX_CAPABILITY_BYTES, reason_prefix="postback_authority"
    )
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise AuthorityError("postback_authority_payload_invalid_json") from exc
    if not isinstance(value, dict):
        raise AuthorityError("postback_authority_payload_not_object")
    validated = _validate_capability(
        value,
        provider=provider,
        run_id=run_id,
        worker_id=worker_id,
        public_key_path=public_key_path,
        now_epoch=now_epoch,
    )
    operation = {
        "schema": OPERATION_SCHEMA,
        "operation_id": validated["operation_id"],
        "onboarding_execution_id": validated["onboarding_execution_id"],
        "onboarding_workflow_id": validated["onboarding_workflow_id"],
        "execution_mode": validated["execution_mode"],
        "manifest_run_id": validated["manifest_run_id"],
        "provider": validated["provider"],
        "worker_id": validated["worker_id"],
        "program": validated["program"],
        "bridge_entry_admission_sha256": validated["bridge_entry_admission_sha256"],
        "authority_sha256": sha256_value(validated),
    }
    return validated, operation


def attest_receipt(
    receipt: dict[str, Any], *, key_path: Path = AUTHORITY_KEY_PATH
) -> dict[str, Any]:
    if "authority_attestation" in receipt:
        raise AuthorityError("receipt_already_attested")
    key = _load_private_key(key_path)
    return {
        **receipt,
        "authority_attestation": {
            "schema": ATTESTATION_SCHEMA,
            "hmac_sha256": _mac(receipt, key),
        },
    }


def verify_receipt_attestation(
    receipt: dict[str, Any], *, key_path: Path = AUTHORITY_KEY_PATH
) -> tuple[bool, str]:
    attestation = receipt.get("authority_attestation")
    if not isinstance(attestation, dict) or set(attestation) != {"schema", "hmac_sha256"}:
        return False, "receipt_attestation_missing_or_invalid"
    if attestation.get("schema") != ATTESTATION_SCHEMA:
        return False, "receipt_attestation_schema_invalid"
    signature = str(attestation.get("hmac_sha256") or "")
    unsigned = {key: item for key, item in receipt.items() if key != "authority_attestation"}
    try:
        expected = _mac(unsigned, _load_private_key(key_path))
    except AuthorityError as exc:
        return False, str(exc)
    if not _SHA256.fullmatch(signature) or not hmac.compare_digest(signature, expected):
        return False, "receipt_attestation_hmac_invalid"
    return True, ""
