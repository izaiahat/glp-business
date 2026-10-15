#!/usr/bin/env python3
"""Canonical authenticated Discord authority guard for Boring AI.

Only this module may load the Discord bot token or perform authenticated
Discord message GETs for the Boring governance chains.  Public acceptance APIs
accept message IDs and bindings, never caller-supplied message dictionaries.
Tests inject network by patching the private ``_fetch_discord_message`` seam.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

FIXED_BRIDGE_CHANNEL_ID = "1524279642276626534"
KAEL_SHARED_BOT_AUTHOR_ID = "1486880423568211968"
FABLE_BRIDGE_AUTHOR_ID_ENV = "BORING_AI_FABLE_BRIDGE_AUTHOR_ID"
FABLE_BRIDGE_AUTHORITY_CONFIG = Path(__file__).resolve().parents[1] / "config/bridge-authority-v1.json"
BRIDGE_AUTHORITY_SCHEMA = "boring_ai_bridge_authority_v1"
BRIDGE_ISSUE_SCHEMA_V1 = "boring_ai_bridge_request_issue_v1"
BRIDGE_ISSUE_SCHEMA_V2 = "boring_ai_bridge_request_issue_v2"
BRIDGE_ISSUE_SCHEMA = BRIDGE_ISSUE_SCHEMA_V2
HISTORICAL_V1_ACCEPTED_PACKET_IDS = {"pkt_dfdb71d6c7ebbf877012"}
BRIDGE_ISSUE_ROOT = Path("/home/ubuntu/business/reports/boring-ai/bridge-authority-issues")
REVIEW_ARTIFACT_ROOT = Path("/home/ubuntu/business/reports")
MIN_EXTERNAL_REVIEW_SECONDS = 30.0
REQUIRED_AUTHORITY_MARKERS = ("TRUE-BRIDGE FABLE", "SOLE valid acceptance")
EXACT_ACCEPTANCE_DECISION_LINE = "TRUE-BRIDGE FABLE — ACCEPT (this message is the SOLE valid acceptance)"
INSPECTION_NOTE_PREFIX = "inspection_note:"
MIN_INSPECTION_NOTE_CHARS = 80
MAX_INSPECTION_NOTE_CHARS = 500
FORBIDDEN_AUTHORITY_MARKERS = (
    "instrument advisory",
    "operator-direct acceptance is already authorized",
    "authority: izaiah operator-direct command",
    "operator_direct_current_session",
    "automatic_acceptance",
    "claude-opus",
    "opus instrument",
    "instrument session",
)
_SNOWFLAKE_RE = re.compile(r"[0-9]{17,20}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_CHALLENGE_RE = re.compile(r"[A-Za-z0-9_-]{43}")
_SUBJECT_RE = re.compile(r"[A-Za-z0-9:/._-]{8,240}")
_ISSUE_ID_RE = re.compile(r"bridge-issue-[0-9a-f]{32}")
_DEFAULT_HERMES_ENV = Path("/home/ubuntu/.hermes/.env")
_DISCORD_API_BASE = "https://discord.com/api/v10"
_MAX_ACCEPTANCE_ATTACHMENT_BYTES = 1024 * 1024
_ACCEPTANCE_ATTACHMENT_URL_RE = re.compile(
    rf"https://cdn\.discordapp\.com/attachments/{FIXED_BRIDGE_CHANNEL_ID}/"
    r"[0-9]{17,20}/reviewer-response\.txt(?:\?[^#\s]*)?"
)

# Retained operator designation, narrowed to the frozen PR8/current-release
# subject assigned by GLP73. Not a generic design waiver or a moving PR head.
GLP42_DESIGNATION = "glp42-pr8-astra-20260926"
GLP42_REVIEW_AUTHORITY = Path("/home/ubuntu/business/ops/linear/approvals/OPERATOR-DECISION-20260926-release-review-astra.md")
GLP42_AUTHORITY_SHA256 = "7d0d49def4254a19e088d4d6586c9963325311da17cfbd6054dd175876240fb0"
GLP42_REVIEW_SUBJECT = {
    "subject_id": "glp:GLP-42:pr8:38133df58fc5406adeb87ed88771cc6ceeb26342",
    "site_head": "38133df58fc5406adeb87ed88771cc6ceeb26342",
    "sealed_root": "/home/ubuntu/business/reports/ops/glp43-wrangler-shape-repair-20260927/prepared-final381/release",
    "seal_closure_sha256": "87aec3395aa7e234fa441ac7c47e5972ae3216bf874f2375e62dc8688e179ec2",
    "public_tree_sha256": "f186ce38f36fcc1eab8c9f2f000289a09cd76f8327b8fec255969802dcb7b82d",
    "deployment_manifest_sha256": "61461fe6d224571f2bfb0d579c347b779d2026e1294b0ed40bd22b26e1bd8c8c",
    "deployment_id": "8291d24b-a1c6-4340-bd42-2efa9f2ba2b7",
    "rollback_target": "ca0251a2-dd4e-469c-be3b-79ee8805c5f3",
    "reviewer_authority_sha256": GLP42_AUTHORITY_SHA256,
}


class BridgeAuthorityError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def generate_challenge() -> str:
    """Return a 256-bit URL-safe challenge for a new authority request."""
    challenge = secrets.token_urlsafe(32)
    if not _CHALLENGE_RE.fullmatch(challenge):  # pragma: no cover - stdlib contract tripwire
        raise BridgeAuthorityError("bridge_challenge_generation_failed")
    return challenge


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _record_hash(payload: dict[str, Any]) -> str:
    body = dict(payload)
    body.pop("record_sha256", None)
    return sha256_text(canonical_json(body))


def _review_artifact(path_value: Path | str) -> tuple[Path, str]:
    supplied = Path(path_value).expanduser().absolute()
    resolved = supplied.resolve()
    root = REVIEW_ARTIFACT_ROOT.resolve()
    if supplied.is_symlink() or not resolved.is_file() or not resolved.is_relative_to(root):
        raise BridgeAuthorityError("bridge_review_artifact_invalid")
    return resolved, hashlib.sha256(resolved.read_bytes()).hexdigest()


def _reviewer_designation(artifact: Path, subject_id: str, tuple_sha256: str) -> str | None:
    """Authenticate an explicit new-packet opt-in, not a caller's routing marker.

    Routing binds the exact subject, not evidence sufficiency: the independent
    reviewer still owes the full gate/renders/consent/package/rollback inspection.
    """
    try:
        packet = json.loads(artifact.read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return None  # Ordinary historical artifacts need not be JSON.
    if not isinstance(packet, dict) or "reviewer_designation" not in packet:
        return None
    subject = packet.get("tuple")
    if (packet["reviewer_designation"] != GLP42_DESIGNATION
            or packet.get("schema") != "glp.q44.independent-review.v1"
            or packet.get("status") != "READY_FOR_INDEPENDENT_REVIEW_NOT_ACCEPTED"
            or not isinstance(subject, dict)
            or any(subject.get(key) != value for key, value in GLP42_REVIEW_SUBJECT.items())
            or subject_id != GLP42_REVIEW_SUBJECT["subject_id"]
            or packet.get("tuple_sha256") != tuple_sha256
            or sha256_text(canonical_json(subject)) != tuple_sha256):
        raise BridgeAuthorityError("bridge_reviewer_designation_scope_mismatch")
    if (GLP42_REVIEW_AUTHORITY.is_symlink()
            or hashlib.sha256(GLP42_REVIEW_AUTHORITY.read_bytes()).hexdigest() != GLP42_AUTHORITY_SHA256):
        raise BridgeAuthorityError("bridge_reviewer_designation_authority_changed")
    return GLP42_DESIGNATION


def build_issue_record(
    *,
    subject_id: str,
    tuple_sha256: str,
    review_artifact_path: Path | str,
    issued_at_utc: str | None = None,
) -> dict[str, Any]:
    """Create canonical bytes for a fresh guard-owned request issue."""
    if not _SUBJECT_RE.fullmatch(subject_id):
        raise BridgeAuthorityError("bridge_subject_invalid")
    if not _SHA256_RE.fullmatch(tuple_sha256):
        raise BridgeAuthorityError("tuple_sha256_invalid")
    challenge = generate_challenge()
    issue_nonce = secrets.token_urlsafe(32)
    if not _CHALLENGE_RE.fullmatch(issue_nonce):  # pragma: no cover - stdlib contract tripwire
        raise BridgeAuthorityError("bridge_issue_generation_failed")
    issue_id = "bridge-issue-" + sha256_text(issue_nonce)[:32]
    artifact_path, artifact_sha256 = _review_artifact(review_artifact_path)
    fable_author_id = _configured_fable_author_id()
    request_content = "\n".join((
        f"<@{fable_author_id}> TRUE-BRIDGE FABLE REVIEW REQUEST",
        f"issue_id: {issue_id}",
        f"subject_id: {subject_id}",
        f"tuple_sha256: {tuple_sha256}",
        f"challenge: {challenge}",
        f"review_artifact_path: {artifact_path}",
        f"review_artifact_sha256: {artifact_sha256}",
        "response_contract_mandatory:",
        "response_max_chars: 1400",
        "inspection_note_max_chars: 500",
        "inspection_note_min_chars: 80",
        "inspection_note_required_prefix: inspection_note:",
        "do_not_repeat_contract_lines: true",
        f"exact_decision_line: {EXACT_ACCEPTANCE_DECISION_LINE}",
        f"include_exact_issue_id: {issue_id}",
        f"include_exact_subject_id: {subject_id}",
        f"include_exact_tuple_sha256: {tuple_sha256}",
        f"include_exact_challenge: {challenge}",
    ))
    designation = _reviewer_designation(artifact_path, subject_id, tuple_sha256)
    if designation is not None:
        request_content += f"\nreviewer_designation: {designation}"
    record: dict[str, Any] = {
        "schema": BRIDGE_ISSUE_SCHEMA,
        "status": "ISSUED",
        "issue_id": issue_id,
        "issued_at_utc": issued_at_utc or _utc_now(),
        "channel_id": FIXED_BRIDGE_CHANNEL_ID,
        "request_author_id": KAEL_SHARED_BOT_AUTHOR_ID,
        "subject_id": subject_id,
        "tuple_sha256": tuple_sha256,
        "challenge": challenge,
        "review_artifact_path": str(artifact_path),
        "review_artifact_sha256": artifact_sha256,
        "request_content": request_content,
        "request_content_sha256": sha256_text(request_content),
    }
    if designation is not None:
        record["reviewer_designation"] = designation
    _timestamp(record["issued_at_utc"], "issue")
    record["record_sha256"] = _record_hash(record)
    return record


def issue_request(
    *, subject_id: str, tuple_sha256: str, review_artifact_path: Path | str, output: Path | None = None
) -> tuple[Path, dict[str, Any]]:
    """Issue and atomically persist a canonical Bridge request record."""
    record = build_issue_record(
        subject_id=subject_id,
        tuple_sha256=tuple_sha256,
        review_artifact_path=review_artifact_path,
    )
    root = BRIDGE_ISSUE_ROOT.resolve()
    path = (output or (root / f"{record['issue_id']}.json")).expanduser().absolute()
    if path.parent.resolve() != root or path.name != f"{record['issue_id']}.json" or path.exists() or path.is_symlink():
        raise BridgeAuthorityError("bridge_issue_output_path_invalid")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
    temporary.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return path, record


def load_issue_record(
    path_value: Path | str,
    *,
    expected_subject_id: str,
    expected_tuple_sha256: str,
    require_bound: bool = True,
) -> tuple[Path, dict[str, Any]]:
    """Verify an issue from the guard-owned registry and return its canonical binding."""
    supplied = Path(path_value).expanduser().absolute()
    root = BRIDGE_ISSUE_ROOT.resolve()
    path = supplied.resolve()
    if supplied.is_symlink() or path.parent != root or not path.is_file():
        raise BridgeAuthorityError("bridge_issue_record_path_invalid")
    try:
        record = json.loads(path.read_text(encoding="utf-8", errors="strict"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BridgeAuthorityError("bridge_issue_record_unreadable") from error
    if not isinstance(record, dict) or record.get("schema") not in {BRIDGE_ISSUE_SCHEMA_V1, BRIDGE_ISSUE_SCHEMA_V2}:
        raise BridgeAuthorityError("bridge_issue_record_schema_invalid")
    expected_status = "BOUND" if require_bound else "ISSUED"
    if record.get("status") != expected_status:
        raise BridgeAuthorityError("bridge_issue_record_not_bound" if require_bound else "bridge_issue_record_not_issuable")
    issue_id = str(record.get("issue_id") or "")
    if not _ISSUE_ID_RE.fullmatch(issue_id) or path.name != f"{issue_id}.json":
        raise BridgeAuthorityError("bridge_issue_record_identity_invalid")
    if record.get("record_sha256") != _record_hash(record):
        raise BridgeAuthorityError("bridge_issue_record_hash_mismatch")
    if record.get("channel_id") != FIXED_BRIDGE_CHANNEL_ID or record.get("request_author_id") != KAEL_SHARED_BOT_AUTHOR_ID:
        raise BridgeAuthorityError("bridge_issue_record_authority_mismatch")
    if record.get("subject_id") != expected_subject_id or record.get("tuple_sha256") != expected_tuple_sha256:
        raise BridgeAuthorityError("bridge_issue_record_binding_mismatch")
    challenge = str(record.get("challenge") or "")
    artifact_path, artifact_sha256 = _review_artifact(str(record.get("review_artifact_path") or ""))
    if record.get("review_artifact_sha256") != artifact_sha256:
        raise BridgeAuthorityError("bridge_review_artifact_hash_mismatch")
    fable_author_id = _configured_fable_author_id()
    request_content = str(record.get("request_content") or "")
    expected_lines = [
        f"<@{fable_author_id}> TRUE-BRIDGE FABLE REVIEW REQUEST",
        f"issue_id: {issue_id}",
        f"subject_id: {expected_subject_id}",
        f"tuple_sha256: {expected_tuple_sha256}",
        f"challenge: {challenge}",
        f"review_artifact_path: {artifact_path}",
        f"review_artifact_sha256: {artifact_sha256}",
        "response_contract_mandatory:",
        "response_max_chars: 1400",
        "inspection_note_max_chars: 500",
    ]
    if record.get("schema") == BRIDGE_ISSUE_SCHEMA_V2:
        expected_lines.extend((
            "inspection_note_min_chars: 80",
            "inspection_note_required_prefix: inspection_note:",
        ))
    expected_lines.extend((
        "do_not_repeat_contract_lines: true",
        f"exact_decision_line: {EXACT_ACCEPTANCE_DECISION_LINE}",
        f"include_exact_issue_id: {issue_id}",
        f"include_exact_subject_id: {expected_subject_id}",
        f"include_exact_tuple_sha256: {expected_tuple_sha256}",
        f"include_exact_challenge: {challenge}",
    ))
    # Absence preserves issued v1/v2 bytes; never retrofit historical requests.
    if "reviewer_designation" in record:
        designation = _reviewer_designation(artifact_path, expected_subject_id, expected_tuple_sha256)
        if (record.get("schema") != BRIDGE_ISSUE_SCHEMA_V2 or designation is None
                or record["reviewer_designation"] != designation):
            raise BridgeAuthorityError("bridge_reviewer_designation_scope_mismatch")
        expected_lines.append(f"reviewer_designation: {designation}")
    expected_content = "\n".join(expected_lines)
    if not _CHALLENGE_RE.fullmatch(challenge) or request_content != expected_content:
        raise BridgeAuthorityError("bridge_issue_record_request_invalid")
    if record.get("request_content_sha256") != sha256_text(request_content):
        raise BridgeAuthorityError("bridge_issue_record_request_hash_mismatch")
    _timestamp(record.get("issued_at_utc"), "issue")
    if require_bound:
        if not _SNOWFLAKE_RE.fullmatch(str(record.get("request_message_id") or "")):
            raise BridgeAuthorityError("bridge_issue_request_message_id_invalid")
        _timestamp(record.get("request_timestamp"), "request")
        _timestamp(record.get("bound_at_utc"), "bound")
        if record.get("request_edited_timestamp") is not None:
            raise BridgeAuthorityError("bridge_issue_request_edited")
    return path, record


def validate_scoped_review_request(*, request_message_id: str, request_text: str) -> dict[str, Any]:
    """Read back the BOUND canonical request before using the narrow designation.

    A pasted marker, a replay under another message ID, or caller-supplied
    Discord objects cannot confer reviewer authority.
    """
    ids = re.findall(r"^issue_id: (bridge-issue-[0-9a-f]{32})$", request_text, re.M)
    tuples = re.findall(r"^tuple_sha256: ([0-9a-f]{64})$", request_text, re.M)
    if (not isinstance(request_message_id, str) or not _SNOWFLAKE_RE.fullmatch(request_message_id)
            or len(ids) != 1 or len(tuples) != 1):
        raise BridgeAuthorityError("bridge_scoped_request_identity_invalid")
    _, issue = load_issue_record(BRIDGE_ISSUE_ROOT / f"{ids[0]}.json",
        expected_subject_id=GLP42_REVIEW_SUBJECT["subject_id"], expected_tuple_sha256=tuples[0])
    if (issue.get("reviewer_designation") != GLP42_DESIGNATION
            or issue.get("request_message_id") != request_message_id
            or issue["request_content"] != f"<@{_configured_fable_author_id()}> {request_text}"):
        raise BridgeAuthorityError("bridge_scoped_request_binding_mismatch")
    message = _fetch_discord_message(FIXED_BRIDGE_CHANNEL_ID, request_message_id)
    _successor_message_eligible(message)
    _reject_edited_message(message, "request")
    content = _message_identity(message, channel_id=FIXED_BRIDGE_CHANNEL_ID,
        message_id=request_message_id, author_id=KAEL_SHARED_BOT_AUTHOR_ID, role="request")
    if content != issue["request_content"] or message.get("timestamp") != issue["request_timestamp"]:
        raise BridgeAuthorityError("bridge_scoped_request_readback_mismatch")
    return issue


def successor_request_payload(issue: dict[str, Any]) -> dict[str, Any]:
    """Only the configured reviewer may be mentioned; no blanket user parsing."""
    return {"content": issue["request_content"],
            "allowed_mentions": {"parse": [], "users": [_configured_fable_author_id()]},
            "nonce": sha256_text(issue["issue_id"])[:24], "enforce_nonce": True}


def validate_successor_payload(issue: dict[str, Any], payload: dict[str, Any]) -> None:
    # Validate the actual outgoing payload, not merely the canonical text.
    if payload != {"content": issue["request_content"],
                   "allowed_mentions": {"parse": [], "users": [_configured_fable_author_id()]},
                   "nonce": sha256_text(issue["issue_id"])[:24], "enforce_nonce": True}:
        raise BridgeAuthorityError("bridge_successor_payload_suppresses_mention_or_drift")


def _successor_message_eligible(message: dict[str, Any]) -> None:
    mentions = message.get("mentions")
    if (not isinstance(mentions, list)
            or not any(isinstance(item, dict) and item.get("id") == _configured_fable_author_id()
                       for item in mentions)
            or (message.get("author") or {}).get("bot") is not True
            or message.get("webhook_id") is not None
            or message.get("type") != 0):
        raise BridgeAuthorityError("bridge_successor_structured_mention_or_eligibility_missing")


def send_successor_issue(*, issue_record_path: Path | str) -> tuple[Path, dict[str, Any]]:
    """Send/bind one registered successor. Any claimed/ambiguous send never retries.

    Separate send authorization is required. Do not use this to recover a BOUND
    request; the bridge's exact-message recovery never resends or edits it.
    """
    supplied = Path(issue_record_path)
    preliminary = json.loads(supplied.read_text(encoding="utf-8"))
    path, issue = load_issue_record(supplied, expected_subject_id=preliminary["subject_id"],
        expected_tuple_sha256=preliminary["tuple_sha256"], require_bound=False)
    if (issue.get("schema") != BRIDGE_ISSUE_SCHEMA_V2
            or issue.get("succession_reason") != "terminal_no_authority_decision"
            or not _SNOWFLAKE_RE.fullmatch(str(issue.get("predecessor_request_message_id", "")))
            or not _SNOWFLAKE_RE.fullmatch(str(issue.get("predecessor_reply_message_id", "")))
            or not _ISSUE_ID_RE.fullmatch(str(issue.get("predecessor_issue_id", "")))):
        raise BridgeAuthorityError("bridge_registered_successor_required")
    return _send_registered_issue(path, issue)


def _send_registered_issue(path, issue, *, before_post=None):
    """Shared canonical sender; public callers separately authenticate their scope."""
    payload = successor_request_payload(issue)
    validate_successor_payload(issue, payload)  # Before token I/O or claim or send.
    if len(payload["content"]) > 2000:
        raise BridgeAuthorityError("bridge_successor_content_too_long")
    # The create-exclusive claim survives exceptions, crashes and ambiguous POSTs.
    # Never remove it or automatically issue another successor.
    claim_path = path.with_suffix(".send-claim.json")
    try:
        with claim_path.open("x", encoding="utf-8") as handle:
            json.dump({"issue_id": issue["issue_id"], "record_sha256": issue["record_sha256"],
                       "status": "send_reserved", "nonce": payload["nonce"]}, handle)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as error:
        raise BridgeAuthorityError("bridge_successor_send_already_claimed") from error
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    if json.loads(path.read_text(encoding="utf-8")) != issue:
        raise BridgeAuthorityError("bridge_successor_issue_changed")
    validate_successor_payload(issue, payload)
    request = urllib.request.Request(
        f"{_DISCORD_API_BASE}/channels/{FIXED_BRIDGE_CHANNEL_ID}/messages",
        data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Authorization": f"Bot {_discord_bot_token()}", "Content-Type": "application/json",
                 "User-Agent": "BoringAI-BridgeAuthorityGuard/3.0"})
    # Token lookup can block: the caller's final interlock belongs after it.
    if before_post is not None:
        before_post()
    # No blind retry, including 429, timeout or a missing/invalid POST response.
    with urllib.request.urlopen(request, timeout=30) as response:
        posted = json.loads(response.read().decode("utf-8"))
    message_id = str(posted.get("id") or "")
    if not _SNOWFLAKE_RE.fullmatch(message_id):
        raise BridgeAuthorityError("bridge_successor_post_ambiguous")
    _successor_message_eligible(posted)
    return bind_request_issue(issue_record_path=path, request_message_id=message_id,
                              require_reviewer_mention=True)


def bind_request_issue(*, issue_record_path: Path | str, request_message_id: str,
                       require_reviewer_mention: bool = False) -> tuple[Path, dict[str, Any]]:
    """Bind an issued record once to the exact live, unedited Discord request."""
    if not _SNOWFLAKE_RE.fullmatch(request_message_id):
        raise BridgeAuthorityError("bridge_issue_request_message_id_invalid")
    supplied = Path(issue_record_path).expanduser().absolute()
    try:
        preliminary = json.loads(supplied.read_text(encoding="utf-8", errors="strict"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BridgeAuthorityError("bridge_issue_record_unreadable") from error
    if not isinstance(preliminary, dict):
        raise BridgeAuthorityError("bridge_issue_record_schema_invalid")
    path, issue = load_issue_record(
        supplied,
        expected_subject_id=str(preliminary.get("subject_id") or ""),
        expected_tuple_sha256=str(preliminary.get("tuple_sha256") or ""),
        require_bound=False,
    )
    message = _fetch_discord_message(FIXED_BRIDGE_CHANNEL_ID, request_message_id)
    if require_reviewer_mention or "predecessor_request_message_id" in issue:
        _successor_message_eligible(message)
    _reject_edited_message(message, "request")
    content = _message_identity(
        message,
        channel_id=FIXED_BRIDGE_CHANNEL_ID,
        message_id=request_message_id,
        author_id=KAEL_SHARED_BOT_AUTHOR_ID,
        role="request",
    )
    if content != issue["request_content"]:
        raise BridgeAuthorityError("bridge_request_content_mismatch")
    request_timestamp = str(message.get("timestamp") or "")
    request_time = _timestamp(request_timestamp, "request")
    if request_time < _timestamp(issue.get("issued_at_utc"), "issue"):
        raise BridgeAuthorityError("bridge_request_precedes_issue")
    bound = dict(issue)
    bound.update({
        "status": "BOUND",
        "request_message_id": request_message_id,
        "request_timestamp": request_timestamp,
        "request_edited_timestamp": None,
        "bound_at_utc": _utc_now(),
    })
    bound["record_sha256"] = _record_hash(bound)
    temporary = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
    temporary.write_text(json.dumps(bound, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return path, bound


def _discord_bot_token() -> str:
    token = str(os.environ.get("DISCORD_BOT_TOKEN") or "").strip()
    if token:
        return token
    if _DEFAULT_HERMES_ENV.is_file():
        for raw in _DEFAULT_HERMES_ENV.read_text(encoding="utf-8", errors="strict").splitlines():
            if raw.startswith("DISCORD_BOT_TOKEN="):
                token = raw.split("=", 1)[1].strip().strip('"').strip("'")
                if token:
                    return token
    raise BridgeAuthorityError("discord_bot_token_missing")


def _fetch_attachment_bytes(url: str) -> bytes:
    """Private, bounded unauthenticated CDN GET seam; never follow redirects."""
    if not isinstance(url, str) or not _ACCEPTANCE_ATTACHMENT_URL_RE.fullmatch(url):
        raise BridgeAuthorityError("bridge_acceptance_attachment_url_invalid")

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    request = urllib.request.Request(url, headers={"User-Agent": "BoringAI-BridgeAuthorityGuard/3.0"})
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=30) as response:
            if int(response.status) != 200:
                raise BridgeAuthorityError("bridge_acceptance_attachment_readback_failed")
            raw = response.read(_MAX_ACCEPTANCE_ATTACHMENT_BYTES + 1)
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise BridgeAuthorityError("bridge_acceptance_attachment_readback_failed") from error
    if len(raw) > _MAX_ACCEPTANCE_ATTACHMENT_BYTES:
        raise BridgeAuthorityError("bridge_acceptance_attachment_too_large")
    return raw


def _fetch_discord_message(channel_id: str, message_id: str) -> dict[str, Any]:
    """Private authenticated GET seam. Tests patch this function, never token I/O."""
    if not _SNOWFLAKE_RE.fullmatch(channel_id) or not _SNOWFLAKE_RE.fullmatch(message_id):
        raise BridgeAuthorityError("discord_message_identifier_invalid")
    request = urllib.request.Request(
        f"{_DISCORD_API_BASE}/channels/{channel_id}/messages/{message_id}",
        headers={
            "Authorization": f"Bot {_discord_bot_token()}",
            "User-Agent": "BoringAI-BridgeAuthorityGuard/3.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            if int(response.status) != 200:
                raise BridgeAuthorityError("discord_message_readback_failed")
            value = json.loads(response.read().decode("utf-8"))
    except BridgeAuthorityError:
        raise
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError,
            UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BridgeAuthorityError("discord_message_readback_failed") from error
    if not isinstance(value, dict):
        raise BridgeAuthorityError("discord_message_readback_invalid")
    return value


def _configured_fable_author_id() -> str:
    """Read and validate the checked-in non-secret Bridge authority identity.

    An environment value is only a consistency assertion. It can never override
    the checked-in channel/request/acceptance binding.
    """
    try:
        value = json.loads(FABLE_BRIDGE_AUTHORITY_CONFIG.read_text(encoding="utf-8", errors="strict"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BridgeAuthorityError("fable_bridge_authority_config_missing_or_invalid") from error
    if not isinstance(value, dict) or value.get("schema") != BRIDGE_AUTHORITY_SCHEMA:
        raise BridgeAuthorityError("fable_bridge_authority_config_schema_invalid")
    channel_id = str(value.get("channel_id") or "")
    request_author_id = str(value.get("request_author_id") or "")
    author_id = str(value.get("acceptance_author_id") or "")
    if channel_id != FIXED_BRIDGE_CHANNEL_ID:
        raise BridgeAuthorityError("fable_bridge_authority_channel_mismatch")
    if request_author_id != KAEL_SHARED_BOT_AUTHOR_ID:
        raise BridgeAuthorityError("fable_bridge_request_author_mismatch")
    if not _SNOWFLAKE_RE.fullmatch(author_id):
        raise BridgeAuthorityError("fable_bridge_author_id_invalid")
    if author_id == KAEL_SHARED_BOT_AUTHOR_ID:
        raise BridgeAuthorityError("fable_bridge_author_id_not_distinct")
    environment_assertion = str(os.environ.get(FABLE_BRIDGE_AUTHOR_ID_ENV) or "").strip()
    if environment_assertion and environment_assertion != author_id:
        raise BridgeAuthorityError("fable_bridge_author_id_env_mismatch")
    return author_id


def _timestamp(value: Any, role: str) -> dt.datetime:
    if not isinstance(value, str) or not value:
        raise BridgeAuthorityError(f"{role}_timestamp_missing")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise BridgeAuthorityError(f"{role}_timestamp_invalid") from error
    if parsed.tzinfo is None:
        raise BridgeAuthorityError(f"{role}_timestamp_timezone_missing")
    return parsed


def _message_identity(
    message: dict[str, Any], *, channel_id: str, message_id: str, author_id: str, role: str
) -> str:
    if str(message.get("channel_id") or "") != channel_id:
        raise BridgeAuthorityError(f"{role}_channel_mismatch")
    if str(message.get("id") or "") != message_id:
        raise BridgeAuthorityError(f"{role}_message_id_mismatch")
    if str((message.get("author") or {}).get("id") or "") != author_id:
        raise BridgeAuthorityError(f"{role}_author_mismatch")
    content = message.get("content")
    if not isinstance(content, str) or not content:
        raise BridgeAuthorityError(f"{role}_content_missing")
    return content


def _reject_edited_message(message: dict[str, Any], role: str) -> None:
    if message.get("edited_timestamp") is not None:
        raise BridgeAuthorityError(f"{role}_message_edited")


def validate_live_message_binding(
    *,
    channel_id: str,
    message_id: str,
    expected_author_id: str,
    expected_content_sha256: str,
) -> dict[str, Any]:
    """Validate one non-Bridge live message without exposing token handling."""
    if not _SHA256_RE.fullmatch(expected_content_sha256):
        raise BridgeAuthorityError("message_content_sha256_invalid")
    message = _fetch_discord_message(channel_id, message_id)
    _reject_edited_message(message, "message")
    content = _message_identity(
        message,
        channel_id=channel_id,
        message_id=message_id,
        author_id=expected_author_id,
        role="message",
    )
    if sha256_text(content) != expected_content_sha256:
        raise BridgeAuthorityError("message_live_content_drift")
    return {
        "channel_id": channel_id,
        "message_id": message_id,
        "author_id": expected_author_id,
        "content_sha256": expected_content_sha256,
        "content": content,
        "timestamp": str(message.get("timestamp") or ""),
    }


def validate_live_operator_command(
    *,
    channel_id: str,
    message_id: str,
    expected_author_id: str,
    authorization_id: str,
    approval_payload_sha256: str,
) -> dict[str, Any]:
    """Validate the exact production deploy operator command via a live GET."""
    if not _SHA256_RE.fullmatch(approval_payload_sha256):
        raise BridgeAuthorityError("operator_approval_payload_sha256_invalid")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{7,119}", authorization_id):
        raise BridgeAuthorityError("operator_authorization_id_invalid")
    message = _fetch_discord_message(channel_id, message_id)
    _reject_edited_message(message, "operator")
    content = _message_identity(
        message,
        channel_id=channel_id,
        message_id=message_id,
        author_id=expected_author_id,
        role="operator",
    )
    expected = re.compile(
        rf"(?:APPROVE|AUTHORIZE)\s+DEPLOY\s+{re.escape(authorization_id)}\s+{re.escape(approval_payload_sha256)}"
    )
    if not expected.fullmatch(content.strip()):
        raise BridgeAuthorityError("operator_command_not_explicit_approval")
    return {
        "channel_id": channel_id,
        "message_id": message_id,
        "author_id": expected_author_id,
        "content_sha256": sha256_text(content),
        "approval_payload_sha256": approval_payload_sha256,
    }


def validate_live_acceptance(
    *,
    request_message_id: str,
    acceptance_message_id: str,
    subject_id: str,
    tuple_sha256: str,
    issue_record_path: Path | str,
    expected_decision: str = "ACCEPT",
    legacy_v1_packet_id: str | None = None,
) -> dict[str, Any]:
    """Validate one issue-bound, unedited acceptance from the distinct Fable ID."""
    if request_message_id == acceptance_message_id:
        raise BridgeAuthorityError("acceptance_reuses_request_message")
    if not _SNOWFLAKE_RE.fullmatch(request_message_id) or not _SNOWFLAKE_RE.fullmatch(acceptance_message_id):
        raise BridgeAuthorityError("bridge_message_identifier_invalid")
    if not _SUBJECT_RE.fullmatch(subject_id):
        raise BridgeAuthorityError("bridge_subject_invalid")
    if not _SHA256_RE.fullmatch(tuple_sha256):
        raise BridgeAuthorityError("tuple_sha256_invalid")
    if expected_decision != "ACCEPT":
        raise BridgeAuthorityError("bridge_expected_decision_invalid")

    resolved_issue_path, issue = load_issue_record(
        issue_record_path,
        expected_subject_id=subject_id,
        expected_tuple_sha256=tuple_sha256,
    )
    if issue.get("schema") == BRIDGE_ISSUE_SCHEMA_V1:
        if legacy_v1_packet_id not in HISTORICAL_V1_ACCEPTED_PACKET_IDS:
            raise BridgeAuthorityError("external_authority_legacy_v1_packet_not_allowlisted")
        if subject_id != f"preview:{legacy_v1_packet_id}":
            raise BridgeAuthorityError("external_authority_legacy_v1_subject_mismatch")
    challenge = str(issue["challenge"])
    if issue.get("request_message_id") != request_message_id:
        raise BridgeAuthorityError("bridge_issue_request_replay")
    fable_author_id = _configured_fable_author_id()
    request_message = _fetch_discord_message(FIXED_BRIDGE_CHANNEL_ID, request_message_id)
    acceptance_message = _fetch_discord_message(FIXED_BRIDGE_CHANNEL_ID, acceptance_message_id)
    _reject_edited_message(request_message, "request")
    _reject_edited_message(acceptance_message, "acceptance")
    request_content = _message_identity(
        request_message,
        channel_id=FIXED_BRIDGE_CHANNEL_ID,
        message_id=request_message_id,
        author_id=KAEL_SHARED_BOT_AUTHOR_ID,
        role="request",
    )
    acceptance_content = _message_identity(
        acceptance_message,
        channel_id=FIXED_BRIDGE_CHANNEL_ID,
        message_id=acceptance_message_id,
        author_id=fable_author_id,
        role="acceptance",
    )
    transport = {"acceptance_transport": "message_content"}
    attachments = acceptance_message.get("attachments")
    if attachments:
        wrapper_content = acceptance_content
        wrapper = re.fullmatch(
            r"Verbatim independent reviewer response attached \(([1-9][0-9]{0,6}) UTF-8 bytes; "
            r"SHA-256: ([0-9a-f]{64})\)\. No transport-generated decision\.",
            wrapper_content,
        )
        if not wrapper:
            raise BridgeAuthorityError("bridge_acceptance_attachment_wrapper_invalid")
        if not isinstance(attachments, list) or len(attachments) != 1:
            raise BridgeAuthorityError("bridge_acceptance_attachment_ambiguous")
        attachment = attachments[0]
        expected_size = int(wrapper[1])
        if (not isinstance(attachment, dict)
                or attachment.get("filename") != "reviewer-response.txt"
                or attachment.get("content_type") != "text/plain; charset=utf-8"
                or type(attachment.get("size")) is not int
                or attachment["size"] != expected_size
                or expected_size > _MAX_ACCEPTANCE_ATTACHMENT_BYTES):
            raise BridgeAuthorityError("bridge_acceptance_attachment_metadata_invalid")
        url = attachment.get("url")
        if not isinstance(url, str) or not _ACCEPTANCE_ATTACHMENT_URL_RE.fullmatch(url):
            raise BridgeAuthorityError("bridge_acceptance_attachment_url_invalid")
        raw = _fetch_attachment_bytes(url)
        if len(raw) != expected_size or hashlib.sha256(raw).hexdigest() != wrapper[2]:
            raise BridgeAuthorityError("bridge_acceptance_attachment_bytes_mismatch")
        try:
            acceptance_content = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise BridgeAuthorityError("bridge_acceptance_attachment_utf8_invalid") from error
        transport = {
            "acceptance_transport": "verbatim_attachment",
            "acceptance_wrapper_sha256": sha256_text(wrapper_content),
            "acceptance_attachment_bytes": len(raw),
        }
    request_author_id = str((request_message.get("author") or {}).get("id") or "")
    acceptance_author_id = str((acceptance_message.get("author") or {}).get("id") or "")
    if acceptance_author_id in {request_author_id, KAEL_SHARED_BOT_AUTHOR_ID}:
        raise BridgeAuthorityError("bridge_acceptance_author_not_distinct")

    if request_content != issue["request_content"]:
        raise BridgeAuthorityError("bridge_request_content_mismatch")
    if sha256_text(request_content) != issue["request_content_sha256"]:
        raise BridgeAuthorityError("bridge_request_content_hash_mismatch")
    for binding in (
        subject_id,
        tuple_sha256,
        challenge,
        str(issue["issue_id"]),
    ):
        if binding not in acceptance_content:
            raise BridgeAuthorityError("bridge_acceptance_binding_missing")
    if EXACT_ACCEPTANCE_DECISION_LINE not in {line.strip() for line in acceptance_content.splitlines()}:
        raise BridgeAuthorityError("bridge_acceptance_decision_missing")
    if re.search(r"\b(?:REJECT(?:ED|ION)?|REVISE(?:D)?|DO\s+NOT\s+ACCEPT|NOT\s+ACCEPT(?:ED)?)\b", acceptance_content, re.I):
        raise BridgeAuthorityError("bridge_acceptance_decision_contradictory")
    if any(marker.casefold() not in acceptance_content.casefold() for marker in REQUIRED_AUTHORITY_MARKERS):
        raise BridgeAuthorityError("bridge_true_authority_marker_missing")
    lowered = acceptance_content.casefold()
    if any(marker in lowered for marker in FORBIDDEN_AUTHORITY_MARKERS):
        raise BridgeAuthorityError("bridge_forbidden_authority_marker")
    inspection_note: str | None = None
    if issue.get("schema") == BRIDGE_ISSUE_SCHEMA_V2:
        note_lines = [
            line[len(INSPECTION_NOTE_PREFIX):].strip()
            for line in acceptance_content.splitlines()
            if line.startswith(INSPECTION_NOTE_PREFIX)
        ]
        if len(note_lines) != 1:
            raise BridgeAuthorityError("bridge_inspection_note_missing_or_ambiguous")
        inspection_note = note_lines[0]
        if not MIN_INSPECTION_NOTE_CHARS <= len(inspection_note) <= MAX_INSPECTION_NOTE_CHARS:
            raise BridgeAuthorityError("bridge_inspection_note_length_invalid")

    reference = acceptance_message.get("message_reference")
    if not isinstance(reference, dict) or str(reference.get("message_id") or "") != request_message_id:
        raise BridgeAuthorityError("bridge_acceptance_wrong_reference")
    request_timestamp = str(request_message.get("timestamp") or "")
    acceptance_timestamp = str(acceptance_message.get("timestamp") or "")
    if request_timestamp != issue["request_timestamp"]:
        raise BridgeAuthorityError("bridge_request_timestamp_mismatch")
    request_time = _timestamp(request_timestamp, "request")
    acceptance_time = _timestamp(acceptance_timestamp, "acceptance")
    if request_time < _timestamp(issue.get("issued_at_utc"), "issue"):
        raise BridgeAuthorityError("bridge_request_precedes_issue")
    elapsed = (acceptance_time - request_time).total_seconds()
    if elapsed < MIN_EXTERNAL_REVIEW_SECONDS:
        raise BridgeAuthorityError("bridge_acceptance_too_fast")

    return {
        "authority_class": "bridge",
        "authority_origin": "guard_owned_authenticated_discord_readback",
        "channel_id": FIXED_BRIDGE_CHANNEL_ID,
        "request_author_id": request_author_id,
        "acceptance_author_id": acceptance_author_id,
        "request_message_id": request_message_id,
        "acceptance_message_id": acceptance_message_id,
        "request_content_sha256": sha256_text(request_content),
        "acceptance_content_sha256": sha256_text(acceptance_content),
        **transport,
        "inspection_note": inspection_note,
        "inspection_note_sha256": sha256_text(inspection_note) if inspection_note is not None else None,
        "issue_schema": issue.get("schema"),
        "request_timestamp": request_timestamp,
        "acceptance_timestamp": acceptance_timestamp,
        "request_edited_timestamp": None,
        "acceptance_edited_timestamp": None,
        "subject_id": subject_id,
        "tuple_sha256": tuple_sha256,
        "issue_id": issue["issue_id"],
        "issue_record_path": str(resolved_issue_path),
        "issue_record_sha256": hashlib.sha256(resolved_issue_path.read_bytes()).hexdigest(),
        "challenge_sha256": sha256_text(challenge),
        "elapsed_after_request_seconds": elapsed,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    issue = sub.add_parser("issue", help="issue a fresh guard-owned Bridge request")
    issue.add_argument("--subject-id", required=True)
    issue.add_argument("--tuple-sha256", required=True)
    issue.add_argument("--review-artifact", required=True, type=Path)
    issue.add_argument("--output", type=Path)
    sender = sub.add_parser("send-successor", help="send/bind one issued successor with explicit reviewer mention")
    sender.add_argument("--issue-record", required=True, type=Path)
    bind = sub.add_parser("bind-request", help="bind an issued record once to the live request")
    bind.add_argument("--issue-record", required=True, type=Path)
    bind.add_argument("--request-message-id", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "issue":
            path, record = issue_request(
                subject_id=args.subject_id,
                tuple_sha256=args.tuple_sha256.lower(),
                review_artifact_path=args.review_artifact,
                output=args.output,
            )
            output = {
                "status": "ISSUED",
                "issue_id": record["issue_id"],
                "issue_record_path": str(path),
                "issue_record_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "request_content": record["request_content"],
            }
        else:
            if args.command == "send-successor":
                path, record = send_successor_issue(issue_record_path=args.issue_record)
            else:
                path, record = bind_request_issue(
                    issue_record_path=args.issue_record,
                    request_message_id=args.request_message_id,
                )
            output = {
                "status": "BOUND",
                "issue_id": record["issue_id"],
                "issue_record_path": str(path),
                "issue_record_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "request_message_id": record["request_message_id"],
                "request_timestamp": record["request_timestamp"],
            }
        print(json.dumps(output, sort_keys=True))
        return 0
    except BridgeAuthorityError as error:
        print(json.dumps({"status": "BLOCKED", "code": error.code}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
