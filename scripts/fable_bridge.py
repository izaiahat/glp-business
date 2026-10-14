#!/usr/bin/env python3
"""Strict Discord <-> persistent Fable reviewer bridge.

The bridge uses a dedicated Discord bot identity, accepts only mention-gated
messages from Izaiah or marker-gated Kael, and emits one nonce-protected reply
to the exact source message. It runs in tmux session ``claude-fable``.
"""
from __future__ import annotations

import fcntl
import hashlib
import importlib.util
import json
import os
import re
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import requests

CHANNEL = "1524279642276626534"
KAEL_BOT_ID = "1486880423568211968"
OPERATOR_ID = "392067741500309517"
BASE = "https://discord.com/api/v10"
TOKEN_FILE = Path("/home/ubuntu/.hermes/secrets/fable-discord/bot_token")
AUTHOR_ID_FILE = Path("/home/ubuntu/.hermes/secrets/fable-discord/author_id")
AUTHORITY_CONFIG = Path("/home/ubuntu/business/ventures/boring-ai/holistic-machine/config/bridge-authority-v1.json")
SID_FILE = Path("/home/ubuntu/business/reports/claude-audit-20260707/fable-console-session-id.txt")
STATE = Path("/home/ubuntu/business/reports/claude-audit-20260707/fable-bridge-state.json")
ISSUE_DIR = Path("/home/ubuntu/business/reports/boring-ai/bridge-authority-issues")
ARTIFACT_ROOT = Path("/home/ubuntu/business/reports/ops")
LOG = Path("/home/ubuntu/business/reports/claude-audit-20260707/fable-bridge.log")
BRIDGE_TEST_MARKER = "[FABLE_BRIDGE_TEST]"
REVIEW_REQUEST_MARKER = "TRUE-BRIDGE FABLE REVIEW REQUEST"
KAEL_REQUEST_MARKERS = (BRIDGE_TEST_MARKER, REVIEW_REQUEST_MARKER, "BRIDGE REQUEST")
HERMES_PYTHON = Path("/home/ubuntu/.hermes/hermes-agent/venv/bin/python")
HERMES_STATE = Path.home() / ".hermes" / "state.db"
MODEL_POLICY = Path("/home/ubuntu/business/ops/claude-model-policy.json")
USAGE_FILE = Path.home() / ".claude" / "usage-latest.json"
MAX_REPLY_CHARS = 1900
STOP_REQUESTED = False
TOP_STAKES_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "production_or_high_impact_change",
        re.compile(
            r"\b(production|deploy(?:ment)?|cutover|activate|activation|migration|irreversible|finder arm)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "live_sales_or_marketing_asset",
        re.compile(
            r"\b(live sales|sales asset|marketing asset|pricing|checkout|money[- ]path|prospect delivery)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "privileged_account_action",
        re.compile(
            r"\b(stripe|cloudflare|gmail|payment link|provider mutation|credential|secret binding|account action)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "finance_contract_or_evidence_sensitive",
        re.compile(
            r"\b(payment|revenue|finance|contract|legal|evidence[- ]sensitive|authority decision)\b",
            re.IGNORECASE,
        ),
    ),
)

TOKEN = ""
SELF_ID = ""
SESSION_ID = ""
HTTP: requests.Session | None = None


def _session_transcript(session_id: str) -> Path | None:
    """Find a persisted Claude transcript without reading its contents."""
    try:
        if str(uuid.UUID(session_id)) != session_id:
            return None
    except (ValueError, AttributeError):
        return None
    config_root = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))
    projects = config_root / "projects"
    if not projects.is_dir():
        return None
    for path in projects.glob(f"*/{session_id}.jsonl"):
        if path.is_file():
            return path
    return None


def _native_session_id(session_id: str) -> str:
    """Require a verified native import of the legacy conversation; never create an empty substitute."""
    try:
        if str(uuid.UUID(session_id)) != session_id:
            raise ValueError
    except (ValueError, AttributeError) as error:
        raise RuntimeError("fable_session_id_invalid") from error
    native_id = os.environ.get("FABLE_HERMES_SESSION_ID") or _routing_policy().get("native_session_id", "")
    if not re.fullmatch(r"[0-9]{8}_[0-9]{6}_[a-f0-9]{6}", native_id):
        raise RuntimeError("fable_native_session_import_required")
    transcript = _session_transcript(session_id)
    if transcript is None:
        raise RuntimeError("fable_legacy_transcript_missing")
    try:
        with sqlite3.connect(f"file:{HERMES_STATE}?mode=ro", uri=True) as db:
            row = db.execute("SELECT source, origin_json FROM sessions WHERE id=?", (native_id,)).fetchone()
        origin = json.loads(row[1]) if row and row[1] else {}
        imported = origin.get("imported_from", {})
        if (row[0] != "claude-code" or imported.get("tool") != "claude-code"
                or imported.get("foreign_session_id") != session_id
                or Path(imported.get("path", "")).resolve() != transcript.resolve()):
            raise RuntimeError("fable_native_session_provenance_mismatch")
    except (sqlite3.Error, ValueError, TypeError, OSError) as error:
        raise RuntimeError("fable_native_session_readback_failed") from error
    return native_id


def _claude_failure_reason(stdout: str, stderr: str) -> str:
    """Map provider/CLI text to a bounded non-secret diagnostic code."""
    combined = f"{stdout}\n{stderr}".casefold()
    if "no conversation found" in combined:
        return "session_missing"
    if "does not support this model" in combined and "newer is required" in combined:
        return "cli_outdated"
    if "not logged in" in combined or "please run /login" in combined:
        return "not_authenticated"
    if any(marker in combined for marker in (
        "weekly limit", "usage limit", "rate limit", "rate-limit", "rate_limit",
        "hit your limit", "reached your limit",
    )):
        return "capacity_unavailable"
    if "model" in combined and any(marker in combined for marker in ("not found", "not available", "invalid")):
        return "model_unavailable"
    return "cli_error"


def log(message: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + " " + message + "\n")


def _validate_secret_directory(path: Path) -> None:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeError("dedicated_fable_secret_directory_invalid")
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise RuntimeError("dedicated_fable_secret_directory_unsafe")


def _read_restricted(path: Path, label: str) -> str:
    try:
        metadata = path.lstat()
    except FileNotFoundError as error:
        raise RuntimeError(f"{label}_missing") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError(f"{label}_not_regular")
    if metadata.st_uid != os.getuid():
        raise RuntimeError(f"{label}_wrong_owner")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise RuntimeError(f"{label}_permissions_not_restricted")
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError(f"{label}_empty")
    return value


def token() -> str:
    _validate_secret_directory(TOKEN_FILE.parent)
    return _read_restricted(TOKEN_FILE, "dedicated_fable_token")


def expected_author_id() -> str:
    runtime_id = _read_restricted(AUTHOR_ID_FILE, "dedicated_fable_author_id")
    try:
        config = json.loads(AUTHORITY_CONFIG.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("bridge_authority_config_invalid") from error
    if not isinstance(config, dict) or config.get("schema") != "boring_ai_bridge_authority_v1":
        raise RuntimeError("bridge_authority_config_schema_invalid")
    if str(config.get("channel_id") or "") != CHANNEL:
        raise RuntimeError("bridge_authority_channel_mismatch")
    if str(config.get("request_author_id") or "") != KAEL_BOT_ID:
        raise RuntimeError("bridge_authority_request_author_mismatch")
    configured_id = str(config.get("acceptance_author_id") or "")
    if runtime_id != configured_id:
        raise RuntimeError("bridge_authority_runtime_id_mismatch")
    if not re.fullmatch(r"[0-9]{17,20}", runtime_id) or runtime_id == KAEL_BOT_ID:
        raise RuntimeError("dedicated_fable_author_id_invalid_or_shared")
    return runtime_id


def _resolve_model() -> tuple[str, str]:
    spec = importlib.util.spec_from_file_location(
        "claude_model_policy", "/home/ubuntu/business/ops/claude_model_policy.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("model_policy_import_failed")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.resolve(seat="bridge")


def _routing_policy() -> dict[str, Any]:
    value = json.loads(MODEL_POLICY.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema") != "claude_model_policy_v2":
        raise RuntimeError("reviewer_policy_invalid")
    routing = value.get("reviewer_routing")
    if not isinstance(routing, dict):
        raise RuntimeError("reviewer_routing_missing")
    threshold = routing.get("threshold_percent", 95)
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not 0 < threshold <= 100:
        raise RuntimeError("reviewer_threshold_invalid")
    model = routing.get("hermes_model")
    if not isinstance(model, str) or not model.strip() or model != model.strip() or model.startswith("-"):
        raise RuntimeError("reviewer_hermes_model_invalid")
    return routing


def _usage_exhausted(threshold: float) -> bool:
    """Read the statusLine document, not usage-check.sh's unrelated 85% exit gate."""
    try:
        with USAGE_FILE.open(encoding="utf-8") as handle:
            observed_at = os.fstat(handle.fileno()).st_mtime
            value = json.load(handle)
        windows = value.get("rate_limits") if isinstance(value, dict) else None
        if not isinstance(windows, dict):
            raise ValueError("usage_windows_missing")
    except (OSError, ValueError, UnicodeError):
        log("claude_usage_unknown action=try_fable")
        return False
    for name in ("five_hour", "seven_day"):
        window = windows.get(name)
        if not isinstance(window, dict):
            continue
        raw = window.get("used_percentage")
        fraction = raw is None
        raw = window.get("utilization") if fraction else raw
        if raw is None or isinstance(raw, bool):
            continue
        try:
            used = float(raw) * (100 if fraction else 1)
        except (TypeError, ValueError, OverflowError):
            continue
        if not 0 <= used <= 100:
            continue
        try:
            reset = window.get("resets_at")
            if reset is not None and not isinstance(reset, bool):
                reset = float(reset)
                reset = reset / 1000 if reset > 1e12 else reset
                # A pre-reset observation is not evidence of post-reset exhaustion.
                # Probe Fable; a genuine refusal still falls back on this request.
                if observed_at < reset <= time.time():
                    continue
        except (TypeError, ValueError, OverflowError):
            # An unusable reset timestamp must not erase a valid high usage value.
            pass
        if used >= threshold:
            return True
    return False


def _included_quota_available(snapshot: dict[str, Any] | None = None) -> bool:
    """Require current, positive included session AND weekly capacity."""
    try:
        if snapshot is None:
            snapshot = json.loads(Path("/home/ubuntu/business/ops/subscription-capacity/latest.json").read_text())
        if not isinstance(snapshot, dict):
            return False
        observed = datetime.fromisoformat(snapshot["collected_at_utc"].replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - observed).total_seconds()
        return 0 <= age <= 600 and any(
            row.get("provider") == "anthropic" and row.get("freshness") == "live"
            and row.get("capacity_status") in {"available", "limited"}
            and all(type(row.get(key)) in (int, float) and 0 < row[key] <= 100
                    for key in ("weekly_remaining_pct", "session_remaining_pct"))
            for row in snapshot["records"]
        )
    except (OSError, KeyError, ValueError, TypeError, AttributeError):
        return False


def refresh_included_capacity() -> dict[str, Any]:
    """Use the existing read-only collector, not cached/reset or paid capacity."""
    scripts = Path("/home/ubuntu/business/scripts")
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    from subscription_capacity_monitor import collect_monitored_capacity
    report = collect_monitored_capacity(now=datetime.now(timezone.utc))
    return {"collected_at_utc": report.collected_at_utc,
            "records": [row.to_dict() for row in report.records
                        if row.provider == "anthropic" and row.source == "oauth_usage_api"]}


def _design_review(kind: str, text: str) -> bool:
    # Unknown classification retains the designated reviewer. Only an explicit
    # non-design request may use the older, separately authorized stand-in.
    return kind == "review_request" and (
        "NON-DESIGN REVIEW" not in text or re.search(
            r"\b(design|visual|ux|ui|layout|landing page|art direction|creative|typography|responsive|motion|outward-facing)\b",
            text.replace("NON-DESIGN REVIEW", ""), re.IGNORECASE,
        ) is not None
    )


def select_backend(*, claude_exhausted: bool = False, design_review: bool = False,
                   request_text: str = "", source_message_id: str | None = None) -> tuple[str, str, str]:
    """Re-evaluate authority/policy per request; never latch a fallback."""
    if "reviewer_designation:" in request_text:
        _authority_guard().validate_scoped_review_request(
            request_message_id=source_message_id, request_text=request_text)
        return "astra-glp42", "gpt-6-astra-900k", "xhigh"
    routing = _routing_policy()
    if not design_review and (claude_exhausted or _usage_exhausted(routing.get("threshold_percent", 95))):
        return "astra-standin", routing["hermes_model"], "xhigh"
    model, effort = _resolve_model()
    if design_review and model != "claude-fable-5-1":
        raise RuntimeError("design_review_requires_fable_5_1")
    if effort != "xhigh":
        raise RuntimeError("reviewer_fable_effort_not_xhigh")
    return "fable", model, effort


def _tag_reply(kind: str, output: str, identity: str) -> str:
    """Keep the complete reviewer output until the transport can attach it."""
    suffix = f"\n[reviewer: {identity}]"
    limit = min(MAX_REPLY_CHARS, 1399) if kind == "review_request" else MAX_REPLY_CHARS
    body = output.strip() or "(Reviewer returned no output; no authority decision was issued.)"
    if "[reviewer:" in body.casefold():
        body = "(Reviewer supplied reserved identity metadata; no authority decision was issued.)"
    # Short replies retain their historical content; long replies retain raw
    # stdout, including terminal newlines, in the attachment payload.
    return (body if len(body) + len(suffix) <= limit else output) + suffix


def _oversize_reply(reply: str, kind: str) -> tuple[str, bytes, str] | None:
    """Build a bounded wrapper; never infer a decision from quoted contract text."""
    limit = min(MAX_REPLY_CHARS, 1399) if kind == "review_request" else MAX_REPLY_CHARS
    if len(reply) <= limit:
        return None
    body, separator, identity = reply.rpartition("\n[reviewer: ")
    if not separator or not identity.endswith("]"):
        raise RuntimeError("oversize_reviewer_identity_unbound")
    identity = identity[:-1]
    # Oversized prose has no acceptance authority, regardless of its first line.
    # A compact second inference must take the ordinary short-reply path instead.
    first = body.partition("\n")[0]
    if kind == "review_request" and first == "TRUE-BRIDGE FABLE — REJECT":
        verdict = first
    else:
        verdict = "Reviewer response attached; no parsable verdict, no authority decision was issued."
    raw = body.encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    wrapper = (f"{verdict}\nFull verbatim reviewer response: attached as reviewer-response.txt "
               f"({len(raw)} UTF-8 bytes; SHA-256: {digest}).\n[reviewer: {identity}]")
    if len(wrapper) > MAX_REPLY_CHARS:
        raise RuntimeError("oversize_reply_wrapper_invalid")
    return wrapper, raw, digest


def api(method: str, path: str, body: dict[str, Any] | None = None, *,
        single_attempt: bool = False) -> Any:
    if HTTP is None:
        raise RuntimeError("dedicated_fable_http_session_not_initialized")
    method = method.upper()
    for attempt in range(1 if single_attempt else 4):
        try:
            response = HTTP.request(method, BASE + path, json=body, timeout=30)
        except requests.RequestException as error:
            log(f"discord_transport_error method={method} type={type(error).__name__}")
            if method == "POST":
                return None
            if attempt < 3:
                time.sleep(2 + attempt)
                continue
            return None
        if response.status_code == 429:
            if single_attempt:
                # Manual handoff authority was checked once, immediately before
                # this owned POST. Never sleep/retry behind that boundary.
                return None
            try:
                retry_after = float(response.json().get("retry_after", 2.0))
            except Exception:
                retry_after = 2.0
            time.sleep(retry_after + 0.5)
            continue
        if method == "GET" and response.status_code >= 500 and attempt < 3:
            log(f"discord_server_retry status={response.status_code} method=GET")
            time.sleep(2 + attempt)
            continue
        if response.status_code < 200 or response.status_code >= 300:
            log(f"discord_http_error status={response.status_code} method={method} path={path}")
            return None
        if response.status_code == 204:
            return {}
        try:
            return response.json()
        except ValueError:
            log(f"discord_json_error method={method} path={path}")
            return None
    return None


def validate_startup_identity() -> str:
    identity = api("GET", "/users/@me")
    if not isinstance(identity, dict):
        raise RuntimeError("dedicated_fable_identity_readback_failed")
    actual = str(identity.get("id") or "")
    expected = expected_author_id()
    if actual != expected or identity.get("bot") is not True or actual == KAEL_BOT_ID:
        raise RuntimeError("dedicated_fable_identity_mismatch")
    channel = api("GET", f"/channels/{CHANNEL}")
    if not isinstance(channel, dict) or str(channel.get("id") or "") != CHANNEL:
        raise RuntimeError("dedicated_fable_channel_access_failed")
    return actual


def post(content: str, *, reply_to_message_id: str, nonce: str | None = None,
         kind: str = "review_request", original_stdout: str | None = None,
         single_attempt: bool = False) -> str:
    oversize = _oversize_reply(content, kind)
    if original_stdout is not None and oversize is not None:
        raise RuntimeError("compact_followup_exceeds_short_limit")
    reply = oversize[0] if oversize else (content.strip() or "(Fable returned no output; no authority decision was issued.)")
    attachment = oversize[1] if oversize else (original_stdout.encode("utf-8") if original_stdout is not None else None)
    if len(reply) > MAX_REPLY_CHARS:
        raise RuntimeError("reply_exceeds_discord_limit")
    body: dict[str, Any] = {
        "content": reply,
        "message_reference": {
            "message_id": reply_to_message_id,
            "channel_id": CHANNEL,
            "fail_if_not_exists": True,
        },
        "allowed_mentions": {"parse": [], "replied_user": False},
        "nonce": nonce or reply_to_message_id,
        "enforce_nonce": True,
    }
    if attachment is not None:
        if HTTP is None:
            raise RuntimeError("dedicated_fable_http_session_not_initialized")
        body["attachments"] = [{"id": 0, "filename": "reviewer-response.txt"}]
        # One POST, one Discord message, no unjournaled PATCH or blind retry.
        try:
            response = HTTP.request("POST", BASE + f"/channels/{CHANNEL}/messages",
                                    data={"payload_json": json.dumps(body)},
                                    files={"files[0]": ("reviewer-response.txt", attachment,
                                                        "text/plain; charset=utf-8")}, timeout=30)
            result = response.json() if 200 <= response.status_code < 300 else None
        except (requests.RequestException, ValueError) as error:
            raise RuntimeError("dedicated_fable_message_post_failed_or_ambiguous") from error
    else:
        result = api("POST", f"/channels/{CHANNEL}/messages", body,
                     **({"single_attempt": True} if single_attempt else {}))
    if not isinstance(result, dict) or not str(result.get("id") or "").isdigit():
        raise RuntimeError("dedicated_fable_message_post_failed_or_ambiguous")
    return str(result["id"])


def verify_oversize_post(posted_id: str, source_id: str, reply: str, kind: str,
                         original_stdout: str | None = None) -> dict[str, Any] | None:
    """Read back the exact posted message and CDN bytes without bot auth."""
    oversized = _oversize_reply(reply, kind)
    if original_stdout is not None and oversized is not None:
        raise RuntimeError("compact_followup_exceeds_short_limit")
    if oversized is None and original_stdout is None:
        return None
    wrapper = oversized[0] if oversized else reply.strip()
    raw = oversized[1] if oversized else str(original_stdout).encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    message = _exact_message(posted_id)
    if (str((message.get("author") or {}).get("id")) != SELF_ID
            or str((message.get("message_reference") or {}).get("message_id")) != source_id
            or message.get("content") != wrapper
            or message.get("edited_timestamp") is not None):
        raise RuntimeError("oversize_post_readback_mismatch")
    attachments = message.get("attachments")
    if not isinstance(attachments, list) or len(attachments) != 1:
        raise RuntimeError("oversize_attachment_missing_or_ambiguous")
    attachment = attachments[0]
    url = attachment.get("url") if isinstance(attachment, dict) else None
    if (not isinstance(url, str) or not url.startswith("https://cdn.discordapp.com/")
            or attachment.get("filename") != "reviewer-response.txt"
            or attachment.get("size") != len(raw)):
        raise RuntimeError("oversize_attachment_binding_mismatch")
    try:
        downloaded = requests.get(url, timeout=30)  # No dedicated bot token on CDN.
        downloaded.raise_for_status()
    except requests.RequestException as error:
        raise RuntimeError("oversize_attachment_readback_failed") from error
    if downloaded.content != raw or hashlib.sha256(downloaded.content).hexdigest() != digest:
        raise RuntimeError("oversize_attachment_bytes_mismatch")
    return {"attachment_sha256": digest, "attachment_bytes": len(raw),
            "posted_content_sha256": hashlib.sha256(wrapper.encode("utf-8")).hexdigest()}


def load_state() -> dict[str, Any]:
    try:
        value = json.loads(STATE.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {"last_id": None}
    except Exception:
        return {"last_id": None}


def save_state(value: dict[str, Any]) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE.with_suffix(STATE.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(value, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, STATE)
    directory = os.open(STATE.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


@contextmanager
def bridge_state_lock():
    """Share one inode across daemon and exact-request resume; never unlink it."""
    STATE.parent.mkdir(parents=True, exist_ok=True)
    with STATE.with_suffix(STATE.suffix + ".lock").open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _structured_self_mention(message: dict[str, Any]) -> bool:
    if not SELF_ID:
        return False
    mentions = message.get("mentions")
    return isinstance(mentions, list) and any(
        str(item.get("id") or "") == SELF_ID for item in mentions if isinstance(item, dict)
    )


def classify_message(message: dict[str, Any]) -> tuple[str, str] | None:
    """Return ``(kind, clean_text)`` only for strict allowlisted messages."""
    author = message.get("author") if isinstance(message.get("author"), dict) else {}
    author_id = str(author.get("id") or "")
    author_is_bot = author.get("bot") is True
    text = str(message.get("content") or "").strip()
    if (
        not text
        or author_id == SELF_ID
        or message.get("webhook_id") is not None
        or message.get("edited_timestamp") is not None
        or int(message.get("type") or 0) != 0
        or not _structured_self_mention(message)
    ):
        return None
    clean = re.sub(rf"<@!?{re.escape(SELF_ID)}>", "", text).strip()
    if author_id == OPERATOR_ID and not author_is_bot:
        return ("operator", clean) if clean else None
    if author_id == KAEL_BOT_ID and author_is_bot:
        if clean.startswith(BRIDGE_TEST_MARKER):
            return "transport_test", clean
        if clean.casefold().startswith(REVIEW_REQUEST_MARKER.casefold()) or clean.startswith("BRIDGE REQUEST"):
            return "review_request", clean
    return None


def _model_prompt(kind: str, text: str) -> str:
    if kind == "transport_test":
        return (
            "This is an internal transport test, not an authority or quality decision. "
            "Reply with exactly one line: FABLE_BRIDGE_READY followed by the exact nonce in the request. "
            "Do not use ACCEPT, REJECT, TRUE-BRIDGE, SOLE valid acceptance, or additional text.\n\n"
            f"REQUEST:\n{text}"
        )
    if kind == "review_request":
        return (
            "You are the independent Bridge Fable reviewer. Perform the requested inspection using the exact "
            "bound artifacts, URLs, hashes, and challenge in the request. Do not accept by parroting the request. "
            "If and only if the inspected bytes satisfy the criteria, answer in one Discord message beginning "
            "`TRUE-BRIDGE FABLE — ACCEPT (this message is the SOLE valid acceptance)` and include the exact subject, "
            "tuple SHA-256, challenge, and a substantive inspection note. Otherwise begin `TRUE-BRIDGE FABLE — REJECT` "
            "and state the defects. When accepting, do not use the words REJECT, REJECTED, REVISE, REVISED, "
            "DO NOT ACCEPT, NOT ACCEPT, or NOT ACCEPTED anywhere after the exact decision line; describe preserved "
            "negative source states as nonterminal or declined instead. Never describe "
            "yourself as an instrument, Opus, operator-direct authority, or automatic acceptance. Keep the entire answer "
            "under 1,400 characters and keep the substantive inspection note under 500 characters.\n\n"
            f"REQUEST:\n{text}"
        )
    return text


def top_stakes_category(kind: str, text: str) -> str | None:
    """Classify only review requests; ordinary operator/transport traffic stays live."""
    if kind != "review_request":
        return None
    normalized = " ".join(text.split())
    for category, pattern in TOP_STAKES_RULES:
        if pattern.search(normalized):
            return category
    return None


def review_action(
    kind: str, text: str, *, source_message_id: str, receipt: dict[str, Any] | None = None,
    capacity_snapshot: dict[str, Any] | None = None,
    before_invoke: Callable[[], None] | None = None,
) -> tuple[str, str]:
    """Return ``(mode, reply)`` while preventing top-stakes in-place decisions."""
    category = top_stakes_category(kind, text)
    if category:
        identity = "none (top_stakes_deferred)"
        if receipt is not None:
            receipt.update(reviewer_identity=identity, attempts=[])
        return "deferred", _tag_reply(kind, "\n".join([
            "TOP-STAKES DEFERRED — no authority decision was issued by the bridge daemon.",
            f"category: `{category}`",
            f"source message: `{source_message_id}`",
            "action: inspect this exact request in the persistent Fable/Opus thread, then reply here with the independent decision.",
        ]), identity)
    if "reviewer_designation:" in text:
        receipt = receipt if receipt is not None else {}
        receipt["source_message_id"] = source_message_id
    if receipt is None:
        return "model", ask_fable(kind, text)
    return "model", ask_fable(kind, text, receipt=receipt,
                              capacity_snapshot=capacity_snapshot, before_invoke=before_invoke)


def _invoke_reviewer(
    backend: tuple[str, str, str], prompt: str, receipt: dict[str, Any], deadline: float,
    *, capacity_snapshot: dict[str, Any] | None = None,
    before_invoke: Callable[[], None] | None = None,
    parent_handoff: bool = False,
) -> tuple[str, bool]:
    name, model, effort = backend
    identity = ("astra-glp42 (designated; Standard)" if name == "astra-glp42" else
                "fable-xhigh" if name == "fable" else "astra-standin (claude_exhausted)")
    receipt.update(reviewer_identity=identity, backend=name, model=model, effort=effort)
    attempt: dict[str, Any] = {"backend": name, "model": model, "effort": effort, "invoked": False}
    receipt.setdefault("attempts", []).append(attempt)
    try:
        with tempfile.TemporaryDirectory(prefix="fable-bridge-review-") as directory:
            if name == "fable":
                if not _included_quota_available(capacity_snapshot):
                    attempt["status"] = "included_capacity_unverified_or_depleted"
                    return "(Included Claude quota unavailable or unverified; no authority decision was issued.)", True
                native_id = _native_session_id(SESSION_ID)
                attempt.update(session_id=native_id, session_mode="native_import_resume",
                               legacy_session_id=SESSION_ID)
                prompt_path = Path(directory) / "prompt.txt"
                prompt_path.write_text(prompt, encoding="utf-8")
                command = [
                    str(HERMES_PYTHON), str(Path(__file__).resolve()),
                    "--native-parent-reviewer" if parent_handoff else "--native-reviewer",
                    "chat", "--query-file", str(prompt_path), "-Q", "--oneshot",
                    "--resume", native_id, "--no-restore-cwd", "--provider", "claude-subscription-directsdk-experimental",
                    "-m", model, "--reasoning", effort, "--toolsets", "fable-review-readonly",
                    "--source", "fable-bridge-reviewer",
                ]
            else:
                session_name = receipt.setdefault("standin_session_name", "fable-bridge-astra-standin-" + uuid.uuid4().hex)
                attempt["session_name"] = session_name
                prompt_path = Path(directory) / "prompt.txt"
                prompt_path.write_text(prompt, encoding="utf-8")
                command = [
                    "hermes", "chat", "--query-file", str(prompt_path), "-Q", "--oneshot",
                    "-m", model, "--reasoning", effort, "--source", session_name,
                    "--continue", session_name,
                    *([] if len(receipt["attempts"]) > 1 else ["--create-if-missing"]),
                    "--provider", "openai-codex",
                    "--skills", "agent-evaluation-review,site-design,cloudflare-release-operations,discord-delivery",
                ]
                if name == "astra-glp42":
                    # Reuse the existing native read-only entry, with process-local
                    # Standard configuration. Never change the user's saved profile.
                    command[:2] = [str(HERMES_PYTHON), str(Path(__file__).resolve()), "--native-reviewer", "chat"]
                    command += ["--toolsets", "fable-review-readonly", "--no-restore-cwd"]
                    command[command.index("--skills") + 1] = "offline-guarded-verifier-review,cloudflare-release-operations"
                    attempt.update(provider="openai-codex", speed="Standard",
                                   service_tier_config="default", session_mode="fresh_scoped_review_then_continue")
                    receipt.update(provider="openai-codex", speed="Standard", service_tier_config="default")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, 0)
            log(f"reviewer_cli_start backend={name} model={model} effort={effort}")
            if name != "astra-glp42":
                attempt["invoked"] = True
            if before_invoke is not None:
                before_invoke()  # Durable claim before an uncertain external inference.
            attempt["invoked"] = True
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=remaining,
                cwd="/home/ubuntu/business",
            )
        attempt["exit"] = result.returncode
        output = result.stdout or ""
        reason = _claude_failure_reason(output, result.stderr or "")
        refusal = name == "fable" and (
            (result.returncode != 0 and reason == "capacity_unavailable")
            or (result.returncode == 0 and not output and reason == "capacity_unavailable")
            or (result.returncode == 0 and re.match(
                r"^(?:you(?:['’]ve| have) (?:hit|reached) your (?:usage )?limit|"
                r"(?:error:\s*)?(?:weekly limit|usage limit|rate[-_ ]limit)(?:\b|_error))",
                output, re.IGNORECASE,
            ) is not None)
        )
        if result.returncode != 0 or refusal:
            attempt["status"] = "capacity_unavailable" if refusal else reason
            log(f"reviewer_cli_failed backend={name} exit={result.returncode} reason={attempt['status']}")
            return f"(Reviewer CLI failed: {attempt['status']}; no authority decision was issued.)", refusal
        attempt["status"] = "ok" if output else "empty"
        return output, False
    except subprocess.TimeoutExpired:
        attempt["status"] = "timeout"
        return "(Reviewer exceeded the 20-minute bridge window; no authority decision was issued.)", False
    except (OSError, RuntimeError, UnicodeError) as error:
        attempt["status"] = "unavailable"
        log(f"reviewer_cli_unavailable backend={name} type={type(error).__name__}")
        return "(Reviewer backend unavailable; no authority decision was issued.)", False


def ask_fable(kind: str, text: str, *, receipt: dict[str, Any] | None = None,
              capacity_snapshot: dict[str, Any] | None = None,
              before_invoke: Callable[[], None] | None = None,
              required_backend: tuple[str, str, str] | None = None) -> str:
    record = receipt if receipt is not None else {}
    record.update(reviewer_identity="none (model_policy_unavailable)", attempts=[])
    deadline = time.monotonic() + 1200
    prompt = _model_prompt(kind, text)
    try:
        design = _design_review(kind, text)
        backend = select_backend(design_review=design, request_text=text if kind == "review_request" else "",
                                 source_message_id=record.get("source_message_id"))
        if backend[0] == "astra-glp42":
            record.update(reviewer_designation="glp42-pr8-astra-20260926",
                          request_text_sha256=hashlib.sha256(text.encode()).hexdigest())
            prompt = ("You are the designated non-author Astra release reviewer, not a quota fallback. "
                      "If you authored any change in this release, issue no acceptance. Inspect the exact "
                      "PR8 head, sealed public tree and package manifest, full gate receipt, renders, "
                      "consent/analytics (default-on and GA4 disclosures), and rollback target in the packet. "
                      "The existing TRUE-BRIDGE protocol is transport identity, not a claim to be Fable.\n\n" + prompt)
            prior_claim = before_invoke

            def scoped_claim() -> None:
                # Recheck the same authenticated issue, packet and retained authority
                # before each inference, including same-session compact follow-up.
                if select_backend(request_text=text, source_message_id=record.get("source_message_id")) != backend:
                    raise RuntimeError("reviewer_model_changed")
                if prior_claim is not None:
                    prior_claim()

            before_invoke = scoped_claim
        if required_backend is not None and backend != required_backend:
            raise RuntimeError("reviewer_model_changed")
        output, exhausted = _invoke_reviewer(backend, prompt, record, deadline,
                                             capacity_snapshot=capacity_snapshot,
                                             before_invoke=before_invoke)
        if exhausted and not design and before_invoke is None:
            backend = select_backend(claude_exhausted=True)
            output, _ = _invoke_reviewer(backend, prompt, record, deadline)
        limit = min(MAX_REPLY_CHARS, 1399) if kind == "review_request" else MAX_REPLY_CHARS
        if kind == "review_request" and len(_tag_reply(kind, output, record["reviewer_identity"])) > limit:
            original = output
            record["original_stdout"] = original  # Transient; removed before the receipt is saved.
            record["original_stdout_sha256"] = hashlib.sha256(original.encode("utf-8")).hexdigest()
            # A second inference belongs to the same claim, reviewer, model, effort,
            # session, deadline and lock. Never reroute or replay on failure.
            if record["attempts"][-1].get("status") == "ok":
                followup_prompt = (
                    "In this SAME review session, give your FINAL decision on the preceding review. "
                    "Reconsider any qualification or withdrawal in your preceding answer. "
                    "Do not repeat the long analysis. Under 1000 characters total, use the exact "
                    "decision line and subject, tuple SHA-256, challenge, and a substantive inspection note "
                    "required by the original request. If not unconditionally accepted, REJECT or "
                    "state no decision. This answer supersedes your preceding long answer."
                )
                compact, _ = _invoke_reviewer(backend, followup_prompt, record, deadline,
                                               capacity_snapshot=capacity_snapshot,
                                               before_invoke=before_invoke)
                valid = record["attempts"][-1].get("status") == "ok"
            else:
                compact, valid = "", False
            if not valid or len(_tag_reply(kind, compact, record["reviewer_identity"])) > limit:
                output = "(Compact reviewer decision unavailable; no authority decision was issued.)"
            else:
                output = compact
            raw = original.encode("utf-8")
            note = (f"Full verbatim initial reviewer response: attached as reviewer-response.txt "
                    f"({len(raw)} UTF-8 bytes; SHA-256: {record['original_stdout_sha256']}).")
            candidate = output.rstrip() + "\n" + note
            if len(_tag_reply(kind, candidate, record["reviewer_identity"])) > limit:
                candidate = ("(Compact reviewer decision exceeds delivery limit; "
                             "no authority decision was issued.)\n" + note)
            output = candidate
    except Exception as error:
        log(f"fail_closed model_policy type={type(error).__name__}")
        output = "(model policy unavailable; Fable lane paused and no authority decision was issued.)"
        if isinstance(record.get("original_stdout"), str):
            raw = record["original_stdout"].encode("utf-8")
            output += ("\nFull verbatim initial reviewer response: attached as reviewer-response.txt "
                       f"({len(raw)} UTF-8 bytes; SHA-256: {hashlib.sha256(raw).hexdigest()}).")
    return _tag_reply(kind, output, record["reviewer_identity"])


def _request_stop(signum: int, frame: Any) -> None:
    """Drain the current message, including its post/receipt, before exiting."""
    global STOP_REQUESTED
    STOP_REQUESTED = True


def _exact_message(message_id: str) -> dict[str, Any]:
    message = api("GET", f"/channels/{CHANNEL}/messages/{message_id}")
    if not isinstance(message, dict) or str(message.get("id")) != message_id:
        raise RuntimeError("exact_message_readback_failed")
    return message


def _only_expected_replies(source_id: str, expected: set[str]) -> None:
    """Fail closed on extra replies or incomplete newest-first history."""
    cursor = ((int(time.time() * 1000) - 1420070400000) << 22) | 0x3fffff
    seen: set[str] = set()
    for _ in range(100):
        page = api("GET", f"/channels/{CHANNEL}/messages?before={cursor}&limit=100")
        if not isinstance(page, list):
            raise RuntimeError("reply_history_unavailable")
        if not page:
            raise RuntimeError("reply_history_incomplete")
        ids = [int(item["id"]) for item in page]
        if max(ids) >= cursor or len(ids) != len(set(ids)):
            raise RuntimeError("reply_history_nonmonotonic")
        for item in page:
            message_id = str(item["id"])
            if int(message_id) <= int(source_id):
                continue
            if str((item.get("message_reference") or {}).get("message_id")) != source_id:
                continue
            if (str((item.get("author") or {}).get("id")) != SELF_ID
                    or message_id not in expected or message_id in seen):
                raise RuntimeError("additional_reviewer_reply_present")
            seen.add(message_id)
        cursor = min(ids)
        if cursor <= int(source_id):
            if seen != expected:
                raise RuntimeError("expected_reply_not_in_history")
            return
    raise RuntimeError("reply_history_incomplete")


def _only_original_refusal(source_id: str, refusal_id: str) -> None:
    _only_expected_replies(source_id, {refusal_id})


def resume_preinvocation_quota(source_id: str, issue_id: str, tuple_sha256: str) -> dict[str, Any]:
    """One exact registered request; no cursor rewind, repost or second inference."""
    if not re.fullmatch(r"[0-9]{17,20}", source_id) or not re.fullmatch(
        r"bridge-issue-[a-f0-9]{32}", issue_id
    ) or not re.fullmatch(r"[a-f0-9]{64}", tuple_sha256):
        raise RuntimeError("resume_identity_invalid")
    if SELF_ID != expected_author_id() or SELF_ID == KAEL_BOT_ID:
        raise RuntimeError("independent_signer_mismatch")
    issue_path = ISSUE_DIR / f"{issue_id}.json"
    with bridge_state_lock():
        state = load_state()
        receipts = state.get("reviewer_receipts")
        receipt = receipts.get(source_id) if isinstance(receipts, dict) else None
        if (not isinstance(receipt, dict) or receipt.get("source_message_id") != source_id
                or receipt.get("kind") != "review_request" or receipt.get("mode") != "model"
                or receipt.get("posting") != "posted" or receipt.get("resume") is not None
                or not isinstance(state.get("last_id"), str)
                or int(state["last_id"]) < int(source_id)):
            raise RuntimeError("resume_receipt_not_eligible")
        attempts = receipt.get("attempts")
        if (not isinstance(attempts, list) or len(attempts) != 1
                or not isinstance(attempts[0], dict) or attempts[0] != {
                    "backend": "fable", "model": "claude-fable-5-1", "effort": "xhigh",
                    "invoked": False, "status": "included_capacity_unverified_or_depleted",
                } or receipt.get("reviewer_identity") != "fable-xhigh"
                or receipt.get("backend") != "fable" or receipt.get("model") != "claude-fable-5-1"
                or receipt.get("effort") != "xhigh"):
            raise RuntimeError("resume_not_preinvocation_quota_refusal")
        refusal_id = str(receipt.get("posted_message_id") or "")
        if not refusal_id.isdigit():
            raise RuntimeError("original_refusal_unbound")
        issue = json.loads(issue_path.read_text())
        issue_body = {key: value for key, value in issue.items() if key != "record_sha256"}
        issue_hash = hashlib.sha256(json.dumps(issue_body, sort_keys=True, separators=(",", ":"),
                                               ensure_ascii=False).encode("utf-8")).hexdigest()
        if (issue.get("schema") != "boring_ai_bridge_request_issue_v2"
                or issue.get("record_sha256") != issue_hash
                or issue.get("status") != "BOUND"
                or issue.get("issue_id") != issue_id
                or issue.get("request_message_id") != source_id
                or issue.get("channel_id") != CHANNEL
                or issue.get("request_author_id") != KAEL_BOT_ID
                or issue.get("tuple_sha256") != tuple_sha256
                or issue.get("review_artifact_sha256") != tuple_sha256):
            raise RuntimeError("issue_identity_or_status_changed")
        content = issue.get("request_content")
        artifact = Path(issue.get("review_artifact_path", ""))
        if (not isinstance(content, str)
                or hashlib.sha256(content.encode()).hexdigest() != issue.get("request_content_sha256")
                or not artifact.resolve(strict=True).is_relative_to(ARTIFACT_ROOT.resolve())
                or hashlib.sha256(artifact.read_bytes()).hexdigest() != tuple_sha256
                or f"<@{SELF_ID}> {REVIEW_REQUEST_MARKER}" not in content
                or f"issue_id: {issue_id}" not in content
                or f"tuple_sha256: {tuple_sha256}" not in content
                or f"review_artifact_sha256: {tuple_sha256}" not in content):
            raise RuntimeError("request_or_artifact_drift")
        message = _exact_message(source_id)
        if (message.get("content") != content or message.get("edited_timestamp") is not None
                or message.get("timestamp") != issue.get("request_timestamp")
                or str(message.get("author", {}).get("id")) != KAEL_BOT_ID
                or classify_message(message) is None
                or classify_message(message)[0] != "review_request"):
            raise RuntimeError("request_message_drift")
        refusal = _exact_message(refusal_id)
        refusal_content = refusal.get("content")
        if (not isinstance(refusal_content, str)
                or str(refusal.get("author", {}).get("id")) != SELF_ID
                or str(refusal.get("message_reference", {}).get("message_id")) != source_id
                or refusal.get("edited_timestamp") is not None
                or len(refusal_content) != receipt.get("reply_chars")
                or hashlib.sha256(refusal_content.encode()).hexdigest() != receipt.get("reply_sha256")
                or "no authority decision was issued" not in refusal_content
                or "ACCEPT" in refusal_content):
            raise RuntimeError("original_refusal_drift_or_decision")
        _only_original_refusal(source_id, refusal_id)
        kind, text = classify_message(message)  # same normal bridge classification
        if top_stakes_category(kind, text) is not None or not _design_review(kind, text):
            raise RuntimeError("request_routing_changed")
        backend = select_backend(design_review=True)
        if backend != ("fable", "claude-fable-5-1", "xhigh"):
            raise RuntimeError("reviewer_model_changed")

        def revalidate_bound_source() -> None:
            # Capacity collection and inference block while independent senders
            # can reply. Recheck the original bindings and complete history at
            # both external-effect boundaries; the state lock is only local.
            if json.loads(issue_path.read_text()) != issue:
                raise RuntimeError("issue_identity_or_status_changed")
            if (not artifact.resolve(strict=True).is_relative_to(ARTIFACT_ROOT.resolve())
                    or hashlib.sha256(artifact.read_bytes()).hexdigest() != tuple_sha256):
                raise RuntimeError("request_or_artifact_drift")
            current = _exact_message(source_id)
            current_classification = classify_message(current)
            if (current.get("content") != content or current.get("edited_timestamp") is not None
                    or current.get("timestamp") != issue.get("request_timestamp")
                    or str(current.get("author", {}).get("id")) != KAEL_BOT_ID
                    or current_classification is None
                    or current_classification[0] != "review_request"):
                raise RuntimeError("request_message_drift")
            current_refusal = _exact_message(refusal_id)
            current_content = current_refusal.get("content")
            if (not isinstance(current_content, str)
                    or str(current_refusal.get("author", {}).get("id")) != SELF_ID
                    or str(current_refusal.get("message_reference", {}).get("message_id")) != source_id
                    or current_refusal.get("edited_timestamp") is not None
                    or len(current_content) != receipt.get("reply_chars")
                    or hashlib.sha256(current_content.encode()).hexdigest() != receipt.get("reply_sha256")
                    or "no authority decision was issued" not in current_content
                    or "ACCEPT" in current_content):
                raise RuntimeError("original_refusal_drift_or_decision")
            _only_original_refusal(source_id, refusal_id)

        snapshot = refresh_included_capacity()
        if not _included_quota_available(snapshot):
            raise RuntimeError("fresh_positive_included_capacity_required")
        revalidate_bound_source()  # Before reserving and invoking the reviewer.
        # The claim precedes every possible invocation. Unknown/crashed claims never retry.
        resumed: dict[str, Any] = {"status": "reserved", "issue_id": issue_id,
            "tuple_sha256": tuple_sha256, "original_refusal_message_id": refusal_id,
            "nonce": source_id + "-r1", "attempts": [], "posting": "not_prepared"}
        receipt["resume"] = resumed
        save_state(state)

        def claim_invocation() -> None:
            resumed["status"] = "invocation_reserved"
            resumed["attempts"] = list(review_receipt.get("attempts", []))
            save_state(state)

        review_receipt: dict[str, Any] = {}
        mode, reply = review_action(kind, text, source_message_id=source_id,
                                    receipt=review_receipt, capacity_snapshot=snapshot,
                                    before_invoke=claim_invocation)
        original_stdout = review_receipt.pop("original_stdout", None)
        resumed.update(mode=mode, reviewer_identity=review_receipt.get("reviewer_identity"),
                       attempts=review_receipt.get("attempts", []),
                       reply_chars=len(reply), reply_sha256=hashlib.sha256(reply.encode()).hexdigest())
        if original_stdout is not None:
            raw = original_stdout.encode("utf-8")
            resumed["original_stdout_sha256"] = hashlib.sha256(raw).hexdigest()
            resumed["original_stdout_bytes"] = len(raw)
        if (mode != "model" or review_receipt.get("reviewer_identity") != "fable-xhigh"
                or review_receipt.get("backend") != "fable"
                or len(resumed["attempts"]) not in (1, 2)
                or any(attempt.get("invoked") is not True for attempt in resumed["attempts"])
                or resumed["attempts"][0].get("status") != "ok"):
            resumed["status"] = "no_inference"
            save_state(state)
            raise RuntimeError("resume_no_safe_inference")
        resumed.update(status="reply_prepared", posting="prepared")
        save_state(state)
        try:
            revalidate_bound_source()  # Immediately before the outbound reply.
        except Exception:
            # Inference may already have occurred. Retain its terminal claim;
            # withholding the reply must never make this request replayable.
            resumed.update(status="reply_withheld_reconciliation", posting="withheld")
            save_state(state)
            raise
        try:
            posted = post(reply, reply_to_message_id=source_id, nonce=resumed["nonce"], kind=kind,
                          original_stdout=original_stdout)
        except Exception:
            resumed.update(status="post_failed_or_ambiguous", posting="failed_or_ambiguous")
            save_state(state)
            raise
        resumed.update(status="posted", posting="posted", posted_message_id=posted)
        save_state(state)
        try:
            verified = verify_oversize_post(posted, source_id, reply, kind,
                                            original_stdout=original_stdout)
        except Exception:
            resumed.update(status="posted_unverified", posting="posted_unverified")
            save_state(state)
            raise
        if verified:
            resumed["attachment_verification"] = verified
            save_state(state)
        return {"source_message_id": source_id, "posted_message_id": posted,
                "reviewer_identity": resumed["reviewer_identity"], "status": "posted"}


def _authority_guard():
    path = Path(__file__).resolve().parents[1] / "ventures/boring-ai/holistic-machine/scripts/bridge_authority_guard.py"
    spec = importlib.util.spec_from_file_location("bridge_authority_guard", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("registered_issue_guard_unavailable")
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    return guard


def recover_missing_mention(source_id: str, issue_id: str, tuple_sha256: str) -> dict[str, Any]:
    """Explicit once-only recovery of a skipped BOUND successor; never edit/resend.

    Normal classification remains mention-gated. Only this exact-request entry
    can prove that adding the structured mention *in memory* removes the sole
    eligibility defect. Complete history and canonical bindings are rechecked
    at every external-effect boundary under the same lock as the poller.
    """
    if (not re.fullmatch(r"[0-9]{17,20}", source_id)
            or not re.fullmatch(r"bridge-issue-[a-f0-9]{32}", issue_id)
            or not re.fullmatch(r"[a-f0-9]{64}", tuple_sha256)):
        raise RuntimeError("mention_recovery_identity_invalid")
    if SELF_ID != expected_author_id() or SELF_ID == KAEL_BOT_ID:
        raise RuntimeError("independent_signer_mismatch")
    guard = _authority_guard()
    path = ISSUE_DIR / f"{issue_id}.json"
    with bridge_state_lock():
        # Unlike daemon boot, missing/corrupt state cannot establish no invocation.
        state = json.loads(STATE.read_text(encoding="utf-8"))
        receipts = state.get("reviewer_receipts")
        if (not isinstance(receipts, dict) or source_id in receipts
                or any(isinstance(item, dict) and item.get("source_message_id") == source_id
                       for item in receipts.values())
                or not isinstance(state.get("last_id"), str)
                or not state["last_id"].isdigit() or int(state["last_id"]) < int(source_id)):
            raise RuntimeError("mention_recovery_receipt_or_cursor_ineligible")
        preliminary = json.loads(path.read_text(encoding="utf-8"))
        _, issue = guard.load_issue_record(path, expected_subject_id=preliminary.get("subject_id"),
                                           expected_tuple_sha256=tuple_sha256)
        predecessor = issue.get("predecessor_request_message_id")
        successors = state.get("successor_issues")
        registered = successors.get(predecessor) if isinstance(successors, dict) else None
        if (issue.get("schema") != "boring_ai_bridge_request_issue_v2"
                or issue.get("request_message_id") != source_id
                or issue.get("succession_reason") != "terminal_no_authority_decision"
                or not isinstance(predecessor, str) or not predecessor.isdigit()
                or int(predecessor) >= int(source_id)
                or not isinstance(registered, dict) or registered.get("status") != "issued"
                or registered.get("issue_id") != issue_id
                or registered.get("predecessor_issue_id") != issue.get("predecessor_issue_id")
                or registered.get("predecessor_reply_message_id") != issue.get("predecessor_reply_message_id")
                or registered.get("issue_record_path") != str(path)
                or issue.get("review_artifact_sha256") != tuple_sha256):
            raise RuntimeError("mention_recovery_registered_successor_required")
        artifact = Path(issue["review_artifact_path"])
        content = issue["request_content"]
        required_backend = ("fable", "claude-fable-5-1", "xhigh")

        def revalidate() -> tuple[str, str]:
            _, current_issue = guard.load_issue_record(path, expected_subject_id=issue["subject_id"],
                                                       expected_tuple_sha256=tuple_sha256)
            if current_issue != issue:
                raise RuntimeError("mention_recovery_issue_changed")
            if not artifact.resolve(strict=True).is_relative_to(ARTIFACT_ROOT.resolve()):
                raise RuntimeError("mention_recovery_artifact_outside_ops")
            message = _exact_message(source_id)
            if (message.get("channel_id") != CHANNEL or message.get("content") != content
                    or hashlib.sha256(message["content"].encode("utf-8")).hexdigest() != issue["request_content_sha256"]
                    or message.get("timestamp") != issue["request_timestamp"]
                    or not isinstance(message.get("mentions"), list)
                    or _structured_self_mention(message) or classify_message(message) is not None):
                raise RuntimeError("mention_recovery_not_missing_mention_only")
            # Do not mutate the Discord object, content, registry, or normal gate.
            eligible = dict(message, mentions=[*message["mentions"], {"id": SELF_ID}])
            classified = classify_message(eligible)
            if classified is None or classified[0] != "review_request":
                raise RuntimeError("mention_recovery_not_missing_mention_only")
            kind, text = classified
            if top_stakes_category(kind, text) is not None or not _design_review(kind, text):
                raise RuntimeError("request_routing_changed")
            if select_backend(design_review=True) != required_backend:
                raise RuntimeError("reviewer_model_changed")
            _only_expected_replies(source_id, set())
            return kind, text

        kind, text = revalidate()
        snapshot = refresh_included_capacity()
        if not isinstance(snapshot, dict) or not _included_quota_available(snapshot):
            raise RuntimeError("fresh_positive_included_capacity_required")
        revalidate()
        receipt: dict[str, Any] = {"source_message_id": source_id, "kind": kind, "mode": "model",
            "posting": "not_prepared", "attempts": [], "recovery": {
                "reason": "missing_structured_mention", "status": "reserved", "issue_id": issue_id,
                "request_content_sha256": issue["request_content_sha256"], "tuple_sha256": tuple_sha256}}
        receipts[source_id] = receipt
        save_state(state)  # All claims, including crashes before inference, are terminal.

        def claim_invocation() -> None:
            revalidate()
            if not _included_quota_available(snapshot):
                raise RuntimeError("fresh_positive_included_capacity_required")
            receipt["recovery"]["status"] = "invocation_reserved"
            receipt["invocation_claimed"] = True
            durable = dict(state, reviewer_receipts=dict(receipts))
            durable["reviewer_receipts"][source_id] = {
                key: value for key, value in receipt.items() if key != "original_stdout"}
            save_state(durable)

        reply = ask_fable(kind, text, receipt=receipt, capacity_snapshot=snapshot,
                          before_invoke=claim_invocation, required_backend=required_backend)
        original_stdout = receipt.pop("original_stdout", None)
        attempts = receipt.get("attempts", [])
        if (len(attempts) not in (1, 2) or attempts[0].get("status") != "ok"
                or any((a.get("backend"), a.get("model"), a.get("effort")) != required_backend
                       or a.get("invoked") is not True for a in attempts)):
            receipt["recovery"]["status"] = "no_safe_inference"
            save_state(state)
            raise RuntimeError("mention_recovery_no_safe_inference")
        receipt.update(reply_chars=len(reply), reply_sha256=hashlib.sha256(reply.encode()).hexdigest(),
                       posting="prepared")
        if original_stdout is not None:
            receipt.update(original_stdout_sha256=hashlib.sha256(original_stdout.encode()).hexdigest(),
                           original_stdout_bytes=len(original_stdout.encode()))
        save_state(state)
        try:
            revalidate()
        except Exception:
            receipt.update(posting="withheld")
            receipt["recovery"]["status"] = "reply_withheld_reconciliation"
            save_state(state)
            raise
        try:
            posted = post(reply, reply_to_message_id=source_id, nonce=source_id, kind=kind,
                          original_stdout=original_stdout)
        except Exception:
            receipt.update(posting="failed_or_ambiguous")
            receipt["recovery"]["status"] = "post_failed_or_ambiguous"
            save_state(state)
            raise
        receipt.update(posting="posted", posted_message_id=posted)
        receipt["recovery"]["status"] = "posted"
        save_state(state)
        try:
            verified = verify_oversize_post(posted, source_id, reply, kind, original_stdout=original_stdout)
        except Exception:
            receipt.update(posting="posted_unverified")
            save_state(state)
            raise
        if verified:
            receipt["attachment_verification"] = verified
            save_state(state)
        return {"source_message_id": source_id, "posted_message_id": posted,
                "reviewer_identity": receipt["reviewer_identity"], "status": "posted"}


def _successor_verified_attachment(reply: dict[str, Any], record: dict[str, Any],
                                   content: str) -> bool:
    """Bind the one initial-stdout attachment to the durable receipt and CDN bytes."""
    verified = record.get("attachment_verification")
    attachments = reply.get("attachments")
    if (not isinstance(verified, dict) or not isinstance(attachments, list)
            or len(attachments) != 1 or not isinstance(attachments[0], dict)):
        return False
    attachment = attachments[0]
    digest, size = verified.get("attachment_sha256"), verified.get("attachment_bytes")
    original_digest = record.get("original_stdout_sha256")
    original_size = record.get("original_stdout_bytes")
    url = attachment.get("url")
    if (not isinstance(digest, str) or re.fullmatch(r"[a-f0-9]{64}", digest) is None
            or type(size) is not int or size <= 0
            or verified.get("posted_content_sha256") != hashlib.sha256(content.encode()).hexdigest()
            or not isinstance(original_digest, str)
            or re.fullmatch(r"[a-f0-9]{64}", original_digest) is None
            or original_digest != digest
            or type(original_size) is not int or original_size <= 0
            or original_size != size
            or attachment.get("filename") != "reviewer-response.txt"
            or attachment.get("size") != size
            or not isinstance(url, str) or not url.startswith("https://cdn.discordapp.com/")
            or f"({size} UTF-8 bytes; SHA-256: {digest})" not in content):
        return False
    try:
        downloaded = requests.get(url, timeout=30)  # Never send bot auth to the CDN.
        downloaded.raise_for_status()
    except requests.RequestException:
        return False
    return len(downloaded.content) == size and hashlib.sha256(downloaded.content).hexdigest() == digest


def issue_no_decision_successor(source_id: str, issue_id: str, tuple_sha256: str,
                                terminal_reply_id: str) -> dict[str, Any]:
    """Register one new issue after a terminal, non-authoritative resume reply.

    This does not send a Discord message or invoke a reviewer. A reserved claim
    is deliberately non-replayable after a crash or ambiguous issue write.
    """
    if (not re.fullmatch(r"[0-9]{17,20}", source_id)
            or not re.fullmatch(r"[0-9]{17,20}", terminal_reply_id)
            or not re.fullmatch(r"bridge-issue-[a-f0-9]{32}", issue_id)
            or not re.fullmatch(r"[a-f0-9]{64}", tuple_sha256)):
        raise RuntimeError("successor_identity_invalid")
    if SELF_ID != expected_author_id() or SELF_ID == KAEL_BOT_ID:
        raise RuntimeError("independent_signer_mismatch")
    with bridge_state_lock():
        state = load_state()
        receipts = state.get("reviewer_receipts")
        receipt = receipts.get(source_id) if isinstance(receipts, dict) else None
        resume = receipt.get("resume") if isinstance(receipt, dict) else None
        attempts = resume.get("attempts") if isinstance(resume, dict) else None
        if (not isinstance(receipt, dict) or receipt.get("source_message_id") != source_id
                or receipt.get("kind") != "review_request" or receipt.get("posting") != "posted"
                or not isinstance(resume, dict) or resume.get("status") != "posted"
                or resume.get("posting") != "posted"
                or resume.get("issue_id") != issue_id
                or resume.get("tuple_sha256") != tuple_sha256
                or resume.get("posted_message_id") != terminal_reply_id
                or resume.get("reviewer_identity") != "fable-xhigh"
                or not isinstance(attempts, list) or len(attempts) not in (1, 2)
                or any(not isinstance(attempt, dict) or attempt.get("invoked") is not True
                       for attempt in attempts)
                or attempts[0].get("status") != "ok"
                or (len(attempts) == 2 and (
                    any(attempt.get("backend") != "fable" or
                        attempt.get("model") != attempts[0].get("model") or
                        attempt.get("effort") != attempts[0].get("effort") for attempt in attempts)
                    or not attempts[0].get("model") or not attempts[0].get("effort")
                    or attempts[0].get("session_id") != attempts[1].get("session_id")
                    or not attempts[0].get("session_id")
                    or attempts[0].get("session_mode") != attempts[1].get("session_mode")
                    or attempts[0].get("legacy_session_id") != attempts[1].get("legacy_session_id")))
                or source_id in state.get("successor_issues", {})):
            raise RuntimeError("successor_terminal_receipt_not_eligible")
        original_reply_id = str(receipt.get("posted_message_id") or "")
        issue_path = ISSUE_DIR / f"{issue_id}.json"
        issue = json.loads(issue_path.read_text(encoding="utf-8"))
        body = {key: value for key, value in issue.items() if key != "record_sha256"}
        if (issue.get("record_sha256") != hashlib.sha256(json.dumps(
                body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
                or issue.get("schema") != "boring_ai_bridge_request_issue_v2"
                or issue.get("status") != "BOUND" or issue.get("issue_id") != issue_id
                or issue.get("request_message_id") != source_id
                or issue.get("tuple_sha256") != tuple_sha256
                or issue.get("review_artifact_sha256") != tuple_sha256
                or issue.get("channel_id") != CHANNEL
                or issue.get("request_author_id") != KAEL_BOT_ID):
            raise RuntimeError("successor_predecessor_issue_invalid")
        artifact = Path(issue["review_artifact_path"])
        if (not artifact.resolve(strict=True).is_relative_to(ARTIFACT_ROOT.resolve())
                or hashlib.sha256(artifact.read_bytes()).hexdigest() != tuple_sha256):
            raise RuntimeError("successor_artifact_drift")
        request = _exact_message(source_id)
        if (request.get("content") != issue.get("request_content")
                or request.get("timestamp") != issue.get("request_timestamp")
                or request.get("edited_timestamp") is not None
                or str((request.get("author") or {}).get("id")) != KAEL_BOT_ID
                or classify_message(request) is None
                or classify_message(request)[0] != "review_request"):
            raise RuntimeError("successor_predecessor_request_drift")
        for message_id, record in ((original_reply_id, receipt), (terminal_reply_id, resume)):
            reply = _exact_message(message_id)
            content = reply.get("content")
            if (not isinstance(content, str)
                    or str((reply.get("author") or {}).get("id")) != SELF_ID
                    or str((reply.get("message_reference") or {}).get("message_id")) != source_id
                    or reply.get("edited_timestamp") is not None
                    or len(content) != record.get("reply_chars")
                    or hashlib.sha256(content.encode()).hexdigest() != record.get("reply_sha256")
                    or "no authority decision was issued" not in content
                    or re.search(r"TRUE-BRIDGE FABLE\s*[—–-]\s*(?:ACCEPT|REJECT)\b",
                                 content, re.IGNORECASE)
                    or (record is receipt and reply.get("attachments"))
                    or (record is resume and (
                        not _successor_verified_attachment(reply, resume, content)
                        if len(attempts) == 2 else bool(reply.get("attachments"))))):
                raise RuntimeError("successor_predecessor_reply_not_no_decision")
        _only_expected_replies(source_id, {original_reply_id, terminal_reply_id})
        # Recheck against existing registered successor records as well as the
        # locked bridge state; no second issue on a repeated CLI invocation.
        for path in ISSUE_DIR.glob("bridge-issue-*.json"):
            registered = json.loads(path.read_text(encoding="utf-8"))
            if registered.get("predecessor_request_message_id") == source_id:
                raise RuntimeError("successor_already_registered")
        state.setdefault("successor_issues", {})[source_id] = {
            "status": "reserved", "predecessor_issue_id": issue_id,
            "predecessor_reply_message_id": terminal_reply_id}
        save_state(state)
        guard = _authority_guard()
        path, fresh = guard.issue_request(subject_id=issue["subject_id"],
                                          tuple_sha256=tuple_sha256,
                                          review_artifact_path=artifact)
        fresh.update(predecessor_issue_id=issue_id,
                     predecessor_request_message_id=source_id,
                     predecessor_reply_message_id=terminal_reply_id,
                     succession_reason="terminal_no_authority_decision")
        fresh["record_sha256"] = guard._record_hash(fresh)
        temporary = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
        temporary.write_text(json.dumps(fresh, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, path)
        state["successor_issues"][source_id].update(status="issued", issue_id=fresh["issue_id"],
                                                      issue_record_path=str(path))
        save_state(state)
        return {"status": "ISSUED_NOT_SENT", "issue_id": fresh["issue_id"],
                "issue_record_path": str(path), "request_content": fresh["request_content"],
                "predecessor_request_message_id": source_id,
                "predecessor_reply_message_id": terminal_reply_id}


def main() -> None:
    global TOKEN, SELF_ID, SESSION_ID, HTTP
    signal.signal(signal.SIGTERM, _request_stop)
    TOKEN = token()
    HTTP = requests.Session()
    HTTP.headers.update({
        "Authorization": "Bot " + TOKEN,
        "User-Agent": "DiscordBot (https://tryboringai.com, 2.0) fable-bridge",
    })
    SESSION_ID = SID_FILE.read_text(encoding="utf-8").strip()
    if not SESSION_ID:
        raise RuntimeError("fable_session_id_missing")
    SELF_ID = validate_startup_identity()

    state = load_state()
    if state.get("last_id") is None:
        messages = api("GET", f"/channels/{CHANNEL}/messages?limit=1") or []
        state["last_id"] = messages[0]["id"] if messages else "0"
        save_state(state)
    log(f"bridge_up identity={SELF_ID} channel={CHANNEL} session_configured=true")

    while not STOP_REQUESTED:
        with bridge_state_lock():
            state = load_state()  # Resume may have amended a receipt since the last poll.
            messages = api("GET", f"/channels/{CHANNEL}/messages?after={state['last_id']}&limit=10")
            if messages:
                for message in sorted(messages, key=lambda item: int(item["id"])):
                    if STOP_REQUESTED:
                        break
                    state["last_id"] = message["id"]
                    save_state(state)
                    classified = classify_message(message)
                    if classified is None:
                        continue
                    kind, text = classified
                    log(f"inbound kind={kind} author={message.get('author', {}).get('id')} message={message['id']}")
                    source_id = str(message["id"])
                    if source_id in state.get("reviewer_receipts", {}):
                        raise RuntimeError("already_receipted_source_message")
                    receipt: dict[str, Any] = {"source_message_id": source_id, "kind": kind}
                    def claim_review() -> None:
                        receipt["invocation_claimed"] = True
                        state.setdefault("reviewer_receipts", {})[source_id] = {
                            key: value for key, value in receipt.items() if key != "original_stdout"}
                        save_state(state)
                    action_mode, reply = review_action(
                        kind, text, source_message_id=source_id, receipt=receipt,
                        before_invoke=claim_review if kind == "review_request" else None)
                    original_stdout = receipt.pop("original_stdout", None)
                    receipt.update(
                        mode=action_mode, reply_chars=len(reply),
                        reply_sha256=hashlib.sha256(reply.encode("utf-8")).hexdigest(),
                        posting="prepared", posted_message_id=None,
                    )
                    receipts = state.setdefault("reviewer_receipts", {})
                    if not isinstance(receipts, dict):
                        raise RuntimeError("reviewer_receipts_invalid")
                    receipts[source_id] = receipt
                    save_state(state)
                    try:
                        posted = post(reply, reply_to_message_id=source_id, kind=kind,
                                      original_stdout=original_stdout)
                    except Exception:
                        receipt["posting"] = "failed_or_ambiguous"
                        save_state(state)
                        raise
                    receipt.update(posting="posted", posted_message_id=posted)
                    save_state(state)
                    try:
                        verified = verify_oversize_post(posted, source_id, reply, kind,
                                            original_stdout=original_stdout)
                    except Exception:
                        receipt["posting"] = "posted_unverified"
                        save_state(state)
                        raise
                    if verified:
                        receipt["attachment_verification"] = verified
                        save_state(state)
                    log(f"outbound kind={kind} mode={action_mode} chars={len(reply)} message={posted} reviewer_identity={receipt['reviewer_identity']}")
        if not STOP_REQUESTED:
            time.sleep(5)


if __name__ == "__main__":
    if sys.argv[1:2] in (["--native-reviewer"], ["--native-parent-reviewer"]):
        manual_native = sys.argv[1] == "--native-parent-reviewer"
        if manual_native:
            spec = importlib.util.spec_from_file_location("fable_parent_handoff_native",
                Path(__file__).with_name("fable_parent_handoff.py"))
            if spec is None or spec.loader is None:
                raise RuntimeError("parent_handoff_native_entry_unavailable")
            parent_native = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(parent_native)
            parent_native.pin_native_profile()  # BEFORE toolsets/main/cli import.
        # Process-local native Hermes toolset: no write/terminal/browser/delegation tools.
        from toolsets import create_custom_toolset
        from hermes_cli.main import main as hermes_main
        # Neither the designated Fable route nor GLP42 may change reviewer via
        # the CLI's saved fallback chain. This changes only this child process.
        from cli import CLI_CONFIG
        CLI_CONFIG["fallback_providers"] = []
        CLI_CONFIG.pop("fallback_model", None)
        if "--provider" in sys.argv and sys.argv[sys.argv.index("--provider") + 1] == "openai-codex":
            CLI_CONFIG.setdefault("agent", {})["service_tier"] = "default"
        create_custom_toolset("fable-review-readonly", "Read-only independent review", [
            "read_file", "search_files", "web_search", "web_extract", "vision_analyze",
        ])
        if not manual_native:
            sys.argv = ["hermes", *sys.argv[2:]]
        if "--provider" in sys.argv and sys.argv[sys.argv.index("--provider") + 1] == "claude-subscription-directsdk-experimental":
            # Installed oneshot._run_agent reloads config; CLI_CONFIG alone
            # cannot disable resolution-time or mid-turn fallback on this route.
            import copy
            import hermes_cli.config as native_config
            original_load_config = native_config.load_config
            def readonly_fable_config(*args, **kwargs):
                value = copy.deepcopy(original_load_config(*args, **kwargs))
                value["fallback_providers"] = []
                value.pop("fallback_model", None)
                return value
            native_config.load_config = readonly_fable_config
            try:
                if manual_native:
                    with parent_native.native_resume_guard(HERMES_STATE,
                            _session_transcript(parent_native.LEGACY_SESSION_ID)):
                        hermes_main()
                else:
                    hermes_main()
            finally:
                native_config.load_config = original_load_config
        else:
            hermes_main()
    elif sys.argv[1:2] in (["--resume-preinvocation-quota"], ["--issue-no-decision-successor"],
                          ["--recover-missing-mention"], ["--parent-review-handoff"],
                          ["--send-deferred-request"], ["--dispatch-deferred-review"]):
        if sys.argv[1] == "--parent-review-handoff" and len(sys.argv) != 6:
            raise SystemExit("usage: fable_bridge.py --parent-review-handoff REQUEST_ID ISSUE_ID TUPLE_SHA256 OPERATOR_INTENT_MESSAGE_ID")
        if len(sys.argv) != 5 and sys.argv[1] in {"--resume-preinvocation-quota", "--recover-missing-mention"}:
            raise SystemExit(f"usage: fable_bridge.py {sys.argv[1]} REQUEST_ID ISSUE_ID TUPLE_SHA256")
        if len(sys.argv) != 6 and sys.argv[1] == "--issue-no-decision-successor":
            raise SystemExit("usage: fable_bridge.py --issue-no-decision-successor REQUEST_ID ISSUE_ID TUPLE_SHA256 TERMINAL_REPLY_ID")
        if sys.argv[1] == '--send-deferred-request' and len(sys.argv) != 5:
            raise SystemExit('usage: --send-deferred-request ISSUE_ID TUPLE_SHA256 ADMISSION')
        if sys.argv[1] == '--dispatch-deferred-review' and len(sys.argv) != 6:
            raise SystemExit('usage: --dispatch-deferred-review REQUEST_ID ISSUE_ID TUPLE_SHA256 ADMISSION')
        TOKEN = token()
        HTTP = requests.Session()
        HTTP.headers.update({"Authorization": "Bot " + TOKEN,
                             "User-Agent": "DiscordBot (https://tryboringai.com, 2.0) fable-bridge"})
        SESSION_ID = SID_FILE.read_text(encoding="utf-8").strip()
        if not SESSION_ID:
            raise RuntimeError("fable_session_id_missing")
        SELF_ID = validate_startup_identity()
        from fable_parent_handoff import dispatch as parent_handoff_dispatch
        from fable_deferred_handoff import send_request, dispatch as deferred_dispatch
        result = {"--send-deferred-request": lambda *args: send_request(sys.modules[__name__], *args),
                  "--dispatch-deferred-review": lambda *args: deferred_dispatch(sys.modules[__name__], *args),
                  "--parent-review-handoff": lambda *args: parent_handoff_dispatch(sys.modules[__name__], *args),
                  "--resume-preinvocation-quota": resume_preinvocation_quota,
                  "--recover-missing-mention": recover_missing_mention,
                  "--issue-no-decision-successor": issue_no_decision_successor}[sys.argv[1]](*sys.argv[2:])
        print(json.dumps(result, sort_keys=True))
    else:
        main()
