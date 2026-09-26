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
            and float(row.get("weekly_remaining_pct") or 0) > 0
            and float(row.get("session_remaining_pct") or 0) > 0
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


def select_backend(*, claude_exhausted: bool = False, design_review: bool = False) -> tuple[str, str, str]:
    """Re-evaluate policy and usage for every request; never latch a fallback."""
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
    """Append metadata, never truncate an authority decision to make it fit."""
    suffix = f"\n[reviewer: {identity}]"
    limit = min(MAX_REPLY_CHARS, 1399) if kind == "review_request" else MAX_REPLY_CHARS
    body = output.strip() or "(Reviewer returned no output; no authority decision was issued.)"
    if "[reviewer:" in body.casefold():
        body = "(Reviewer supplied reserved identity metadata; no authority decision was issued.)"
    if len(body) + len(suffix) > limit:
        body = "(Reviewer response exceeded the single-message safety limit; no authority decision was issued.)"
    return body + suffix


def api(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    if HTTP is None:
        raise RuntimeError("dedicated_fable_http_session_not_initialized")
    method = method.upper()
    for attempt in range(4):
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


def post(content: str, *, reply_to_message_id: str, nonce: str | None = None) -> str:
    reply = content.strip() or "(Fable returned no output; no authority decision was issued.)"
    if len(reply) > MAX_REPLY_CHARS:
        reply = "(Fable response exceeded the single-message safety limit; no authority decision was issued.)"
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
    result = api("POST", f"/channels/{CHANNEL}/messages", body)
    if not isinstance(result, dict) or not str(result.get("id") or "").isdigit():
        raise RuntimeError("dedicated_fable_message_post_failed_or_ambiguous")
    return str(result["id"])


def load_state() -> dict[str, Any]:
    try:
        value = json.loads(STATE.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {"last_id": None}
    except Exception:
        return {"last_id": None}


def save_state(value: dict[str, Any]) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE.with_suffix(STATE.suffix + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, STATE)


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
    if receipt is None:
        return "model", ask_fable(kind, text)
    return "model", ask_fable(kind, text, receipt=receipt,
                              capacity_snapshot=capacity_snapshot, before_invoke=before_invoke)


def _invoke_reviewer(
    backend: tuple[str, str, str], prompt: str, receipt: dict[str, Any], deadline: float,
    *, capacity_snapshot: dict[str, Any] | None = None,
    before_invoke: Callable[[], None] | None = None,
) -> tuple[str, bool]:
    name, model, effort = backend
    identity = "fable-xhigh" if name == "fable" else "astra-standin (claude_exhausted)"
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
                    str(HERMES_PYTHON), str(Path(__file__).resolve()), "--native-reviewer",
                    "chat", "--query-file", str(prompt_path), "-Q", "--oneshot",
                    "--resume", native_id, "--no-restore-cwd", "--provider", "claude-subscription-directsdk-experimental",
                    "-m", model, "--reasoning", effort, "--toolsets", "fable-review-readonly",
                    "--source", "fable-bridge-reviewer",
                ]
            else:
                session_name = "fable-bridge-astra-standin-" + uuid.uuid4().hex
                attempt["session_name"] = session_name
                prompt_path = Path(directory) / "prompt.txt"
                prompt_path.write_text(prompt, encoding="utf-8")
                command = [
                    "hermes", "chat", "--query-file", str(prompt_path), "-Q", "--oneshot",
                    "-m", model, "--reasoning", effort, "--source", session_name,
                    "--provider", "openai-codex",
                    "--skills", "agent-evaluation-review,site-design,cloudflare-release-operations,discord-delivery",
                ]
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, 0)
            log(f"reviewer_cli_start backend={name} model={model} effort={effort}")
            attempt["invoked"] = True
            if before_invoke is not None:
                before_invoke()  # Durable claim before an uncertain external inference.
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=remaining,
                cwd="/home/ubuntu/business",
            )
        attempt["exit"] = result.returncode
        output = (result.stdout or "").strip()
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
              before_invoke: Callable[[], None] | None = None) -> str:
    record = receipt if receipt is not None else {}
    record.update(reviewer_identity="none (model_policy_unavailable)", attempts=[])
    deadline = time.monotonic() + 1200
    prompt = _model_prompt(kind, text)
    try:
        design = _design_review(kind, text)
        backend = select_backend(design_review=design)
        output, exhausted = _invoke_reviewer(backend, prompt, record, deadline,
                                             capacity_snapshot=capacity_snapshot,
                                             before_invoke=before_invoke)
        if exhausted and not design and before_invoke is None:
            backend = select_backend(claude_exhausted=True)
            output, _ = _invoke_reviewer(backend, prompt, record, deadline)
    except Exception as error:
        log(f"fail_closed model_policy type={type(error).__name__}")
        output = "(model policy unavailable; Fable lane paused and no authority decision was issued.)"
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


def _only_original_refusal(source_id: str, refusal_id: str) -> None:
    """Fail closed on later replies or incomplete newest-first history."""
    # Discord `after` can return the newest page, silently skipping intervening
    # decision replies. Walk backwards from the current snowflake instead.
    cursor = ((int(time.time() * 1000) - 1420070400000) << 22) | 0x3fffff
    seen = False
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
            if int(item["id"]) <= int(source_id):
                continue
            if str((item.get("message_reference") or {}).get("message_id")) != source_id:
                continue
            if str((item.get("author") or {}).get("id")) == SELF_ID:
                if str(item["id"]) != refusal_id or seen:
                    raise RuntimeError("additional_reviewer_reply_present")
                seen = True
            else:
                raise RuntimeError("other_source_reply_present")
        cursor = min(ids)
        if cursor <= int(source_id):
            if not seen:
                raise RuntimeError("original_refusal_not_in_history")
            return
    raise RuntimeError("reply_history_incomplete")


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
        resumed.update(mode=mode, reviewer_identity=review_receipt.get("reviewer_identity"),
                       attempts=review_receipt.get("attempts", []),
                       reply_chars=len(reply), reply_sha256=hashlib.sha256(reply.encode()).hexdigest())
        if (mode != "model" or review_receipt.get("reviewer_identity") != "fable-xhigh"
                or review_receipt.get("backend") != "fable"
                or len(resumed["attempts"]) != 1
                or resumed["attempts"][0].get("invoked") is not True
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
            posted = post(reply, reply_to_message_id=source_id, nonce=resumed["nonce"])
        except Exception:
            resumed.update(status="post_failed_or_ambiguous", posting="failed_or_ambiguous")
            save_state(state)
            raise
        resumed.update(status="posted", posting="posted", posted_message_id=posted)
        save_state(state)
        return {"source_message_id": source_id, "posted_message_id": posted,
                "reviewer_identity": resumed["reviewer_identity"], "status": "posted"}


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
                    action_mode, reply = review_action(kind, text, source_message_id=source_id, receipt=receipt)
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
                        posted = post(reply, reply_to_message_id=source_id)
                    except Exception:
                        receipt["posting"] = "failed_or_ambiguous"
                        save_state(state)
                        raise
                    receipt.update(posting="posted", posted_message_id=posted)
                    save_state(state)
                    log(f"outbound kind={kind} mode={action_mode} chars={len(reply)} message={posted} reviewer_identity={receipt['reviewer_identity']}")
        if not STOP_REQUESTED:
            time.sleep(5)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--native-reviewer"]:
        # Process-local native Hermes toolset: no write/terminal/browser/delegation tools.
        from toolsets import create_custom_toolset
        from hermes_cli.main import main as hermes_main
        create_custom_toolset("fable-review-readonly", "Read-only independent review", [
            "read_file", "search_files", "web_search", "web_extract", "vision_analyze",
        ])
        sys.argv = ["hermes", *sys.argv[2:]]
        hermes_main()
    elif sys.argv[1:2] == ["--resume-preinvocation-quota"]:
        if len(sys.argv) != 5:
            raise SystemExit("usage: fable_bridge.py --resume-preinvocation-quota REQUEST_ID ISSUE_ID TUPLE_SHA256")
        TOKEN = token()
        HTTP = requests.Session()
        HTTP.headers.update({"Authorization": "Bot " + TOKEN,
                             "User-Agent": "DiscordBot (https://tryboringai.com, 2.0) fable-bridge"})
        SESSION_ID = SID_FILE.read_text(encoding="utf-8").strip()
        if not SESSION_ID:
            raise RuntimeError("fable_session_id_missing")
        SELF_ID = validate_startup_identity()
        print(json.dumps(resume_preinvocation_quota(*sys.argv[2:]), sort_keys=True))
    else:
        main()
