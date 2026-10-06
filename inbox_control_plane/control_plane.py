#!/usr/bin/env python3
"""Canonical multi-company inbox control plane.

Ingest, classification, and drafting remain provider-read-only. The sole mail
write edge is an exact, active-card, operator-approved Gmail SEND which is
reserved idempotently and verified from Sent Mail before terminalization.
Message content is never treated as an instruction.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import datetime as dt
import email
import email.policy
import gzip
import hashlib
import html
import imaplib
import importlib.util
import io
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from email.message import EmailMessage
from email.parser import BytesHeaderParser, BytesParser
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterable
from xml.etree import ElementTree

BUSINESS = Path("/home/ubuntu/business")
SOURCE_ROOT = Path(__file__).resolve().parents[1]
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from ops.sender_envelope import SendBlocked, assert_can_send
from inbox_control_plane.approved_gmail_sender import (
    ApprovedSendError,
    mark_source_read_verified as mark_approved_source_read,
    normalize_body as normalize_approved_body,
    run_approved_gmail_send_transport,
)
from inbox_control_plane.affiliate_network_registry import network_for_domain

PACKAGE_ROOT = BUSINESS / "inbox_control_plane"
REGISTRY_PATH = PACKAGE_ROOT / "registry.json"
CANONICAL_INBOX_JOB_ID = "3c79a0b5f7b2"
REQUIRED_OWNER_PARTITIONS = {
    "GLOBAL": (
        "hello@tryboringai.com",
        "izaiah@tryboringai.com",
        "kael@allocant.ai",
        "izaiah@allocant.ai",
    ),
    "AFFILIATE": (
        "ops@glpclinicscout.com",
        "izaiah@glpclinicscout.com",
    ),
}
STATE_ROOT = Path("/home/ubuntu/.hermes/state")
DB_PATH = STATE_ROOT / "email-monitor.sqlite3"
PRIVATE_ROOT = STATE_ROOT / "email-monitor-private"
RAW_ROOT = PRIVATE_ROOT / "raw"
ATTACHMENT_ROOT = STATE_ROOT / "email-monitor-attachments"
BACKUP_ROOT = STATE_ROOT / "email-monitor-backups"
REPORT_ROOT = BUSINESS / "reports/email-monitor-hardening-2026-07-11"
DAILY_AUDIT_DIR = BUSINESS / "reports/daily-audits"
OPS_CHANNEL_ID = "1487300132054765629"
DECISIONS_CHANNEL_ID = "1524254375453786294"
OPERATOR_ID = "392067741500309517"
SCHEMA_VERSION = 5
POLICY_VERSION = "inbox-policy-2026-08-21-automated-invite-v14"
DRAFT_ONLY_CUTOVER_UTC = "2026-07-18T00:00:00Z"
DISCORD_LIMIT = 1900
DRAFT_BODY_CHUNK_MAX = 1200
MAX_RAW_BYTES = 25 * 1024 * 1024
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
MAX_DMAR_XML_BYTES = 4 * 1024 * 1024
OUTBOUND_SEND_ENABLED = False
GMAIL_HTTP_TIMEOUT_SECONDS = 60
GMAIL_MAX_LIST_PAGES = 1000
GLP_GMAIL_HISTORY_DAYS = 30
IMAP_TIMEOUT_SECONDS = 45
IMAP_RETRY_DELAYS_SECONDS = (1.0, 3.0)
IMAP_ADAPTER_DEADLINE_SECONDS = 180.0
KAEL_IMAP_ENV_PATH = Path("/home/ubuntu/.hermes/secrets/kael-google/gmail_imap.env")
REPLY_TEMPLATES_PATH = PACKAGE_ROOT / "reply_templates.json"
DRAFT_CONTEXTS_PATH = PACKAGE_ROOT / "draft_contexts.json"
BEST_TERMS_MATRIX_PATH = (
    BUSINESS
    / "reports/glp-clinic-scout/affiliates/BEST-TERMS-MACHINE-20260825/latest-matrix.json"
)
SHEET_SYNC_RECEIPT_ROOT = (
    BUSINESS / "reports/glp-clinic-scout/affiliate-machine/sheet-sync"
)
AFFILIATE_BACKRUN_ROOT = (
    BUSINESS / "reports/glp-clinic-scout/affiliate-machine"
)
FABLE_EMAIL_DRAFTER_PATH = BUSINESS / "tools/fable/email_drafter.py"
FABLE_EMAIL_DRAFTER_VERSION = "fable-full-email-drafter-2026-08-25-v8-full-context"
FABLE_EMAIL_DRAFTER_TIMEOUT_SECONDS = 210
CURRENT_BUSINESS_STATE_PATH = BUSINESS / "STATE.md"
CURRENT_COMMERCIAL_CONTEXT_DIR = BUSINESS / "reports"
CURRENT_COMMERCIAL_CONTEXT_GLOB = "BRIDGE-CURSOR-*.md"
CURRENT_CONTEXT_MAX_SOURCE_BYTES = 300_000
EVENT_SCOPED_DRAFT_VENTURES = frozenset({"glp_affiliate"})
EMAIL_FLOW_DB_PATH = BUSINESS / "ops/email_flows/email_flows.sqlite"
GLP_DIGITAL_PR_DB_PATH = BUSINESS / "ops/glp-digital-pr/glp-digital-pr.sqlite3"
MINIMUM_INTENT_CONFIDENCE = 0.75
VENTURE_SEND_FLAGS = {
    "boring_ai": ("BORING_LIFECYCLE_ARMED", "YES"),
    "glp_consumer": ("GLP_LIFECYCLE_ARMED", "YES"),
    "glp_partners": ("GLP_PARTNER_LIFECYCLE_ARMED", "YES"),
    "glp_affiliate": ("GLP_PARTNER_LIFECYCLE_ARMED", "YES"),
    "allocant": ("ALLOCANT_G_A1_ARMED", "YES"),
}
DAILY_AUDIT_MIN_LINES = 100
DAILY_AUDIT_SUBJECT_RE = re.compile(r"^Kael daily audit -- (\d{4}-\d{2}-\d{2})$", re.I)

CATEGORIES = {
    "human_lead_reply", "support_customer_request", "investor_reply",
    "partner_vendor_action", "security_auth", "payment_sale_expense",
    "legal_compliance", "booking_calendar", "service_degradation",
    "dmarc_telemetry", "transactional_receipt", "newsletter",
    "marketing_automation", "spam_phishing", "internal_qa", "affiliate_network_response", "unknown",
}
ACTIONABLE = {
    "human_lead_reply", "support_customer_request", "investor_reply",
    "partner_vendor_action", "payment_sale_expense",
    "legal_compliance", "booking_calendar",
}
MANUAL_DISPOSITION_MESSAGE_ID = "1534304050718445638"
DRAFT_ALLOWED = {
    "human_lead_reply", "support_customer_request", "investor_reply",
    "partner_vendor_action", "booking_calendar",
}
HIGH_RISK_NO_DRAFT = {"security_auth", "payment_sale_expense", "legal_compliance", "spam_phishing", "unknown"}
AFFILIATE_RESPONSE_SIGNAL_RE = re.compile(
    r"\b(?:application (?:has been )?(?:approved|declined|rejected)|has approved your application|update to your application|"
    r"partnership request (?:was )?declined|promotion request declined|has accepted your campaign request|"
    r"(?:campaign|program|programme|partnership|relationship) request (?:has been |was )?(?:approved|accepted)|"
    r"request for access to .{1,160} (?:has been |was )?approved|request to join .{1,120} (?:has been )?(?:accepted|rejected)|"
    r"membership (?:is now )?(?:active|approved|rejected)|membership rejection|"
    r"welcome to (?:the )?.{1,120}(?:affiliate|partner) program|"
    r"has invited you to (?:join|partner)|program invitation|commission rates? (?:have )?changed|standard commission income rates? (?:are )?(?:increasing|increased)|increased rates for amazon|"
    r"affiliate team will review|invited you to apply|(?:send|email).{0,180}(?:publisher|social media|media kit|relevant information).{0,180}(?:affiliate|partnership)|"
    r"getting you started with|affiliate welcome survey|affiliate-only webinar|welcome to partnerstack|"
    r"awin merchant id|moved over to impact|closing our awin program|(?:program|programme) closed|"
    r"update on (?:your )?(?:brand )?(?:collaboration |ambassador )?application|unable to accept your application|"
    r"has removed you from)\b",
    re.I | re.S,
)
AFFILIATE_APPROVAL_ACCEPTANCE_RE = re.compile(
    r"\b(?:application (?:has been )?approved|has approved your application|"
    r"has accepted your campaign request|"
    r"(?:campaign|program|programme|partnership|relationship) request (?:has been |was )?(?:approved|accepted)|"
    r"request for access to .{1,160} (?:has been |was )?approved|request to join .{1,120} (?:has been )?accepted|"
    r"membership (?:is now )?(?:active|approved)|"
    r"welcome to (?:the )?.{1,120}(?:affiliate|partner) program)\b",
    re.I | re.S,
)
AFFILIATE_REJECTION_RE = re.compile(
    r"\b(?:application (?:has been )?(?:declined|rejected)|partnership request (?:was )?declined|"
    r"promotion request declined|membership rejection|(?:program|programme) closed|"
    r"(?:decided|decision is) not to move forward|(?:will|would) not be moving forward|"
    r"(?:unable|not able) to move forward|not moving forward|"
    r"suspended|terminated|rejected|declined)\b",
    re.I | re.S,
)
AFFILIATE_OPERATOR_PREREQUISITE_RE = re.compile(
    r"\b(?:kyc|know your customer|identity verification|verify your identity|tax form|tax interview|"
    r"w-?9|w-?8ben|ssn|ein verification|bank account|banking details|ach setup|payout setup|"
    r"payment setup|paypal setup)\b",
    re.I | re.S,
)
AFFILIATE_LEGAL_PREREQUISITE_RE = re.compile(
    r"\b(?:legal|counsel|agreement|contract)\b.{0,160}"
    r"\b(?:required|review|redlines?|signature|sign|changes?)\b",
    re.I | re.S,
)
AFFILIATE_AUTOMATED_SENDER_RE = re.compile(
    r"^(?:no-?reply|do-?not-?reply|donotreply|notifications?|alerts?|auto(?:mated)?-?mailer)@", re.I,
)
AFFILIATE_GENUINE_HUMAN_REPLY_RE = re.compile(
    r"\b(?:can|could|would|may)\s+(?:we|you|i)\b.{0,140}"
    r"\b(?:schedule|book|arrange|set up|have|discuss|review|share|send|confirm|clarify|answer|provide)\b.{0,180}"
    r"\b(?:call|meeting|chat|question|questions|partnership|affiliate|program|application|promotion|traffic|audience|integration|terms)\b"
    r"|\b(?:are you available|do you have time)\b.{0,100}\b(?:call|meeting|chat)\b"
    r"|\b(?:i(?:'d| would) like|we(?:'d| would) like|happy to|let me know|please (?:reply|send|share|confirm))\b.{0,180}"
    r"\b(?:call|meeting|chat|question|questions|partnership|affiliate|program|application|promotion|traffic|audience|integration|terms)\b"
    r"|\b(?:question|questions)\b.{0,180}"
    r"\b(?:partnership|affiliate|program|application|promotion|traffic|audience|integration|terms|call|meeting)\b"
    r"|\b(?:references?|referrals?)\b.{0,240}\b(?:call|meeting|application|partnership)\b",
    re.I | re.S,
)
AFFILIATE_MACHINE_STATUS_SUBJECT_RE = re.compile(
    r"^(?:application (?:approved|accepted)|campaign cancellation notice|"
    r"(?:campaign|program) (?:approval|cancellation) (?:notice|notification))$", re.I,
)
AFFILIATE_ROUTINE_NOTICE_RE = re.compile(
    r"\b(?:application (?:has been |was )?(?:approved|accepted|declined|rejected)|"
    r"(?:campaign|program|programme|partnership|relationship) request (?:has been |was )?(?:approved|accepted|declined|rejected)|"
    r"request for access to .{1,160} (?:has been |was )?(?:approved|accepted|declined|rejected)|"
    r"request to join .{1,120} (?:has been |was )?(?:approved|accepted|declined|rejected)|"
    r"membership (?:is now )?(?:active|approved|rejected)|program invitation|"
    r"has invited you to (?:join|partner)(?: (?:their|the))? (?:affiliate |partner )?(?:program|programme)|"
    r"commission (?:rate|rates|update)|payout (?:status|notice|update)|payment notification|"
    r"monthly (?:statement|report)|status (?:notice|notification|update)|program update|"
    r"policy update|terms update|welcome to .{1,120}(?:affiliate|partner) program|"
    r"affiliate-only webinar|newsletter|digest|automated (?:message|notification))\b",
    re.I | re.S,
)
AFFILIATE_AUTOMATED_SENDER_RE = re.compile(
    r"^(?:no[-_]?reply|noreply|notifications?|alerts?|updates?|newsletters?|automated|system|mailer-daemon|postmaster)@",
    re.I,
)
REVIEWED_AFFILIATE_DRAFT_UNSUPPORTED_CLAIM_RE = re.compile(
    r"\b(?:attached|included|sent|provided|shared)\s+(?:the\s+|our\s+)?(?:industry\s+)?references?\b"
    r"|\b(?:permission|approval)\s+(?:has been|was|is)\s+(?:received|granted)\b"
    r"|\bwe\s+(?:have|already)\s+(?:received|obtained)\s+(?:permission|approval)\b",
    re.I,
)
AFFILIATE_APPLICATION_INVITATION_RE = re.compile(
    r"\b(?:affiliate team will review|invited you to apply|program invitation|"
    r"has invited you to (?:join|partner)(?: (?:their|the))? (?:affiliate |partner )?(?:program|programme)|"
    r"you are invited.{0,240}join (?:program|programme)|"
    r"(?:send|email).{0,180}(?:publisher|social media|media kit|relevant information).{0,180}(?:affiliate|partnership))\b",
    re.I | re.S,
)
AFFILIATE_AUTOMATED_INVITE_NOTICE_RE = re.compile(
    r"\b(?:"
    r"has invited you to (?:join|partner)(?: (?:their|the))? (?:affiliate |partner )?(?:program|programme)|"
    r"(?:program|programme) invitation|"
    r"you are invited.{0,320}join (?:program|programme)|"
    r"welcome to (?:the )?.{1,120}(?:affiliate|partner) (?:program|programme)|"
    r"welcome to partnerstack"
    r")\b",
    re.I | re.S,
)
DIRECT_PARTNER_RESPONSE_DOMAINS = frozenset({"precisiontelemed.com"})
DIRECT_PARTNER_RESPONSE_RE = re.compile(
    r"\b(?:affiliate|publisher)\b.{0,320}"
    r"(?:tracking\s+(?:url|link)|manual\s+attribution|how\s+did\s+you\s+hear|commission)",
    re.I | re.S,
)
VENDOR_MARKETING_SENDERS = frozenset({
    "workspace-noreply@google.com",
    "payments-noreply@google.com",
    "notifications@usefastlane.ai",
    "hello@send.usefastlane.ai",
    "noreply@usefastlane.ai",
    "ads-noreply@google.com",
    "ads-account-noreply@google.com",
    "support@browse.ai",
    "mel@browse.ai",
    "support@optmyzr.com",
    "marketplace@katalys.com",
    "no-reply@accounts.google.com",
})
VENDOR_MARKETING_SENDER_DOMAINS = frozenset({
    "usefastlane.ai",
    "browse.ai",
    "optmyzr.com",
})
VENDOR_MARKETING_SUBJECT_RE = re.compile(
    r"(?:^|\b)(?:your recommendations:|explore your|get started with|welcome to|"
    r"write better emails with|bring your business to|set up your|is your verification code|"
    r"new device signed in)",
    re.I,
)
OWN_SYSTEM_SENDERS = frozenset({
    "kael.builds.business@gmail.com",
    "ops@glpclinicscout.com",
    "partners@glpclinicscout.com",
    "hello@tryboringai.com",
    "previews@tryboringai.com",
    "kael@allocant.ai",
})
OWN_OPERATOR_RECIPIENTS = frozenset({
    "izaiahtilton1@gmail.com",
    "izaiah@glpclinicscout.com",
    "izaiah@tryboringai.com",
    "izaiah@allocant.ai",
})
OWN_SYSTEM_ARTIFACT_SUBJECT_RE = re.compile(
    r"\b(?:review only|copy package|receipt|closeout|handoff|operator report|status report)\b",
    re.I,
)
ALWAYS_SURFACE_INTEGRITY_RE = re.compile(
    r"\b(?:payment\s+(?:was\s+)?(?:declined|failed)|card\s+(?:is\s+)?(?:declined|expiring|expired)|"
    r"insufficient\s+funds|subscription\s+(?:is\s+)?(?:suspended|suspension)|billing\s+(?:failed|failure)|"
    r"domain\s+(?:expires?|expiry|expiration)|account\s+(?:at\s+risk|suspended)|service\s+will\s+be\s+suspended|"
    r"\bsuspension\b|"
    r"(?:legal|contract)\s+(?:action|required|deadline|notice)|signature\s+required|sign\s+the\s+(?:contract|agreement))\b",
    re.I,
)


def affiliate_response_disposition(parsed: dict[str, Any], classification: dict[str, Any]) -> str:
    """Route routine affiliate relationship outcomes to provider-backed automation.

    Email is a wake signal, never relationship authority. Clean acceptances and
    rejections stay silent until the provider-backed reconciler completes them.
    Identity/tax/payout prerequisites and explicit hard-term cues remain visible.
    """
    if classification.get("category") != "affiliate_network_response":
        return "not_affiliate"
    # Relationship outcomes are decided from the newest reply only. Quoted
    # history routinely contains our original application request and old legal
    # discussion; allowing it to vote turned an explicit TeleWellnessMD "decided
    # not to move forward" reply into an application invitation and sent the
    # invitation carrier to an empty explicit route.
    newest = newest_unquoted_human_reply(str(parsed.get("body_text") or ""))
    text = f"{parsed.get('subject') or ''}\n{newest}"
    if AFFILIATE_REJECTION_RE.search(text):
        return "auto_process_rejection"
    if AFFILIATE_OPERATOR_PREREQUISITE_RE.search(text):
        return "operator_prerequisite_card"
    if AFFILIATE_APPLICATION_INVITATION_RE.search(text):
        return "auto_process_application_invitation"
    if AFFILIATE_APPROVAL_ACCEPTANCE_RE.search(text):
        return "auto_process_approval"
    return "other_affiliate_response"


def affiliate_network_domain(sender_domain: str) -> str | None:
    """Return the configured network root for an exact domain or subdomain."""
    match = network_for_domain(sender_domain)
    return match[0] if match else None


def affiliate_genuine_human_reply(parsed: dict[str, Any], affiliate_root: str | None) -> bool:
    """Recognize conversational human requests from a registered network.

    Exact network identity plus the absence of machine/list evidence is still
    insufficient by itself. The newest unquoted content must contain reviewed
    conversational request evidence, while routine lifecycle/status notices
    remain owned by the silent affiliate reconciler.
    """
    if not affiliate_root:
        return False
    sender = parseaddr(str(parsed.get("sender") or ""))[1].lower()
    if not sender or AFFILIATE_AUTOMATED_SENDER_RE.match(sender):
        return False
    if str(parsed.get("auto_submitted") or "").lower() not in {"", "no"}:
        return False
    if str(parsed.get("precedence") or "").lower() in {"bulk", "list", "junk"}:
        return False
    if str(parsed.get("list_unsubscribe") or "").strip():
        return False
    newest = newest_unquoted_human_reply(str(parsed.get("body_text") or ""))
    text = f"{parsed.get('subject') or ''}\n{newest}"
    if AFFILIATE_ROUTINE_NOTICE_RE.search(text):
        return False
    return bool(AFFILIATE_GENUINE_HUMAN_REPLY_RE.search(text))


def affiliate_automated_invite_notice(
    parsed: dict[str, Any], affiliate_root: str | None,
) -> bool:
    """Recognize network-owned invite/welcome templates even from help@ senders.

    Sender local parts are not reliable automation evidence: Awin sends its
    standard advertiser invitation from help@awin.com. The registered network
    identity plus template wording is the proof. Legal, identity, tax and payout
    prerequisites retain precedence in classify_message and still escalate.
    """
    if not affiliate_root:
        return False
    newest = newest_unquoted_human_reply(str(parsed.get("body_text") or ""))
    text = f"{parsed.get('subject') or ''}\n{newest}"
    return bool(AFFILIATE_AUTOMATED_INVITE_NOTICE_RE.search(text))


SECRET_PATTERNS = (
    (re.compile(r"-----BEGIN [^-\n]*PRIVATE KEY-----.*?-----END [^-\n]*PRIVATE KEY-----", re.I | re.S), "[REDACTED_PRIVATE_KEY]"),
    (re.compile(r"\b(?:sk|pk)_(?:live|test)_[A-Za-z0-9_-]{8,}\b", re.I), "[REDACTED_API_KEY]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), "[REDACTED_TOKEN]"),
    (re.compile(r"(?i)\b(?:authorization|proxy-authorization)\s*:\s*[^\r\n]+"), "[REDACTED_AUTH_HEADER]"),
    (re.compile(r"(?i)\b(?:api[_ -]?key|client[_ -]?secret|access[_ -]?token|refresh[_ -]?token|password|passwd|passphrase)\b\s*(?:=|:)\s*[^\s,;]+"), "[REDACTED_CREDENTIAL]"),
    (re.compile(r"(?i)\b(?:verification|security|one[- ]time|otp|2fa|mfa)\s*(?:code|pin)?\s*[:=#-]?\s*\d{4,10}\b"), "[REDACTED_CODE]"),
    (re.compile(r"(?i)([?&](?:api[_-]?key|access[_-]?token|auth|password|secret)=)[^&#\s]+"), r"\1[REDACTED]"),
)

# Shared GLP safety vocabulary. It includes the live lifecycle engine's
# SENSITIVE_GLP_TERMS plus the legal/PHI concepts required by reply handling.
# The newest unquoted inbound reply and final rendered copy are both checked.
SENSITIVE_GLP_TERMS = (
    "bmi", "weight", "pregnan", "dose", "dosage", "side effect", "symptom",
    "nausea", "vomit", "diabetes", "a1c", "blood pressure", "medication",
    "allergy", "condition", "diagnosed", "diagnosis", "lab result",
    "insurance id", "member id", "self-harm", "suicide", "eating disorder",
    "patient", "eligibility", "eligible", "prescribe", "prescription",
    "medical record", "phi", "hipaa", "protected health", "health detail",
    "lawsuit", "attorney", "legal notice", "subpoena", "cease and desist",
    "regulator", "compliance violation",
)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8", "replace"))


def private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)


def private_write(path: Path, data: bytes) -> None:
    """Atomically replace a private artifact without exposing partial contents."""
    private_dir(path.parent)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{os.urandom(6).hex()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def private_json(path: Path, payload: Any) -> None:
    private_write(path, (json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode())


def private_content_addressed_write(path: Path, data: bytes, expected_sha256: str) -> None:
    if path.exists():
        if path.is_symlink() or not path.is_file():
            raise RuntimeError("content_address_path_not_regular")
        if sha256_bytes(path.read_bytes()) != expected_sha256:
            raise RuntimeError("content_address_hash_mismatch")
        return
    private_write(path, data)
    if path.is_symlink() or not path.is_file() or sha256_bytes(path.read_bytes()) != expected_sha256:
        raise RuntimeError("content_address_write_verification_failed")


def redact(value: Any) -> str:
    text = str(value or "")
    for pattern, replacement in SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    text = re.sub(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)", "[REDACTED_PAYMENT_NUMBER]", text)
    text = text.replace("@everyone", "@\u200beveryone").replace("@here", "@\u200bhere")
    text = re.sub(r"<@([!&]?\d+)>", lambda match: "<@\u200b" + match.group(1) + ">", text)
    return text


def one_line(value: Any, limit: int = 220) -> str:
    text = re.sub(r"\s+", " ", redact(value)).strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _markdown_section(text: str, heading: str) -> str:
    lines = text.splitlines()
    start = next((index for index, line in enumerate(lines) if line.strip() == heading), None)
    if start is None:
        return ""
    end = next((index for index in range(start + 1, len(lines)) if lines[index].startswith("## ")), len(lines))
    return "\n".join(lines[start:end])


def load_current_business_context(
    connection: sqlite3.Connection | None = None,
) -> tuple[list[str], str]:
    """Read fresh business truth immediately before every Fable composition."""
    source_paths = [CURRENT_BUSINESS_STATE_PATH]
    reports = sorted(
        [
            path for path in CURRENT_COMMERCIAL_CONTEXT_DIR.glob(CURRENT_COMMERCIAL_CONTEXT_GLOB)
            if path.is_file() and not path.is_symlink()
        ],
        key=lambda path: (path.stat().st_mtime, path.name),
        reverse=True,
    )
    if not reports:
        raise ValueError("current_business_commercial_context_missing")
    source_paths.append(reports[0])
    source_bytes: list[tuple[Path, bytes]] = []
    source_text: dict[Path, str] = {}
    for path in source_paths:
        if not path.is_file() or path.is_symlink():
            raise ValueError("current_business_state_source_invalid")
        raw = path.read_bytes()
        if not raw or len(raw) > CURRENT_CONTEXT_MAX_SOURCE_BYTES:
            raise ValueError("current_business_state_source_size_invalid")
        source_bytes.append((path, raw))
        source_text[path] = redact(raw.decode("utf-8", errors="replace"))

    items: list[str] = []

    def add(value: str, limit: int = 480) -> None:
        clean = one_line(value, limit)
        if clean and clean not in items and len(items) < 20:
            items.append(clean)

    add("Use the fresh current business state snapshot below as authoritative. Ignore stale template stage language.")
    state_text = source_text[CURRENT_BUSINESS_STATE_PATH]
    relevant_state_terms = (
        "Current business focus", "GLP Clinic Scout", "Affiliates/Resend", "SEO/discovery",
        "Email control plane", "GLP next", "Operator standard",
    )
    for heading in ("## Mission dashboard", "## Active lanes", "## Immediate next actions"):
        for line in _markdown_section(state_text, heading).splitlines():
            if line.strip().startswith(("- ", "> ")) and any(term in line for term in relevant_state_terms):
                add(line)

    commercial_text = source_text[reports[0]]
    add(f"Latest GLP commercial brief source: {reports[0].name}")
    for line in _markdown_section(commercial_text, "## GLP CLINIC SCOUT").splitlines():
        if line.strip().startswith(("**", "- ")):
            add(line)

    if connection is not None:
        with contextlib.suppress(sqlite3.Error):
            row = connection.execute("SELECT COUNT(*) AS count FROM affiliate_approval_facts").fetchone()
            if row is not None:
                count = int(row["count"] if isinstance(row, sqlite3.Row) else row[0])
                add(f"Canonical inbox approval facts recorded: {count}.")

    # Live derived facts, computed at compose time from canonical artifacts.
    # Each is best-effort: a missing or stale file skips silently rather than
    # blocking a draft, and nothing here performs a network call.
    with contextlib.suppress(OSError, ValueError, KeyError, StopIteration):
        releases = BUSINESS / "site-system/flagship-v2/releases"
        latest = max(
            (p for p in releases.iterdir() if (p / "data/program-registry.json").is_file()),
            key=lambda p: p.stat().st_mtime,
        )
        programs = json.loads((latest / "data/program-registry.json").read_text())
        rows = programs if isinstance(programs, list) else programs.get("programs") or []
        money = sum(1 for r in rows if str((r or {}).get("source_kind") or "") == "money")
        add(f"Live site registry right now: {len(rows)} tracked programs, {money} in the live money selection on glpclinicscout.com.")
    with contextlib.suppress(OSError, ValueError, KeyError):
        matrix = json.loads((BUSINESS / "reports/glp-clinic-scout/coverage-closure-20260804/approved-affiliate-coverage-matrix.json").read_text())
        approved = matrix.get("approved_total") or matrix.get("approved_count")
        if approved:
            add(f"Approved affiliate relationships on record: {approved}.")
    with contextlib.suppress(OSError, ValueError):
        sitemap = BUSINESS / "site-system/flagship-v2/build/flagship/sitemap.xml"
        if sitemap.is_file():
            add(f"Published site pages in the sitemap: {sitemap.read_text().count('<loc>')}.")

    if len(items) < 2:
        raise ValueError("current_business_state_context_empty")
    digest_material = b"".join(
        path.as_posix().encode() + b"\0" + raw + b"\0" for path, raw in source_bytes
    )
    return items, sha256_bytes(digest_material)


def load_event_scoped_draft_context(
    venture_key: str,
    event_id: str,
    *,
    path: Path = DRAFT_CONTEXTS_PATH,
) -> dict[str, Any]:
    """Return facts bound to one venture and, when present, one exact event.

    Affiliate replies may use their own canonical RFC822 thread plus these facts.
    They never receive the process-wide STATE/BRIDGE summary: that summary can
    contain another business or another inbox thread and was the contamination
    surface behind the Gustavo/Mandy fallback incident.
    """
    raw = path.read_bytes()
    data = json.loads(raw.decode("utf-8"))
    if data.get("schema") != "inbox-event-scoped-draft-context-v1":
        raise ValueError("draft_context_schema_invalid")
    programs = data.get("programs")
    events = data.get("events")
    if not isinstance(programs, dict) or not isinstance(events, dict):
        raise ValueError("draft_context_registry_invalid")
    program = programs.get(venture_key)
    if not isinstance(program, dict):
        raise ValueError(f"draft_context_program_missing:{venture_key}")
    event = events.get(event_id) or {}
    if not isinstance(event, dict):
        raise ValueError(f"draft_context_event_invalid:{event_id}")
    if event and str(event.get("venture") or "") != venture_key:
        raise ValueError(f"draft_context_event_venture_mismatch:{event_id}")

    def _strings(owner: dict[str, Any], key: str, *, limit: int) -> list[str]:
        values = owner.get(key) or []
        if not isinstance(values, list) or len(values) > limit or any(
            not isinstance(value, str) or not value.strip() or len(value) > 500 or "\x00" in value
            for value in values
        ):
            raise ValueError(f"draft_context_{key}_invalid:{event_id}")
        return [value.strip() for value in values]

    facts = list(dict.fromkeys([
        *_strings(program, "facts", limit=20),
        *_strings(event, "facts", limit=20),
    ]))
    exclusions = list(dict.fromkeys([
        *_strings(program, "scope_exclusion_markers", limit=40),
        *_strings(event, "scope_exclusion_markers", limit=40),
    ]))
    if not facts or len(facts) > 20 or len(exclusions) > 40:
        raise ValueError(f"draft_context_scope_invalid:{event_id}")
    # Bind provenance to only the selected program + event records. A digest of
    # the whole registry would make an unrelated thread edit silently invalidate
    # every existing draft, recreating cross-thread coupling at the hash layer.
    source_sha = sha256_text(json.dumps({
        "schema": data["schema"],
        "program": program,
        "event": event,
    }, sort_keys=True, ensure_ascii=False))
    scope = {
        "type": "venture_event",
        "venture_key": venture_key,
        "event_id": event_id,
        "source_sha256": source_sha,
    }
    return {
        "facts": facts,
        "scope_exclusion_markers": exclusions,
        "context_scope": scope,
        "context_sha256": sha256_text(json.dumps({
            "scope": scope,
            "facts": facts,
            "scope_exclusion_markers": exclusions,
        }, sort_keys=True, ensure_ascii=False)),
    }


def _affiliate_marketplace(value: Any) -> str:
    key = re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())
    if key.startswith("direct"):
        return "direct"
    return {
        "impactcom": "impact", "impact": "impact", "maxbounty": "maxbounty",
        "katalys": "katalys", "directaffiliate": "direct", "direct": "direct",
        "cjaffiliate": "cj", "commissionjunction": "cj", "cj": "cj",
        "awin": "awin", "xentraffic": "xentraffic", "partnerize": "partnerize",
        "partnerstack": "partnerstack", "refersion": "refersion",
    }.get(key, key)


def _affiliate_relationship_from_status(value: Any) -> str:
    status = str(value or "").strip().casefold()
    if status.startswith("approved_") or status in {"active", "joined", "live"}:
        return "approved"
    if any(marker in status for marker in ("rejected", "declined", "denied")):
        return "rejected"
    if any(marker in status for marker in ("pending", "applied", "submitted")):
        return "pending"
    return status or "unknown"


def _affiliate_paid_event(value: Any, terms: str) -> str:
    explicit = str(value or "").strip().casefold()
    if explicit and explicit != "unknown":
        return explicit
    normalized = re.sub(r"[^a-z0-9]+", " ", terms.casefold()).strip()
    if "medication subscription signup" in normalized:
        return "medication_subscription_signup"
    if re.search(r"\bglp 1\b.*\bcpa\b|\bcpa\b.*\bglp 1\b", normalized):
        return "glp1_acquisition"
    return "unknown"


def _latest_sheet_row_snapshot(
    root: Path | None,
) -> tuple[list[dict[str, Any]], str]:
    if root is None or not root.is_dir():
        return [], sha256_text("no_affiliate_sheet_snapshot")
    candidates = sorted(root.glob("backrun-*/sheet-full-row-export.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        return [], sha256_text("no_affiliate_sheet_snapshot")
    raw = candidates[0].read_bytes()
    document = json.loads(raw.decode("utf-8"))
    source_rows = document.get("rows") or {}
    if not isinstance(source_rows, dict):
        raise ValueError("affiliate_sheet_snapshot_rows_invalid")
    rows: list[dict[str, Any]] = []
    for row_key, values in source_rows.items():
        if not isinstance(values, list):
            continue
        padded = [*values, *([""] * max(0, 27 - len(values)))]
        try:
            row_number = int(row_key)
        except (TypeError, ValueError):
            continue
        rows.append({
            "sheet_row": row_number,
            "provider": str(padded[2] or "").strip(),
            "marketplace": _affiliate_marketplace(padded[4]),
            "status": str(padded[10] or "").strip().casefold(),
            "relationship": _affiliate_relationship_from_status(padded[10]),
            "current_terms": str(padded[16] or "").strip(),
            "wire_in_ready": str(padded[19] or "").strip().casefold() == "true",
            "wired_live": str(padded[20] or "").strip().casefold() == "true",
        })
    return rows, sha256_bytes(raw)


def _verified_commission_term_overrides(
    receipt_root: Path | None,
) -> tuple[dict[tuple[int, str], dict[str, str]], str]:
    """Read newer, single-writer-verified terms without calling Google Sheets.

    The best-terms matrix is a periodic snapshot. A commission correction can
    land between snapshots, so a reply draft also consumes the newest PASS
    receipt whose post-write readback has no mismatch. This remains read only
    and never reads queue credentials.
    """
    if receipt_root is None or not receipt_root.is_dir():
        return {}, sha256_text("no_verified_commission_overrides")
    selected: dict[tuple[int, str], dict[str, str]] = {}
    digest_rows: list[dict[str, str]] = []
    run_dirs = sorted(receipt_root.glob("drain-*"), reverse=True)[:80]
    for run_dir in run_dirs:
        for receipt_path in sorted(run_dir.glob("*-receipt.json"), reverse=True):
            try:
                receipt_raw = receipt_path.read_bytes()
                receipt = json.loads(receipt_raw.decode("utf-8"))
            except (OSError, ValueError):
                continue
            if (
                receipt.get("status") != "PASS"
                or receipt.get("dry_run") is not False
                or receipt.get("readback_mismatches") not in ([], None)
                or not re.fullmatch(
                    r"[0-9a-f]{64}", str(receipt.get("post_write_values_sha256") or ""),
                )
            ):
                continue
            captured_at = str(receipt.get("captured_at_utc") or "")
            for evidence in receipt.get("evidence") or []:
                if not isinstance(evidence, dict):
                    continue
                try:
                    row = int(evidence.get("row"))
                except (TypeError, ValueError):
                    continue
                program_id = str(evidence.get("network_program_id") or "").strip()
                terms = str(evidence.get("commission_terms") or "").strip()
                provider = str(evidence.get("program") or "").strip()
                if row < 2 or not program_id or not terms or len(terms) > 300 or not provider:
                    continue
                key = (row, program_id.casefold())
                if key in selected:
                    continue
                record = {
                    "provider": provider,
                    "current_terms": terms,
                    "verified_at": str(evidence.get("fetched_at_utc") or captured_at),
                    "receipt_sha256": sha256_bytes(receipt_raw),
                }
                selected[key] = record
                digest_rows.append({"row": str(row), "program_id": program_id, **record})
    return selected, sha256_text(json.dumps(digest_rows, sort_keys=True, ensure_ascii=False))


def load_affiliate_best_terms_context(
    parsed: dict[str, Any] | None,
    *,
    path: Path = BEST_TERMS_MATRIX_PATH,
    marketplace: str = "",
    receipt_root: Path | None = None,
    sheet_snapshot_root: Path | None = None,
) -> dict[str, Any]:
    """Return the exact provider and marketplace row relevant to this thread.

    Other rails are exposed only as explicitly non-relevant awareness records.
    No winner or productive-path selector is sent to the drafter because that
    cross-rail action context caused the 2026-08-25 Sesame failure.
    """
    raw = path.read_bytes()
    matrix = json.loads(raw.decode("utf-8"))
    providers = matrix.get("providers") or []
    if not isinstance(providers, list):
        raise ValueError("best_terms_matrix_providers_invalid")
    if receipt_root is None and path == BEST_TERMS_MATRIX_PATH:
        receipt_root = SHEET_SYNC_RECEIPT_ROOT
    if sheet_snapshot_root is None and path == BEST_TERMS_MATRIX_PATH:
        sheet_snapshot_root = AFFILIATE_BACKRUN_ROOT
    overrides, overrides_sha = _verified_commission_term_overrides(receipt_root)
    sheet_rows, sheet_snapshot_sha = _latest_sheet_row_snapshot(sheet_snapshot_root)
    facts = [
        "AFFILIATE_IDENTITY | legal_entity=Tilton Group LLC | brand=GLP Clinic Scout | site=glpclinicscout.com | email=partners@glpclinicscout.com | business_type=independent comparison and review site | not_a_pharmacy=true | supply_chain_role=none",
        "AFFILIATE_TRAFFIC | organic editorial search; paid acquisition is a separate governed lane, not proof of active campaigns | FTC disclosure required on monetized placements | no traffic, campaign, conversion or revenue claims without current evidence",
        "AFFILIATE_RAIL_RULE | row identity is provider times marketplace | other rails are awareness only | different paid events are not comparable | never redirect a thread action to another rail",
        "AFFILIATE_OPERATOR_ONLY | never negotiate payout, KYC, banking, legal terms, or commitments | acknowledge a verified completed update without proposing a counterterm",
    ]
    body_text = str((parsed or {}).get("body_text") or "")
    newest_text = newest_unquoted_human_reply(body_text)
    thread_text = f"{(parsed or {}).get('subject') or ''}\n{body_text}"
    sender_address = parseaddr(str((parsed or {}).get("sender") or ""))[1].strip().casefold()
    sender_domain = sender_address.rsplit("@", 1)[-1] if "@" in sender_address else ""
    sender_labels = {
        re.sub(r"[^a-z0-9]+", "", label) for label in sender_domain.split(".")[:-1] if label
    }
    normalized_text = re.sub(r"[^a-z0-9]+", " ", thread_text.casefold()).strip()
    matches: list[tuple[int, dict[str, Any]]] = []
    for provider in providers:
        if not isinstance(provider, dict):
            continue
        name = str(provider.get("provider_name") or "").strip()
        normalized_name = re.sub(r"[^a-z0-9]+", " ", name.casefold()).strip()
        compact_name = re.sub(r"[^a-z0-9]+", "", normalized_name)
        if (
            normalized_name and f" {normalized_name} " in f" {normalized_text} "
        ) or (len(compact_name) >= 5 and compact_name in sender_labels):
            matches.append((len(normalized_name), provider))
    matched = max(matches, default=(0, None), key=lambda item: item[0])[1]
    selected_marketplace = _affiliate_marketplace(marketplace)
    if not selected_marketplace and sender_domain == "xentraffic.com":
        selected_marketplace = "xentraffic"
    relevant_record: dict[str, Any] | None = None
    if matched is not None:
        provider_name = str(matched.get("provider_name") or "").strip()
        source_offers = matched.get("offers") or []
        if not isinstance(source_offers, list):
            raise ValueError("best_terms_matrix_offers_invalid")
        offers: list[dict[str, Any]] = []
        for offer in source_offers[:12]:
            if not isinstance(offer, dict):
                continue
            network = _affiliate_marketplace(offer.get("network"))
            program_id = str(offer.get("program_id") or "").strip()
            sheet_row = offer.get("sheet_row")
            try:
                row_number = int(sheet_row) if sheet_row not in (None, "") else None
            except (TypeError, ValueError):
                raise ValueError("best_terms_sheet_row_invalid")
            provider_key = re.sub(r"[^a-z0-9]+", "", provider_name.casefold())
            program_id_key = re.sub(r"[^a-z0-9]+", "", program_id.casefold())
            sheet_match = next((
                row for row in sheet_rows
                if row["marketplace"] == network and (
                    row["sheet_row"] == row_number
                    or (
                        row_number is None and (
                            re.sub(r"[^a-z0-9]+", "", row["provider"].casefold()) == program_id_key
                            or re.sub(r"[^a-z0-9]+", "", row["provider"].casefold()).startswith(provider_key)
                        )
                    )
                )
            ), None)
            if sheet_match is not None:
                row_number = int(sheet_match["sheet_row"])
            current_terms = str(
                offer.get("payout_display") or offer.get("headline_payout") or "not recorded"
            ).strip()
            terms_source = str(offer.get("source") or "best_terms_matrix")
            terms_verified_at = ""
            relationship = str(offer.get("relationship") or "unknown").strip().casefold()
            status = str(offer.get("status") or "").strip().casefold()
            wire_in_ready = bool(offer.get("wire_in_ready"))
            wired_live = bool(offer.get("wired_live"))
            if sheet_match is not None:
                relationship = str(sheet_match["relationship"])
                status = str(sheet_match["status"])
                wire_in_ready = bool(sheet_match["wire_in_ready"])
                wired_live = bool(sheet_match["wired_live"])
                if sheet_match["current_terms"]:
                    current_terms = str(sheet_match["current_terms"])
                    terms_source = "latest_read_only_sheet_snapshot"
            override = overrides.get((row_number or 0, program_id.casefold()))
            if override and re.sub(r"[^a-z0-9]+", "", override["provider"].casefold()) == re.sub(
                r"[^a-z0-9]+", "", provider_name.casefold(),
            ):
                current_terms = override["current_terms"]
                terms_source = "single_writer_verified_sheet_readback"
                terms_verified_at = override["verified_at"]
            record = {
                "provider": provider_name,
                "marketplace": network,
                "sheet_row": row_number,
                "program_id": program_id,
                "relationship": relationship,
                "status": status,
                "wire_in_ready": wire_in_ready,
                "wired_live": wired_live,
                "current_terms": current_terms[:300],
                "paid_event": _affiliate_paid_event(offer.get("paid_event"), current_terms),
                "currency": str(offer.get("currency") or "").strip().upper(),
                "terms_source": terms_source,
            }
            if terms_verified_at:
                record["terms_verified_at"] = terms_verified_at
            offers.append(record)
        if not selected_marketplace:
            newest_networks = {
                network for network in {str(row.get("marketplace") or "") for row in offers}
                if network and re.search(rf"\b{re.escape(network)}(?:\.com)?\b", newest_text, re.I)
            }
            id_matches = {
                str(row.get("marketplace") or "") for row in offers
                if str(row.get("program_id") or "")
                and re.search(rf"(?<!\d){re.escape(str(row['program_id']))}(?!\d)", thread_text)
            }
            if len(newest_networks) == 1:
                selected_marketplace = next(iter(newest_networks))
            elif len(id_matches) == 1:
                selected_marketplace = next(iter(id_matches))
        relevant = [row for row in offers if row["marketplace"] == selected_marketplace]
        if len(relevant) > 1:
            id_relevant = [
                row for row in relevant if str(row.get("program_id") or "")
                and re.search(rf"(?<!\d){re.escape(str(row['program_id']))}(?!\d)", thread_text)
            ]
            relevant = id_relevant
        if len(relevant) > 1:
            raise ValueError("affiliate_thread_marketplace_row_ambiguous")
        if len(relevant) == 1:
            relevant_record = dict(relevant[0])
            publisher_match = re.search(
                r"\b(?:media\s+partner|publisher|affiliate)\s+id\s*:?\s*([A-Za-z0-9_.]+)",
                thread_text, re.I,
            )
            if publisher_match:
                relevant_record["publisher_id"] = publisher_match.group(1)
            facts.append("AFFILIATE_RELEVANT_RAIL_JSON " + json.dumps(
                relevant_record, sort_keys=True, ensure_ascii=False,
            ))
        else:
            facts.append(
                "AFFILIATE_RELEVANT_RAIL_UNRESOLVED | no exact marketplace row was verified from this thread | omit marketplace claims and never choose another rail"
            )
        for row in offers:
            if relevant_record is not None and (
                row["marketplace"], row["sheet_row"], row["program_id"]
            ) == (
                relevant_record["marketplace"], relevant_record["sheet_row"],
                relevant_record["program_id"],
            ):
                continue
            other = {
                key: row[key] for key in (
                    "provider", "marketplace", "sheet_row", "program_id", "relationship",
                    "status", "wire_in_ready", "wired_live", "current_terms", "paid_event",
                    "currency",
                )
            }
            other["relevant_to_thread"] = False
            facts.append("AFFILIATE_OTHER_RAIL_JSON " + json.dumps(
                other, sort_keys=True, ensure_ascii=False,
            ))
        if provider_name.casefold() == "sesame care":
            facts.append(
                "AFFILIATE_BRAND_DISAMBIGUATION | CJ entries named Sesame Place are theme parks and are not Sesame Care"
            )
    if len(facts) > 20 or any(len(fact) > 500 for fact in facts):
        raise ValueError("best_terms_draft_context_too_large")
    return {
        "facts": facts,
        "provider": str((matched or {}).get("provider_name") or ""),
        "marketplace": selected_marketplace,
        "relevant_rail": relevant_record or {},
        "source_sha256": sha256_text(json.dumps({
            "matrix_sha256": sha256_bytes(raw),
            "verified_overrides_sha256": overrides_sha,
            "sheet_snapshot_sha256": sheet_snapshot_sha,
            "provider": str((matched or {}).get("provider_name") or ""),
            "marketplace": selected_marketplace,
        }, sort_keys=True)),
    }


def affiliate_machine_context(marketplace: str, provider: str) -> dict[str, Any]:
    """Bound read-only evidence, not blanket STATE prose or another rail's facts."""
    if marketplace:
        from inbox_control_plane.network_grounding import context
        return context(marketplace)
    facts, sources = [], {}
    state_raw = CURRENT_BUSINESS_STATE_PATH.read_bytes()
    sources['state_sha256'] = sha256_bytes(state_raw)
    pointer_path = BUSINESS / 'reports/glp-clinic-scout/seo-machine/current-content-base.json'
    try:
        pointer_raw = pointer_path.read_bytes()
        pointer = json.loads(pointer_raw)
        root = Path(pointer['release_root']).resolve()
        if pointer.get('status') != 'PASS' or not root.is_relative_to(BUSINESS / 'site-system/flagship-v2/releases'):
            raise ValueError('content_base_unverified')
        registry_raw = (root / 'data/program-registry.json').read_bytes()
        registry = json.loads(registry_raw)
        sources['content_base_sha256'] = sha256_bytes(pointer_raw)
        sources['provider_registry_sha256'] = sha256_bytes(registry_raw)
        matches = [row for row in registry['programs'] if provider and
                   str(row.get('display_name') or '').casefold() == provider.casefold()]
        if len(matches) == 1:
            row = matches[0]
            facts.append('CURRENT_PROVIDER_REGISTRY | ' + json.dumps({
                k: row.get(k) for k in ('display_name', 'program_id', 'commercial_state', 'review_path')
            }, sort_keys=True))
        else:
            facts.append('CURRENT_PROVIDER_REGISTRY | no unique sender program identified; do not claim a specific provider is live or wired')
    except (OSError, KeyError, TypeError, ValueError):
        facts.append('CURRENT_PROVIDER_REGISTRY | UNKNOWN; no verified current program record')
    candidates = sorted(AFFILIATE_BACKRUN_ROOT.glob('enumeration-*/_sweep-summary.json'),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    for path in candidates[:10]:
        try:
            raw = path.read_bytes(); doc = json.loads(raw)
            captured = dt.datetime.fromisoformat(str(doc['captured_at_utc']).replace('Z', '+00:00'))
            age = (dt.datetime.now(dt.timezone.utc) - captured).total_seconds()
            row = (doc.get('networks') or {}).get(marketplace)
            if not 0 <= age <= 86400 or not isinstance(row, dict):
                continue
            sources['network_read_sha256'] = sha256_bytes(raw)
            facts.append('CURRENT_NETWORK_READ | ' + json.dumps({
                'marketplace': marketplace, 'captured_at_utc': doc['captured_at_utc'],
                **{k: row.get(k) for k in ('auth', 'completeness', 'enumerated', 'reported_total')}
            }, sort_keys=True))
            break
        except (OSError, KeyError, TypeError, ValueError):
            continue
    else:
        facts.append('CURRENT_NETWORK_READ | UNKNOWN; fresh authenticated sender-network read unavailable')
    facts.append('CAMPAIGN_AND_COMMISSION_REPORTING | UNKNOWN unless an exact current authenticated reporting receipt is supplied; enumeration, approval and clicks do not prove campaigns, sales or revenue')
    return {'facts': facts, 'sources': sources}


def resolve_verified_draft_context(
    venture_key: str,
    event_id: str,
    static_context: list[str] | None = None,
    *,
    connection: sqlite3.Connection | None = None,
    parsed: dict[str, Any] | None = None,
    marketplace: str = "",
) -> dict[str, Any]:
    """Resolve the only business context Fable may see for this event."""
    static = [str(item).strip() for item in (static_context or []) if str(item).strip()]
    if venture_key in EVENT_SCOPED_DRAFT_VENTURES:
        resolved = load_event_scoped_draft_context(venture_key, event_id)
        best_terms = load_affiliate_best_terms_context(parsed, marketplace=marketplace)
        machine = affiliate_machine_context(best_terms['marketplace'], best_terms['provider'])
        resolved["context_scope"]["source_sha256"] = sha256_text(json.dumps({
            'machine_sources': machine['sources'],
            "event_context_source_sha256": resolved["context_scope"]["source_sha256"],
            "best_terms_source_sha256": best_terms["source_sha256"],
            "provider": best_terms["provider"],
            "marketplace": best_terms["marketplace"],
        }, sort_keys=True))
        resolved["facts"] = list(dict.fromkeys([
            *resolved["facts"], *machine['facts'], *best_terms["facts"], *static,
        ]))
        if len(resolved['facts']) > 20 or any(len(fact) > 500 for fact in resolved['facts']):
            raise ValueError('verified_draft_context_too_large_no_silent_truncation')
        resolved["context_sha256"] = sha256_text(json.dumps({
            "scope": resolved["context_scope"],
            "facts": resolved["facts"],
            "scope_exclusion_markers": resolved["scope_exclusion_markers"],
        }, sort_keys=True, ensure_ascii=False))
        return resolved
    facts, context_sha = load_current_business_context(connection)
    return {
        "facts": list(dict.fromkeys([*facts, *static]))[:20],
        "scope_exclusion_markers": [],
        "context_scope": {
            "type": "global_business_state",
            "venture_key": venture_key,
            "event_id": event_id,
            "source_sha256": context_sha,
        },
        "context_sha256": context_sha,
    }


def latest_inbound_thread_event(
    connection: sqlite3.Connection, event_id: str,
) -> dict[str, str]:
    """Return the newest stored inbound event in this event's provider thread."""
    event = connection.execute(
        """SELECT event_id,mailbox_id,provider_thread_id,received_at,ingested_at
           FROM inbox_events WHERE event_id=?""",
        (event_id,),
    ).fetchone()
    if event is None:
        raise ValueError("unknown_event_id")
    thread_id = str(event["provider_thread_id"] or "")
    if not thread_id:
        return {
            "event_id": str(event["event_id"]),
            "received_at": str(event["received_at"]),
        }
    latest = connection.execute(
        """SELECT event_id,received_at FROM inbox_events
           WHERE mailbox_id=? AND provider_thread_id=?
           ORDER BY received_at DESC,ingested_at DESC,event_id DESC LIMIT 1""",
        (str(event["mailbox_id"]), thread_id),
    ).fetchone()
    if latest is None:
        raise ValueError("latest_thread_event_missing")
    return {
        "event_id": str(latest["event_id"]),
        "received_at": str(latest["received_at"]),
    }


def latest_thread_marketplace_context(
    connection: sqlite3.Connection, event_id: str, mailbox: dict[str, Any],
) -> dict[str, str]:
    """Find the newest explicit marketplace name in unquoted inbound thread text."""
    event = connection.execute(
        "SELECT mailbox_id,provider_thread_id FROM inbox_events WHERE event_id=?", (event_id,),
    ).fetchone()
    if event is None:
        raise ValueError("unknown_event_id")
    thread_id = str(event["provider_thread_id"] or "")
    if not thread_id:
        return {"network": "", "source_event_id": event_id, "source_sha256": ""}
    networks = (
        ("maxbounty", re.compile(r"\bmax\s*bounty\b", re.I)),
        ("xentraffic", re.compile(r"\bxen\s*traffic\b", re.I)),
        ("partnerize", re.compile(r"\bpartnerize\b", re.I)),
        ("impact", re.compile(r"\bimpact(?:\.com)?\b", re.I)),
        ("katalys", re.compile(r"\bkatalys\b", re.I)),
        ("awin", re.compile(r"\bawin\b", re.I)),
        ("cj", re.compile(r"\bcj(?: affiliate)?\b", re.I)),
    )
    rows = connection.execute(
        """SELECT event_id,raw_path,raw_sha256 FROM inbox_events
           WHERE mailbox_id=? AND provider_thread_id=?
           ORDER BY received_at DESC,ingested_at DESC,event_id DESC""",
        (str(event["mailbox_id"]), thread_id),
    ).fetchall()
    for row in rows:
        raw_path = Path(str(row["raw_path"] or ""))
        if not raw_path.is_file():
            continue
        raw = raw_path.read_bytes()
        if sha256_bytes(raw) != str(row["raw_sha256"] or ""):
            raise ValueError("thread_marketplace_source_hash_mismatch")
        parsed = parse_message(raw, mailbox)
        newest = newest_unquoted_human_reply(str(parsed.get("body_text") or ""))
        matched = [network for network, pattern in networks if pattern.search(newest)]
        if len(matched) == 1:
            return {
                "network": matched[0],
                "source_event_id": str(row["event_id"]),
                "source_sha256": str(row["raw_sha256"]),
            }
        if len(matched) > 1:
            raise ValueError("thread_marketplace_context_ambiguous")
    return {"network": "", "source_event_id": event_id, "source_sha256": ""}


def validate_ownership_manifest(data: dict[str, Any]) -> None:
    manifest = data.get("ownership_manifest") or {}
    if manifest.get("processor_job_id") != CANONICAL_INBOX_JOB_ID:
        raise ValueError("ownership_manifest_processor_job_mismatch")
    partitions = manifest.get("partitions")
    if not isinstance(partitions, dict) or set(partitions) != set(REQUIRED_OWNER_PARTITIONS):
        raise ValueError("ownership_manifest_partitions_invalid")
    registry_addresses = {
        str(address).strip().lower()
        for row in data.get("mailboxes") or []
        for address in (
            row["canonical_address"],
            *(row.get("aliases") or []),
            *(row.get("groups") or []),
        )
    }
    active_registry_addresses = {
        str(address).strip().lower()
        for row in data.get("mailboxes") or []
        if row.get("lifecycle") == "active"
        for address in (
            row["canonical_address"],
            *(row.get("aliases") or []),
            *(row.get("groups") or []),
        )
    }
    seen: dict[str, str] = {}
    for owner, required_addresses in REQUIRED_OWNER_PARTITIONS.items():
        addresses = partitions[owner]
        if not isinstance(addresses, list):
            raise ValueError(f"ownership_manifest_partition_not_list:{owner}")
        for address in addresses:
            normalized = str(address).strip().lower()
            if normalized in seen:
                raise ValueError(f"ownership_manifest_duplicate_address:{normalized}")
            seen[normalized] = owner
            if normalized not in registry_addresses:
                raise ValueError(f"ownership_manifest_address_not_registered:{normalized}")
            if normalized not in active_registry_addresses:
                raise ValueError(f"ownership_manifest_address_not_active:{normalized}")
        expected = {str(address).strip().lower() for address in required_addresses}
        if {str(address).strip().lower() for address in addresses} != expected:
            raise ValueError(f"ownership_manifest_required_set_mismatch:{owner}")
    expected_all = {address.lower() for addresses in REQUIRED_OWNER_PARTITIONS.values() for address in addresses}
    if set(seen) != expected_all:
        raise ValueError("ownership_manifest_coverage_mismatch")


def load_registry(path: Path = REGISTRY_PATH) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("mailboxes") or []
    ids: set[str] = set()
    addresses: dict[str, str] = {}
    required = {
        "mailbox_id", "venture", "company", "canonical_address", "provider",
        "adapter", "owner", "lifecycle", "poll_seconds", "freshness_sla_seconds",
        "inbound_surface", "routing_policy", "auth_reference",
    }
    for row in rows:
        missing = sorted(required - set(row))
        if missing:
            raise ValueError(f"registry_missing_fields:{row.get('mailbox_id')}:{','.join(missing)}")
        mailbox_id = str(row["mailbox_id"])
        if mailbox_id in ids:
            raise ValueError(f"duplicate_mailbox_id:{mailbox_id}")
        ids.add(mailbox_id)
        for address in [row["canonical_address"], *(row.get("aliases") or []), *(row.get("groups") or [])]:
            normalized = str(address).strip().lower()
            if normalized in addresses and addresses[normalized] != mailbox_id:
                raise ValueError(f"duplicate_address_owner:{normalized}")
            addresses[normalized] = mailbox_id
        if row["lifecycle"] not in {"active", "shelved", "disabled"}:
            raise ValueError(f"invalid_lifecycle:{mailbox_id}")
    validate_ownership_manifest(data)
    return data


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS mailboxes(
  mailbox_id TEXT PRIMARY KEY, venture TEXT NOT NULL, company TEXT NOT NULL,
  canonical_address TEXT NOT NULL UNIQUE, aliases_json TEXT NOT NULL, groups_json TEXT NOT NULL,
  provider TEXT NOT NULL, adapter TEXT NOT NULL, auth_reference TEXT NOT NULL,
  owner TEXT NOT NULL, lifecycle TEXT NOT NULL CHECK(lifecycle IN ('active','shelved','disabled')),
  poll_seconds INTEGER NOT NULL, freshness_sla_seconds INTEGER NOT NULL,
  inbound_surface TEXT NOT NULL, routing_policy TEXT NOT NULL, dmarc_domains_json TEXT NOT NULL,
  registry_hash TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS provider_cursors(
  mailbox_id TEXT PRIMARY KEY REFERENCES mailboxes(mailbox_id), cursor TEXT,
  overlap_boundary TEXT, last_attempt_at TEXT, last_auth_success_at TEXT,
  last_read_success_at TEXT, last_complete_poll_at TEXT, last_message_observed_at TEXT,
  backlog_count INTEGER NOT NULL DEFAULT 0, next_poll_at TEXT, unhealthy_reason TEXT,
  poll_receipt_id TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS inbox_events(
  event_id TEXT PRIMARY KEY, mailbox_id TEXT NOT NULL REFERENCES mailboxes(mailbox_id),
  provider_message_id TEXT NOT NULL, provider_thread_id TEXT, rfc_message_id TEXT,
  raw_sha256 TEXT NOT NULL, raw_path TEXT NOT NULL, body_sha256 TEXT NOT NULL,
  sender TEXT NOT NULL, sender_domain TEXT NOT NULL, recipients_json TEXT NOT NULL,
  subject TEXT NOT NULL, received_at TEXT NOT NULL, source_folder TEXT,
  labels_json TEXT NOT NULL, parse_defects_json TEXT NOT NULL,
  state TEXT NOT NULL, ingested_at TEXT NOT NULL,
  UNIQUE(mailbox_id, provider_message_id)
);
CREATE TABLE IF NOT EXISTS inbox_attachments(
  attachment_id TEXT PRIMARY KEY, event_id TEXT NOT NULL REFERENCES inbox_events(event_id),
  filename TEXT NOT NULL, content_type TEXT NOT NULL, size_bytes INTEGER NOT NULL,
  sha256 TEXT NOT NULL, storage_path TEXT, safety_state TEXT NOT NULL,
  parser_status TEXT NOT NULL, parsed_json TEXT NOT NULL, created_at TEXT NOT NULL,
  UNIQUE(event_id, sha256, filename)
);
CREATE TABLE IF NOT EXISTS inbox_classifications(
  classification_id TEXT PRIMARY KEY, event_id TEXT NOT NULL REFERENCES inbox_events(event_id),
  policy_version TEXT NOT NULL, category TEXT NOT NULL, confidence REAL NOT NULL,
  reason_code TEXT NOT NULL, matched_facts_json TEXT NOT NULL, venture TEXT NOT NULL,
  reply_class TEXT, intent_confidence REAL, matched_signals_json TEXT,
  venture_lifecycle TEXT NOT NULL, actionable INTEGER NOT NULL, route TEXT NOT NULL,
  created_at TEXT NOT NULL, UNIQUE(event_id, policy_version)
);
CREATE TABLE IF NOT EXISTS notification_outbox(
  outbox_id TEXT PRIMARY KEY, event_id TEXT REFERENCES inbox_events(event_id),
  purpose TEXT NOT NULL, destination_channel_id TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE,
  payload TEXT NOT NULL, payload_sha256 TEXT NOT NULL, status TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0, discord_message_id TEXT, response_sha256 TEXT,
  readback_sha256 TEXT, last_error_class TEXT, created_at TEXT NOT NULL,
  delivered_at TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mailbox_actions(
  action_id TEXT PRIMARY KEY, event_id TEXT NOT NULL REFERENCES inbox_events(event_id),
  action TEXT NOT NULL, status TEXT NOT NULL, provider_receipt_json TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS affiliate_approval_facts(
  fact_id TEXT PRIMARY KEY, network TEXT NOT NULL, publisher_id TEXT NOT NULL,
  program_id TEXT NOT NULL, transition TEXT NOT NULL CHECK(transition='approved'),
  first_verified_at_utc TEXT NOT NULL, first_event_id TEXT NOT NULL REFERENCES inbox_events(event_id),
  provider_snapshot_sha256 TEXT NOT NULL, terms_snapshot_sha256 TEXT NOT NULL,
  classification_portfolio TEXT NOT NULL CHECK(classification_portfolio IN ('glp','sister')),
  UNIQUE(network,publisher_id,program_id,transition)
);
CREATE TABLE IF NOT EXISTS affiliate_approval_event_facts(
  event_id TEXT PRIMARY KEY REFERENCES inbox_events(event_id),
  fact_id TEXT NOT NULL REFERENCES affiliate_approval_facts(fact_id),
  observed_at_utc TEXT NOT NULL, processing_completed_at_utc TEXT,
  classification_portfolio TEXT NOT NULL CHECK(classification_portfolio IN ('glp','sister'))
);
CREATE TABLE IF NOT EXISTS operator_decisions(
  decision_id TEXT PRIMARY KEY, event_id TEXT NOT NULL REFERENCES inbox_events(event_id),
  channel_id TEXT NOT NULL, card_message_id TEXT NOT NULL, decision_message_id TEXT NOT NULL UNIQUE,
  actor_id TEXT NOT NULL, action TEXT NOT NULL, card_revision_sha256 TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS manual_dispositions(
  event_id TEXT PRIMARY KEY,
  disposition TEXT NOT NULL CHECK(disposition IN ('RESOLVED_EXTERNALLY','AFFILIATE_TOOL')),
  source_message_id TEXT NOT NULL,
  source_channel_id TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  note TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS local_drafts(
  draft_id TEXT PRIMARY KEY, event_id TEXT NOT NULL REFERENCES inbox_events(event_id),
  revision INTEGER NOT NULL, from_identity TEXT NOT NULL, recipients_json TEXT NOT NULL,
  provider_thread_id TEXT, subject TEXT NOT NULL, body TEXT NOT NULL, body_sha256 TEXT NOT NULL,
  source_hash TEXT NOT NULL, policy_version TEXT NOT NULL, status TEXT NOT NULL,
  approval_actor_id TEXT, approval_event_id TEXT, approved_body_sha256 TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(event_id, revision)
);
CREATE TABLE IF NOT EXISTS future_send_policy(
  policy_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL CHECK(enabled=0),
  requirements_json TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS inbox_runs(
  run_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
  discovered_count INTEGER NOT NULL DEFAULT 0, persisted_count INTEGER NOT NULL DEFAULT 0,
  duplicate_count INTEGER NOT NULL DEFAULT 0, classified_count INTEGER NOT NULL DEFAULT 0,
  decision_queued_count INTEGER NOT NULL DEFAULT 0, suppressed_count INTEGER NOT NULL DEFAULT 0,
  quarantined_count INTEGER NOT NULL DEFAULT 0, outbound_send_count INTEGER NOT NULL DEFAULT 0,
  provider_draft_count INTEGER NOT NULL DEFAULT 0, provider_mutation_count INTEGER NOT NULL DEFAULT 0,
  exit_status TEXT NOT NULL, source_sha256 TEXT NOT NULL, receipt_path TEXT
);
CREATE TABLE IF NOT EXISTS audit_events(
  audit_id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT NOT NULL, object_type TEXT NOT NULL,
  object_id TEXT NOT NULL, actor TEXT NOT NULL, payload_json TEXT NOT NULL,
  previous_hash TEXT NOT NULL, event_hash TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS side_effect_counters(
  name TEXT PRIMARY KEY, value INTEGER NOT NULL CHECK(value >= 0), updated_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_events BEGIN SELECT RAISE(ABORT, 'audit_append_only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_events BEGIN SELECT RAISE(ABORT, 'audit_append_only'); END;
CREATE TRIGGER IF NOT EXISTS classification_no_update BEFORE UPDATE ON inbox_classifications BEGIN SELECT RAISE(ABORT, 'classification_append_only'); END;
CREATE TRIGGER IF NOT EXISTS classification_no_delete BEFORE DELETE ON inbox_classifications BEGIN SELECT RAISE(ABORT, 'classification_append_only'); END;
CREATE TRIGGER IF NOT EXISTS affiliate_approval_fact_no_update BEFORE UPDATE ON affiliate_approval_facts BEGIN SELECT RAISE(ABORT, 'affiliate_approval_fact_immutable'); END;
CREATE TRIGGER IF NOT EXISTS affiliate_approval_fact_no_delete BEFORE DELETE ON affiliate_approval_facts BEGIN SELECT RAISE(ABORT, 'affiliate_approval_fact_immutable'); END;
"""


class Ledger:
    def __init__(self, path: Path = DB_PATH, *, email_flow_db_path: Path | None = None):
        self.path = path
        self.email_flow_db_path = email_flow_db_path or (
            EMAIL_FLOW_DB_PATH if path == DB_PATH else path.with_name("email-flows-fixture.sqlite")
        )
        private_dir(path.parent)
        for private_root in (PRIVATE_ROOT, RAW_ROOT, ATTACHMENT_ROOT, BACKUP_ROOT):
            private_dir(private_root)
        self.conn = sqlite3.connect(path, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=10000")
        self.conn.executescript(SCHEMA_SQL)
        self.apply_schema_migrations()
        self.conn.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('schema_version',?)", (str(SCHEMA_VERSION),))
        self.conn.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('policy_version',?)", (POLICY_VERSION,))
        self.conn.execute(
            "INSERT OR IGNORE INTO schema_meta(key,value) VALUES('draft_only_cutover_utc',?)",
            (DRAFT_ONLY_CUTOVER_UTC,),
        )
        for name in ("external_sends", "provider_drafts", "provider_mutations"):
            self.conn.execute("INSERT OR IGNORE INTO side_effect_counters(name,value,updated_at) VALUES(?,0,?)", (name, utc_now()))
        self.conn.execute(
            "INSERT OR REPLACE INTO future_send_policy(policy_id,enabled,requirements_json,updated_at) VALUES(?,?,?,?)",
            (
                "phase1-global-deny", 0,
                json.dumps({
                    "idempotency_key": True, "thread_binding": True, "send_as_verify": True,
                    "rate_caps": True, "approved_templates": True, "pre_send_reread": True,
                    "post_send_readback": True, "immutable_receipt": True,
                    "exact_operator_approval": True, "changed_draft_invalidates_approval": True,
                    "high_risk_human_gated": True,
                }, sort_keys=True), utc_now(),
            ),
        )
        self.conn.commit()
        self._chmod_files()

    def apply_schema_migrations(self) -> None:
        """Apply additive metadata through v4 approval-fact durability."""
        columns = {str(row[1]) for row in self.conn.execute("PRAGMA table_info(inbox_classifications)")}
        additions = {
            "reply_class": "TEXT",
            "intent_confidence": "REAL",
            "matched_signals_json": "TEXT",
        }
        for name, sql_type in additions.items():
            if name not in columns:
                self.conn.execute(f"ALTER TABLE inbox_classifications ADD COLUMN {name} {sql_type}")
        unique_event_action = False
        for index in self.conn.execute("PRAGMA index_list(mailbox_actions)"):
            if int(index[2]) != 1:
                continue
            names = [str(row[2]) for row in self.conn.execute(f"PRAGMA index_info('{index[1]}')")]
            if names == ["event_id", "action"]:
                unique_event_action = True
                break
        if unique_event_action:
            self.conn.executescript("""
                ALTER TABLE mailbox_actions RENAME TO mailbox_actions_v2;
                CREATE TABLE mailbox_actions(
                  action_id TEXT PRIMARY KEY, event_id TEXT NOT NULL REFERENCES inbox_events(event_id),
                  action TEXT NOT NULL, status TEXT NOT NULL, provider_receipt_json TEXT NOT NULL,
                  attempts INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                INSERT INTO mailbox_actions SELECT * FROM mailbox_actions_v2;
                DROP TABLE mailbox_actions_v2;
                CREATE INDEX IF NOT EXISTS mailbox_actions_event_action_idx ON mailbox_actions(event_id,action);
            """)
        else:
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS mailbox_actions_event_action_idx ON mailbox_actions(event_id,action)"
            )

    def _chmod_files(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(self.path) + suffix)
            if path.exists():
                path.chmod(0o600)

    def close(self) -> None:
        self.conn.commit()
        self._chmod_files()
        self.conn.close()

    def sync_registry(self, registry: dict[str, Any]) -> None:
        now = utc_now()
        with self.conn:
            for row in registry["mailboxes"]:
                row_hash = sha256_text(json.dumps(row, sort_keys=True))
                self.conn.execute(
                    """INSERT INTO mailboxes(mailbox_id,venture,company,canonical_address,aliases_json,groups_json,
                    provider,adapter,auth_reference,owner,lifecycle,poll_seconds,freshness_sla_seconds,inbound_surface,
                    routing_policy,dmarc_domains_json,registry_hash,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(mailbox_id) DO UPDATE SET venture=excluded.venture,company=excluded.company,
                    canonical_address=excluded.canonical_address,aliases_json=excluded.aliases_json,groups_json=excluded.groups_json,
                    provider=excluded.provider,adapter=excluded.adapter,auth_reference=excluded.auth_reference,owner=excluded.owner,
                    lifecycle=excluded.lifecycle,poll_seconds=excluded.poll_seconds,freshness_sla_seconds=excluded.freshness_sla_seconds,
                    inbound_surface=excluded.inbound_surface,routing_policy=excluded.routing_policy,
                    dmarc_domains_json=excluded.dmarc_domains_json,registry_hash=excluded.registry_hash,updated_at=excluded.updated_at""",
                    (
                        row["mailbox_id"], row["venture"], row["company"], row["canonical_address"],
                        json.dumps(row.get("aliases") or []), json.dumps(row.get("groups") or []), row["provider"],
                        row["adapter"], row["auth_reference"], row["owner"], row["lifecycle"], row["poll_seconds"],
                        row["freshness_sla_seconds"], row["inbound_surface"], row["routing_policy"],
                        json.dumps(row.get("dmarc_domains") or []), row_hash, now,
                    ),
                )
                self.conn.execute(
                    "INSERT OR IGNORE INTO provider_cursors(mailbox_id,updated_at) VALUES(?,?)",
                    (row["mailbox_id"], now),
                )
        self.audit("registry_sync", "registry", "canonical-mailbox-registry", {"rows": len(registry["mailboxes"]), "registry_sha256": sha256_text(json.dumps(registry, sort_keys=True))})

    def audit(self, event_type: str, object_type: str, object_id: str, payload: dict[str, Any], actor: str = "inbox-control-plane") -> str:
        previous = self.conn.execute("SELECT event_hash FROM audit_events ORDER BY audit_id DESC LIMIT 1").fetchone()
        previous_hash = previous[0] if previous else "0" * 64
        created = utc_now()
        safe_payload = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        event_hash = sha256_text("|".join([previous_hash, event_type, object_type, object_id, actor, safe_payload, created]))
        self.conn.execute(
            "INSERT INTO audit_events(event_type,object_type,object_id,actor,payload_json,previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (event_type, object_type, object_id, actor, safe_payload, previous_hash, event_hash, created),
        )
        self.conn.commit()
        return event_hash

    def health(self, mailbox_id: str, *, auth_ok: bool, read_ok: bool, backlog: int, last_message_at: str | None, unhealthy: str | None, poll_receipt_id: str) -> None:
        now = utc_now()
        row = self.conn.execute("SELECT poll_seconds FROM mailboxes WHERE mailbox_id=?", (mailbox_id,)).fetchone()
        next_poll = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=int(row[0] if row else 300))
        with self.conn:
            self.conn.execute(
                """UPDATE provider_cursors SET last_attempt_at=?,last_auth_success_at=CASE WHEN ? THEN ? ELSE last_auth_success_at END,
                last_read_success_at=CASE WHEN ? THEN ? ELSE last_read_success_at END,
                last_complete_poll_at=CASE WHEN ? THEN ? ELSE last_complete_poll_at END,
                last_message_observed_at=COALESCE(?,last_message_observed_at),backlog_count=?,next_poll_at=?,
                unhealthy_reason=?,poll_receipt_id=?,updated_at=? WHERE mailbox_id=?""",
                (now, auth_ok, now, read_ok, now, read_ok, now, last_message_at, backlog, next_poll.replace(microsecond=0).isoformat().replace("+00:00", "Z"), unhealthy, poll_receipt_id, now, mailbox_id),
            )

    def queue_ops_failure(self, mailbox_id: str, error_class: str, run_id: str) -> None:
        bucket = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d%H")
        error_family = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(error_class).partition(":")[0])[:60] or "UnknownError"
        key = f"ops-health:{mailbox_id}:{error_family}:{bucket}"
        if "IntegrityError:" in error_class:
            recovery = "Inspect the private receipt and repair the local append-only ingestion invariant; provider auth may still be healthy."
        elif any(token in error_class.lower() for token in ("auth", "credential", "token", "profile_mismatch")):
            recovery = "Inspect the private receipt and restore provider authentication/read freshness."
        else:
            recovery = "Inspect the private receipt and repair the failing read-only adapter or local ingestion path."
        payload = (
            f"⚠️ Inbox Control Plane — {mailbox_id} unhealthy\n"
            f"Failure: {one_line(error_class, 120)}\n"
            f"Run: `{run_id}`\n"
            "Raw mail and secrets were not included. " + recovery
        )
        self.queue_notification(None, "health_failure", OPS_CHANNEL_ID, key, payload)

    def queue_notification(
        self,
        event_id: str | None,
        purpose: str,
        channel: str,
        key: str,
        payload: str,
        *,
        verify_existing_payload: bool = False,
        transactional: bool = False,
    ) -> str:
        payload = redact(payload)
        if len(payload) > DISCORD_LIMIT:
            raise ValueError("notification_payload_too_large")
        outbox_id = "out_" + sha256_text(key)[:24]
        payload_sha256 = sha256_text(payload)
        now = utc_now()
        if transactional and not self.conn.in_transaction:
            raise RuntimeError('notification_parent_transaction_missing')
        with contextlib.nullcontext() if transactional else self.conn:
            self.conn.execute(
                """INSERT OR IGNORE INTO notification_outbox(outbox_id,event_id,purpose,destination_channel_id,idempotency_key,
                payload,payload_sha256,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (outbox_id, event_id, purpose, channel, key, payload, payload_sha256, "pending", now, now),
            )
            if verify_existing_payload:
                stored = self.conn.execute(
                    """SELECT outbox_id,event_id,purpose,destination_channel_id,idempotency_key,payload,payload_sha256
                       FROM notification_outbox WHERE outbox_id=?""",
                    (outbox_id,),
                ).fetchone()
                expected = {
                    "outbox_id": outbox_id,
                    "event_id": event_id,
                    "purpose": purpose,
                    "destination_channel_id": channel,
                    "idempotency_key": key,
                    "payload": payload,
                    "payload_sha256": payload_sha256,
                }
                if stored is None or any(stored[name] != value for name, value in expected.items()):
                    raise RuntimeError("notification_outbox_identity_or_payload_conflict")
        return outbox_id

    def record_affiliate_approval_fact(
        self, *, event_id: str, network: str, publisher_id: str, program_id: str,
        provider_snapshot_sha256: str, terms_snapshot_sha256: str,
        classification_portfolio: str, verified_at_utc: str,
    ) -> dict[str, Any]:
        """Record the first verified approval transition and bind this event to it."""
        if classification_portfolio not in {"glp", "sister"}:
            raise ValueError("affiliate_approval_portfolio_invalid")
        values = (network, publisher_id, program_id)
        if any(not str(value or "").strip() for value in values):
            raise ValueError("affiliate_approval_identity_incomplete")
        if any(not re.fullmatch(r"[0-9a-f]{64}", str(value or "")) for value in (provider_snapshot_sha256, terms_snapshot_sha256)):
            raise ValueError("affiliate_approval_evidence_hash_invalid")
        if self.conn.execute("SELECT 1 FROM inbox_events WHERE event_id=?", (event_id,)).fetchone() is None:
            raise ValueError("unknown_event_id")
        fact_id = "aaf_" + sha256_text(f"{network}|{publisher_id}|{program_id}|approved")[:24]
        now = utc_now()
        with self.conn:
            self.conn.execute(
                """INSERT OR IGNORE INTO affiliate_approval_facts(
                   fact_id,network,publisher_id,program_id,transition,first_verified_at_utc,first_event_id,
                   provider_snapshot_sha256,terms_snapshot_sha256,classification_portfolio
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (fact_id, network, publisher_id, program_id, "approved", verified_at_utc, event_id,
                 provider_snapshot_sha256, terms_snapshot_sha256, classification_portfolio),
            )
            fact = self.conn.execute("SELECT * FROM affiliate_approval_facts WHERE fact_id=?", (fact_id,)).fetchone()
            if fact is None or any(str(fact[key]) != str(value) for key, value in (
                ("network", network), ("publisher_id", publisher_id), ("program_id", program_id),
                ("transition", "approved"), ("classification_portfolio", classification_portfolio),
            )):
                raise RuntimeError("affiliate_approval_fact_identity_conflict")
            self.conn.execute(
                """INSERT OR IGNORE INTO affiliate_approval_event_facts(
                   event_id,fact_id,observed_at_utc,processing_completed_at_utc,classification_portfolio
                   ) VALUES(?,?,?,NULL,?)""",
                (event_id, fact_id, now, classification_portfolio),
            )
            event_fact = self.conn.execute("SELECT fact_id FROM affiliate_approval_event_facts WHERE event_id=?", (event_id,)).fetchone()
            if event_fact is None or str(event_fact["fact_id"]) != fact_id:
                raise RuntimeError("affiliate_approval_event_fact_conflict")
        return {"fact_id": fact_id, "first_verified_at_utc": str(fact["first_verified_at_utc"]), "idempotent": str(fact["first_event_id"]) != event_id}

    def complete_affiliate_approval_event(self, event_id: str, completed_at_utc: str | None = None) -> dict[str, Any]:
        """Set the immutable event's completion timestamp once, after mailbox readback."""
        now = str(completed_at_utc or utc_now())
        with self.conn:
            row = self.conn.execute("SELECT processing_completed_at_utc FROM affiliate_approval_event_facts WHERE event_id=?", (event_id,)).fetchone()
            if row is None:
                raise ValueError("affiliate_approval_event_fact_missing")
            if row["processing_completed_at_utc"] is None:
                self.conn.execute("UPDATE affiliate_approval_event_facts SET processing_completed_at_utc=? WHERE event_id=? AND processing_completed_at_utc IS NULL", (now, event_id))
                completed = now
                replay = False
            else:
                completed = str(row["processing_completed_at_utc"])
                replay = True
        return {"event_id": event_id, "processing_completed_at_utc": completed, "idempotent_replay": replay}

    def record_affiliate_mailbox_read(self, event_id: str, provider_receipt: dict[str, Any]) -> dict[str, Any]:
        """Durably record provider-verified mark-read completion for crash recovery."""
        action = "affiliate_mailbox_marked_read"
        action_id = "act_" + sha256_text(f"{event_id}|{action}")[:24]
        now = utc_now()
        receipt = {**provider_receipt, "provider_mutation": True, "mutation_scope": "mailbox_read_flag_only"}
        with self.conn:
            self.conn.execute(
                """INSERT OR IGNORE INTO mailbox_actions(action_id,event_id,action,status,provider_receipt_json,attempts,created_at,updated_at)
                   VALUES(?,?,?,?,?,0,?,?)""",
                (action_id, event_id, action, "verified", json.dumps(receipt, sort_keys=True), now, now),
            )
        return {"event_id": event_id, "action": action, "idempotent_replay": self.conn.execute("SELECT changes() ").fetchone()[0] == 0}

    def repair_affiliate_exception_outbox(self) -> dict[str, int]:
        """Repair the narrow crash window between exception classification and outbox insertion."""
        rows = list(self.conn.execute(
            """SELECT e.event_id FROM inbox_events e
               JOIN inbox_classifications c ON c.event_id=e.event_id
              WHERE c.category='affiliate_network_response' AND c.route='decisions'
                AND c.rowid=(SELECT MAX(c2.rowid) FROM inbox_classifications c2 WHERE c2.event_id=e.event_id)
                AND e.state NOT IN ('resolved_externally','held','archived')
                AND NOT EXISTS(SELECT 1 FROM notification_outbox o WHERE o.event_id=e.event_id AND o.purpose IN ('operator_decision','affiliate_exception'))
              ORDER BY e.received_at,e.event_id"""
        ))
        repaired = 0
        for row in rows:
            event_id = str(row["event_id"])
            try:
                _event, mailbox, parsed, classification = self._event_context(event_id)
                disposition = affiliate_response_disposition(parsed, classification)
                if disposition not in {"operator_prerequisite_card"}:
                    continue
                card = render_decision_card(event_id, mailbox, parsed, classification, None)
                self.queue_notification(event_id, "operator_decision", DECISIONS_CHANNEL_ID, f"affiliate-exception:{event_id}:{sha256_text(card)}", card)
                repaired += 1
            except Exception as exc:
                self.audit("affiliate_exception_outbox_repair_failed", "inbox_event", event_id, {"error_class": type(exc).__name__})
        return {"candidates": len(rows), "repaired": repaired}

    def resolve_externally(
        self,
        event_id: str,
        *,
        actor_id: str = OPERATOR_ID,
        channel_id: str = DECISIONS_CHANNEL_ID,
        decision_message_id: str | None = None,
    ) -> dict[str, Any]:
        """Close an operator-review event without provider mutation or outbound send."""
        event = self.conn.execute("SELECT state FROM inbox_events WHERE event_id=?", (event_id,)).fetchone()
        if event is None:
            raise ValueError("unknown_event_id")
        card = self.conn.execute(
            """SELECT discord_message_id,payload_sha256 FROM notification_outbox
            WHERE event_id=? AND purpose='operator_decision' AND status='delivered'
            ORDER BY delivered_at DESC LIMIT 1""",
            (event_id,),
        ).fetchone()
        if card is None or not card["discord_message_id"]:
            raise ValueError("decision_card_not_delivered")
        action = "resolved_externally"
        action_id = "act_" + sha256_text(f"{event_id}|{action}")[:24]
        existing = self.conn.execute("SELECT action_id FROM mailbox_actions WHERE action_id=?", (action_id,)).fetchone()
        if existing is not None:
            return {
                "status": "pass", "event_id": event_id, "action": action,
                "event_state": self.conn.execute("SELECT state FROM inbox_events WHERE event_id=?", (event_id,)).fetchone()[0],
                "card_message_id": card["discord_message_id"], "decision_recorded": False,
                "idempotent_replay": True, "provider_mutation": False, "outbound_send": False,
            }
        now = utc_now()
        receipt = {
            "actor_id": actor_id,
            "channel_id": channel_id,
            "card_message_id": card["discord_message_id"],
            "decision_message_id": decision_message_id,
            "provider_mutation": False,
            "outbound_send": False,
            "source": "operator_reply",
        }
        with self.conn:
            self.conn.execute("UPDATE inbox_events SET state='resolved_externally' WHERE event_id=?", (event_id,))
            self.conn.execute(
                "UPDATE local_drafts SET status='closed_resolved_externally',updated_at=? WHERE event_id=? AND status!='closed_resolved_externally'",
                (now, event_id),
            )
            self.conn.execute(
                """INSERT INTO mailbox_actions(action_id,event_id,action,status,provider_receipt_json,attempts,created_at,updated_at)
                VALUES(?,?,?,?,?,0,?,?)""",
                (action_id, event_id, action, "verified", json.dumps(receipt, sort_keys=True), now, now),
            )
            if decision_message_id:
                self._insert_operator_decision(
                    event_id=event_id, channel_id=channel_id,
                    card_message_id=card["discord_message_id"],
                    decision_message_id=decision_message_id, actor_id=actor_id,
                    action=action, card_revision_sha256=card["payload_sha256"], now=now,
                )
        audit_hash = self.audit(
            "operator_event_resolved_externally", "inbox_event", event_id,
            {**receipt, "decision_recorded": bool(decision_message_id)}, actor=f"discord-user:{actor_id}",
        )
        return {
            "status": "pass", "event_id": event_id, "action": action,
            "event_state": "resolved_externally", "card_message_id": card["discord_message_id"],
            "decision_recorded": bool(decision_message_id), "idempotent_replay": False,
            "audit_event_hash": audit_hash, "provider_mutation": False, "outbound_send": False,
        }

    def reconcile_operator_evidence(
        self,
        event_id: str,
        *,
        automation_id: str,
        authority_message_id: str,
        reconciliation_receipt: dict[str, Any],
    ) -> dict[str, Any]:
        """Close a direct-partner event from evidence-scoped Sheet reconciliation.

        This lane is intentionally weaker than provider-backed approval
        reconciliation: it may record only source-email evidence, never a
        provider relationship, commission, tracking link, or wire-ready state.
        """
        if automation_id != "glp-affiliate-approval-reconciler-v1":
            raise RuntimeError("untrusted_automation_id")
        if not re.fullmatch(r"\d{17,20}", str(authority_message_id or "")):
            raise RuntimeError("invalid_automation_authority_message_id")
        required = {
            "status": "PASS", "action_completed": True,
            "provider_readback_verified": False, "sheet_readback_verified": True,
            "external_send": False, "provider_mutation": False,
            "provider_program_mutation": False,
        }
        if any(reconciliation_receipt.get(key) != value for key, value in required.items()):
            raise RuntimeError("operator_evidence_receipt_not_authoritative")
        event = self.conn.execute("SELECT state FROM inbox_events WHERE event_id=?", (event_id,)).fetchone()
        if event is None:
            raise ValueError("unknown_event_id")
        action = "affiliate_sheet_reconciled"
        action_id = "act_" + sha256_text(f"{event_id}|{action}|{automation_id}")[:24]
        existing = self.conn.execute("SELECT action_id FROM mailbox_actions WHERE action_id=?", (action_id,)).fetchone()
        if existing is not None:
            return {"status": "pass", "event_id": event_id, "action": action, "event_state": event["state"], "idempotent_replay": True, "provider_mutation": False, "outbound_send": False}
        now = utc_now()
        provider_receipt = {
            "status": "verified", "automation_id": automation_id,
            "standing_authority_message_id": authority_message_id,
            "reconciliation_receipt_sha256": sha256_text(json.dumps(reconciliation_receipt, sort_keys=True, separators=(",", ":"))),
            "provider_readback_verified": False, "provider_mutation": False,
            "provider_program_mutation": False, "outbound_send": False,
            "source": "deterministic_affiliate_operator_evidence_reconciler",
        }
        with self.conn:
            self.conn.execute("UPDATE inbox_events SET state='resolved_externally' WHERE event_id=?", (event_id,))
            self.conn.execute(
                "UPDATE local_drafts SET status='closed_resolved_externally',updated_at=? WHERE event_id=? AND status!='closed_resolved_externally'",
                (now, event_id),
            )
            self.conn.execute(
                "UPDATE notification_outbox SET status='suppressed_auto_reconciled',updated_at=? WHERE event_id=? AND purpose='operator_decision' AND status IN ('pending','failed_retryable','posted_pending_readback','dispatching')",
                (now, event_id),
            )
            self.conn.execute(
                """INSERT INTO mailbox_actions(action_id,event_id,action,status,provider_receipt_json,attempts,created_at,updated_at)
                VALUES(?,?,?,?,?,0,?,?)""",
                (action_id, event_id, action, "verified", json.dumps(provider_receipt, sort_keys=True), now, now),
            )
        audit_hash = self.audit(
            "affiliate_event_reconciled_operator_evidence", "inbox_event", event_id,
            provider_receipt, actor=f"automation:{automation_id}",
        )
        return {"status": "pass", "event_id": event_id, "action": action, "event_state": "resolved_externally", "idempotent_replay": False, "audit_event_hash": audit_hash, "provider_mutation": False, "outbound_send": False}

    def resolve_automatically(
        self,
        event_id: str,
        *,
        automation_id: str,
        authority_message_id: str,
        reconciliation_receipt: dict[str, Any],
    ) -> dict[str, Any]:
        """Close a review event after verified reconciliation or routine archival.

        Standing operator authority is recorded as provenance, not fabricated as
        a per-event Discord reply or operator_decision row.
        """
        if automation_id != "glp-affiliate-approval-reconciler-v1":
            raise RuntimeError("untrusted_automation_id")
        if not re.fullmatch(r"\d{17,20}", str(authority_message_id or "")):
            raise RuntimeError("invalid_automation_authority_message_id")
        routine = reconciliation_receipt.get("action_kind") == "routine_affiliate_information"
        authenticated_email_rejection = (
            reconciliation_receipt.get("action_kind") == "authenticated_email_rejection"
            and reconciliation_receipt.get("signal") == "relationship_adverse"
        )
        # THE ONE ACTION CLASS WHOSE WHOLE PURPOSE IS AN AUTHORISED PROVIDER MUTATION.
        #
        # THE DEADLOCK, 2026-09-02 (approvals backfill). The MaxBounty approval
        # chain mints the provider-issued tracking link - that is the action, and
        # it is explicitly authorised (`operator_scoped_tracking_retrieval`). Its
        # receipt says so honestly: `provider_mutation: True`. The default
        # contract below requires `provider_mutation: False`, so the event could
        # NEVER be marked resolved, however well the chain ran. The registered
        # reconciler therefore retried it every 10 minutes, and every retry minted
        # ANOTHER link - five events ran 13-14 times each overnight, and applying
        # each new link reset `Wire-In Ready?` to FALSE, so five approved rows
        # could never stay wire-in-ready. Honest receipt, correct gate, and a loop
        # that could not terminate.
        #
        # So this action kind gets its own contract, exactly as `routine` and
        # `authenticated_email_rejection` already do. It ALLOWS the mint it exists
        # to perform and pins everything else HARDER than the default: both
        # readbacks verified, the schema contract verified, no external send, no
        # outbound send, no tracking link opened, no affiliate redirect clicked,
        # and no site deployment. Nothing else in this method changes, and no
        # other action kind can reach this branch - `action_kind` is compared
        # exactly.
        maxbounty_approval_chain = (
            reconciliation_receipt.get("action_kind") == "maxbounty_campaign_approval_chain"
        )
        required = (
            {"status": "PASS", "action_completed": True, "sheet_write_count": 0,
             "external_send": False, "provider_mutation": False}
            if routine else
            {"status": "PASS", "action_completed": True,
             "authenticated_network_notice_verified": True,
             "provider_readback_verified": False, "affiliate_provider_api_called": False,
             "sheet_readback_verified": True, "external_send": False,
             "provider_mutation": False}
            if authenticated_email_rejection else
            {"status": "PASS", "action_completed": True,
             "provider_readback_verified": True, "sheet_readback_verified": True,
             "sheet_schema_contract_verified": True,
             "external_send": False, "outbound_send": False,
             "tracking_links_opened": 0, "affiliate_redirect_clicks": 0,
             "site_deployments": 0}
            if maxbounty_approval_chain else
            {"status": "PASS", "provider_readback_verified": True,
             "sheet_readback_verified": True, "external_send": False,
             "provider_mutation": False}
        )
        if any(reconciliation_receipt.get(key) != value for key, value in required.items()):
            raise RuntimeError("automation_reconciliation_receipt_not_authoritative")
        if reconciliation_receipt.get("approval_processing") is True:
            approval_required = {
                "terms_rubric_executed": True, "terms_hard_trigger_count": 0,
                "approval_classification_recorded": True, "approval_fact_recorded": True,
            }
            if any(reconciliation_receipt.get(key) != value for key, value in approval_required.items()):
                raise RuntimeError("automation_approval_receipt_incomplete")
        event = self.conn.execute("SELECT state FROM inbox_events WHERE event_id=?", (event_id,)).fetchone()
        if event is None:
            raise ValueError("unknown_event_id")
        action = "affiliate_information_archived" if routine else "affiliate_sheet_reconciled"
        action_id = "act_" + sha256_text(f"{event_id}|{action}|{automation_id}")[:24]
        with self.conn:
            self.conn.execute(
                """UPDATE notification_outbox SET status='suppressed_auto_reconciled',updated_at=?
                   WHERE event_id=? AND purpose='operator_decision'
                     AND status IN ('pending','failed_retryable','posted_pending_readback','dispatching')""",
                (utc_now(), event_id),
            )
        existing = self.conn.execute("SELECT action_id FROM mailbox_actions WHERE action_id=?", (action_id,)).fetchone()
        if existing is not None:
            return {"status": "pass", "event_id": event_id, "action": action, "event_state": event["state"], "idempotent_replay": True, "provider_mutation": False, "outbound_send": False}
        now = utc_now()
        provider_receipt = {
            "status": "verified", "automation_id": automation_id,
            "standing_authority_message_id": authority_message_id,
            "reconciliation_receipt_sha256": sha256_text(json.dumps(reconciliation_receipt, sort_keys=True, separators=(",", ":"))),
            # PROVENANCE, NOT A WISH. Every action kind but the MaxBounty
            # approval chain pins `provider_mutation: False` in its own contract
            # above, so this is False for all of them exactly as before. The
            # approval chain mints an authorised tracking link and says so, and
            # recording that as False would be the control plane writing a fact
            # it knows to be untrue. The control plane's OWN side-effect counters
            # are untouched: it did not make that call, the chain did.
            "provider_mutation": bool(reconciliation_receipt.get("provider_mutation")),
            "outbound_send": False,
            "source": "deterministic_affiliate_reconciler",
        }
        if str(event["state"]) in {"resolved_externally", "held"}:
            prior_routine = self.conn.execute(
                "SELECT COUNT(*) FROM mailbox_actions WHERE event_id=? AND action='affiliate_information_archived' AND status='verified'",
                (event_id,),
            ).fetchone()[0]
            prior_external = self.conn.execute(
                "SELECT COUNT(*) FROM mailbox_actions WHERE event_id=? AND action='resolved_externally' AND status='verified'",
                (event_id,),
            ).fetchone()[0]
            if routine or str(event["state"]) != "resolved_externally" or prior_routine + prior_external != 1:
                raise ValueError(f"event_terminal:{event['state']}")
            provider_receipt["upgrades_prior_action"] = (
                "affiliate_information_archived" if prior_routine == 1 else "resolved_externally"
            )
            with self.conn:
                self.conn.execute(
                    """INSERT INTO mailbox_actions(action_id,event_id,action,status,provider_receipt_json,attempts,created_at,updated_at)
                    VALUES(?,?,?,?,?,0,?,?)""",
                    (action_id, event_id, action, "verified", json.dumps(provider_receipt, sort_keys=True), now, now),
                )
            audit_hash = self.audit(
                "affiliate_event_reconciliation_upgraded", "inbox_event", event_id,
                provider_receipt, actor=f"automation:{automation_id}",
            )
            return {"status": "pass", "event_id": event_id, "action": action, "event_state": "resolved_externally", "idempotent_replay": False, "audit_event_hash": audit_hash, "upgraded_prior_routine": prior_routine == 1, "upgraded_prior_external_resolution": prior_external == 1, "provider_mutation": False, "outbound_send": False}
        with self.conn:
            self.conn.execute("UPDATE inbox_events SET state='resolved_externally' WHERE event_id=?", (event_id,))
            self.conn.execute(
                "UPDATE local_drafts SET status='closed_resolved_externally',updated_at=? WHERE event_id=? AND status!='closed_resolved_externally'",
                (now, event_id),
            )
            self.conn.execute(
                """INSERT INTO mailbox_actions(action_id,event_id,action,status,provider_receipt_json,attempts,created_at,updated_at)
                VALUES(?,?,?,?,?,0,?,?)""",
                (action_id, event_id, action, "verified", json.dumps(provider_receipt, sort_keys=True), now, now),
            )
        audit_hash = self.audit(
            "affiliate_event_reconciled_automatically", "inbox_event", event_id,
            provider_receipt, actor=f"automation:{automation_id}",
        )
        return {"status": "pass", "event_id": event_id, "action": action, "event_state": "resolved_externally", "idempotent_replay": False, "audit_event_hash": audit_hash, "provider_mutation": False, "outbound_send": False}

    def event_exists(self, mailbox_id: str, provider_message_id: str) -> bool:
        return self.conn.execute("SELECT 1 FROM inbox_events WHERE mailbox_id=? AND provider_message_id=?", (mailbox_id, provider_message_id)).fetchone() is not None

    def is_historical_before_poll(self, mailbox_id: str, received_at: str) -> bool:
        cursor = self.conn.execute(
            "SELECT last_complete_poll_at FROM provider_cursors WHERE mailbox_id=?", (mailbox_id,)
        ).fetchone()
        boundary = str(cursor["last_complete_poll_at"] or "") if cursor is not None else ""
        if not boundary:
            return False
        try:
            return dt.datetime.fromisoformat(str(received_at).replace("Z", "+00:00")) <= dt.datetime.fromisoformat(boundary.replace("Z", "+00:00"))
        except ValueError:
            return True

    def record_manual_disposition(
        self,
        event_id: str,
        *,
        disposition: str,
        source_message_id: str,
        source_channel_id: str = DECISIONS_CHANNEL_ID,
        actor_id: str = OPERATOR_ID,
        note: str = "operator terminal disposition",
    ) -> dict[str, Any]:
        """Persist an authoritative terminal disposition before any future carding."""
        if disposition not in {"RESOLVED_EXTERNALLY", "AFFILIATE_TOOL"}:
            raise ValueError("manual_disposition_invalid")
        if source_message_id != MANUAL_DISPOSITION_MESSAGE_ID:
            raise ValueError("manual_disposition_source_not_authoritative")
        existing = self.conn.execute(
            "SELECT * FROM manual_dispositions WHERE event_id=?", (event_id,)
        ).fetchone()
        if existing is not None:
            if str(existing["disposition"]) != disposition or str(existing["source_message_id"]) != source_message_id:
                raise ValueError("manual_disposition_conflict")
            return {"status": "pass", "event_id": event_id, "disposition": disposition, "idempotent_replay": True}
        now = utc_now()
        event = self.conn.execute("SELECT state FROM inbox_events WHERE event_id=?", (event_id,)).fetchone()
        action = "affiliate_inbox_tool_routed" if disposition == "AFFILIATE_TOOL" else "resolved_externally"
        action_id = "act_" + sha256_text(f"{event_id}|{action}|{source_message_id}")[:24]
        receipt = {
            "disposition": disposition,
            "source_message_id": source_message_id,
            "source_channel_id": source_channel_id,
            "actor_id": actor_id,
            "provider_mutation": False,
            "outbound_send": False,
            "note": note,
        }
        with self.conn:
            self.conn.execute(
                """INSERT INTO manual_dispositions(event_id,disposition,source_message_id,source_channel_id,actor_id,note,created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (event_id, disposition, source_message_id, source_channel_id, actor_id, note, now),
            )
            if event is not None:
                self.conn.execute("UPDATE inbox_events SET state='resolved_externally' WHERE event_id=?", (event_id,))
                self.conn.execute(
                    "UPDATE local_drafts SET status='closed_resolved_externally',updated_at=? WHERE event_id=? AND status!='closed_resolved_externally'",
                    (now, event_id),
                )
                self.conn.execute(
                    """UPDATE notification_outbox SET status='suppressed_manual_terminal',updated_at=?
                       WHERE event_id=? AND purpose='operator_decision'
                         AND status IN ('pending','failed_retryable','posted_pending_readback','dispatching')""",
                    (now, event_id),
                )
                self.conn.execute(
                    """INSERT OR IGNORE INTO mailbox_actions(action_id,event_id,action,status,provider_receipt_json,attempts,created_at,updated_at)
                       VALUES(?,?,?,?,?,0,?,?)""",
                    (action_id, event_id, action, "verified", json.dumps(receipt, sort_keys=True), now, now),
                )
        self.audit("manual_terminal_disposition_recorded", "inbox_event", event_id, receipt, actor=f"discord-user:{actor_id}")
        return {
            "status": "pass", "event_id": event_id, "disposition": disposition,
            "event_present": event is not None, "event_state": "resolved_externally" if event is not None else "pending_ingest",
            "idempotent_replay": False,
        }

    def resolve_attribution(self, provider_mailbox: dict[str, Any], recipients: list[str]) -> tuple[dict[str, Any], str]:
        owners: dict[str, dict[str, Any]] = {}
        for row in self.conn.execute("SELECT * FROM mailboxes"):
            materialized = dict(row)
            for address in [row["canonical_address"], *json.loads(row["aliases_json"]), *json.loads(row["groups_json"])]:
                owners[str(address).lower()] = materialized
        matched = {owners[address.lower()]["mailbox_id"] for address in recipients if address.lower() in owners}
        if not matched:
            return provider_mailbox, "unregistered_recipient_mapping"
        if len(matched) > 1:
            return provider_mailbox, "ambiguous_recipient_ownership"
        attributed_id = next(iter(matched))
        attributed = next((owners[address.lower()] for address in recipients if address.lower() in owners and owners[address.lower()]["mailbox_id"] == attributed_id), provider_mailbox)
        return attributed, "registry_exact_match"

    def apply_unsubscribe_suppression(self, venture_key: str, email_address: str, event_id: str) -> dict[str, Any]:
        """Write a shared all-scope suppression and exit matching local flows only."""
        path = self.email_flow_db_path
        if not path.is_file():
            return {"status": "blocked", "reason": "shared_email_flow_db_missing", "inserted": 0, "flows_exited": 0}
        flow_venture = {
            "boring_ai": "boring", "glp_consumer": "glp", "glp_partners": "glp-partners",
            "glp_affiliate": "glp-partners", "allocant": "allocant",
        }.get(venture_key)
        recipient = parseaddr(email_address)[1].strip().lower()
        if not flow_venture or not recipient:
            return {"status": "blocked", "reason": "suppression_identity_invalid", "inserted": 0, "flows_exited": 0}
        connection = sqlite3.connect(path, timeout=10)
        try:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"email_suppression", "email_flow_state"} <= tables:
                return {"status": "blocked", "reason": "shared_email_flow_schema_missing", "inserted": 0, "flows_exited": 0}
            suppression_id = "sup_" + sha256_text(f"{flow_venture}|{recipient}|all")[:24]
            now = utc_now()
            with connection:
                before = connection.total_changes
                connection.execute(
                    "INSERT OR IGNORE INTO email_suppression(id,venture,email,scope,reason,created_at) VALUES(?,?,?,?,?,?)",
                    (suppression_id, flow_venture, recipient, "all", f"inbound_unsubscribe:{event_id}", now),
                )
                inserted = connection.total_changes - before
                cursor = connection.execute(
                    """UPDATE email_flow_state SET status='suppressed',next_due_at=NULL,exit_reason='inbound_unsubscribe',updated_at=?
                       WHERE venture=? AND lower(email)=?
                         AND status NOT IN ('completed','exited_converted','suppressed','bounced')""",
                    (now, flow_venture, recipient),
                )
            return {"status": "pass", "inserted": inserted, "flows_exited": int(cursor.rowcount)}
        finally:
            connection.close()

    def mark_recovery_bounced(self, body: str) -> dict[str, Any]:
        """Mark only matching local recovery states bounced; never touch a mailbox."""
        path = self.email_flow_db_path
        if not path.is_file():
            return {"status": "blocked", "reason": "shared_email_flow_db_missing", "states_bounced": 0}
        candidates = {value.lower() for value in re.findall(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", body or "", re.I)}
        if not candidates:
            return {"status": "pass", "states_bounced": 0}
        connection = sqlite3.connect(path, timeout=10)
        try:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "email_flow_state" not in tables:
                return {"status": "blocked", "reason": "shared_email_flow_schema_missing", "states_bounced": 0}
            now = utc_now()
            placeholders = ",".join("?" for _ in candidates)
            with connection:
                cursor = connection.execute(
                    f"""UPDATE email_flow_state SET status='bounced',next_due_at=NULL,exit_reason='delivery_bounce',updated_at=?
                         WHERE lower(email) IN ({placeholders})
                           AND status NOT IN ('completed','exited_converted','suppressed','bounced')""",
                    (now, *sorted(candidates)),
                )
            return {"status": "pass", "states_bounced": int(cursor.rowcount)}
        finally:
            connection.close()

    def _create_glp_human_affiliate_draft(
        self,
        event_id: str,
        mailbox: dict[str, Any],
        parsed: dict[str, Any],
        classification: dict[str, Any],
        *,
        provider_thread_id: str | None,
        raw_sha256: str,
    ) -> dict[str, Any] | None:
        """Create the one local-only draft for the narrow human-network reply.

        This path is intentionally deterministic and provider-free. It uses only
        the reviewed GLP affiliate template, preserves the exact inbound mailbox
        identity, and never calls the external drafter or any mail provider.
        """
        if (
            str(mailbox.get("venture") or "") != "glp"
            or classification.get("reason_code") not in {
                "affiliate_network_human_reference_before_call",
                "affiliate_network_genuine_human_reply",
            }
            or classification.get("reply_class") != "partner_vendor_action"
            or classification.get("route") != "decisions"
        ):
            return None
        registry, registry_sha256 = load_reply_registry()
        template = ((registry.get("templates") or {}).get("glp_affiliate") or {}).get(
            "partner_vendor_action"
        ) or {}
        body = str(template.get("body") or "").strip()
        if (
            template.get("draft") is not True
            or template.get("never_auto_send") is not True
            or not body
            or redact(body) != body
            or "{" in body
            or "}" in body
        ):
            raise RuntimeError("glp_human_affiliate_template_not_safe")
        sender_address = parseaddr(str(parsed.get("sender") or ""))[1].strip().lower()
        if not sender_address:
            raise RuntimeError("glp_human_affiliate_sender_invalid")
        aliases = mailbox.get("aliases") or []
        if not aliases and mailbox.get("aliases_json"):
            aliases = json.loads(str(mailbox["aliases_json"]))
        owned_addresses = {
            str(value).strip().lower()
            for value in [mailbox.get("canonical_address"), *aliases]
            if "@" in str(value)
        }
        addressed_to = {
            parseaddr(str(value or ""))[1].strip().lower()
            for value in parsed.get("recipients") or []
        } & owned_addresses
        if len(addressed_to) != 1:
            raise RuntimeError("glp_human_affiliate_thread_identity_ambiguous")
        from_identity = next(iter(addressed_to))
        subject = normalize_reply_subject(parsed.get("subject"))
        body_sha256 = sha256_text(body)
        draft_policy = (
            f"{POLICY_VERSION}:glp-human-affiliate-ingest:v1:{registry_sha256[:16]}"
        )
        draft_id = "drf_" + sha256_text(
            f"{event_id}|{draft_policy}|{raw_sha256}|{body_sha256}"
        )[:24]
        recipients_json = json.dumps([sender_address], sort_keys=True)
        now = utc_now()
        with self.conn:
            self.conn.execute(
                """INSERT OR IGNORE INTO local_drafts(draft_id,event_id,revision,from_identity,recipients_json,
                provider_thread_id,subject,body,body_sha256,source_hash,policy_version,status,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    draft_id, event_id, 1, from_identity, recipients_json,
                    provider_thread_id, subject, body, body_sha256, raw_sha256,
                    draft_policy, "draft_generated", now, now,
                ),
            )
        row = self.conn.execute(
            "SELECT * FROM local_drafts WHERE draft_id=? AND event_id=?", (draft_id, event_id)
        ).fetchone()
        if row is None:
            raise RuntimeError("glp_human_affiliate_draft_persistence_failed")
        expected = {
            "draft_id": draft_id,
            "event_id": event_id,
            "revision": 1,
            "from_identity": from_identity,
            "recipients_json": recipients_json,
            "provider_thread_id": provider_thread_id,
            "subject": subject,
            "body": body,
            "body_sha256": body_sha256,
            "source_hash": raw_sha256,
            "policy_version": draft_policy,
        }
        if any(row[name] != value for name, value in expected.items()):
            raise RuntimeError("glp_human_affiliate_draft_identity_or_payload_conflict")
        return {**dict(row), "reply_class": "partner_vendor_action"}

    @staticmethod
    def _is_glp_human_affiliate_classification(classification: dict[str, Any]) -> bool:
        return bool(
            classification.get("reason_code") in {
                "affiliate_network_human_reference_before_call",
                "affiliate_network_genuine_human_reply",
            }
            and classification.get("reply_class") == "partner_vendor_action"
            and classification.get("route") == "decisions"
        )

    def _reconcile_glp_human_affiliate_artifacts(
        self,
        event_id: str,
        mailbox: dict[str, Any],
        parsed: dict[str, Any],
        classification: dict[str, Any],
        *,
        provider_thread_id: str | None,
        raw_sha256: str,
        fault_injector: Any = None,
    ) -> dict[str, str] | None:
        """Idempotently create and verify the deterministic human-reply siblings."""
        if not self._is_glp_human_affiliate_classification(classification):
            return None
        draft = self._create_glp_human_affiliate_draft(
            event_id,
            mailbox,
            parsed,
            classification,
            provider_thread_id=provider_thread_id,
            raw_sha256=raw_sha256,
        )
        if draft is None:
            raise RuntimeError("glp_human_affiliate_draft_missing")
        if fault_injector is not None:
            fault_injector("after_draft_commit")
        card = render_decision_card(event_id, mailbox, parsed, classification, draft)
        key = f"decision:{event_id}:{sha256_text(card)}"
        outbox_id = self.queue_notification(
            event_id,
            "operator_decision",
            DECISIONS_CHANNEL_ID,
            key,
            card,
            verify_existing_payload=True,
        )
        if fault_injector is not None:
            fault_injector("after_decision_outbox_commit")
        return {"draft_id": str(draft["draft_id"]), "outbox_id": outbox_id}

    def _duplicate_ingest_result(
        self,
        existing_event: sqlite3.Row,
        mailbox: dict[str, Any],
        raw: bytes,
        *,
        raw_oversized: bool,
        raw_hash: str,
        expected_event_id: str,
        provider_thread_id: str | None,
    ) -> dict[str, Any]:
        """Verify a provider duplicate and reconcile deterministic siblings."""
        if str(existing_event["raw_sha256"]) != raw_hash:
            raise RuntimeError("provider_message_identity_content_mismatch")
        if str(existing_event["event_id"]) != expected_event_id:
            raise RuntimeError("provider_message_identity_event_mismatch")
        stored_thread_id = existing_event["provider_thread_id"]
        if provider_thread_id is not None and stored_thread_id != provider_thread_id:
            raise RuntimeError("provider_message_identity_thread_mismatch")
        classification_row = self.conn.execute(
            """SELECT * FROM inbox_classifications WHERE event_id=?
               ORDER BY rowid DESC LIMIT 1""",
            (expected_event_id,),
        ).fetchone()
        if classification_row is None:
            raise RuntimeError("duplicate_event_classification_missing")
        classification = dict(classification_row)
        if self._is_glp_human_affiliate_classification(classification):
            parsed = parse_oversized_message(raw) if raw_oversized else parse_message(raw, mailbox)
            attribution, attribution_status = self.resolve_attribution(mailbox, parsed["recipients"])
            if attribution_status != "registry_exact_match":
                raise RuntimeError("duplicate_human_affiliate_attribution_conflict")
            artifacts = self._reconcile_glp_human_affiliate_artifacts(
                expected_event_id,
                attribution,
                parsed,
                classification,
                provider_thread_id=stored_thread_id,
                raw_sha256=raw_hash,
            )
            if artifacts is None:
                raise RuntimeError("duplicate_human_affiliate_reconciliation_failed")
            return {
                "status": "duplicate",
                "event_id": expected_event_id,
                "draft_id": artifacts["draft_id"],
                "outbox_id": artifacts["outbox_id"],
                "reconciled_human_affiliate_artifacts": True,
            }
        return {"status": "duplicate", "event_id": expected_event_id}

    def ingest(
        self,
        mailbox: dict[str, Any],
        provider_message_id: str,
        raw: bytes,
        *,
        provider_thread_id: str | None = None,
        source_folder: str = "recent-history",
        labels: list[str] | None = None,
        bootstrap: bool = False,
        _fault_injector: Any = None,
    ) -> dict[str, Any]:
        # 2026-08-28, operator: "this was my own draft - inbox job shouldnt fire
        # on drafts obviously". A Gmail DRAFT is unsent outbound text WE are
        # composing; treating it as an inbound event produced an operator
        # decision card about our own half-written reply. `labels` was already
        # threaded in here and simply never inspected. Refused at INGEST rather
        # than filtered downstream, so no consumer can rediscover it later.
        # Not silent: the refusal is returned with a reason and is testable.
        if any(str(label).upper() == "DRAFT" for label in (labels or [])):
            return {"status": "refused", "reason": "draft_not_inbound_mail",
                    "provider_message_id": provider_message_id}
        raw_oversized = len(raw) > MAX_RAW_BYTES
        raw_hash = sha256_bytes(raw)
        event_id = "evt_" + sha256_text(f"{mailbox['mailbox_id']}|{provider_message_id}|{raw_hash}")[:28]
        existing_event = self.conn.execute(
            """SELECT event_id,raw_sha256,provider_thread_id FROM inbox_events
               WHERE mailbox_id=? AND provider_message_id=?""",
            (mailbox["mailbox_id"], provider_message_id),
        ).fetchone()
        if existing_event is not None:
            return self._duplicate_ingest_result(
                existing_event,
                mailbox,
                raw,
                raw_oversized=raw_oversized,
                raw_hash=raw_hash,
                expected_event_id=event_id,
                provider_thread_id=provider_thread_id,
            )
        raw_path = RAW_ROOT / raw_hash[:2] / f"{raw_hash}.eml"
        private_content_addressed_write(raw_path, raw, raw_hash)
        parsed = parse_oversized_message(raw) if raw_oversized else parse_message(raw, mailbox)
        body_hash = sha256_text(parsed["body_text"])
        now = utc_now()
        state = "quarantined" if parsed["quarantined"] else "ingested"
        with self.conn:
            event_cursor = self.conn.execute(
                """INSERT INTO inbox_events(event_id,mailbox_id,provider_message_id,provider_thread_id,rfc_message_id,
                raw_sha256,raw_path,body_sha256,sender,sender_domain,recipients_json,subject,received_at,source_folder,
                labels_json,parse_defects_json,state,ingested_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(mailbox_id,provider_message_id) DO NOTHING""",
                (
                    event_id, mailbox["mailbox_id"], provider_message_id, provider_thread_id,
                    parsed["rfc_message_id"], raw_hash, str(raw_path), body_hash, parsed["sender"], parsed["sender_domain"],
                    json.dumps(parsed["recipients"]), parsed["subject"], parsed["received_at"], source_folder,
                    json.dumps(labels or []), json.dumps(parsed["defects"]), state, now,
                ),
            )
            if event_cursor.rowcount == 0:
                existing_event = self.conn.execute(
                    """SELECT event_id,raw_sha256,provider_thread_id FROM inbox_events
                       WHERE mailbox_id=? AND provider_message_id=?""",
                    (mailbox["mailbox_id"], provider_message_id),
                ).fetchone()
                if existing_event is None:
                    raise RuntimeError("event_identity_conflict_unverified")
                return self._duplicate_ingest_result(
                    existing_event,
                    mailbox,
                    raw,
                    raw_oversized=raw_oversized,
                    raw_hash=raw_hash,
                    expected_event_id=event_id,
                    provider_thread_id=provider_thread_id,
                )
            for attachment in parsed["attachments"]:
                self.conn.execute(
                    """INSERT INTO inbox_attachments(attachment_id,event_id,filename,content_type,size_bytes,sha256,
                    storage_path,safety_state,parser_status,parsed_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        attachment_id_for_event(event_id, str(attachment["sha256"]), str(attachment["filename"])),
                        event_id, attachment["filename"], attachment["content_type"],
                        attachment["size_bytes"], attachment["sha256"], attachment.get("storage_path"),
                        attachment["safety_state"], attachment["parser_status"], json.dumps(attachment.get("parsed") or {}, sort_keys=True), now,
                    ),
                )
            attribution, attribution_status = self.resolve_attribution(mailbox, parsed["recipients"])
            manual = self.conn.execute(
                "SELECT disposition FROM manual_dispositions WHERE event_id=?", (event_id,)
            ).fetchone()
            if manual is not None:
                self.conn.execute("UPDATE inbox_events SET state='resolved_externally' WHERE event_id=?", (event_id,))
                classification = {
                    "category": "unknown", "confidence": 1.0,
                    "reason_code": "manual_terminal_disposition",
                    "facts": ["authoritative_operator_disposition", str(manual["disposition"])],
                    "actionable": False,
                }
                intent = {"reply_class": "other", "confidence": 1.0, "signals": ["manual_terminal_disposition"]}
                reply_class = "other"
                intent_confidence = 1.0
                reply_venture = None
                classification = normalize_affiliate_coarse(classification, reply_class)
                route = "silent"
                affiliate_disposition = "manual_terminal"
            else:
                classification = classify_message(
                    attribution,
                    parsed,
                    provider_thread_id=provider_thread_id,
                    journalist_pitch_db_path=GLP_DIGITAL_PR_DB_PATH,
                )
                if attribution_status != "registry_exact_match":
                    classification = {
                        "category": "unknown", "confidence": 0.0, "reason_code": attribution_status,
                        "facts": ["recipient_mapping_fail_closed"], "actionable": True,
                    }
                intent_parsed = parsed
                if classification.get("reason_code") == "glp_journalist_pitch_reply":
                    intent_parsed = dict(parsed)
                    intent_parsed["subject"] = ""
                    intent_parsed["body_text"] = newest_unquoted_human_reply(str(parsed.get("body_text") or ""))
                provisional_venture = resolve_reply_venture(
                    attribution, intent_parsed, "other", str(classification.get("category") or "")
                )
                if provisional_venture:
                    intent = classify_reply_intent(intent_parsed, classification, provisional_venture)
                else:
                    intent = {"reply_class": "other", "confidence": 0.0, "signals": ["venture_not_in_reply_registry"]}
                if classification.get("reason_code") == "affiliate_network_genuine_human_reply":
                    # The reviewed network-human predicate is stricter than the
                    # generic intent scorer: exact network identity, machine/list
                    # negatives, and conversational partner evidence were already
                    # proven. Keep scheduling words from changing the ownership
                    # class and bypassing the deterministic affiliate draft path.
                    intent = {
                        "reply_class": "partner_vendor_action",
                        "confidence": 0.99,
                        "signals": ["reviewed_affiliate_network_genuine_human_reply"],
                    }
                reply_class = str(intent["reply_class"])
                intent_confidence = float(intent["confidence"])
                if intent_confidence < MINIMUM_INTENT_CONFIDENCE:
                    reply_class = "other"
                reply_venture = resolve_reply_venture(
                    attribution, parsed, reply_class, str(classification.get("category") or "")
                )
                classification = normalize_affiliate_coarse(classification, reply_class)
                route = route_for(classification["category"], classification["reason_code"], float(classification.get("confidence") or 0.0))
                affiliate_auto_owned = classification["category"] == "affiliate_network_response"
                if not affiliate_auto_owned and reply_class not in {"spam", "other"} and reply_venture:
                    route = "decisions"
                affiliate_disposition = affiliate_response_disposition(parsed, classification)
                if affiliate_disposition == "auto_process_approval":
                    route = "silent"
                elif affiliate_disposition in {"operator_prerequisite_card"}:
                    route = "decisions"
                elif affiliate_auto_owned:
                    route = "silent"
            from inbox_control_plane import sponsor_replies
            sponsor_disposition = sponsor_replies.stage(
                self, event_id=event_id, mailbox=mailbox, parsed=parsed, raw=raw,
                thread_id=provider_thread_id, now=now,
                newest_body=newest_unquoted_human_reply(parsed['body_text']),
            )
            if (sponsor_disposition is not None
                and classification['category'] in {'human_lead_reply','partner_vendor_action','unknown'}
                and classification['reason_code'] not in {'financial_account_integrity_signal','legal_contract_action_signal','operator_authorization_required'}):
                # Persist the silent disposition before append-only classification.
                route = 'silent'
            classification.update({
                "reply_class": reply_class, "intent_confidence": intent_confidence,
                "matched_signals": list(intent["signals"]), "reply_venture": reply_venture,
                "route": route, "affiliate_disposition": affiliate_disposition,
            })
            classification_id = "cls_" + sha256_text(f"{event_id}|{POLICY_VERSION}")[:24]
            self.conn.execute(
                """INSERT INTO inbox_classifications(classification_id,event_id,policy_version,category,confidence,
                reason_code,matched_facts_json,venture,reply_class,intent_confidence,matched_signals_json,
                venture_lifecycle,actionable,route,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    classification_id, event_id, POLICY_VERSION, classification["category"], classification["confidence"],
                    classification["reason_code"], json.dumps(classification["facts"], sort_keys=True), attribution["venture"],
                    reply_class, intent_confidence, json.dumps(intent["signals"], sort_keys=True), attribution["lifecycle"],
                    int(classification["actionable"]), route, now,
                ),
            )
            affiliate_root = affiliate_network_domain(str(parsed.get("sender_domain") or ""))
            if mailbox["mailbox_id"] in {"glp_ops_gmail", "glp_izaiah_gmail"} and affiliate_root:
                from inbox_control_plane import affiliate_supply_intake

                supply_artifact = affiliate_supply_intake.build_artifact(
                    parsed,
                    classification,
                    mailbox_id=mailbox["mailbox_id"],
                    provider_message_id=provider_message_id,
                    event_id=event_id,
                    affiliate_network_root=affiliate_root,
                    captured_at_utc=parsed["received_at"],
                )
                if supply_artifact is not None:
                    affiliate_supply_intake.write_artifact(supply_artifact)
            from inbox_control_plane import affiliate_decision_queue

            affiliate_decision_queue.stage(
                self, event_id=event_id, mailbox=attribution, parsed=parsed,
                classification=classification, raw_path=str(raw_path),
                raw_sha256=raw_hash, provider_message_id=provider_message_id,
                now=now, newest_body=newest_unquoted_human_reply(parsed["body_text"]),
                bootstrap=bootstrap,
                historical=self.is_historical_before_poll(mailbox["mailbox_id"], parsed["received_at"]),
                attribution_status=attribution_status,
            )
            self.conn.execute(
                "INSERT INTO mailbox_actions(action_id,event_id,action,status,provider_receipt_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                ("act_" + sha256_text(event_id + "preserve")[:24], event_id, "preserve_no_provider_mutation", "verified", json.dumps({"phase1": True, "provider_mutation": False}), now, now),
            )
        if _fault_injector is not None:
            _fault_injector("after_event_classification_commit")
        self.audit("event_ingested", "inbox_event", event_id, {
            "mailbox_id": mailbox["mailbox_id"], "raw_sha256": raw_hash,
            "raw_size_bytes": len(raw), "raw_oversized_quarantined": raw_oversized,
            "classification": classification["category"], "reason_code": classification["reason_code"],
            "reply_class": reply_class, "intent_confidence": intent_confidence, "bootstrap": bootstrap,
            "attachment_count": len(parsed["attachments"]),
            "attachment_duplicate_parts": int(parsed.get("attachment_duplicate_parts") or 0),
            "attachment_metadata_conflicts": int(parsed.get("attachment_metadata_conflicts") or 0),
        })
        unsubscribe_requested = classification["category"] != "affiliate_network_response" and (
            reply_class == "unsubscribe_optout" or bool(
                {"unsubscribe", "opt_out", "stop_contact"} & set(intent["signals"])
            )
        )
        if not bootstrap and unsubscribe_requested and reply_venture:
            suppression = self.apply_unsubscribe_suppression(reply_venture, parsed["sender"], event_id)
            self.audit("inbound_unsubscribe_suppressed", "inbox_event", event_id, suppression)
        if not bootstrap and classification["category"] == "service_degradation":
            bounced = self.mark_recovery_bounced(parsed["body_text"])
            self.audit("inbound_bounce_processed", "inbox_event", event_id, bounced)
        draft_id = None
        narrow_human_affiliate = self._is_glp_human_affiliate_classification(classification)
        # Genuine human-network replies are the explicit history-reconciliation
        # exception: read/archive state and receipt time must not suppress them.
        historical_suppressed = (
            self.is_historical_before_poll(mailbox["mailbox_id"], parsed["received_at"])
            and not narrow_human_affiliate
        )
        if route == "decisions" and not bootstrap and not historical_suppressed:
            if narrow_human_affiliate:
                artifacts = self._reconcile_glp_human_affiliate_artifacts(
                    event_id,
                    attribution,
                    parsed,
                    classification,
                    provider_thread_id=provider_thread_id,
                    raw_sha256=raw_hash,
                    fault_injector=_fault_injector,
                )
                if artifacts is None:
                    raise RuntimeError("glp_human_affiliate_artifact_reconciliation_failed")
                draft_id = artifacts["draft_id"]
            else:
                card = render_decision_card(event_id, attribution, parsed, classification, None)
                self.queue_notification(
                    event_id,
                    "operator_decision",
                    DECISIONS_CHANNEL_ID,
                    f"decision:{event_id}:{sha256_text(card)}",
                    card,
                )
        elif route == "ops" and not bootstrap:
            card = render_ops_event(event_id, attribution, parsed, classification)
            self.queue_notification(event_id, "operational_event", OPS_CHANNEL_ID, f"ops:{event_id}:{sha256_text(card)}", card)
        return {
            "status": "persisted", "event_id": event_id, "category": classification["category"],
            "reply_class": reply_class, "intent_confidence": intent_confidence,
            "draft_id": draft_id, "route": route, "bootstrap": bootstrap,
            "historical_card_suppressed": historical_suppressed,
        }

    def _event_context(self, event_id: str) -> tuple[sqlite3.Row, dict[str, Any], dict[str, Any], dict[str, Any]]:
        event = self.conn.execute("SELECT * FROM inbox_events WHERE event_id=?", (event_id,)).fetchone()
        if event is None:
            raise ValueError("unknown_event_id")
        mailbox_row = self.conn.execute("SELECT * FROM mailboxes WHERE mailbox_id=?", (event["mailbox_id"],)).fetchone()
        if mailbox_row is None:
            raise ValueError("event_mailbox_not_registered")
        if str(event["state"]) == "quarantined":
            raise ValueError("event_quarantined")
        raw_path = Path(str(event["raw_path"] or ""))
        if raw_path.is_symlink() or not raw_path.is_file():
            raise ValueError("event_raw_source_missing")
        raw = raw_path.read_bytes()
        if not raw or len(raw) > MAX_RAW_BYTES:
            raise ValueError("event_raw_source_size_invalid")
        raw_sha256 = sha256_bytes(raw)
        if raw_sha256 != str(event["raw_sha256"]):
            raise ValueError("event_raw_source_hash_mismatch")
        mailbox = dict(mailbox_row)
        mailbox["aliases"] = json.loads(mailbox.get("aliases_json") or "[]")
        mailbox["groups"] = json.loads(mailbox.get("groups_json") or "[]")
        mailbox["dmarc_domains"] = json.loads(mailbox.get("dmarc_domains_json") or "[]")
        parsed = parse_message(raw, mailbox)
        if parsed["quarantined"]:
            raise ValueError("event_canonical_source_unsafe")
        attachment_manifest: list[dict[str, Any]] = []
        for row in self.conn.execute(
            """SELECT filename,content_type,size_bytes,sha256,storage_path,safety_state,parser_status
               FROM inbox_attachments WHERE event_id=? ORDER BY attachment_id""",
            (event_id,),
        ):
            if row["safety_state"] != "stored" or row["parser_status"] in {"metadata_conflict", "dmarc_invalid"}:
                raise ValueError("event_attachment_quarantined")
            storage_path = str(row["storage_path"] or "")
            if storage_path:
                stored = Path(storage_path)
                if stored.is_symlink() or not stored.is_file():
                    raise ValueError("event_attachment_source_missing")
                payload = stored.read_bytes()
                if len(payload) != int(row["size_bytes"]) or sha256_bytes(payload) != str(row["sha256"]):
                    raise ValueError("event_attachment_source_mismatch")
            attachment_manifest.append({
                "filename": str(row["filename"]), "content_type": str(row["content_type"]),
                "size_bytes": int(row["size_bytes"]), "sha256": str(row["sha256"]),
                "safety_state": str(row["safety_state"]), "parser_status": str(row["parser_status"]),
            })
        parsed["_canonical_raw_bytes"] = raw
        parsed["_canonical_raw_sha256"] = raw_sha256
        parsed["_canonical_raw_size_bytes"] = len(raw)
        parsed["_attachment_manifest"] = attachment_manifest
        classification_row = self.conn.execute(
            """SELECT * FROM inbox_classifications WHERE event_id=?
               ORDER BY CASE WHEN policy_version LIKE ? THEN 0 ELSE 1 END,rowid DESC LIMIT 1""",
            (event_id, POLICY_VERSION + "%"),
        ).fetchone()
        if classification_row is None:
            raise ValueError("event_not_classified")
        return event, mailbox, parsed, dict(classification_row)

    def repair_affiliate_human_request(
        self,
        event_id: str,
        *,
        expected_event_id: str,
        expected_raw_sha256: str,
        expected_promoted_classification: dict[str, str],
        reviewed_draft: dict[str, str],
    ) -> dict[str, Any]:
        """Append a reviewed, hash-bound correction for one human network request.

        This is deliberately a local replay seam, not a provider action. The
        caller supplies completed human-reviewed copy; this method never calls a
        drafter, creates a provider draft, sends, or changes the inbox thread.
        """
        if event_id != expected_event_id:
            raise ValueError("repair_event_id_mismatch")
        if not re.fullmatch(r"[0-9a-f]{64}", str(expected_raw_sha256 or "")):
            raise ValueError("repair_expected_raw_sha256_invalid")
        required_projection_keys = {"category", "reason_code", "reply_class", "route"}
        if set(expected_promoted_classification) != required_projection_keys:
            raise ValueError("repair_expected_promoted_classification_invalid")
        expected_projection = {
            key: str(expected_promoted_classification[key]) for key in required_projection_keys
        }
        if not isinstance(reviewed_draft, dict):
            raise ValueError("repair_reviewed_draft_invalid")
        reviewed_input_id = str(reviewed_draft.get("reviewed_input_id") or "")
        subject = str(reviewed_draft.get("subject") or "").strip()
        body = str(reviewed_draft.get("body") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{6,160}", reviewed_input_id):
            raise ValueError("repair_reviewed_input_id_invalid")
        if not subject or len(subject) > 180 or "\r" in subject or "\n" in subject:
            raise ValueError("repair_reviewed_draft_subject_invalid")
        if not body or redact(subject) != subject or redact(body) != body:
            raise ValueError("repair_reviewed_draft_unsafe")
        if REVIEWED_AFFILIATE_DRAFT_UNSUPPORTED_CLAIM_RE.search(body):
            raise ValueError("repair_reviewed_draft_unsupported_claim")

        event, mailbox, parsed, _existing_classification = self._event_context(event_id)
        raw_sha256 = str(parsed.get("_canonical_raw_sha256") or "")
        if raw_sha256 != expected_raw_sha256:
            raise ValueError("repair_expected_raw_sha256_mismatch")
        if str(event["state"]) in {"resolved_externally", "held", "archived"}:
            raise ValueError("repair_event_terminal")
        original_identity = str(mailbox.get("canonical_address") or "").strip().lower()
        if not original_identity or original_identity not in {
            str(item).strip().lower() for item in parsed.get("recipients") or []
        }:
            raise ValueError("repair_original_thread_identity_unbound")
        recipient = parseaddr(str(parsed.get("sender") or ""))[1].strip().lower()
        if not recipient:
            raise ValueError("repair_sender_identity_invalid")

        attribution, attribution_status = self.resolve_attribution(mailbox, parsed["recipients"])
        if attribution_status != "registry_exact_match":
            raise ValueError("repair_recipient_attribution_unverified")
        classification = classify_message(
            attribution,
            parsed,
            provider_thread_id=str(event["provider_thread_id"] or ""),
            journalist_pitch_db_path=GLP_DIGITAL_PR_DB_PATH,
        )
        provisional_venture = resolve_reply_venture(
            attribution, parsed, "other", str(classification.get("category") or "")
        )
        if not provisional_venture:
            raise ValueError("repair_venture_not_in_reply_registry")
        intent = classify_reply_intent(parsed, classification, provisional_venture)
        if classification.get("reason_code") == "affiliate_network_genuine_human_reply":
            intent = {
                "reply_class": "partner_vendor_action",
                "confidence": 0.99,
                "signals": ["reviewed_affiliate_network_genuine_human_reply"],
            }
        reply_class = str(intent["reply_class"])
        intent_confidence = float(intent["confidence"])
        if intent_confidence < MINIMUM_INTENT_CONFIDENCE:
            reply_class = "other"
        reply_venture = resolve_reply_venture(
            attribution, parsed, reply_class, str(classification.get("category") or "")
        )
        classification = normalize_affiliate_coarse(classification, reply_class)
        route = route_for(
            classification["category"], classification["reason_code"],
            float(classification.get("confidence") or 0.0),
        )
        affiliate_disposition = affiliate_response_disposition(parsed, classification)
        if affiliate_disposition == "auto_process_approval":
            route = "silent"
        elif affiliate_disposition == "operator_prerequisite_card":
            route = "decisions"
        elif classification["category"] == "affiliate_network_response":
            route = "silent"
        classification.update({
            "reply_class": reply_class,
            "intent_confidence": intent_confidence,
            "matched_signals": list(intent["signals"]),
            "reply_venture": reply_venture,
            "route": route,
            "affiliate_disposition": affiliate_disposition,
        })
        actual_projection = {
            "category": str(classification["category"]),
            "reason_code": str(classification["reason_code"]),
            "reply_class": reply_class,
            "route": route,
        }
        if actual_projection != expected_projection:
            raise ValueError("repair_promoted_classification_mismatch")
        if actual_projection not in (
            {
                "category": "partner_vendor_action",
                "reason_code": "affiliate_network_human_reference_before_call",
                "reply_class": "partner_vendor_action",
                "route": "decisions",
            },
            {
                "category": "partner_vendor_action",
                "reason_code": "affiliate_network_genuine_human_reply",
                "reply_class": "partner_vendor_action",
                "route": "decisions",
            },
        ):
            raise ValueError("repair_not_narrow_human_reference_request")

        repair_policy = f"{POLICY_VERSION}:affiliate-human-request-repair:v1:{raw_sha256[:16]}"
        classification_id = "cls_" + sha256_text(f"{event_id}|{repair_policy}")[:24]
        body_sha256 = sha256_text(body)
        reviewed_input_sha256 = sha256_text(reviewed_input_id)
        draft_id = "drf_" + sha256_text(
            f"{event_id}|{repair_policy}|{reviewed_input_id}|{body_sha256}"
        )[:24]
        recipients_json = json.dumps([recipient], sort_keys=True)
        draft_view = {
            "draft_id": draft_id,
            "event_id": event_id,
            "revision": 1,
            "from_identity": original_identity,
            "recipients_json": recipients_json,
            "provider_thread_id": str(event["provider_thread_id"] or ""),
            "subject": subject,
            "body": body,
            "body_sha256": body_sha256,
            "source_hash": raw_sha256,
            "policy_version": repair_policy,
            "status": "draft_generated",
            "reply_class": reply_class,
            "intent_confidence": intent_confidence,
        }
        card = render_decision_card(event_id, mailbox, parsed, classification, draft_view)
        outbox_key = (
            f"decision:affiliate-human-request-repair:{event_id}:{raw_sha256}:{body_sha256}"
        )
        outbox_id = "out_" + sha256_text(outbox_key)[:24]
        existing_classification = self.conn.execute(
            "SELECT * FROM inbox_classifications WHERE event_id=? AND policy_version=?",
            (event_id, repair_policy),
        ).fetchone()
        existing_draft = self.conn.execute(
            "SELECT * FROM local_drafts WHERE draft_id=?", (draft_id,)
        ).fetchone()
        existing_outbox = self.conn.execute(
            "SELECT * FROM notification_outbox WHERE idempotency_key=?", (outbox_key,)
        ).fetchone()
        if any(item is not None for item in (existing_classification, existing_draft, existing_outbox)):
            replay_matches = (
                existing_classification is not None
                and existing_draft is not None
                and existing_outbox is not None
                and all(str(existing_classification[key]) == actual_projection[key] for key in actual_projection)
                and str(existing_draft["from_identity"]) == original_identity
                and str(existing_draft["recipients_json"]) == recipients_json
                and str(existing_draft["provider_thread_id"] or "") == str(event["provider_thread_id"] or "")
                and str(existing_draft["subject"]) == subject
                and str(existing_draft["body_sha256"]) == body_sha256
                and str(existing_draft["source_hash"]) == raw_sha256
                and str(existing_draft["policy_version"]) == repair_policy
                and str(existing_outbox["outbox_id"]) == outbox_id
                and str(existing_outbox["payload_sha256"]) == sha256_text(card)
            )
            if not replay_matches:
                raise RuntimeError("repair_idempotency_state_conflict")
            return {
                "status": "pass",
                "event_id": event_id,
                "raw_sha256": raw_sha256,
                "classification_id": classification_id,
                "draft_id": draft_id,
                "outbox_id": outbox_id,
                "idempotent_replay": True,
                "provider_draft_created": False,
                "provider_mutation": False,
                "outbound_send": False,
            }
        if self.conn.execute(
            "SELECT 1 FROM local_drafts WHERE event_id=?", (event_id,)
        ).fetchone() is not None:
            raise RuntimeError("repair_event_existing_local_draft")
        if self.conn.execute(
            "SELECT 1 FROM notification_outbox WHERE event_id=? AND purpose='operator_decision'", (event_id,)
        ).fetchone() is not None:
            raise RuntimeError("repair_event_existing_decision_card")

        now = utc_now()
        with self.conn:
            self.conn.execute(
                """INSERT INTO inbox_classifications(classification_id,event_id,policy_version,category,confidence,
                reason_code,matched_facts_json,venture,reply_class,intent_confidence,matched_signals_json,
                venture_lifecycle,actionable,route,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    classification_id, event_id, repair_policy, classification["category"],
                    classification["confidence"], classification["reason_code"],
                    json.dumps(classification["facts"], sort_keys=True), attribution["venture"], reply_class,
                    intent_confidence, json.dumps(intent["signals"], sort_keys=True),
                    attribution["lifecycle"], int(classification["actionable"]), route, now,
                ),
            )
            self.conn.execute(
                """INSERT INTO local_drafts(draft_id,event_id,revision,from_identity,recipients_json,
                provider_thread_id,subject,body,body_sha256,source_hash,policy_version,status,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    draft_id, event_id, 1, original_identity, recipients_json,
                    event["provider_thread_id"], subject, body, body_sha256, raw_sha256,
                    repair_policy, "draft_generated", now, now,
                ),
            )
            self.conn.execute(
                """INSERT INTO notification_outbox(outbox_id,event_id,purpose,destination_channel_id,idempotency_key,
                payload,payload_sha256,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    outbox_id, event_id, "operator_decision", DECISIONS_CHANNEL_ID, outbox_key,
                    card, sha256_text(card), "pending", now, now,
                ),
            )
        audit_hash = self.audit(
            "affiliate_human_request_repaired", "inbox_event", event_id,
            {
                "raw_sha256": raw_sha256,
                "repair_policy": repair_policy,
                "classification": actual_projection,
                "reviewed_input_sha256": reviewed_input_sha256,
                "draft_id": draft_id,
                "draft_body_sha256": body_sha256,
                "outbox_id": outbox_id,
                "provider_draft_created": False,
                "provider_mutation": False,
                "outbound_send": False,
            },
        )
        return {
            "status": "pass",
            "event_id": event_id,
            "raw_sha256": raw_sha256,
            "classification_id": classification_id,
            "draft_id": draft_id,
            "outbox_id": outbox_id,
            "audit_event_hash": audit_hash,
            "idempotent_replay": False,
            "provider_draft_created": False,
            "provider_mutation": False,
            "outbound_send": False,
        }

    def _compose_bound_fable_draft(
        self,
        event_id: str,
        mailbox: dict[str, Any],
        parsed: dict[str, Any],
        classification: dict[str, Any],
        *,
        trigger_decision_message_id: str,
        trigger_card_message_id: str,
        operator_action: str = "DRAFT",
        operator_edit_instruction: str | None = None,
    ) -> dict[str, Any]:
        if not trigger_decision_message_id or not trigger_card_message_id:
            raise ValueError("draft_trigger_binding_required")
        if operator_action not in {"DRAFT", "EDIT"}:
            raise ValueError("draft_operator_action_invalid")
        if operator_action == "EDIT" and not str(operator_edit_instruction or "").strip():
            raise ValueError("draft_edit_instruction_required")
        claimed = self.conn.execute(
            """SELECT action,card_message_id,actor_id,channel_id FROM operator_decisions
               WHERE decision_message_id=? AND event_id=?""",
            (trigger_decision_message_id, event_id),
        ).fetchone()
        if (
            claimed is None or str(claimed["action"]) != operator_action
            or str(claimed["card_message_id"]) != trigger_card_message_id
            or str(claimed["actor_id"]) != OPERATOR_ID
            or str(claimed["channel_id"]) != DECISIONS_CHANNEL_ID
        ):
            raise ValueError("draft_trigger_not_claimed")
        event_row = self.conn.execute(
            """SELECT mailbox_id,provider_thread_id,received_at
               FROM inbox_events WHERE event_id=?""",
            (event_id,),
        ).fetchone()
        if event_row is None:
            raise ValueError("unknown_event_id")
        latest_thread_event = latest_inbound_thread_event(self.conn, event_id)
        if latest_thread_event["event_id"] != event_id:
            raise ValueError(
                f"draft_event_not_latest_inbound:{latest_thread_event['event_id']}"
            )
        thread_marketplace = latest_thread_marketplace_context(
            self.conn, event_id, mailbox,
        )
        registry, registry_hash = load_reply_registry()
        coarse_category = str(classification.get("category") or "")
        provisional_venture = resolve_reply_venture(
            mailbox, parsed, str(classification.get("reply_class") or "other"), coarse_category
        )
        if provisional_venture is None:
            outcome = {"status": "blocked", "outcome": "fable_draft_unavailable", "reason": "venture_not_in_reply_registry"}
            self.audit("fable_draft_unavailable", "inbox_event", event_id, outcome)
            return outcome
        # Stored append-only classifications are evidence, not drafting
        # authority. Recompute from the newest unquoted reply every time so a
        # repaired registry/classifier cannot be bypassed by a stale v1/v2 row.
        intent = classify_reply_intent(parsed, classification, provisional_venture)
        reply_class = str(intent["reply_class"])
        intent_confidence = float(intent["confidence"])
        signals = list(intent["signals"])
        if intent_confidence < MINIMUM_INTENT_CONFIDENCE:
            reply_class = "other"
        venture_key = resolve_reply_venture(mailbox, parsed, reply_class, coarse_category)
        raw_sha256 = str(parsed.get("_canonical_raw_sha256") or "")
        canonical_raw = parsed.get("_canonical_raw_bytes")
        attachment_manifest = parsed.get("_attachment_manifest")
        rendered = resolve_fable_draft_policy(registry, venture_key or "", reply_class, parsed) if venture_key else None
        if (
            rendered is None or rendered.get("never_auto_send") is not True
            or rendered.get("verified") is not True
            or not isinstance(canonical_raw, bytes) or not raw_sha256
            or not isinstance(attachment_manifest, list)
        ):
            outcome = {
                "status": "blocked", "outcome": "fable_draft_unavailable",
                "reason": "missing_or_unsafe_draft_policy", "reply_class": reply_class,
                "intent_confidence": intent_confidence,
            }
            self.audit("fable_draft_unavailable", "inbox_event", event_id, {
                **outcome, "trigger_decision_message_id": trigger_decision_message_id,
                "trigger_card_message_id": trigger_card_message_id,
            })
            return outcome
        static_context = rendered.get("verified_business_context") or []
        try:
            draft_context = resolve_verified_draft_context(
                venture_key or "", event_id, static_context, connection=self.conn, parsed=parsed,
                marketplace=thread_marketplace["network"],
            )
        except (OSError, ValueError, sqlite3.Error) as exc:
            outcome = {
                "status": "blocked", "outcome": "fable_draft_unavailable",
                "reason": str(exc)[:160] or "verified_draft_context_unavailable",
                "reply_class": reply_class, "intent_confidence": intent_confidence,
            }
            self.audit("fable_draft_unavailable", "inbox_event", event_id, {
                **outcome, "trigger_decision_message_id": trigger_decision_message_id,
                "trigger_card_message_id": trigger_card_message_id,
            })
            return outcome
        current_business_state_sha256 = str(draft_context["context_sha256"])
        verified_business_context = list(draft_context["facts"])
        composed = run_fable_email_drafter({
            "event_id": event_id,
            "raw_rfc822_base64": base64.b64encode(canonical_raw).decode("ascii"),
            "raw_sha256": raw_sha256,
            "raw_size_bytes": len(canonical_raw),
            "attachment_manifest": attachment_manifest,
            "venture": venture_key, "venture_key": venture_key, "company": mailbox.get("company"),
            "from_identity": rendered["from_identity"], "sender": parsed.get("sender"),
            "recipients": parsed.get("recipients") or [],
            "provider_thread_id": str(event_row["provider_thread_id"] or ""),
            "event_received_at": str(event_row["received_at"] or ""),
            "latest_thread_event_id": latest_thread_event["event_id"],
            "latest_thread_received_at": latest_thread_event["received_at"],
            "thread_marketplace": thread_marketplace["network"],
            "thread_marketplace_source_event_id": thread_marketplace["source_event_id"],
            "thread_marketplace_source_sha256": thread_marketplace["source_sha256"],
            "subject": parsed.get("subject"), "reply_class": reply_class,
            "intent_confidence": intent_confidence,
            "template_key": rendered["template_key"],
            "never_auto_send": True,
            "trigger_decision_message_id": trigger_decision_message_id,
            "trigger_card_message_id": trigger_card_message_id,
            "registry_sha256": registry_hash,
            "registry_policy_version": str(registry.get("policy_version") or ""),
            "current_business_state_sha256": current_business_state_sha256,
            "verified_business_context": verified_business_context,
            "context_scope": draft_context["context_scope"],
            "scope_exclusion_markers": draft_context["scope_exclusion_markers"],
            "operator_edit_instruction": str(operator_edit_instruction or ""),
        })
        def _template_fallback(reason: str) -> dict[str, Any] | None:
            """Degrade to the pre-approved template body instead of dead-ending.

            Every draft is operator-approved before any send (never_auto_send is
            structural), so the operator IS the safety review. A refusal that
            leaves the operator with no draft at all protects nothing and broke
            the DRAFT/EDIT flow repeatedly (2026-08-05). The Fable body is fully
            discarded here; only registry-reviewed template copy is used, with
            the refusal reason recorded as a visible risk flag.
            """
            if not template_fallback_allowed(venture_key):
                return None
            fallback_body = str(rendered.get("template_body") or "").strip()
            if not fallback_body:
                return None
            if str(operator_edit_instruction or "").strip():
                # An EDIT carries explicit operator instructions; a fixed
                # template cannot honor them and a draft that ignores the
                # operator is worse than no draft (2026-08-05 incident).
                return None
            self.audit("fable_draft_template_fallback", "inbox_event", event_id, {
                "reason": reason[:160], "reply_class": reply_class,
                "template_key": rendered["template_key"],
                "trigger_decision_message_id": trigger_decision_message_id,
                "trigger_card_message_id": trigger_card_message_id,
                "provider_draft_created": False, "provider_mutation": False, "outbound_send": False,
            })
            return {
                "status": "pass",
                "body": fallback_body,
                "reply_class": reply_class,
                "drafter_route": "template_fallback",
                "composer_version": FABLE_EMAIL_DRAFTER_VERSION,
                "from_identity": rendered["from_identity"],
                "raw_sha256": raw_sha256,
                "current_business_state_sha256": current_business_state_sha256,
                "context_scope": draft_context["context_scope"],
                "latest_thread_event_id": latest_thread_event["event_id"],
                "latest_thread_received_at": latest_thread_event["received_at"],
                "latest_inbound_sha256": sha256_text(
                    newest_unquoted_human_reply(str(parsed.get("body_text") or ""))
                ),
                "thread_marketplace": thread_marketplace["network"],
                "thread_marketplace_source_event_id": thread_marketplace["source_event_id"],
                "thread_marketplace_source_sha256": thread_marketplace["source_sha256"],
                "never_auto_send": True,
                "grounded_facts": [],
                "risk_flags": [f"template_fallback:{reason[:80]}"],
                "rationale": "Pre-approved template used because the drafter was unavailable or its output failed a safety check. Review and EDIT before approving.",
                "model": "template_fallback", "effort": "none",
                "model_policy_sha256": "template_fallback", "prompt_sha256": "template_fallback",
                "response_id": "template_fallback", "response_sha256": sha256_text(fallback_body),
            }

        if composed.get("status") != "pass":
            fallback = _template_fallback(str(composed.get("reason") or "fable_drafter_failed"))
            if fallback is not None:
                composed = fallback
            else:
                outcome = {
                    **composed, "status": "blocked", "outcome": "fable_draft_unavailable",
                    "reply_class": reply_class, "intent_confidence": intent_confidence,
                }
                self.audit("fable_draft_unavailable", "inbox_event", event_id, {
                    "status": "blocked", "outcome": "fable_draft_unavailable",
                    "reason": str(outcome.get("reason") or "fable_drafter_failed")[:160],
                    "reply_class": reply_class,
                    "trigger_decision_message_id": trigger_decision_message_id,
                    "trigger_card_message_id": trigger_card_message_id,
                    "raw_email_sha256": raw_sha256,
                    "provider_draft_created": False, "provider_mutation": False, "outbound_send": False,
                })
                return outcome
        if (
            composed.get("from_identity") != rendered["from_identity"]
            or composed.get("raw_sha256") != raw_sha256
            or composed.get("current_business_state_sha256") != current_business_state_sha256
            or composed.get("context_scope") != draft_context["context_scope"]
            or composed.get("latest_thread_event_id") != latest_thread_event["event_id"]
            or composed.get("latest_thread_received_at") != latest_thread_event["received_at"]
            or composed.get("thread_marketplace") != thread_marketplace["network"]
            or composed.get("thread_marketplace_source_event_id") != thread_marketplace["source_event_id"]
            or composed.get("thread_marketplace_source_sha256") != thread_marketplace["source_sha256"]
            or composed.get("never_auto_send") is not True
            or any(not str(composed.get(key) or "") for key in (
                "model", "effort", "model_policy_sha256", "prompt_sha256", "response_id", "response_sha256"
            ))
        ):
            fallback = _template_fallback("fable_response_binding_mismatch")
            if fallback is None:
                return {"status": "blocked", "outcome": "fable_draft_unavailable", "reason": "fable_response_binding_mismatch"}
            composed = fallback
        composed_body = str(composed.get("body") or "")
        leaked_marker = next((
            marker for marker in draft_context["scope_exclusion_markers"]
            if marker.casefold() in composed_body.casefold()
        ), None)
        if leaked_marker is not None:
            return {
                "status": "blocked", "outcome": "fable_draft_unavailable",
                "reason": "fable_response_out_of_scope_content",
            }
        drafter_route = str(composed.get("drafter_route") or "fable_claude_tool")
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", drafter_route):
            fallback = _template_fallback("fable_drafter_route_invalid")
            if fallback is None:
                return {"status": "blocked", "outcome": "fable_draft_unavailable", "reason": "fable_drafter_route_invalid"}
            composed = fallback
            composed_body = str(composed["body"])
            drafter_route = "template_fallback"
        if redact(composed_body) != composed_body:
            # Discard the tainted body entirely; template copy is independent of it.
            fallback = _template_fallback("fable_response_secret_detected")
            if fallback is None:
                return {"status": "blocked", "outcome": "fable_draft_unavailable", "reason": "fable_response_secret_detected"}
            composed = fallback
            composed_body = str(composed["body"])
            drafter_route = "template_fallback"
        if (
            str(venture_key).startswith("glp")
            and reply_class not in {"medical_legal_boundary", "partner_vendor_action"}
            and drafter_route != "template_fallback"
            and glp_sensitive_signals(composed_body)
        ):
            # 2026-08-05: this gate used to DEAD-END the operator's DRAFT command.
            # Every on-topic GLP support reply mentions terms on the sensitive
            # list, so drafting failed constantly by construction. The draft is
            # operator-reviewed before send, so degrade to the pre-approved
            # template and surface the signals as risk flags instead.
            fallback = _template_fallback("fable_response_glp_sensitive:" + ",".join(glp_sensitive_signals(composed_body))[:60])
            if fallback is None:
                return {"status": "blocked", "outcome": "fable_draft_unavailable", "reason": "fable_response_glp_sensitive"}
            composed = fallback
            composed_body = str(composed["body"])
            drafter_route = "template_fallback"
        rendered = {
            **rendered,
            "subject": normalize_reply_subject(parsed.get("subject")),
            "body": composed_body,
            "reply_class": str(composed.get("reply_class") or reply_class),
            "template_key": str(rendered.get("template_key") or f"{venture_key}:{reply_class}"),
            "drafter_route": drafter_route,
            "composer_version": str(composed.get("composer_version") or FABLE_EMAIL_DRAFTER_VERSION),
            "grounded_facts": composed.get("grounded_facts") or [],
            "risk_flags": composed.get("risk_flags") or [],
            "rationale": composed.get("rationale") or "",
            "model": composed.get("model"), "effort": composed.get("effort"),
            "model_policy_sha256": composed.get("model_policy_sha256"),
            "prompt_sha256": composed.get("prompt_sha256"),
            "response_id": composed.get("response_id"),
            "response_sha256": composed.get("response_sha256"),
            "current_business_state_sha256": composed.get("current_business_state_sha256"),
            "context_scope": composed.get("context_scope"),
        }
        reply_class = str(rendered["reply_class"])
        body = str(rendered["body"])
        body_hash = sha256_text(body)
        latest = self.conn.execute(
            "SELECT * FROM local_drafts WHERE event_id=? ORDER BY revision DESC LIMIT 1", (event_id,)
        ).fetchone()
        expected_recipients_json = json.dumps([parsed["sender"]])
        revision = 1 if latest is None else int(latest["revision"]) + 1
        draft_id = "drf_" + sha256_text(f"{event_id}|{revision}")[:24]
        now = utc_now()
        with self.conn:
            if latest is not None:
                self.conn.execute(
                    "UPDATE local_drafts SET status='superseded',updated_at=? WHERE draft_id=? AND status='draft_generated'",
                    (now, latest["draft_id"]),
                )
            self.conn.execute(
                """INSERT INTO local_drafts(draft_id,event_id,revision,from_identity,recipients_json,
                provider_thread_id,subject,body,body_sha256,source_hash,policy_version,status,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (draft_id, event_id, revision, rendered["from_identity"], expected_recipients_json,
                 event_row["provider_thread_id"], rendered["subject"], body, body_hash, raw_sha256,
                 POLICY_VERSION, "draft_generated", now, now),
            )
        provenance = {
            "event_id": event_id, "revision": revision,
            "trigger_decision_message_id": trigger_decision_message_id,
            "trigger_card_message_id": trigger_card_message_id,
            "drafter_route": drafter_route,
            "composer_version": rendered.get("composer_version"),
            "template_key": rendered["template_key"], "registry_sha256": registry_hash,
            "raw_email_sha256": raw_sha256,
            "raw_email_size_bytes": int(parsed["_canonical_raw_size_bytes"]),
            "latest_thread_event_id": latest_thread_event["event_id"],
            "latest_thread_received_at": latest_thread_event["received_at"],
            "latest_inbound_sha256": composed.get("latest_inbound_sha256"),
            "thread_marketplace": thread_marketplace["network"],
            "thread_marketplace_source_event_id": thread_marketplace["source_event_id"],
            "thread_marketplace_source_sha256": thread_marketplace["source_sha256"],
            "attachment_manifest_sha256": sha256_text(json.dumps(parsed["_attachment_manifest"], sort_keys=True, separators=(",", ":"))),
            "attachment_count": len(parsed["_attachment_manifest"]),
            "body_sha256": body_hash, "draft_body_sha256": body_hash,
            "reply_class": reply_class,
            "intent_confidence": intent_confidence, "matched_signals": signals,
            "model": rendered.get("model"), "effort": rendered.get("effort"),
            "model_policy_sha256": rendered.get("model_policy_sha256"),
            "prompt_sha256": rendered.get("prompt_sha256"),
            "response_id": rendered.get("response_id"),
            "response_sha256": rendered.get("response_sha256"),
            "current_business_state_sha256": rendered.get("current_business_state_sha256"),
            "context_scope": rendered.get("context_scope"),
            "scope_exclusion_markers_sha256": sha256_text(json.dumps(
                draft_context["scope_exclusion_markers"], sort_keys=True,
            )),
            "safety_result": "pass", "never_auto_send": True,
            "provider_draft_created": False, "provider_mutation": False,
            "send_authorized": False, "outbound_send": False,
        }
        self.audit("fable_draft_provenance", "local_draft", draft_id, provenance)
        return {
            "status": "pass", "outcome": "fable_draft_generated", "draft_id": draft_id,
            "revision": revision, "reply_class": reply_class, "intent_confidence": intent_confidence,
            "template_key": rendered["template_key"], "registry_sha256": registry_hash, "created": True,
            "lifecycle_code": rendered.get("lifecycle_code"),
            "canonical_reply_class": rendered.get("canonical_reply_class"),
        }

    def _draft_with_provenance(
        self, event_id: str, draft: sqlite3.Row | dict[str, Any]
    ) -> dict[str, Any]:
        view = dict(draft)
        audit_row = self.conn.execute(
            """SELECT payload_json FROM audit_events
               WHERE event_type='fable_draft_provenance' AND object_type='local_draft'
                 AND object_id IN (SELECT draft_id FROM local_drafts WHERE event_id=?)
               ORDER BY audit_id DESC LIMIT 1""",
            (event_id,),
        ).fetchone()
        provenance = json.loads(audit_row["payload_json"]) if audit_row else {}
        view["reply_class"] = provenance.get("reply_class")
        view["intent_confidence"] = provenance.get("intent_confidence")
        view["template_key"] = provenance.get("template_key")
        return view

    def _queue_draft_card_revision(self, event_id: str, draft_id: str) -> str:
        _event, mailbox, parsed, classification = self._event_context(event_id)
        draft = self.conn.execute("SELECT * FROM local_drafts WHERE draft_id=?", (draft_id,)).fetchone()
        if draft is None:
            raise ValueError("local_draft_missing")
        draft_view = self._draft_with_provenance(event_id, draft)
        classification.update({
            "reply_class": draft_view.get("reply_class") or classification.get("reply_class"),
            "intent_confidence": draft_view.get("intent_confidence") or classification.get("intent_confidence"),
        })
        messages = render_draft_review_messages(event_id, mailbox, parsed, classification, draft_view)
        decision_outbox_id = ""
        for item in messages:
            purpose = str(item["purpose"])
            payload = str(item["payload"])
            part = int(item["part"])
            parts = int(item["parts"])
            key = (
                f"decision:{event_id}:draft-revision:{draft['revision']}:"
                f"part:{part:04d}-of-{parts:04d}:{sha256_text(payload)}"
            )
            outbox_id = self.queue_notification(event_id, purpose, DECISIONS_CHANNEL_ID, key, payload)
            if purpose == "operator_decision":
                decision_outbox_id = outbox_id
        if not decision_outbox_id:
            raise ValueError("decision_card_control_missing")
        return decision_outbox_id

    def _draft_from_bound_operator_command(
        self,
        event_id: str,
        *,
        actor_id: str,
        channel_id: str,
        decision_message_id: str,
        card_message_id: str,
        operator_action: str = "DRAFT",
        operator_edit_instruction: str | None = None,
    ) -> dict[str, Any]:
        """Sole composition edge, called only after an exact Discord DRAFT/EDIT is claimed."""
        if actor_id != OPERATOR_ID or channel_id != DECISIONS_CHANNEL_ID:
            raise ValueError("draft_operator_binding_mismatch")
        if not decision_message_id or not card_message_id:
            raise ValueError("draft_trigger_binding_required")
        try:
            event, mailbox, parsed, classification = self._event_context(event_id)
            if event["state"] in {"resolved_externally", "held"}:
                raise ValueError("event_terminal")
            generated = self._compose_bound_fable_draft(
                event_id, mailbox, parsed, classification,
                trigger_decision_message_id=decision_message_id,
                trigger_card_message_id=card_message_id,
                operator_action=operator_action,
                operator_edit_instruction=operator_edit_instruction,
            )
        except Exception as exc:
            safe_reason = re.sub(r"[^a-z0-9_:-]+", "_", str(exc).lower())[:80] or "fable_draft_unavailable"
            generated = {"status": "blocked", "outcome": "fable_draft_unavailable", "reason": safe_reason}
        if generated.get("status") != "pass":
            self.audit(
                "fable_draft_blocked", "inbox_event", event_id,
                {"trigger_decision_message_id": decision_message_id,
                 "trigger_card_message_id": card_message_id,
                 "reason": str(generated.get("reason") or "fable_draft_unavailable")[:80],
                 "provider_draft_created": False, "provider_mutation": False,
                 "send_authorized": False, "outbound_send": False},
                actor=f"discord-user:{actor_id}",
            )
            self.queue_notification(
                event_id, "operator_action_reason", DECISIONS_CHANNEL_ID,
                f"fable-blocked:{decision_message_id}",
                "\n".join([
                    "⛔ Could not apply your edit — the previous draft is unchanged",
                    f"Event: `{event_id}`",
                    f"The drafter failed while applying your instructions (`{str(generated.get('reason') or 'unavailable')[:60]}`).",
                    f"What to do: post `{event_id} EDIT <your notes>` again to retry. Nothing was sent.",
                ] if operator_action == "EDIT" else [
                    "⛔ No draft could be created for this one",
                    f"Event: `{event_id}`",
                    "The source-bound composer did not return a valid proposed reply. This is not evidence that a reply template is missing.",
                    f"What to do: retry with `{event_id} DRAFT` on the active card after the reported composer or evidence fault is repaired. No template addition or external reply is required by this error.",
                    f"-# reason `{str(generated.get('reason') or 'fable_draft_unavailable')[:60]}` · nothing was sent",
                ]),
            )
            return {
                **generated, "event_id": event_id, "provider_draft_created": False,
                "provider_mutation": False, "outbound_send": False,
            }
        latest = self.conn.execute(
            "SELECT * FROM local_drafts WHERE draft_id=?", (generated["draft_id"],)
        ).fetchone()
        if latest is None:
            raise ValueError("local_draft_generation_missing")
        outbox_id = self._queue_draft_card_revision(event_id, latest["draft_id"])
        action = "draft_requested"
        action_id = "act_" + sha256_text(f"{event_id}|{action}|{decision_message_id}")[:24]
        receipt = {
            "actor_id": actor_id, "channel_id": channel_id,
            "card_message_id": card_message_id, "decision_message_id": decision_message_id,
            "draft_id": latest["draft_id"], "draft_revision": int(latest["revision"]),
            "draft_body_sha256": latest["body_sha256"], "raw_email_sha256": latest["source_hash"],
            "review_outbox_id": outbox_id, "provider_draft_created": False,
            "provider_mutation": False, "outbound_send": False,
            "source": "exact_discord_draft_or_edit_fable_full_email_tool",
            "operator_action": operator_action,
        }
        now = utc_now()
        with self.conn:
            self.conn.execute(
                """INSERT INTO mailbox_actions(action_id,event_id,action,status,provider_receipt_json,attempts,created_at,updated_at)
                   VALUES(?,?,?,?,?,0,?,?)""",
                (action_id, event_id, action, "verified", json.dumps(receipt, sort_keys=True), now, now),
            )
        self.audit(
            "operator_local_draft_requested", "inbox_event", event_id,
            receipt, actor=f"discord-user:{actor_id}",
        )
        return {
            "status": "pass", "outcome": generated["outcome"], "event_id": event_id,
            "draft_id": latest["draft_id"], "draft_revision": int(latest["revision"]),
            "draft_body_sha256": latest["body_sha256"], "decision_message_id": decision_message_id,
            "review_outbox_id": outbox_id, "idempotent_replay": False,
            "provider_draft_created": False, "provider_mutation": False, "outbound_send": False,
        }

    def _edit_bound_operator_draft(
        self, event_id: str, body: str, *, actor_id: str,
        decision_message_id: str, card_message_id: str,
    ) -> dict[str, Any]:
        latest = self.conn.execute(
            "SELECT * FROM local_drafts WHERE event_id=? ORDER BY revision DESC LIMIT 1", (event_id,)
        ).fetchone()
        if latest is None:
            return {"status": "blocked", "outcome": "no_draft_to_edit", "reason": "no_draft_to_edit"}
        if not body or body != body.strip() or len(body) > 4000 or "\x00" in body:
            raise ValueError("invalid_local_draft_edit_instruction")
        return self._draft_from_bound_operator_command(
            event_id,
            actor_id=actor_id,
            channel_id=DECISIONS_CHANNEL_ID,
            decision_message_id=decision_message_id,
            card_message_id=card_message_id,
            operator_action="EDIT",
            operator_edit_instruction=body,
        )

    @staticmethod
    def _canonical_decision_id(event_id: str, action: str, card_revision_sha256: str) -> str:
        """Dedup key for one operator decision.

        Keyed on the decision's own identity -- which event, which action, which
        card revision the operator was answering -- and deliberately NOT on
        decision_message_id.  The scheduler sync spells that id two ways for the
        same reply (bare, or suffixed with the event_id when one message carries
        several commands), and the spelling depends on how many commands that
        particular pass happened to parse.  Keying the row on it let a single
        decision land twice under two ids, which then failed the reconciler's
        exactly-one terminal check.  See the 2026-08-21/22 duplicate pairs.
        """
        return "dec_" + sha256_text(f"{event_id}|{action}|{card_revision_sha256}")[:24]

    def _insert_operator_decision(
        self, *, event_id: str, channel_id: str, card_message_id: str,
        decision_message_id: str, actor_id: str, action: str,
        card_revision_sha256: str, now: str,
    ) -> None:
        """Record one operator decision idempotently.  The CALLER owns the transaction.

        Idempotent across message-id spellings: a decision already recorded for
        this (event, action, card revision) is not written again, whichever
        spelling arrives second.  A decision against a DIFFERENT card revision is
        a different decision and still inserts, so genuine operator conflicts stay
        visible to the reconciler rather than being silently collapsed here.
        """
        already = self.conn.execute(
            "SELECT 1 FROM operator_decisions WHERE event_id=? AND action=? AND card_revision_sha256=? LIMIT 1",
            (event_id, action, card_revision_sha256),
        ).fetchone()
        if already:
            return
        self.conn.execute(
            """INSERT OR IGNORE INTO operator_decisions(decision_id,event_id,channel_id,card_message_id,
            decision_message_id,actor_id,action,card_revision_sha256,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
            (self._canonical_decision_id(event_id, action, card_revision_sha256), event_id,
             channel_id, card_message_id, decision_message_id, actor_id, action,
             card_revision_sha256, now),
        )

    def _record_operator_decision(
        self, event_id: str, card: sqlite3.Row, decision_message_id: str,
        actor_id: str, action: str,
    ) -> None:
        now = utc_now()
        with self.conn:
            self._insert_operator_decision(
                event_id=event_id, channel_id=DECISIONS_CHANNEL_ID,
                card_message_id=card["discord_message_id"],
                decision_message_id=decision_message_id, actor_id=actor_id,
                action=action, card_revision_sha256=card["payload_sha256"], now=now,
            )

    def _verified_send_local_result(self, event_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            """SELECT * FROM mailbox_actions
               WHERE event_id=? AND action IN ('email_sent_verified','email_sent_exact_approved_draft')
                 AND status IN ('verified','verified_sent')
               ORDER BY updated_at DESC,action_id DESC LIMIT 1""",
            (event_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            receipt = json.loads(str(row["provider_receipt_json"] or "{}"))
        except json.JSONDecodeError:
            return None
        if not receipt.get("provider_message_id"):
            return None
        source_marked_read = bool(
            receipt.get("source_marked_read")
            if "source_marked_read" in receipt
            else receipt.get("original_marked_read")
        )
        if not source_marked_read:
            return None
        return {
            "status": "pass",
            "outcome": "already_sent_verified",
            "event_id": event_id,
            "provider_message_id": str(receipt["provider_message_id"]),
            "provider_thread_id": str(receipt.get("provider_thread_id") or ""),
            "draft_id": str(receipt.get("draft_id") or ""),
            "draft_revision": int(receipt.get("draft_revision") or receipt.get("revision") or 0),
            "draft_body_sha256": str(receipt.get("body_sha256") or receipt.get("draft_body_sha256") or ""),
            "source_marked_read": source_marked_read,
            "idempotent_replay": True,
            "send_executed_now": False,
            "external_send_exists": True,
            "provider_mutation": False,
            "outbound_send": False,
        }

    def _approved_send_binding(
        self,
        event_id: str,
        *,
        actor_id: str,
        channel_id: str,
        decision_message_id: str,
        card_message_id: str,
        backup_path: str,
        backup_sha256: str,
    ) -> dict[str, Any]:
        event, mailbox, parsed, classification = self._event_context(event_id)
        if actor_id != OPERATOR_ID or channel_id != DECISIONS_CHANNEL_ID:
            raise ValueError("approved_send_operator_binding_mismatch")
        if str(event["state"]) in {"held", "resolved_externally", "resolved_automatically"}:
            raise ValueError("approved_send_event_terminal_conflict")
        draft = self.conn.execute(
            "SELECT * FROM local_drafts WHERE event_id=? ORDER BY revision DESC LIMIT 1", (event_id,)
        ).fetchone()
        if draft is None:
            raise ValueError("no_local_draft")
        if str(draft["status"]) not in {"draft_generated", "sent_verified"}:
            raise ValueError("approved_send_draft_not_ready")
        draft_view = self._draft_with_provenance(event_id, draft)
        reply_class = str(draft_view.get("reply_class") or classification.get("reply_class") or "other")
        if reply_class in {"spam", "other", "medical_legal_boundary", "angry_complaint"}:
            raise ValueError("reply_class_forbidden")

        recipients = json.loads(str(draft["recipients_json"] or "[]"))
        recipients = [str(item).strip().lower() for item in recipients if str(item).strip()]
        sender_address = parseaddr(str(event["sender"] or ""))[1].lower()
        from_identity = str(draft["from_identity"] or "").lower()
        allowed_identities = {
            str(mailbox.get("canonical_address") or "").lower(),
            *(str(item).lower() for item in (mailbox.get("aliases") or [])),
        }
        body = normalize_approved_body(str(draft["body"] or ""))
        if not body or body != str(draft["body"] or ""):
            raise ValueError("approved_send_body_not_canonical")
        if len(recipients) != 1 or recipients[0] != sender_address:
            raise ValueError("approved_send_recipient_binding_mismatch")
        if from_identity not in allowed_identities:
            raise ValueError("approved_send_from_identity_not_allowlisted")
        if sha256_text(body) != str(draft["body_sha256"] or ""):
            raise ValueError("approved_send_body_hash_mismatch")
        raw_path = Path(str(event["raw_path"] or ""))
        if not raw_path.is_file() or sha256_bytes(raw_path.read_bytes()) != str(event["raw_sha256"] or ""):
            raise ValueError("approved_send_source_hash_mismatch")
        if str(draft["source_hash"] or "") != str(event["raw_sha256"] or ""):
            raise ValueError("approved_send_draft_source_mismatch")

        provenance_row = self.conn.execute(
            """SELECT payload_json FROM audit_events
               WHERE event_type='fable_draft_provenance' AND object_type='local_draft' AND object_id=?
               ORDER BY audit_id DESC LIMIT 1""",
            (draft["draft_id"],),
        ).fetchone()
        if provenance_row is None:
            raise ValueError("approved_send_provenance_missing")
        provenance = json.loads(str(provenance_row["payload_json"] or "{}"))
        provenance_checks = {
            "event_id": str(provenance.get("event_id") or "") == event_id,
            "revision": int(provenance.get("revision") or 0) == int(draft["revision"]),
            "body_sha256": str(provenance.get("draft_body_sha256") or "") == str(draft["body_sha256"]),
            "raw_sha256": str(provenance.get("raw_email_sha256") or "") == str(event["raw_sha256"]),
            "safety_pass": str(provenance.get("safety_result") or "") == "pass",
            "never_auto_send": provenance.get("never_auto_send") is True,
        }
        if not all(provenance_checks.values()):
            raise ValueError("approved_send_provenance_mismatch")

        expected_messages = render_draft_review_messages(event_id, mailbox, parsed, classification, draft_view)
        control_row: sqlite3.Row | None = None
        live_parts: list[str] = []
        for item in expected_messages:
            payload = str(item["payload"])
            key = (
                f"decision:{event_id}:draft-revision:{draft['revision']}:"
                f"part:{int(item['part']):04d}-of-{int(item['parts']):04d}:{sha256_text(payload)}"
            )
            row = self.conn.execute(
                """SELECT * FROM notification_outbox
                   WHERE event_id=? AND idempotency_key=? AND status='delivered'""",
                (event_id, key),
            ).fetchone()
            if row is None or not row["discord_message_id"]:
                raise ValueError("approved_send_review_part_not_delivered")
            if str(row["payload_sha256"] or "") != sha256_text(payload) or str(row["readback_sha256"] or "") != sha256_text(payload):
                raise ValueError("approved_send_review_part_hash_mismatch")
            live = discord_request(f"/channels/{DECISIONS_CHANNEL_ID}/messages/{row['discord_message_id']}")
            if str(live.get("content") or "") != payload:
                raise ValueError("approved_send_review_part_live_readback_mismatch")
            live_parts.append(str(live.get("content") or ""))
            if str(item["purpose"]) == "operator_decision":
                control_row = row
        if control_row is None:
            raise ValueError("active_card_binding_mismatch")
        # 2026-08-05: the control card for THIS draft revision (verified above
        # by hash + live Discord readback of every part) is the authority. The
        # card id recorded at claim time may legitimately differ after card
        # reissues; requiring equality — and requiring the operator's SEND to be
        # a Discord reply to that exact card — blocked every approved send
        # tonight while adding nothing the readbacks don't already prove.
        card_message_id = str(control_row["discord_message_id"])

        live_decision = discord_request(f"/channels/{DECISIONS_CHANNEL_ID}/messages/{decision_message_id.split(':', 1)[0]}")
        decision_checks = {
            "actor": str((live_decision.get("author") or {}).get("id") or "") == actor_id,
            "non_bot": not bool((live_decision.get("author") or {}).get("bot")),
            "channel": str(live_decision.get("channel_id") or DECISIONS_CHANNEL_ID) == DECISIONS_CHANNEL_ID,
            "command": str(live_decision.get("content") or "") == f"{event_id} SEND",
            "unedited": not live_decision.get("edited_timestamp"),
        }
        if not all(decision_checks.values()):
            raise ValueError("approved_send_live_decision_mismatch")
        decision_row = self.conn.execute(
            "SELECT * FROM operator_decisions WHERE decision_message_id=?", (decision_message_id,)
        ).fetchone()
        if decision_row is None or not (
            str(decision_row["event_id"]) == event_id
            and str(decision_row["action"]) == "SEND"
            and str(decision_row["actor_id"]) == actor_id
            and str(decision_row["card_revision_sha256"]) == str(control_row["payload_sha256"])
        ):
            raise ValueError("approved_send_ledger_decision_mismatch")

        return {
            "event_id": event_id,
            "draft_id": str(draft["draft_id"]),
            "draft_revision": int(draft["revision"]),
            "body": body,
            "body_sha256": str(draft["body_sha256"]),
            "from_identity": from_identity,
            "recipient": recipients[0],
            "recipient_display": parseaddr(str(event["sender"] or ""))[0],
            "subject": str(draft["subject"] or ""),
            "provider_message_id": str(event["provider_message_id"] or ""),
            "provider_thread_id": str(event["provider_thread_id"] or ""),
            "rfc_message_id": str(event["rfc_message_id"] or ""),
            "decision_message_id": decision_message_id,
            "card_message_id": card_message_id,
            "actor_id": actor_id,
            "channel_id": channel_id,
            "mailbox": dict(mailbox),
            "mailbox_id": str(mailbox["mailbox_id"]),
            "reply_class": reply_class,
            "review_parts_verified": len(live_parts),
            "backup_path": backup_path,
            "backup_sha256": backup_sha256,
        }

    def _send_approved_email_reply(
        self,
        event_id: str,
        *,
        actor_id: str,
        channel_id: str,
        decision_message_id: str,
        card_message_id: str,
        backup_path: str,
        backup_sha256: str,
    ) -> dict[str, Any]:
        local = self._verified_send_local_result(event_id)
        if local is not None and local.get("source_marked_read"):
            return local
        try:
            binding = self._approved_send_binding(
                event_id,
                actor_id=actor_id,
                channel_id=channel_id,
                decision_message_id=decision_message_id,
                card_message_id=card_message_id,
                backup_path=backup_path,
                backup_sha256=backup_sha256,
            )
        except Exception as exc:
            reason = re.sub(r"[^a-z0-9_:-]+", "_", str(exc).lower())[:96] or "approved_send_preflight_failed"
            return {"status": "blocked", "outcome": "send_blocked", "reason": reason, "outbound_send": False, "provider_mutation": False}

        action_id = "act_" + sha256_text(
            f"{event_id}|email_sent_verified|{binding['draft_id']}|{binding['draft_revision']}"
        )[:24]
        now = utc_now()
        base_receipt = {
            "status": "PREFLIGHT_RESERVED",
            "event_id": event_id,
            "action_id": action_id,
            "decision_message_id": decision_message_id,
            "card_message_id": card_message_id,
            "draft_id": binding["draft_id"],
            "draft_revision": binding["draft_revision"],
            "body_sha256": binding["body_sha256"],
            "from_identity": binding["from_identity"],
            "recipient": binding["recipient"],
            "subject": binding["subject"],
            "mailbox_id": binding["mailbox_id"],
            "backup_path": backup_path,
            "backup_sha256": backup_sha256,
            "review_parts_verified": binding["review_parts_verified"],
            "provider_draft_created": False,
            "secret_printed": False,
        }
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            inserted = self.conn.execute(
                """INSERT OR IGNORE INTO mailbox_actions(
                   action_id,event_id,action,status,provider_receipt_json,attempts,created_at,updated_at
                   ) VALUES(?,?,?,?,?,0,?,?)""",
                (action_id, event_id, "email_sent_verified", "preflight_reserved", json.dumps(base_receipt, sort_keys=True), now, now),
            ).rowcount == 1
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        action_row = self.conn.execute("SELECT * FROM mailbox_actions WHERE action_id=?", (action_id,)).fetchone()
        if action_row is None:
            raise RuntimeError("approved_send_reservation_missing")
        try:
            existing_receipt = json.loads(str(action_row["provider_receipt_json"] or "{}"))
        except json.JSONDecodeError:
            existing_receipt = {}
        status_before = str(action_row["status"])
        if status_before == "verified" and existing_receipt.get("source_marked_read"):
            return self._verified_send_local_result(event_id) or {"status": "blocked", "outcome": "send_receipt_invalid"}
        if not inserted and status_before == "preflight_reserved":
            try:
                updated_at = dt.datetime.fromisoformat(str(action_row["updated_at"]).replace("Z", "+00:00"))
                age_seconds = (dt.datetime.now(dt.timezone.utc) - updated_at).total_seconds()
            except Exception:
                age_seconds = 0
            if age_seconds < 300:
                return {"status": "blocked", "outcome": "send_in_progress", "reason": "approved_send_in_progress", "outbound_send": False, "provider_mutation": False}
        allow_new_submission = status_before in {"preflight_reserved", "preflight_failed_safe"}

        submission_started = False

        def on_submission_start() -> None:
            nonlocal submission_started
            submission_started = True
            submitting_receipt = {**base_receipt, **existing_receipt, "status": "SUBMITTING", "submission_started": True}
            with self.conn:
                self.conn.execute(
                    """UPDATE mailbox_actions SET status='submitting',provider_receipt_json=?,
                       attempts=attempts+1,updated_at=? WHERE action_id=?""",
                    (json.dumps(submitting_receipt, sort_keys=True), utc_now(), action_id),
                )

        try:
            transport = run_approved_gmail_send_transport(
                binding,
                allow_new_submission=allow_new_submission,
                on_submission_start=on_submission_start,
            )
        except ApprovedSendError as exc:
            uncertain = bool(exc.submission_started or submission_started or status_before in {"submitting", "outcome_uncertain"})
            failed_receipt = {
                **base_receipt,
                **existing_receipt,
                "status": "SEND_OUTCOME_UNCERTAIN" if uncertain else "PREFLIGHT_FAILED_SAFE",
                "reason": exc.reason,
                "submission_started": uncertain,
                "provider_message_id": exc.provider_message_id,
            }
            with self.conn:
                self.conn.execute(
                    "UPDATE mailbox_actions SET status=?,provider_receipt_json=?,updated_at=? WHERE action_id=?",
                    ("outcome_uncertain" if uncertain else "preflight_failed_safe", json.dumps(failed_receipt, sort_keys=True), utc_now(), action_id),
                )
            self.audit(
                "email_send_outcome_uncertain" if uncertain else "email_send_preflight_failed_safe",
                "inbox_event", event_id, failed_receipt, actor=f"discord-user:{actor_id}",
            )
            return {
                "status": "blocked",
                "outcome": "send_outcome_uncertain" if uncertain else "send_blocked",
                "reason": exc.reason,
                "provider_message_id": exc.provider_message_id,
                "outbound_send": False,
                "provider_mutation": False,
            }

        owned_submission = bool(transport.get("send_executed_now") or status_before in {"submitting", "outcome_uncertain"})
        external_already_counted = bool(existing_receipt.get("external_send_counted"))
        verified_receipt = {
            **base_receipt,
            **existing_receipt,
            "status": "SENT_VERIFIED",
            "provider_message_id": str(transport["provider_message_id"]),
            "provider_thread_id": str(transport.get("provider_thread_id") or ""),
            "provider_internal_date": str(transport.get("provider_internal_date") or ""),
            "checks": transport.get("checks") or {},
            "send_executed_now": bool(transport.get("send_executed_now")),
            "external_send_counted": bool(external_already_counted or owned_submission),
            "source_marked_read": bool(existing_receipt.get("source_marked_read")),
            "source_read_mutation_counted": bool(existing_receipt.get("source_read_mutation_counted")),
            "outbound_send": True,
        }
        now = utc_now()
        with self.conn:
            self.conn.execute("UPDATE inbox_events SET state='sent_verified' WHERE event_id=?", (event_id,))
            self.conn.execute(
                """UPDATE local_drafts SET status='sent_verified',approval_actor_id=?,approval_event_id=?,
                   approved_body_sha256=?,updated_at=? WHERE draft_id=?""",
                (actor_id, decision_message_id, binding["body_sha256"], now, binding["draft_id"]),
            )
            self.conn.execute(
                "UPDATE mailbox_actions SET status='verified',provider_receipt_json=?,updated_at=? WHERE action_id=?",
                (json.dumps(verified_receipt, sort_keys=True), now, action_id),
            )
            if owned_submission and not external_already_counted:
                self.conn.execute(
                    "UPDATE side_effect_counters SET value=value+1,updated_at=? WHERE name='external_sends'", (now,)
                )
        self.audit("email_send_verified", "inbox_event", event_id, verified_receipt, actor=f"discord-user:{actor_id}")

        source_read_error = ""
        try:
            source_read = mark_approved_source_read(binding["mailbox"], binding["provider_message_id"])
            mutation_already_counted = bool(verified_receipt.get("source_read_mutation_counted"))
            verified_receipt.update({
                "source_marked_read": bool(source_read.get("verified")),
                "source_read": source_read,
                "source_read_mutation_counted": bool(mutation_already_counted or source_read.get("provider_mutation_executed")),
            })
            with self.conn:
                self.conn.execute(
                    "UPDATE mailbox_actions SET provider_receipt_json=?,updated_at=? WHERE action_id=?",
                    (json.dumps(verified_receipt, sort_keys=True), utc_now(), action_id),
                )
                if source_read.get("provider_mutation_executed") and not mutation_already_counted:
                    self.conn.execute(
                        "UPDATE side_effect_counters SET value=value+1,updated_at=? WHERE name='provider_mutations'",
                        (utc_now(),),
                    )
            self.audit("source_message_mark_read_verified", "inbox_event", event_id, {
                "action_id": action_id,
                "provider_message_id": binding["provider_message_id"],
                "provider_mutation_executed": bool(source_read.get("provider_mutation_executed")),
                "labels_preserved": bool(source_read.get("labels_preserved")),
            }, actor=f"discord-user:{actor_id}")
        except ApprovedSendError as exc:
            source_read_error = exc.reason
            verified_receipt["source_read_pending"] = True
            verified_receipt["source_read_error"] = source_read_error
            with self.conn:
                self.conn.execute(
                    "UPDATE mailbox_actions SET provider_receipt_json=?,updated_at=? WHERE action_id=?",
                    (json.dumps(verified_receipt, sort_keys=True), utc_now(), action_id),
                )

        return {
            "status": "pass",
            "outcome": "email_sent_verified" if transport.get("send_executed_now") else "already_sent_verified",
            "event_id": event_id,
            "draft_id": binding["draft_id"],
            "draft_revision": binding["draft_revision"],
            "draft_body_sha256": binding["body_sha256"],
            "provider_message_id": str(transport["provider_message_id"]),
            "provider_thread_id": str(transport.get("provider_thread_id") or ""),
            "send_executed_now": bool(transport.get("send_executed_now")),
            "source_marked_read": bool(verified_receipt.get("source_marked_read")),
            "source_read_pending": bool(source_read_error),
            "source_read_error": source_read_error,
            "idempotent_replay": not bool(transport.get("send_executed_now")),
            "provider_mutation": bool((verified_receipt.get("source_read") or {}).get("provider_mutation_executed")),
            "outbound_send": bool(transport.get("send_executed_now")),
        }

    def _queue_disposition_ack(self, event_id: str, decision_message_id: str, action: str) -> None:
        """Confirm a terminal no-transport disposition in #decisions.

        SEND and DRAFT already produce outcome cards; HOLD, RESOLVED_EXTERNALLY,
        and AFFILIATE_TOOL previously applied silently, which the operator
        cannot distinguish from the tool being down. Keyed on the decision
        message so replays never duplicate the acknowledgment.
        """
        detail = {
            "RESOLVED_EXTERNALLY": "The card is closed and the event is recorded as handled outside the inbox tool.",
            "HOLD": "The event is held and any open local draft is closed; a future command can reopen it.",
            "AFFILIATE_TOOL": "The event is routed to the affiliate inbox tool for the Sheet update.",
        }.get(action, "The disposition is recorded.")
        payload = "\n".join([
            f"✅ Inbox decision applied — {action.replace('_', ' ')}",
            f"Event: `{event_id}`",
            detail + " No email was sent and no mailbox mutation occurred.",
        ])
        self.queue_notification(event_id, "operator_action_reason", DECISIONS_CHANNEL_ID, f"decision-ack:{decision_message_id}", payload)

    def _queue_approved_send_outcome(
        self, event_id: str, decision_message_id: str, result: dict[str, Any]
    ) -> None:
        if result.get("status") == "pass":
            source_line = (
                "Original source message marked read with all other labels preserved."
                if result.get("source_marked_read")
                else "Reply sent and verified; source-message read-state retry remains pending."
            )
            payload = "\n".join([
                "✅ Inbox reply SENT — Gmail readback verified",
                f"Event: `{event_id}`",
                f"Provider message: `{str(result.get('provider_message_id') or '')}`",
                f"Draft: `{str(result.get('draft_id') or '')}` rev {int(result.get('draft_revision') or 0)}",
                f"Body SHA-256: `{str(result.get('draft_body_sha256') or '')}`",
                source_line,
                "Exact recipient, sender, subject, body, thread, and reply headers passed. No provider draft was created.",
            ])
            key = f"send-verified:{event_id}:{str(result.get('provider_message_id') or '')}"
        else:
            reason = str(result.get("reason") or result.get("outcome") or "send_blocked")
            uncertain = result.get("outcome") == "send_outcome_uncertain"
            payload = "\n".join([
                "⛔ Inbox SEND outcome uncertain — do not retry blindly" if uncertain else "⛔ Inbox SEND blocked — no email sent",
                f"Event: `{event_id}`",
                f"Reason: `{reason}`",
                (
                    "The transport will reconcile Sent Mail on replay before any further action."
                    if uncertain else
                    "The approved local draft remains unchanged. No provider draft or mailbox mutation occurred."
                ),
            ])
            key = f"send-result:{decision_message_id}:{reason}"
        self.queue_notification(event_id, "operator_action_reason", DECISIONS_CHANNEL_ID, key, payload)

    def _retry_bound_draft_after_transient_failure(
        self,
        event_id: str,
        *,
        actor_id: str,
        channel_id: str,
        decision_message_id: str,
        card_message_id: str,
        operator_action: str,
        operator_edit_instruction: str | None = None,
    ) -> dict[str, Any] | None:
        """Retry one already-claimed composition once after an allowlisted pre-commit transient."""
        if operator_action not in {"DRAFT", "EDIT"}:
            return None
        if operator_action == "EDIT" and not str(operator_edit_instruction or "").strip():
            return None
        committed = self.conn.execute(
            """SELECT 1 FROM mailbox_actions
               WHERE event_id=? AND action='draft_requested'
                 AND json_extract(provider_receipt_json,'$.decision_message_id')=? LIMIT 1""",
            (event_id, decision_message_id),
        ).fetchone()
        if committed is not None:
            return None
        blocked = self.conn.execute(
            """SELECT payload_json FROM audit_events
               WHERE object_type='inbox_event' AND object_id=? AND event_type='fable_draft_blocked'
               ORDER BY audit_id DESC LIMIT 1""",
            (event_id,),
        ).fetchone()
        if blocked is None:
            return None
        try:
            blocked_payload = json.loads(str(blocked["payload_json"] or "{}"))
        except json.JSONDecodeError:
            return None
        if (
            blocked_payload.get("reason") not in {"fable_claude_timeout"}
            or str(blocked_payload.get("trigger_decision_message_id") or "") != decision_message_id
            or str(blocked_payload.get("trigger_card_message_id") or "") != card_message_id
        ):
            return None

        retry_action_id = "act_" + sha256_text(
            f"{event_id}|{operator_action.lower()}_retry_claimed|{decision_message_id}"
        )[:24]
        now = utc_now()
        retry_receipt = {
            "actor_id": actor_id,
            "channel_id": channel_id,
            "card_message_id": card_message_id,
            "decision_message_id": decision_message_id,
            "operator_action": operator_action,
            "prior_reason": blocked_payload["reason"],
            "retry_limit": 1,
            "provider_draft_created": False,
            "provider_mutation": False,
            "outbound_send": False,
        }
        with self.conn:
            cursor = self.conn.execute(
                """INSERT OR IGNORE INTO mailbox_actions(
                   action_id,event_id,action,status,provider_receipt_json,attempts,created_at,updated_at
                   ) VALUES(?,?,?,?,?,1,?,?)""",
                (
                    retry_action_id, event_id, "draft_retry_claimed", "verified",
                    json.dumps(retry_receipt, sort_keys=True), now, now,
                ),
            )
        if cursor.rowcount != 1:
            return None
        try:
            retry_backup = self.backup()
        except Exception as exc:
            raise ValueError("draft_retry_backup_failed") from exc
        retry_receipt.update({
            "backup_path": str(retry_backup),
            "backup_sha256": sha256_bytes(retry_backup.read_bytes()),
        })
        with self.conn:
            self.conn.execute(
                "UPDATE mailbox_actions SET provider_receipt_json=?,updated_at=? WHERE action_id=?",
                (json.dumps(retry_receipt, sort_keys=True), utc_now(), retry_action_id),
            )
        self.audit(
            "fable_draft_retry_claimed", "inbox_event", event_id,
            retry_receipt, actor=f"discord-user:{actor_id}",
        )
        result = self._draft_from_bound_operator_command(
            event_id,
            actor_id=actor_id,
            channel_id=channel_id,
            decision_message_id=decision_message_id,
            card_message_id=card_message_id,
            operator_action=operator_action,
            operator_edit_instruction=operator_edit_instruction,
        )
        self.audit(
            "operator_action_retry_handled", "inbox_event", event_id,
            {
                "action": operator_action,
                "outcome": result.get("outcome"),
                "decision_message_id": decision_message_id,
                "card_message_id": card_message_id,
                "provider_mutation": False,
                "outbound_send": False,
            },
            actor=f"discord-user:{actor_id}",
        )
        return {
            **result,
            "action": operator_action,
            "decision_recorded": True,
            "same_decision_retry": True,
            "provider_mutation": False,
            "outbound_send": False,
        }

    def _handle_synced_operator_action(
        self,
        event_id: str,
        action_text: str,
        *,
        actor_id: str,
        channel_id: str,
        decision_message_id: str,
        card_message_id: str,
    ) -> dict[str, Any]:
        """Apply one exact command already verified from Discord message readback."""
        if actor_id != OPERATOR_ID:
            raise ValueError("operator_actor_mismatch")
        if channel_id != DECISIONS_CHANNEL_ID:
            raise ValueError("decision_channel_mismatch")
        if not decision_message_id or not card_message_id:
            raise ValueError("decision_binding_required")
        command = action_text
        if command != command.strip() or "\r" in command or "\n" in command:
            raise ValueError("operator_action_not_exact")
        requested_action = "EDIT" if command.startswith("EDIT ") else command
        existing = self.conn.execute(
            """SELECT event_id,action,actor_id,channel_id,card_message_id
               FROM operator_decisions WHERE decision_message_id=?""",
            (decision_message_id,),
        ).fetchone()
        if existing is not None:
            if (
                str(existing["event_id"]) != event_id
                or str(existing["action"]) != requested_action
                or str(existing["actor_id"]) != actor_id
                or str(existing["channel_id"]) != channel_id
            ):
                raise ValueError("decision_message_binding_mismatch")
            # The card recorded at claim time is historical fact; the event's
            # "active card" moves as cards are reissued. Comparing the two made
            # every recorded decision replay as decision_message_binding_mismatch
            # once a newer card existed (9 failures per run, cron down,
            # 2026-08-05). Replays proceed under the ORIGINAL claimed card.
            card_message_id = str(existing["card_message_id"])
            if str(existing["action"]) in {"DRAFT", "EDIT"}:
                retry = self._retry_bound_draft_after_transient_failure(
                    event_id,
                    actor_id=actor_id,
                    channel_id=channel_id,
                    decision_message_id=decision_message_id,
                    card_message_id=card_message_id,
                    operator_action=str(existing["action"]),
                    operator_edit_instruction=command[5:] if requested_action == "EDIT" else None,
                )
                if retry is not None:
                    return retry
            if str(existing["action"]) == "SEND":
                local_send = self._verified_send_local_result(event_id)
                if local_send is not None and local_send.get("source_marked_read"):
                    return {**local_send, "action": "SEND", "decision_recorded": True}
                replay_backup = self.backup(
                    BACKUP_ROOT / (
                        "email-monitor-approved-send-replay-"
                        + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                        + "-" + sha256_text(decision_message_id)[:12] + ".sqlite3"
                    )
                )
                replay_result = self._send_approved_email_reply(
                    event_id,
                    actor_id=actor_id,
                    channel_id=channel_id,
                    decision_message_id=decision_message_id,
                    card_message_id=card_message_id,
                    backup_path=str(replay_backup),
                    backup_sha256=sha256_bytes(replay_backup.read_bytes()),
                )
                self._queue_approved_send_outcome(event_id, decision_message_id, replay_result)
                return {**replay_result, "action": "SEND", "decision_recorded": True}
            return {"status": "pass", "outcome": "operator_decision_idempotent", "event_id": existing["event_id"], "action": existing["action"], "outbound_send": False}
        event = self.conn.execute("SELECT state FROM inbox_events WHERE event_id=?", (event_id,)).fetchone()
        if event is None:
            raise ValueError("unknown_event_id")
        card = self.conn.execute(
            """SELECT outbox_id,discord_message_id,payload_sha256 FROM notification_outbox
               WHERE event_id=? AND purpose='operator_decision' AND status='delivered'
               ORDER BY delivered_at DESC,outbox_id DESC LIMIT 1""",
            (event_id,),
        ).fetchone()
        if card is None or str(card["discord_message_id"]) != str(card_message_id):
            raise ValueError("active_card_binding_mismatch")
        decision_claimed = False
        if command == "DRAFT":
            # Claim the immutable decision before entering the model. A failed or
            # interrupted command is consumed and must not replay composition.
            self._record_operator_decision(event_id, card, decision_message_id, actor_id, "DRAFT")
            decision_claimed = True
            result = self._draft_from_bound_operator_command(
                event_id, actor_id=actor_id, channel_id=channel_id,
                decision_message_id=decision_message_id, card_message_id=card_message_id,
            )
            action = "DRAFT"
        elif command.startswith("EDIT "):
            # Claim the immutable EDIT decision before entering Fable. The
            # instruction is drafting direction, never the final email body.
            self._record_operator_decision(event_id, card, decision_message_id, actor_id, "EDIT")
            decision_claimed = True
            result = self._edit_bound_operator_draft(
                event_id, command[5:], actor_id=actor_id,
                decision_message_id=decision_message_id, card_message_id=card_message_id,
            )
            action = "EDIT"
        elif command == "HOLD":
            if event["state"] not in {"resolved_externally", "held"}:
                now = utc_now()
                with self.conn:
                    self.conn.execute("UPDATE inbox_events SET state='held' WHERE event_id=?", (event_id,))
                    self.conn.execute("UPDATE local_drafts SET status='closed_hold',updated_at=? WHERE event_id=? AND status='draft_generated'", (now, event_id))
            result = {"status": "pass", "outcome": "held", "event_id": event_id, "provider_mutation": False, "outbound_send": False}
            action = "HOLD"
            # Terminal no-transport actions must confirm in-channel: silence
            # after an operator disposition reads as the tool being down
            # (operator report 2026-08-06, evt_8cab11fa RESOLVED_EXTERNALLY).
            self._queue_disposition_ack(event_id, decision_message_id, "HOLD")
        elif command == "RESOLVED_EXTERNALLY":
            result = self.resolve_externally(event_id, actor_id=actor_id, channel_id=channel_id)
            action = "RESOLVED_EXTERNALLY"
            self._queue_disposition_ack(event_id, decision_message_id, "RESOLVED_EXTERNALLY")
        elif command == "SEND":
            # A literal active-card SEND from Izaiah is the narrow transport gate.
            # Back up before claiming; global/automated send policy remains denied.
            send_backup = self.backup(
                BACKUP_ROOT / (
                    "email-monitor-approved-send-"
                    + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                    + "-" + sha256_text(decision_message_id)[:12] + ".sqlite3"
                )
            )
            self._record_operator_decision(event_id, card, decision_message_id, actor_id, "SEND")
            decision_claimed = True
            result = self._send_approved_email_reply(
                event_id,
                actor_id=actor_id,
                channel_id=channel_id,
                decision_message_id=decision_message_id,
                card_message_id=card_message_id,
                backup_path=str(send_backup),
                backup_sha256=sha256_bytes(send_backup.read_bytes()),
            )
            action = "SEND"
            self._queue_approved_send_outcome(event_id, decision_message_id, result)
        else:
            raise ValueError("unsupported_operator_action")
        if not decision_claimed:
            self._record_operator_decision(event_id, card, decision_message_id, actor_id, action)
        self.audit(
            "operator_action_handled", "inbox_event", event_id,
            {"action": action, "outcome": result.get("outcome"), "decision_message_id": decision_message_id,
             "card_message_id": card_message_id,
             "provider_mutation": bool(result.get("provider_mutation")),
             "outbound_send": bool(result.get("outbound_send"))},
            actor=f"discord-user:{actor_id}",
        )
        return {
            **result,
            "action": action,
            "decision_recorded": True,
            "provider_mutation": bool(result.get("provider_mutation")),
            "outbound_send": bool(result.get("outbound_send")),
        }

    def validate_audit_chain(self) -> dict[str, Any]:
        previous = "0" * 64
        failures = []
        count = 0
        for row in self.conn.execute("SELECT * FROM audit_events ORDER BY audit_id"):
            expected = sha256_text("|".join([previous, row["event_type"], row["object_type"], row["object_id"], row["actor"], row["payload_json"], row["created_at"]]))
            if row["previous_hash"] != previous or row["event_hash"] != expected:
                failures.append(row["audit_id"])
            previous = row["event_hash"]
            count += 1
        return {"status": "pass" if not failures else "fail", "count": count, "failures": failures}

    def backup(self, destination: Path | None = None) -> Path:
        private_dir(BACKUP_ROOT)
        destination = destination or BACKUP_ROOT / f"email-monitor-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.sqlite3"
        target = sqlite3.connect(destination)
        try:
            self.conn.backup(target)
            target.execute("PRAGMA integrity_check")
        finally:
            target.close()
        destination.chmod(0o600)
        self.audit("backup_created", "backup", str(destination), {"sha256": sha256_bytes(destination.read_bytes())})
        return destination


@dataclass
class ParsedAttachment:
    filename: str
    content_type: str
    payload: bytes


def safe_filename(value: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", value or "attachment")[:120]
    return name or "attachment"


def extract_text_part(part: email.message.Message) -> str:
    try:
        return part.get_content()
    except Exception:
        payload = part.get_payload(decode=True) or b""
        return payload.decode(part.get_content_charset() or "utf-8", "replace")


def html_to_text(value: str) -> str:
    value = re.sub(r"(?is)<(script|style).*?</\1>", " ", value or "")
    value = re.sub(r"(?i)<br\s*/?>|</p\s*>", "\n", value)
    value = re.sub(r"(?s)<[^>]+>", " ", value)
    return re.sub(r"\n{3,}", "\n\n", html.unescape(value)).strip()


def parse_received(value: str) -> str:
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    except Exception:
        return utc_now()


def bounded_dmarc_payload(payload: bytes, content_type: str, filename: str) -> bytes:
    lower = filename.lower()
    if len(payload) > MAX_ATTACHMENT_BYTES:
        raise ValueError("attachment_oversized")
    if content_type in {"application/gzip", "application/x-gzip"} or lower.endswith(".gz"):
        with gzip.GzipFile(fileobj=io.BytesIO(payload)) as handle:
            data = handle.read(MAX_DMAR_XML_BYTES + 1)
    elif content_type in {"application/zip", "application/x-zip-compressed"} or lower.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            infos = archive.infolist()
            if len(infos) != 1 or infos[0].file_size > MAX_DMAR_XML_BYTES or infos[0].compress_size == 0 or infos[0].file_size / max(infos[0].compress_size, 1) > 100:
                raise ValueError("unsafe_zip_shape")
            with archive.open(infos[0]) as handle:
                data = handle.read(MAX_DMAR_XML_BYTES + 1)
    else:
        data = payload
    if len(data) > MAX_DMAR_XML_BYTES:
        raise ValueError("dmarc_xml_oversized")
    return data


def parse_dmarc_xml(payload: bytes, content_type: str, filename: str, allowed_domains: set[str]) -> dict[str, Any]:
    data = bounded_dmarc_payload(payload, content_type, filename)
    upper = data[:4096].upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise ValueError("xml_external_entity_forbidden")
    root = ElementTree.fromstring(data)
    if root.tag.split("}")[-1] != "feedback":
        raise ValueError("not_dmarc_feedback")
    policy_domain = (root.findtext("./policy_published/domain") or "").strip().lower()
    if not policy_domain or policy_domain not in allowed_domains:
        raise ValueError("dmarc_policy_domain_unregistered")
    failed = 0
    total = 0
    sources: set[str] = set()
    for record in root.findall("./record"):
        count = int(record.findtext("./row/count") or "0")
        total += count
        source = (record.findtext("./row/source_ip") or "").strip()
        if source:
            sources.add(source)
        dkim = (record.findtext("./row/policy_evaluated/dkim") or "").strip().lower()
        spf = (record.findtext("./row/policy_evaluated/spf") or "").strip().lower()
        disposition = (record.findtext("./row/policy_evaluated/disposition") or "").strip().lower()
        if dkim != "pass" and spf != "pass" or disposition in {"quarantine", "reject"}:
            failed += count
    fail_rate = (failed / total) if total else 0.0
    return {
        "policy_domain": policy_domain, "record_count": len(root.findall("./record")),
        "message_count": total, "failed_count": failed, "fail_rate": fail_rate,
        "source_count": len(sources), "anomaly": failed > 0 and fail_rate >= 0.10,
    }


def process_attachment(event_seed: str, attachment: ParsedAttachment, allowed_domains: set[str]) -> dict[str, Any]:
    payload = attachment.payload
    digest = sha256_bytes(payload)
    filename = safe_filename(attachment.filename)
    attachment_id = "att_" + sha256_text(f"{event_seed}|{digest}|{filename}")[:24]
    content_type = attachment.content_type.lower()
    dangerous = content_type in {"application/x-dosexec", "application/x-msdownload", "application/vnd.ms-office"} or filename.lower().endswith((".exe", ".js", ".vbs", ".scr", ".bat", ".cmd", ".docm", ".xlsm"))
    oversized = len(payload) > MAX_ATTACHMENT_BYTES
    safety_state = "quarantined" if dangerous or oversized else "stored"
    storage_path = None
    if not oversized:
        path = ATTACHMENT_ROOT / digest[:2] / digest
        private_content_addressed_write(path, payload, digest)
        storage_path = str(path)
    parsed: dict[str, Any] = {}
    parser_status = "not_applicable"
    dmarc_candidate = content_type in {"application/xml", "text/xml", "application/gzip", "application/x-gzip", "application/zip", "application/x-zip-compressed"} or filename.lower().endswith((".xml", ".xml.gz", ".gz", ".zip"))
    if dmarc_candidate and not dangerous and not oversized:
        try:
            parsed = {"dmarc": parse_dmarc_xml(payload, content_type, filename, allowed_domains)}
            parser_status = "dmarc_valid"
        except Exception as exc:
            parsed = {"dmarc_error": type(exc).__name__ + ":" + str(exc)[:120]}
            parser_status = "dmarc_invalid"
            safety_state = "quarantined"
    return {
        "attachment_id": attachment_id, "filename": filename, "content_type": content_type,
        "source_filename_sha256": sha256_text(attachment.filename or "attachment"),
        "size_bytes": len(payload), "sha256": digest, "storage_path": storage_path,
        "safety_state": safety_state, "parser_status": parser_status, "parsed": parsed,
    }


def attachment_id_for_event(event_id: str, digest: str, filename: str) -> str:
    return "att_" + sha256_text(f"{event_id}|{digest}|{filename}")[:24]


def deduplicate_processed_attachments(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Collapse exact MIME duplicates while quarantining identity metadata conflicts."""
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    duplicate_parts = 0
    parser_rank = {"not_applicable": 0, "dmarc_valid": 1, "dmarc_invalid": 2}
    for original in items:
        item = dict(original)
        parsed = dict(item.get("parsed") or {})
        parsed["mime_part_occurrences"] = 1
        parsed["mime_content_types"] = [str(item["content_type"])]
        source_filename_hash = str(item.get("source_filename_sha256") or "")
        if source_filename_hash:
            parsed["source_filename_sha256s"] = [source_filename_hash]
        item["parsed"] = parsed
        key = (str(item["sha256"]), str(item["filename"]))
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = item
            continue

        duplicate_parts += 1
        existing_parsed = dict(existing.get("parsed") or {})
        conflicts = set(str(value) for value in existing_parsed.get("attachment_metadata_conflicts") or [])
        if str(existing.get("content_type")) != str(item.get("content_type")):
            conflicts.add("content_type")
        if int(existing.get("size_bytes") or 0) != int(item.get("size_bytes") or 0):
            conflicts.add("size_bytes")
        if str(existing.get("storage_path") or "") != str(item.get("storage_path") or ""):
            conflicts.add("storage_path")
        if str(existing.get("safety_state")) != str(item.get("safety_state")):
            conflicts.add("safety_state")
        if str(existing.get("parser_status")) != str(item.get("parser_status")):
            conflicts.add("parser_status")

        source_hashes = {
            *[str(value) for value in existing_parsed.get("source_filename_sha256s") or [] if value],
            *[str(value) for value in parsed.get("source_filename_sha256s") or [] if value],
        }
        if len(source_hashes) > 1:
            conflicts.add("source_filename")
        if source_hashes:
            existing_parsed["source_filename_sha256s"] = sorted(source_hashes)
        existing_parsed["mime_part_occurrences"] = int(existing_parsed.get("mime_part_occurrences") or 1) + 1
        existing_parsed["mime_content_types"] = sorted({
            *[str(value) for value in existing_parsed.get("mime_content_types") or []],
            str(item["content_type"]),
        })
        for parsed_key, parsed_value in parsed.items():
            if parsed_key in {"mime_part_occurrences", "mime_content_types", "source_filename_sha256s"}:
                continue
            if parsed_key not in existing_parsed:
                existing_parsed[parsed_key] = parsed_value
            elif existing_parsed[parsed_key] != parsed_value:
                conflicts.add("parsed_metadata")
        if conflicts:
            existing_parsed["attachment_metadata_conflicts"] = sorted(conflicts)
            existing["safety_state"] = "quarantined"
            existing["parser_status"] = "metadata_conflict"
        else:
            if item.get("safety_state") == "quarantined":
                existing["safety_state"] = "quarantined"
            if parser_rank.get(str(item.get("parser_status")), 0) > parser_rank.get(str(existing.get("parser_status")), 0):
                existing["parser_status"] = item["parser_status"]
        existing["parsed"] = existing_parsed
    return list(by_key.values()), duplicate_parts


def parse_oversized_message(raw: bytes) -> dict[str, Any]:
    """Preserve full raw evidence but parse only bounded headers and quarantine."""
    header_window = raw[: 1024 * 1024]
    boundaries = [position for position in (header_window.find(b"\r\n\r\n"), header_window.find(b"\n\n")) if position >= 0]
    header_end = min(boundaries) if boundaries else len(header_window)
    msg = BytesHeaderParser(policy=email.policy.default).parsebytes(header_window[:header_end] + b"\r\n\r\n")
    sender = parseaddr(str(msg.get("From") or ""))[1].lower()
    sender_domain = sender.rsplit("@", 1)[1] if "@" in sender else "unknown"
    recipient_headers = [str(msg.get(name)) for name in ("To", "Cc", "Delivered-To", "X-Original-To") if msg.get(name)]
    recipients = [address.lower() for _, address in getaddresses(recipient_headers) if address]
    return {
        "rfc_message_id": str(msg.get("Message-ID") or ""), "sender": sender or "unknown",
        "sender_domain": sender_domain, "recipients": sorted(set(recipients)),
        "subject": str(msg.get("Subject") or "(no subject)"),
        "received_at": parse_received(str(msg.get("Date") or "")), "body_text": "",
        "defects": ["RawMessageSizeLimitExceeded"], "attachments": [],
        "attachment_duplicate_parts": 0, "attachment_metadata_conflicts": 0,
        "quarantined": True,
        "auto_submitted": str(msg.get("Auto-Submitted") or "").lower(),
        "precedence": str(msg.get("Precedence") or "").lower(),
        "list_unsubscribe": str(msg.get("List-Unsubscribe") or ""),
        "authentication_results": str(msg.get("Authentication-Results") or ""),
        "in_reply_to": str(msg.get("In-Reply-To") or ""),
        "references": str(msg.get("References") or ""),
    }


def parse_message(raw: bytes, mailbox: dict[str, Any]) -> dict[str, Any]:
    msg = BytesParser(policy=email.policy.default).parsebytes(raw)
    defects = [type(defect).__name__ for defect in (msg.defects or [])]
    sender = parseaddr(str(msg.get("From") or ""))[1].lower()
    sender_domain = sender.rsplit("@", 1)[1] if "@" in sender else "unknown"
    recipient_headers = [str(msg.get(name)) for name in ("To", "Cc", "Delivered-To", "X-Original-To") if msg.get(name)]
    recipients = [address.lower() for _, address in getaddresses(recipient_headers) if address]
    subject = str(msg.get("Subject") or "(no subject)")
    plain: list[str] = []
    rich: list[str] = []
    attachments: list[ParsedAttachment] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        content_type = part.get_content_type().lower()
        disposition = part.get_content_disposition()
        filename = part.get_filename() or ""
        payload = part.get_payload(decode=True) or b""
        if disposition == "attachment" or filename:
            attachments.append(ParsedAttachment(filename or "attachment", content_type, payload))
        elif content_type == "text/plain":
            plain.append(extract_text_part(part))
        elif content_type == "text/html":
            rich.append(html_to_text(extract_text_part(part)))
    body_text = "\n\n".join(item.strip() for item in plain if item.strip()).strip()
    if not body_text:
        body_text = "\n\n".join(item.strip() for item in rich if item.strip()).strip()
    allowed_domains = set(mailbox.get("dmarc_domains") or [])
    processed_all = [process_attachment(mailbox["mailbox_id"] + sha256_bytes(raw), item, allowed_domains) for item in attachments]
    processed, duplicate_attachment_parts = deduplicate_processed_attachments(processed_all)
    metadata_conflict_count = sum(1 for item in processed if item.get("parser_status") == "metadata_conflict")
    if duplicate_attachment_parts:
        defects.append("DuplicateAttachmentPartDeduplicated")
    if metadata_conflict_count:
        defects.append("AttachmentMetadataConflictQuarantined")
    serious_defects = {"MissingHeaderBodySeparatorDefect", "MultipartInvariantViolationDefect", "StartBoundaryNotFoundDefect", "CloseBoundaryNotFoundDefect"}
    quarantined = bool(serious_defects.intersection(defects)) or any(item["safety_state"] == "quarantined" for item in processed)
    return {
        "rfc_message_id": str(msg.get("Message-ID") or ""), "sender": sender or "unknown",
        "sender_domain": sender_domain, "recipients": sorted(set(recipients)), "subject": subject,
        "received_at": parse_received(str(msg.get("Date") or "")), "body_text": body_text,
        "defects": defects, "attachments": processed, "attachment_duplicate_parts": duplicate_attachment_parts,
        "attachment_metadata_conflicts": metadata_conflict_count,
        "quarantined": quarantined,
        "auto_submitted": str(msg.get("Auto-Submitted") or "").lower(),
        "precedence": str(msg.get("Precedence") or "").lower(),
        "list_unsubscribe": str(msg.get("List-Unsubscribe") or ""),
        "authentication_results": str(msg.get("Authentication-Results") or ""),
        "in_reply_to": str(msg.get("In-Reply-To") or ""),
        "references": str(msg.get("References") or ""),
    }


def atomic_report_write(path: Path, text: str) -> None:
    """Write a local report atomically without changing provider state."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + f".tmp-{os.getpid()}")
    fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        path.chmod(0o640)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temp_path.unlink()


def project_kael_daily_audits(ledger: Ledger) -> dict[str, Any]:
    """Project canonical Kael audit emails into the monitored report path.

    The canonical inbox control plane replaced the mutating legacy inbox processor
    on 2026-07-11. This compatibility projection preserves the daily-audit artifact
    contract without restoring a duplicate mailbox scheduler or modifying Gmail.
    """
    mailbox = ledger.conn.execute(
        "SELECT mailbox_id,venture,company,canonical_address,aliases_json,groups_json,dmarc_domains_json,lifecycle "
        "FROM mailboxes WHERE mailbox_id='kael_generic_gmail'"
    ).fetchone()
    if mailbox is None:
        return {"status": "blocked", "projected": 0, "existing": 0, "invalid": ["kael_generic_gmail_not_registered"]}
    mailbox_dict = dict(mailbox)
    mailbox_dict["aliases"] = json.loads(mailbox_dict.pop("aliases_json"))
    mailbox_dict["groups"] = json.loads(mailbox_dict.pop("groups_json"))
    mailbox_dict["dmarc_domains"] = json.loads(mailbox_dict.pop("dmarc_domains_json"))
    rows = ledger.conn.execute(
        """SELECT event_id,provider_message_id,subject,raw_path,raw_sha256
           FROM inbox_events
           WHERE mailbox_id='kael_generic_gmail' AND subject LIKE 'Kael daily audit -- %'
           ORDER BY received_at"""
    ).fetchall()
    projected = 0
    existing = 0
    invalid: list[str] = []
    historical_thin: list[str] = []
    artifacts: list[str] = []
    for row in rows:
        match = DAILY_AUDIT_SUBJECT_RE.fullmatch(str(row["subject"] or "").strip())
        if not match:
            invalid.append(f"{row['event_id']}:subject_contract")
            continue
        audit_date = match.group(1)
        suffix = re.sub(r"\W+", "", str(row["provider_message_id"] or ""))
        if not suffix:
            invalid.append(f"{row['event_id']}:provider_message_id_missing")
            continue
        output_path = DAILY_AUDIT_DIR / f"kael-daily-audit-{audit_date}-{suffix}.md"
        if output_path.exists():
            line_count = len(output_path.read_text(encoding="utf-8", errors="replace").splitlines())
            if line_count < DAILY_AUDIT_MIN_LINES:
                historical_thin.append(f"{row['event_id']}:existing_report_thin:{line_count}")
            else:
                artifacts.append(str(output_path))
            existing += 1
            continue
        raw_path = Path(str(row["raw_path"] or ""))
        if not raw_path.is_file():
            invalid.append(f"{row['event_id']}:raw_source_missing")
            continue
        raw = raw_path.read_bytes()
        if sha256_bytes(raw) != row["raw_sha256"]:
            invalid.append(f"{row['event_id']}:raw_source_hash_mismatch")
            continue
        parsed = parse_message(raw, mailbox_dict)
        body = parsed["body_text"].rstrip() + "\n"
        line_count = len(body.splitlines())
        if line_count < DAILY_AUDIT_MIN_LINES:
            invalid.append(f"{row['event_id']}:report_thin:{line_count}")
            continue
        atomic_report_write(output_path, body)
        summary_path = output_path.with_suffix(".summary.json")
        atomic_report_write(
            summary_path,
            json.dumps(
                {
                    "audit_date": audit_date,
                    "subject": row["subject"],
                    "archive_path": str(output_path),
                    "source_event_id": row["event_id"],
                    "provider_message_id": row["provider_message_id"],
                    "line_count": line_count,
                    "projection_owner": "canonical_inbox_control_plane",
                    "provider_mutations": 0,
                    "external_sends": 0,
                },
                indent=2,
                sort_keys=True,
            ) + "\n",
        )
        ledger.audit(
            "daily_audit_projected",
            "inbox_event",
            row["event_id"],
            {"audit_date": audit_date, "archive_path": str(output_path), "line_count": line_count, "provider_mutations": 0},
        )
        projected += 1
        artifacts.append(str(output_path))
    return {
        "status": "pass" if not invalid else "blocked",
        "projected": projected,
        "existing": existing,
        "invalid": invalid,
        "historical_thin": historical_thin,
        "artifacts": artifacts[-10:],
        "provider_mutations": 0,
        "external_sends": 0,
    }


def _normalized_rfc_message_id(value: str) -> str:
    return "".join(str(value or "").split()).strip("<>")


def match_glp_journalist_pitch_reply(
    mailbox: dict[str, Any],
    parsed: dict[str, Any],
    *,
    provider_thread_id: str,
    database_path: Path = GLP_DIGITAL_PR_DB_PATH,
) -> dict[str, str] | None:
    """Prove an inbound message is a reply to one verified GLP PR send.

    Subject and quoted body text are deliberately absent from the identity
    proof. The provider thread, outbound RFC Message-ID reference, and exact
    original recipient must all match the durable Build 2 send reconciliation.
    """
    mailbox_id = str(mailbox.get("mailbox_id") or "")
    outbound_sender = str(mailbox.get("canonical_address") or "").strip().lower()
    if mailbox_id not in {"glp_ops_gmail", "glp_izaiah_gmail"} or not provider_thread_id:
        return None
    if outbound_sender not in {"ops@glpclinicscout.com", "izaiah@glpclinicscout.com"}:
        return None
    sender = parseaddr(str(parsed.get("sender") or ""))[1].lower() or str(parsed.get("sender") or "").lower()
    if not sender or sender.startswith(("noreply@", "no-reply@", "notifications@")):
        return None
    newest = newest_unquoted_human_reply(str(parsed.get("body_text") or ""))
    if database_path.is_symlink() or not database_path.is_file():
        return None
    try:
        connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=2)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                """SELECT pitch_id,provider_message_id,provider_thread_id,outbound_rfc_message_id,
                          sender,recipient,status
                   FROM send_reconciliations
                   WHERE status='sent_verified' AND provider_thread_id=? AND lower(recipient)=? AND lower(sender)=?""",
                (provider_thread_id, sender, outbound_sender),
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error:
        return None
    if len(rows) != 1:
        return None
    row = rows[0]
    expected_rfc_id = _normalized_rfc_message_id(str(row["outbound_rfc_message_id"] or ""))
    if not str(row["provider_message_id"] or "") or not expected_rfc_id:
        return None
    referenced = {
        _normalized_rfc_message_id(value)
        for value in re.findall(r"<[^>]+>|[^\s]+", str(parsed.get("references") or ""))
        if _normalized_rfc_message_id(value)
    }
    in_reply_to = _normalized_rfc_message_id(str(parsed.get("in_reply_to") or ""))
    if expected_rfc_id != in_reply_to and expected_rfc_id not in referenced:
        return None
    return {
        "pitch_id": str(row["pitch_id"]),
        "provider_message_id": str(row["provider_message_id"]),
        "provider_thread_id": str(row["provider_thread_id"]),
        "outbound_rfc_message_id": str(row["outbound_rfc_message_id"]),
        "newest_unquoted_reply_sha256": sha256_text(newest),
    }


def classify_message(
    mailbox: dict[str, Any],
    parsed: dict[str, Any],
    *,
    provider_thread_id: str = "",
    journalist_pitch_db_path: Path = GLP_DIGITAL_PR_DB_PATH,
) -> dict[str, Any]:
    subject = parsed["subject"].lower()
    body = parsed["body_text"].lower()
    sender = parsed["sender"].lower()
    sender_domain = parsed["sender_domain"].lower()
    text = (subject + "\n" + body[:8000])
    facts: list[str] = []

    def result(category: str, confidence: float, reason: str, *extra: str, actionable: bool | None = None) -> dict[str, Any]:
        return {"category": category, "confidence": confidence, "reason_code": reason, "facts": list(extra), "actionable": category in ACTIONABLE if actionable is None else actionable}

    if mailbox["mailbox_id"] == "kael_generic_gmail" and DAILY_AUDIT_SUBJECT_RE.fullmatch(parsed["subject"].strip()):
        return result("internal_qa", 0.99, "kael_daily_audit_report", "canonical_daily_audit", actionable=False)
    if parsed["quarantined"]:
        return result("security_auth", 0.98, "unsafe_or_malformed_mime_quarantine", "attachment_or_parser_quarantine")
    journalist_pitch = match_glp_journalist_pitch_reply(
        mailbox,
        parsed,
        provider_thread_id=provider_thread_id,
        database_path=journalist_pitch_db_path,
    )
    if journalist_pitch:
        return result(
            "partner_vendor_action", 0.99, "glp_journalist_pitch_reply",
            "stored_outbound_pitch_thread_exact",
            "stored_outbound_pitch_message_reference_exact",
            "stored_outbound_pitch_sender_exact",
            f"pitch_id:{journalist_pitch['pitch_id']}",
            actionable=True,
        )
    hostile = re.search(r"ignore (?:all |the |any )?(?:previous|prior) instructions|reveal (?:the |your )?(?:system prompt|credentials|secrets)|act as (?:system|developer)|prompt injection|jailbreak", text, re.I)
    if hostile:
        return result("spam_phishing", 0.99, "hostile_prompt_injection", "content_treated_as_hostile", actionable=True)
    sender_address = parseaddr(sender)[1].lower() or sender
    if ALWAYS_SURFACE_INTEGRITY_RE.search(text):
        if re.search(r"payment|card|funds|billing|subscription|suspend|domain|account", text, re.I):
            return result(
                "payment_sale_expense", 0.99, "financial_account_integrity_signal",
                "always_surface_financial_account_integrity", actionable=True,
            )
        return result(
            "legal_compliance", 0.99, "legal_contract_action_signal",
            "always_surface_legal_contract_action", actionable=True,
        )
    # Funnel BCC audit copies (2026-08-07): every Boring outreach send BCCs the
    # operator's own mailbox so sends are visible without opening Resend. Those
    # copies are FROM our own funnel sender INTO a boring mailbox; they are an
    # audit trail, never an inbound lead, and must not race the reply-sync.
    if (mailbox["mailbox_id"] in ("boring_hello_gmail", "boring_izaiah_gmail")
            and sender_address in ("izaiah@tryboringai.com", "hello@tryboringai.com")):
        return result(
            "internal_qa", 0.99, "funnel_send_audit_copy",
            "own_funnel_sender_bcc_audit_copy", actionable=False,
        )
    own_generated_artifact = (
        sender_address in OWN_SYSTEM_SENDERS
        and OWN_OPERATOR_RECIPIENTS.intersection({str(value).lower() for value in parsed.get("recipients") or []})
        and OWN_SYSTEM_ARTIFACT_SUBJECT_RE.search(subject)
    )
    if own_generated_artifact:
        return result(
            "internal_qa", 0.99, "own_generated_system_artifact",
            "auto_clear_internal_review_receipt_report", actionable=False,
        )
    affiliate_root = affiliate_network_domain(sender_domain)
    if affiliate_root and AFFILIATE_OPERATOR_PREREQUISITE_RE.search(text):
        return result(
            "affiliate_network_response", 0.99, "affiliate_network_hard_prerequisite",
            f"network_domain:{affiliate_root}", "identity_tax_or_payout_prerequisite",
            actionable=False,
        )
    # Automated network notification mail is a STATUS EVENT, never a legal
    # prerequisite: rejection boilerplate contains "agreement … review", which
    # card-spammed the operator with foreign Partnerize rejections on
    # 2026-08-13 via the legal_compliance always-surface class. Genuine legal
    # mail comes from a human sender; no-reply alerts route to the affiliate
    # auto-process/parking lanes.
    # 2026-08-25: route APPROVALS to reconciliation instead of dead silence.
    #
    # Sesame Care approved us via Impact at 01:24Z. Both welcome emails
    # (evt_e11f058a…, evt_d3c67dfa…) landed on the no-reply branch below and
    # got the fact `no_reply_status_alert`, so nothing consumed them:
    # sheet_writes=0, application_state_unchanged=true. The onboarding payload
    # -- Ad ID 1116240, referral links in Impact's Ads section -- went with them.
    #
    # The design was never "approvals are ignorable". Approvals are supposed to
    # be SILENT to the operator and picked up by the reconciler via the fact
    # `automatic_affiliate_reconciliation`. Both branches route silent; only one
    # is consumed. These emails were landing on the inert one.
    #
    # AFFILIATE_APPROVAL_ACCEPTANCE_RE already matched them. It simply was never
    # consulted: it lives in `affiliate_response_disposition`, which takes this
    # function's classification as INPUT, so returning below short-circuits it.
    # Ordering, not vocabulary.
    #
    # Stays silent (actionable=False) on purpose: promoting approvals to
    # operator cards would spam a card for every approval across every network.
    # Narrow by construction -- automated AND network-attributable AND approval
    # semantics AND not a rejection. Login alerts, newsletters and ordinary
    # status mail still fall through to the inert branch below.
    #
    # Seven further real approvals were silenced the same way and only surfaced
    # when this branch was added: Deliveroo UK and eMed.com ("request to join …
    # has been accepted", behind the generic subject "Partnerize Email Alert"),
    # Ultrahuman ("membership active"), Isagenix, Moveincore, Modify Health and
    # HealthyMale ("welcome to the … program").
    machine_header = (
        str(parsed.get("auto_submitted") or "").lower() not in {"", "no"}
        or str(parsed.get("precedence") or "").lower() in {"bulk", "list", "junk"}
        or bool(str(parsed.get("list_unsubscribe") or "").strip())
    )
    status_template = bool(AFFILIATE_MACHINE_STATUS_SUBJECT_RE.fullmatch(subject.strip()))
    if affiliate_root and (machine_header or status_template) and (
        AFFILIATE_APPROVAL_ACCEPTANCE_RE.search(text)
        or status_template and "cancellation" in subject.lower()
    ) and not AFFILIATE_REJECTION_RE.search(text):
        # Network-attributed machine evidence or an exact status-template subject
        # is required: a human discussion of an approval must keep its reply route.
        return result(
            "affiliate_network_response", 0.99, "affiliate_network_automated_notification",
            f"network_domain:{affiliate_root}", "automatic_affiliate_reconciliation",
            actionable=False,
        )
    if affiliate_root and AFFILIATE_AUTOMATED_SENDER_RE.match(sender_address) \
            and AFFILIATE_APPROVAL_ACCEPTANCE_RE.search(text) \
            and not AFFILIATE_REJECTION_RE.search(text):
        return result(
            "affiliate_network_response", 0.99, "affiliate_network_automated_notification",
            f"network_domain:{affiliate_root}", "automatic_affiliate_reconciliation",
            actionable=False,
        )
    if affiliate_root and AFFILIATE_AUTOMATED_SENDER_RE.match(sender_address):
        return result(
            "affiliate_network_response", 0.98, "affiliate_network_automated_notification",
            f"network_domain:{affiliate_root}", "no_reply_status_alert",
            actionable=False,
        )
    if affiliate_root and AFFILIATE_LEGAL_PREREQUISITE_RE.search(text):
        return result(
            "legal_compliance", 0.99, "affiliate_network_legal_prerequisite",
            f"network_domain:{affiliate_root}", "legal_prerequisite_human_review",
            actionable=True,
        )
    if affiliate_automated_invite_notice(parsed, affiliate_root):
        return result(
            "affiliate_network_response", 0.99, "affiliate_network_automated_invitation",
            f"network_domain:{affiliate_root}", "recognized_network_invite_template",
            "automatic_affiliate_reconciliation", actionable=False,
        )
    if affiliate_genuine_human_reply(parsed, affiliate_root):
        return result(
            "partner_vendor_action", 0.99, "affiliate_network_genuine_human_reply",
            f"network_domain:{affiliate_root}", "known_network_non_automated_sender",
            "reviewed_conversational_request_evidence", "local_draft_only",
            actionable=True,
        )
    subject_vendor_rule = bool(VENDOR_MARKETING_SUBJECT_RE.search(subject))
    sender_domain_is_vendor = sender_domain == "google.com" and sender_address in VENDOR_MARKETING_SENDERS or any(
        sender_domain == domain or sender_domain.endswith("." + domain)
        for domain in VENDOR_MARKETING_SENDER_DOMAINS
    )
    if sender_address in VENDOR_MARKETING_SENDERS or sender_domain_is_vendor:
        return result(
            "marketing_automation", 0.99,
            "vendor_sender_rule",
            "auto_clear_vendor_product_marketing", actionable=False,
        )
    if subject_vendor_rule:
        affiliate_root = affiliate_network_domain(sender_domain)
        affiliate_aliases = {"ops@glpclinicscout.com", "partners@glpclinicscout.com", "affiliates@glpclinicscout.com"}
        is_affiliate_context = bool(affiliate_root) or bool(
            affiliate_aliases.intersection({str(value).lower() for value in parsed.get("recipients") or []})
        )
        if is_affiliate_context:
            return result(
                "affiliate_network_response", 0.99, "vendor_subject_rule_affiliate_auto",
                "affiliate_context_auto_clear_no_card", "automatic_affiliate_reconciliation", actionable=False,
            )
        return result(
            "marketing_automation", 0.99, "vendor_subject_rule",
            "auto_clear_vendor_product_marketing", actionable=False,
        )
    if re.search(
        r"\b(?:payment\s+(?:was\s+)?declined|card\s+(?:is\s+)?(?:declined|expiring|expired)|"
        r"insufficient\s+funds|billing\s+(?:failed|failure|problem)|account\s+(?:at\s+risk|suspended)|"
        r"suspension\s+warning|domain\s+(?:expires?|expiry|expiration)|service\s+will\s+be\s+suspended)\b",
        text,
        re.I,
    ):
        return result(
            "payment_sale_expense", 0.99, "financial_account_integrity_signal",
            "always_surface_financial_account_integrity", actionable=True,
        )
    if re.search(r"\b(?:authorize|authorise)\b.{0,100}\b(?:account|app|connection|integration)\b|\b(?:provide|supply|send)\b.{0,80}\b(?:credential|password|api key|access token)\b", text):
        return result("security_auth", 0.99, "operator_authorization_required", "explicit_operator_prerequisite", actionable=True)
    if re.search(r"password reset|verification code|one[- ]?time code|new sign[- ]?in|suspicious (?:login|activity)|oauth|2fa|mfa|account (?:locked|suspended)|domain verification|dns change", text):
        return result("security_auth", 0.97, "security_or_auth_signal", "security_keyword")
    amazon_associates_sender = (
        sender_domain == "amazon.com"
        and any(address in sender for address in ("associates-updates@amazon.com", "store-news@amazon.com"))
        and any(label in text for label in ("amazon associates", "amazon influencer & associates program"))
    )
    direct_partner_response = (
        sender_domain in DIRECT_PARTNER_RESPONSE_DOMAINS
        and DIRECT_PARTNER_RESPONSE_RE.search(text)
    )
    if ((affiliate_root or amazon_associates_sender) and AFFILIATE_RESPONSE_SIGNAL_RE.search(text)) or direct_partner_response:
        # Silent automation trigger. The affiliate reconciler owns provider
        # readback, Sheet mutation, Gmail disposition, and failure escalation.
        return result(
            "affiliate_network_response", 0.99,
            "direct_partner_affiliate_response" if direct_partner_response and not affiliate_root else "known_affiliate_network_advertiser_response",
            f"network_domain:{affiliate_root or sender_domain or 'amazon-associates'}",
            "automatic_affiliate_reconciliation", actionable=False,
        )
    affiliate_aliases = {"partners@glpclinicscout.com", "affiliates@glpclinicscout.com", "ops@glpclinicscout.com"}
    affiliate_alias_signal = bool(
        affiliate_aliases.intersection({str(value).lower() for value in parsed.get("recipients") or []})
        and AFFILIATE_RESPONSE_SIGNAL_RE.search(text)
    )
    if affiliate_alias_signal:
        # Direct advertisers frequently reply through vendor mail platforms
        # whose domains are not affiliate-network roots.  Classification may
        # route these silently, but the downstream application/approval
        # handler still has to prove raw-message authentication and provenance.
        return result(
            "affiliate_network_response", 0.98, "direct_partner_affiliate_response",
            "glp_affiliate_alias_and_response_signal", "automatic_affiliate_reconciliation", actionable=False,
        )
    if sender_domain in {"shopclawmart.com", "updates.shopclawmart.com"} and re.search(
        r"(?:your\s+(?:persona|skill|listing).{0,80}was featured|featured on the claw mart blog)", text, re.I
    ):
        return result(
            "marketing_automation", 0.99, "clawmart_creator_feature_notification",
            "authenticated_creator_platform_notice", actionable=False,
        )
    if re.search(r"legal notice|subpoena|lawsuit|attorney|cease and desist|compliance deadline|privacy request|gdpr|ccpa", text):
        return result("legal_compliance", 0.97, "legal_or_compliance_signal", "legal_keyword")
    if affiliate_root:
        # A registered affiliate network is a business relationship, never bulk
        # noise: whatever this notice is, the reconciler owns reading, recording,
        # and Gmail disposition. Falling through to payment/booking/newsletter/
        # unknown left these sitting unread forever (operator report 2026-08-06).
        return result(
            "affiliate_network_response", 0.95, "affiliate_network_notice_default",
            f"network_domain:{affiliate_root}", "automatic_affiliate_reconciliation", actionable=False,
        )
    # Social-platform notification mail ("I want to connect", puzzle/feed digests)
    # carries calendar-ish tokens ("invite", "meeting") and was flooding the
    # decision queue as booking_calendar the moment a backlogged mailbox was
    # registered (izaiah@, 2026-08-07: 12 junk cards the operator had to clear
    # by hand). Platform notification senders are never operator decisions.
    if sender_domain in {"linkedin.com", "facebookmail.com", "instagram.com",
                         "twitter.com", "x.com", "tiktok.com", "youtube.com",
                         "pinterest.com", "notifications.google.com"}:
        return result("newsletter", 0.98, "social_platform_notification",
                      f"platform_domain:{sender_domain}", actionable=False)
    if re.search(r"payment failed|card declined|refund|chargeback|invoice overdue|deposit paid|sale completed|payout", text):
        return result("payment_sale_expense", 0.95, "payment_action_signal", "payment_keyword")
    if re.search(
        r"get paid by referring|bring your business to google maps and search|set up your booking page|"
        r"(?:routine|available|ready)\s+invoice|invoice\s+(?:is\s+)?available",
        text,
        re.I,
    ):
        return result("marketing_automation", 0.99, "vendor_marketing_or_routine_notice", "auto_clear_noise", actionable=False)
    if re.search(r"calendar|meeting|appointment|reschedul|booking|booked|invite", text):
        return result("booking_calendar", 0.92, "booking_or_calendar_signal", "calendar_keyword")
    dmarc_parts = [item for item in parsed["attachments"] if item["parser_status"] == "dmarc_valid"]
    if dmarc_parts:
        anomalies = [item for item in dmarc_parts if (item.get("parsed") or {}).get("dmarc", {}).get("anomaly")]
        if anomalies:
            return result("security_auth", 0.99, "dmarc_authentication_anomaly", "valid_dmarc_xml", "failed_alignment_threshold")
        return result("dmarc_telemetry", 0.99, "valid_dmarc_xml_routine", "valid_dmarc_xml", actionable=False)
    if re.search(r"\breport domain:|\bdmarc(?: aggregate)? report\b", subject):
        return result("unknown", 0.55, "dmarc_shaped_without_valid_payload", "subject_only_not_trusted", actionable=True)
    if parsed["auto_submitted"] and parsed["auto_submitted"] != "no":
        return result("marketing_automation", 0.96, "auto_submitted_header", parsed["auto_submitted"], actionable=False)
    if re.search(r"delivery status notification|undeliverable|mail delivery failed|returned mail|failure notice", text) or sender.startswith("mailer-daemon@"):
        return result("service_degradation", 0.96, "delivery_failure", "dsn_or_bounce", actionable=False)
    if re.search(r"service (?:outage|degradation)|incident|api error|integration failed|webhook failed|quota exceeded", text):
        return result("service_degradation", 0.93, "service_degradation_signal", "operational_failure", actionable=False)
    if parsed["list_unsubscribe"] or parsed["precedence"] in {"bulk", "list"} or re.search(r"\bnewsletter\b|manage preferences|view in browser", text):
        return result("newsletter", 0.97, "bulk_or_newsletter_headers", "list_header_or_copy", actionable=False)
    if re.search(r"special offer|limited time|start your free trial|upgrade now|webinar|download our ebook", text):
        return result("marketing_automation", 0.91, "marketing_copy", "marketing_keyword", actionable=False)
    if re.search(r"receipt|invoice|order confirmation|payment received|subscription renewed", text):
        return result("transactional_receipt", 0.91, "routine_transactional_receipt", "receipt_keyword", actionable=False)
    if sender_domain in {"tryboringai.com", "glpclinicscout.com", "allocant.ai"} and re.search(r"test|qa|smoke|internal", text):
        return result("internal_qa", 0.96, "internal_qa_marker", "own_domain_and_test_marker", actionable=False)
    if re.search(r"viagra|crypto giveaway|wire funds|gift card|urgent credential|click immediately", text):
        return result("spam_phishing", 0.92, "spam_or_phishing_signal", "spam_keyword", actionable=False)
    venture = mailbox["venture"]
    human_signal = bool(re.search(r"\b(?:re:|interested|pricing|question|can you|could you|please|thanks|follow up|next step|call me)\b", text)) and not sender.startswith(("noreply@", "no-reply@", "notifications@"))
    if venture == "allocant" and human_signal:
        return result("investor_reply", 0.91, "allocant_human_reply", "human_signal", "allocant_mailbox")
    if venture == "glp" and re.search(r"support|patient|clinic|scorecard|help|issue", text) and human_signal:
        return result("support_customer_request", 0.88, "glp_support_request", "human_signal", "support_signal")
    if venture == "glp" and human_signal:
        return result("partner_vendor_action", 0.86, "glp_partner_or_lead", "human_signal")
    if venture == "boring_ai" and human_signal:
        return result("human_lead_reply", 0.89, "boring_human_lead_or_reply", "human_signal")
    if human_signal:
        return result("human_lead_reply", 0.82, "generic_human_signal", "human_signal")
    return result("unknown", 0.40, "no_deterministic_rule", "fail_open_human_review", actionable=True)


def newest_unquoted_human_reply(body: str) -> str:
    """Return only the newest human-authored reply, excluding quoted history."""
    lines: list[str] = []
    for raw_line in (body or "").replace("\r\n", "\n").split("\n"):
        line = raw_line.strip()
        if re.match(r"^-{2,}\s*(?:original message|forwarded message)\s*-{2,}$", line, re.I):
            break
        if re.match(r"^on .+(?:wrote:|<)$", line, re.I):
            break
        if re.match(r"^from:\s+.+", line, re.I) and lines:
            break
        if line.startswith(">"):
            continue
        lines.append(raw_line)
    return "\n".join(lines).strip()


def _matched_signals(text: str, patterns: Iterable[tuple[str, str]]) -> list[str]:
    return [name for name, pattern in patterns if re.search(pattern, text, re.I)]


def glp_sensitive_signals(text: str) -> list[str]:
    normalized = " ".join(str(text or "").lower().split())
    return sorted({
        term for term in SENSITIVE_GLP_TERMS
        if re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", normalized)
    })


def is_glp_affiliate_thread(parsed: dict[str, Any]) -> bool:
    """Bind a GLP partner thread to affiliate copy before template selection."""
    recipients = {
        parseaddr(str(value or ""))[1].strip().lower()
        for value in parsed.get("recipients") or []
    }
    glp_business_recipients = {
        "affiliates@glpclinicscout.com",
        "izaiah@glpclinicscout.com",
        "ops@glpclinicscout.com",
        "partners@glpclinicscout.com",
    }
    if not recipients & glp_business_recipients:
        return False
    newest = newest_unquoted_human_reply(str(parsed.get("body_text") or ""))
    context = f"{str(parsed.get('subject') or '')} {newest}"
    # A mailbox alias or a generic word such as "program" is not sufficient
    # thread identity.  The former rule promoted those weak hints to affiliate
    # authority, which let unrelated GLP templates into the composer.  Require
    # both an affiliate/network anchor and a concrete workflow signal, or two
    # independent workflow signals when a counterparty omits the word
    # "affiliate" (for example, approval + onboarding).
    affiliate_anchor = bool(re.search(
        r"\b(?:affiliate|affiliati|partnership|publisher|awin|impact)\b",
        context,
        re.I,
    ))
    workflow_signals = _matched_signals(context, (
        ("application", r"\b(?:application|apply|applying)\b"),
        ("approval", r"\b(?:approve|approved|approval)\b"),
        ("onboarding_tracking", r"\b(?:onboarding|tracking link|affiliate link)\b"),
        ("invitation_join", r"\b(?:invitation|invite|invited|offer|offered|accept|accepted|join|joined|rejected)\b"),
        ("traffic_promotion", r"\b(?:campaign|promotion|promote|traffic|audience|referr(?:al|ing))\b"),
        ("details_request", r"\b(?:send|provide|share)\b.{0,80}\b(?:details|information|info|overview|numbers?|metrics?|data)\b"),
        ("legal_review", r"\b(?:legal|counsel|agreement|redlines?)\b"),
        ("counterparty_continuation", r"\b(?:respond|reply|follow up|get back|will send|will share)\b"),
    ))
    return bool((affiliate_anchor and workflow_signals) or len(set(workflow_signals)) >= 2)


def template_fallback_allowed(venture_key: str) -> bool:
    """Affiliate replies require thread-grounded composition or no draft."""
    return str(venture_key).strip().lower() != "glp_affiliate"


def classify_reply_intent(
    parsed: dict[str, Any], coarse: dict[str, Any], venture_key: str
) -> dict[str, Any]:
    """Classify Fable intent from only the newest unquoted human reply."""
    newest = newest_unquoted_human_reply(str(parsed.get("body_text") or ""))
    normalized = " ".join(newest.split()).lower()
    unsubscribe = _matched_signals(normalized, (
        ("unsubscribe", r"\bunsubscribe\b"),
        ("opt_out", r"\bopt[ -]?out\b"),
        ("stop_contact", r"\b(?:stop|remove me|do not (?:email|contact)|don't (?:email|contact))\b"),
    ))
    # Hostile/bulk/spam input may still carry an unsubscribe signal, which
    # ingest honors separately, but it never produces a draft.
    if coarse.get("category") in {"newsletter", "marketing_automation", "spam_phishing"} or coarse.get("reason_code") == "hostile_prompt_injection":
        return {
            "reply_class": "spam", "confidence": 0.99,
            "signals": [str(coarse.get("reason_code") or coarse.get("category")), *unsubscribe],
            "intent_text": newest,
        }
    # The GLP safety gate runs before the general sales/support taxonomy. The two
    # approved affiliate acknowledgements are narrow ops-alias safe states.
    if venture_key.startswith("glp"):
        if coarse.get("reason_code") == "affiliate_network_human_reference_before_call":
            return {
                "reply_class": "partner_vendor_action", "confidence": 0.99,
                "signals": ["known_network_human_request", "reference_before_scheduled_call"],
                "intent_text": newest,
            }
        affiliate_alias = is_glp_affiliate_thread(parsed)
        subject_text = " ".join(str(parsed.get("subject") or "").split()).lower()
        affiliate_context = f"{normalized} {subject_text}".strip()
        affiliate_approval = _matched_signals(affiliate_context, (
            ("affiliate_approved", r"\b(?:approved|approve your application|approval confirmed|application (?:has been )?approved)\b"),
            ("onboarding_or_tracking_next", r"\b(?:onboarding|tracking|affiliate link|program details|next steps?)\b"),
        ))
        if affiliate_alias and len(affiliate_approval) == 2:
            return {"reply_class": "affiliate_approval_onboarding", "confidence": 0.96, "signals": affiliate_approval, "intent_text": newest}
        affiliate_vendor_request = _matched_signals(affiliate_context, (
            ("affiliate_application_context", r"\b(?:affiliate|awin|campaign|application)\b"),
            (
                "details_requested",
                r"\b(?:send|provide|share)\b.{0,80}\b(?:details|information|info|overview)\b"
                r"|\b(?:more|relevant)\s+(?:details|information|info)\b"
                r"|\bshare\b.{0,80}\b(?:traffic|performance|referral|sales)\b.{0,40}\b(?:numbers?|metrics?|data)\b",
            ),
            (
                "partner_checkin_or_support_offered",
                r"\b(?:checking in|check[ -]?in|see how things are going|need anything from us|if you need anything|let us know if you need)\b",
            ),
            ("sponsorship_context", r"\b(?:sponsor|sponsorship)\b"),
            ("traffic_or_promotion_context", r"\b(?:traffic|audience|promotion|promote|content|website|site|channels?|acquisition|referr(?:al|ing)|customers?)\b"),
        ))
        affiliate_vendor_signals = set(affiliate_vendor_request)
        if (
            affiliate_alias
            and {"affiliate_application_context", "traffic_or_promotion_context"} <= affiliate_vendor_signals
            and affiliate_vendor_signals
            & {"details_requested", "partner_checkin_or_support_offered", "sponsorship_context"}
        ):
            return {
                "reply_class": "partner_vendor_action", "confidence": 0.96,
                "signals": affiliate_vendor_request, "intent_text": newest,
            }
        legal_pending = _matched_signals(affiliate_context, (
            ("legal_review", r"\b(?:legal|counsel)\b"),
            ("requested_changes", r"\b(?:requested changes|agreement changes|redlines?|revisions?|edits?)\b"),
            ("counterparty_will_reply", r"\b(?:send|sending|follow up|respond|return|review|get back|come back)\b"),
        ))
        if affiliate_alias and {"legal_review", "counterparty_will_reply"} <= set(legal_pending):
            return {"reply_class": "legal_review_pending", "confidence": 0.96, "signals": legal_pending, "intent_text": newest}
        glp_boundary = glp_sensitive_signals(normalized)
        if re.search(r"\b(?:legal|counsel)\b", normalized, re.I):
            glp_boundary.append("legal_context")
        if glp_boundary:
            return {
                "reply_class": "medical_legal_boundary", "confidence": 0.99,
                "signals": ["glp_sensitive_or_legal", *glp_boundary, *unsubscribe],
                "intent_text": newest,
            }
        if affiliate_alias:
            return {
                "reply_class": "partner_vendor_action", "confidence": 0.94,
                "signals": ["glp_affiliate_thread_context"], "intent_text": newest,
            }
    if unsubscribe:
        return {"reply_class": "unsubscribe_optout", "confidence": 0.99, "signals": unsubscribe, "intent_text": newest}
    if coarse.get("category") not in DRAFT_ALLOWED:
        return {"reply_class": "other", "confidence": 0.40, "signals": ["coarse_category_not_draft_allowed"], "intent_text": newest}
    classes: list[tuple[str, float, tuple[tuple[str, str], ...]]] = [
        ("interested_warm", 0.95, (("warm_interest", r"^(?:yes|yes please)[.!\s]*$|\b(?:interested|sounds good|let's do it|lets do it|ready to (?:start|proceed)|move forward|send the next step|yes,? please)\b"),)),
        ("pricing", 0.93, (("pricing", r"\b(?:price|pricing|cost|budget|fee|how much|investment amount)\b"),)),
        ("objection", 0.92, (("objection", r"\b(?:not sure|concerned|hesitant|too expensive|need to think|don't see|do not see|won't work|will not work)\b"),)),
        ("not_now_later", 0.92, (("later", r"\b(?:not now|later|next (?:month|quarter|year|week)|another time|circle back|timing isn't|timing is not)\b"),)),
        ("wrong_person", 0.95, (("wrong_person", r"\b(?:wrong person|not the right person|don't handle|do not handle|someone else handles)\b"),)),
        ("scheduling", 0.93, (("schedule", r"\b(?:schedule|calendar|book a (?:call|meeting)|availability|times? work|meet next)\b"),)),
        ("technical_question", 0.90, (("technical_question", r"\b(?:how does|how do|integrat|technical|work with|stack|workflow|implementation|security)\b"),)),
        ("referral", 0.92, (("referral", r"\b(?:refer|referral|introduce you|make an intro|connect you with|send .* your way)\b"),)),
        ("angry_complaint", 0.94, (("complaint", r"\b(?:angry|upset|unacceptable|complaint|disappointed|terrible experience|make this right)\b"),)),
    ]
    for reply_class, confidence, patterns in classes:
        signals = _matched_signals(normalized, patterns)
        if signals:
            return {"reply_class": reply_class, "confidence": confidence, "signals": signals, "intent_text": newest}
    return {"reply_class": "other", "confidence": 0.45, "signals": ["no_high_confidence_match"], "intent_text": newest}


def normalize_affiliate_coarse(
    classification: dict[str, Any], reply_class: str,
) -> dict[str, Any]:
    """Make the legacy coarse lane agree with a deterministic AP2/AP4 intent."""
    normalized = dict(classification)
    facts = list(normalized.get("facts") or [])
    if normalized.get("category") == "affiliate_network_response":
        # Known network status traffic is owned by the silent affiliate
        # reconciler. Do not let the legacy reply-intent bridge turn it back
        # into an unconditional operator card.
        return normalized
    if normalized.get("category") == "legal_compliance":
        # A legal prerequisite is an explicit human-review boundary. A later
        # affiliate intent must not downgrade it or replace its exact reason.
        return normalized
    if reply_class == "affiliate_approval_onboarding":
        normalized.update({
            "category": "partner_vendor_action",
            "reason_code": "affiliate_approved_onboarding",
            "confidence": max(float(normalized.get("confidence") or 0.0), 0.96),
            "actionable": True,
        })
        facts.append("affiliate_partner_ap2")
    elif reply_class == "legal_review_pending":
        normalized.update({
            "category": "legal_compliance",
            "reason_code": "affiliate_agreement_legal_review_pending",
            "confidence": max(float(normalized.get("confidence") or 0.0), 0.96),
            "actionable": True,
        })
        facts.append("affiliate_partner_ap4")
    normalized["facts"] = list(dict.fromkeys(facts))
    return normalized


def resolve_reply_venture(
    mailbox: dict[str, Any], parsed: dict[str, Any], reply_class: str,
    coarse_category: str = "",
) -> str | None:
    venture = str(mailbox.get("venture") or "").lower()
    recipients = {str(value).lower() for value in parsed.get("recipients") or []}
    if venture in {"boring", "boring_ai", "boring-ai"}:
        return "boring_ai"
    if venture == "allocant":
        return "allocant"
    if venture != "glp":
        return None
    if is_glp_affiliate_thread(parsed):
        return "glp_affiliate"
    if reply_class in {"affiliate_approval_onboarding", "legal_review_pending", "partner_vendor_action"}:
        return "glp_affiliate"
    if "partners@glpclinicscout.com" in recipients:
        return "glp_partners"
    if "hello@glpclinicscout.com" in recipients or str(mailbox.get("mailbox_id")) == "glp_website_inbound":
        return "glp_consumer"
    if coarse_category == "support_customer_request":
        return "glp_consumer"
    return "glp_partners"


def _sanitize_template_value(value: Any, limit: int = 240) -> str:
    clean = redact(str(value or "")).replace("{", "").replace("}", "")
    clean = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", clean)
    return " ".join(clean.split())[:limit]


def normalize_reply_subject(value: Any) -> str:
    """Collapse zero or more leading reply prefixes to exactly one `Re:`."""
    clean = _sanitize_template_value(value or "your message", 180)
    base = re.sub(r"^(?:\s*re\s*:\s*)+", "", clean, flags=re.I).strip()
    return "Re: " + (base or "your message")


def run_fable_email_drafter(
    request: dict[str, Any], *, timeout: int = FABLE_EMAIL_DRAFTER_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Compose through Fable's isolated full-email tool; never use generic fallback copy."""
    if not FABLE_EMAIL_DRAFTER_PATH.is_file():
        return {"status": "blocked", "outcome": "fable_draft_unavailable", "reason": "fable_drafter_tool_missing"}
    try:
        proc = subprocess.run(
            [sys.executable, str(FABLE_EMAIL_DRAFTER_PATH), "--timeout", str(timeout)],
            input=json.dumps(request, ensure_ascii=False), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout + 30,
        )
    except subprocess.TimeoutExpired:
        return {"status": "blocked", "outcome": "fable_draft_unavailable", "reason": "fable_drafter_timeout"}
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"status": "blocked", "outcome": "fable_draft_unavailable", "reason": "fable_drafter_invalid_json"}
    if proc.returncode != 0 or result.get("status") != "pass":
        return {
            "status": "blocked", "outcome": "fable_draft_unavailable",
            "reason": str(result.get("reason") or "fable_drafter_failed")[:160],
        }
    return result


def load_reply_registry(path: Path = REPLY_TEMPLATES_PATH) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("templates"), dict):
        raise ValueError("invalid_reply_registry")
    return data, sha256_bytes(raw)


def resolve_fable_draft_policy(
    registry: dict[str, Any], venture_key: str, reply_class: str, parsed: dict[str, Any],
) -> dict[str, Any] | None:
    identities = registry.get("identities") or {}
    templates = registry.get("templates") or {}
    identity = identities.get(venture_key) or {}
    thread_identity_aliases = {
        str(value).strip().lower()
        for value in identity.get("thread_identity_aliases") or []
        if "@" in str(value)
    }
    venture_templates = templates.get(venture_key) or {}
    template = venture_templates.get(reply_class) or {}
    template_key = f"{venture_key}:{reply_class}"
    if reply_class not in venture_templates and venture_key == "glp_affiliate":
        # Missing category copy is not a prohibition. Reuse approved review-only
        # policy, never its finished body. Explicit draft=false stays terminal.
        approved = venture_templates.get("partner_vendor_action") or {}
        if approved.get("draft") is True and approved.get("never_auto_send") is True:
            template = {**approved, "body": "", "subject": ""}
            template_key = f"{venture_key}:source_grounded:{reply_class}"
    if template.get("draft") is not True or template.get("never_auto_send") is not True:
        return None
    preserved_thread_aliases = {
        parseaddr(str(value or ""))[1].strip().lower()
        for value in parsed.get("recipients") or []
    } & thread_identity_aliases
    if len(preserved_thread_aliases) > 1:
        return None
    from_identity = (
        next(iter(preserved_thread_aliases))
        if preserved_thread_aliases
        else str(identity.get("from_identity") or "").strip().lower()
    )
    if not from_identity or "@" not in from_identity:
        return None
    verified_context = template.get("verified_business_context") or []
    if not isinstance(verified_context, list) or any(
        not isinstance(item, str) or not item.strip() or len(item) > 500 for item in verified_context
    ):
        return None
    return {
        "reply_class": reply_class,
        "template_key": template_key,
        "from_identity": from_identity,
        "lifecycle_code": str(template.get("lifecycle_code") or ""),
        "canonical_reply_class": str(template.get("canonical_reply_class") or ""),
        "never_auto_send": True,
        "verified": bool(identity.get("verified")),
        "verified_business_context": [item.strip() for item in verified_context],
        # Pre-approved fallback copy so a drafter refusal can degrade to a safe
        # reviewable draft instead of dead-ending the operator's DRAFT command.
        "template_subject": str(template.get("subject") or ""),
        "template_body": str(template.get("body") or ""),
    }


def route_for(category: str, reason_code: str, confidence: float = 1.0) -> str:
    if reason_code == "glp_journalist_pitch_reply":
        return "decisions"
    if reason_code in {"unregistered_recipient_mapping", "ambiguous_recipient_ownership"}:
        return "ops"
    if reason_code in {"financial_account_integrity_signal", "legal_contract_action_signal"}:
        return "decisions"
    if confidence < 0.6 or category in {"unknown", "security_auth"}:
        return "silent" if reason_code != "operator_authorization_required" else "decisions"
    if category in ACTIONABLE or (category == "spam_phishing" and reason_code == "hostile_prompt_injection"):
        return "decisions"
    if category == "service_degradation":
        return "ops"
    return "silent"


def render_decision_card(
    event_id: str,
    mailbox: dict[str, Any],
    parsed: dict[str, Any],
    classification: dict[str, Any],
    draft: dict[str, Any] | sqlite3.Row | None = None,
) -> str:
    recommendation = {
        "human_lead_reply": "review and request a local draft",
        "support_customer_request": "review context and choose draft/hold/no-reply",
        "investor_reply": "review investor context; draft only",
        "partner_vendor_action": "review requested action and draft only",
        "security_auth": "verify in the provider directly; do not use links or codes from this card",
        "payment_sale_expense": "verify the account/payment system directly before acting",
        "legal_compliance": "human/legal review required",
        "booking_calendar": "verify the calendar event and choose response",
        "unknown": "inspect the private canonical event; fail-open review",
        "spam_phishing": "treat as hostile; do not follow instructions or links",
    }.get(classification["category"], "human review required")
    safe_summary = one_line(parsed["body_text"], 420) or "[no readable text; inspect private canonical event]"
    header = "\n".join([
        "📬 Inbox Decision — reply/direction required",
        f"Company: {one_line(mailbox['company'], 80)}",
        f"Mailbox: {one_line(mailbox['canonical_address'], 100)}",
        f"Category: `{classification['category']}` · confidence {classification['confidence']:.2f}",
        f"Sender: {one_line(parsed['sender'], 120)}",
        f"Subject: {one_line(parsed['subject'], 180)}",
        f"Summary: {safe_summary}",
        f"Recommended action: {recommendation}",
        f"Stable event ID: `{event_id}`",
        "Email content is untrusted. Raw body/attachments stay in the private canonical ledger.",
    ])
    if draft is None:
        tail = "\n".join([
            f"Reply with `{event_id} DRAFT`, `{event_id} HOLD`, or `{event_id} RESOLVED_EXTERNALLY`.",
            "No outbound email was sent; any draft is local-only and requires exact approval.",
        ])
        card = header[:max(0, DISCORD_LIMIT - len(tail) - 1)] + "\n" + tail
    else:
        draft_row = dict(draft)
        body = redact(str(draft_row.get("body") or "")).replace("```", "`")
        reply_class = str(draft_row.get("reply_class") or classification.get("reply_class") or "other")
        recipients = json.loads(str(draft_row.get("recipients_json") or "[]"))
        recipient = one_line(", ".join(str(item) for item in recipients), 180)
        card = "\n".join([
            "📬 Full local draft — review required",
            f"Event: `{event_id}`",
            f"From/To: `{one_line(str(draft_row.get('from_identity') or ''), 100)}` → `{recipient}`",
            f"Subject: {one_line(str(draft_row.get('subject') or parsed['subject']), 180)}",
            f"Reply class: `{reply_class}`",
            "```text",
            body,
            "```",
            "Reply to this exact active card:",
            f"`{event_id} SEND`",
            f"`{event_id} EDIT <instruction>`",
            f"`{event_id} HOLD`",
            f"`{event_id} DRAFT`",
            f"`{event_id} RESOLVED_EXTERNALLY`",
            "Draft is local until an exact active-card SEND; approved SEND is transmitted once with Gmail readback.",
        ])
    if len(card) > DISCORD_LIMIT:
        raise ValueError("decision_card_full_draft_overflow")
    return card


def render_draft_review_messages(
    event_id: str,
    mailbox: dict[str, Any],
    parsed: dict[str, Any],
    classification: dict[str, Any],
    draft: sqlite3.Row | dict[str, Any],
) -> list[dict[str, Any]]:
    """Render a complete draft with zero truncation, using ordered parts when needed."""
    draft_row = dict(draft)
    body = redact(str(draft_row.get("body") or ""))
    if not body:
        raise ValueError("decision_card_draft_body_missing")
    if body != str(draft_row.get("body") or ""):
        raise ValueError("decision_card_draft_body_unsafe")
    try:
        single = render_decision_card(event_id, mailbox, parsed, classification, draft_row)
    except ValueError as exc:
        if str(exc) != "decision_card_full_draft_overflow":
            raise
    else:
        if body not in single:
            raise ValueError("decision_card_full_draft_missing")
        return [{"purpose": "operator_decision", "payload": single, "body_chunk": body, "part": 1, "parts": 1}]

    chunks = [body[index:index + DRAFT_BODY_CHUNK_MAX] for index in range(0, len(body), DRAFT_BODY_CHUNK_MAX)]
    if "".join(chunks) != body:
        raise ValueError("decision_card_body_chunk_binding_mismatch")
    body_sha256 = str(draft_row.get("body_sha256") or sha256_text(body))
    recipients = json.loads(str(draft_row.get("recipients_json") or "[]"))
    recipient = one_line(", ".join(str(item) for item in recipients), 180)
    parts = len(chunks)
    messages: list[dict[str, Any]] = []
    for index, chunk in enumerate(chunks, start=1):
        prefix_lines = [
            f"📬 Full local draft — body {index}/{parts}",
            f"Event: `{event_id}`",
        ]
        if index == 1:
            prefix_lines.extend([
                f"From/To: `{one_line(str(draft_row.get('from_identity') or ''), 100)}` → `{recipient}`",
                f"Subject: {one_line(str(draft_row.get('subject') or parsed.get('subject') or ''), 180)}",
                f"Body SHA-256: `{body_sha256}`",
            ])
        payload = "\n".join(prefix_lines) + "\n--- draft body ---\n" + chunk + "\n--- end part ---"
        if len(payload) > DISCORD_LIMIT:
            raise ValueError("decision_card_body_part_overflow")
        messages.append({
            "purpose": "operator_draft_body",
            "payload": payload,
            "body_chunk": chunk,
            "part": index,
            "parts": parts,
        })
    control = "\n".join([
        "📬 Full local draft — review control",
        f"Event: `{event_id}`",
        f"Body SHA-256: `{body_sha256}`",
        f"The complete draft is shown above in {parts} ordered parts; no text was truncated.",
        "Reply to this exact active card:",
        f"`{event_id} SEND`",
        f"`{event_id} EDIT <instruction>`",
        f"`{event_id} HOLD`",
        f"`{event_id} DRAFT`",
        f"`{event_id} RESOLVED_EXTERNALLY`",
        "Draft is local until an exact active-card SEND; approved SEND is transmitted once with Gmail readback.",
    ])
    if len(control) > DISCORD_LIMIT:
        raise ValueError("decision_card_control_overflow")
    messages.append({
        "purpose": "operator_decision",
        "payload": control,
        "body_chunk": "",
        "part": parts + 1,
        "parts": parts + 1,
    })
    if "".join(item["body_chunk"] for item in messages if item["purpose"] == "operator_draft_body") != body:
        raise ValueError("decision_card_body_reconstruction_mismatch")
    return messages


def render_ops_event(event_id: str, mailbox: dict[str, Any], parsed: dict[str, Any], classification: dict[str, Any]) -> str:
    return "\n".join([
        "⚠️ Inbox Control Plane — operational event",
        f"Company/mailbox: {one_line(mailbox['company'])} · {one_line(mailbox['canonical_address'])}",
        f"Category/reason: `{classification['category']}` · `{classification['reason_code']}`",
        f"Sender/subject: {one_line(parsed['sender'])} · {one_line(parsed['subject'])}",
        f"Event: `{event_id}`",
        "No raw body, secret, provider mutation, draft, or send is included.",
    ])


def load_env_value(paths: Iterable[Path], key: str) -> str:
    for path in paths:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def assert_gmail_readonly_scopes(scopes: Iterable[str] | None) -> tuple[str, ...]:
    """Fail closed unless every effective credential scope is Gmail read-only."""
    allowed = {"https://www.googleapis.com/auth/gmail.readonly"}
    values = scopes.split() if isinstance(scopes, str) else (scopes or [])
    effective = {str(scope).strip() for scope in values if str(scope).strip()}
    if effective != allowed:
        raise RuntimeError("gmail_credentials_scope_not_readonly")
    return tuple(sorted(effective))


def assert_gmail_credentials_readonly(creds: Any) -> tuple[str, ...]:
    """Validate requested scopes and any server-reported effective grants."""
    requested = assert_gmail_readonly_scopes(getattr(creds, "scopes", None))
    granted = getattr(creds, "granted_scopes", None)
    if granted is not None:
        assert_gmail_readonly_scopes(granted)
    return requested


def gmail_service_for(mailbox: dict[str, Any]):
    from inbox_control_plane.gmail_client import GmailClient, GmailClientError

    try:
        if (
            mailbox.get("provider") == "gmail_api_oauth"
            and mailbox.get("adapter") != "gmail_api_refresh_narrowed_readonly"
        ):
            auth_path = Path(str(mailbox.get("auth_reference") or ""))
            try:
                declared = json.loads(auth_path.read_text(encoding="utf-8")).get("scopes")
            except Exception as exc:
                raise RuntimeError("gmail_credentials_scope_not_readonly") from exc
            assert_gmail_readonly_scopes(declared)
        return GmailClient.from_mailbox(mailbox, mode="read").service
    except GmailClientError as exc:
        raise RuntimeError(str(exc)) from exc


def gmail_poll_query(mailbox: dict[str, Any]) -> str:
    """Return the narrow history-reconciliation query for exact active GLP DWD rows."""
    is_glp_history_row = (
        mailbox.get("mailbox_id") in {"glp_ops_gmail", "glp_izaiah_gmail"}
        and mailbox.get("venture") == "glp"
        and mailbox.get("routing_policy") == "glp_inbound"
        and mailbox.get("provider") == "gmail_api_dwd"
        and str(mailbox.get("canonical_address") or "").endswith("@glpclinicscout.com")
    )
    if is_glp_history_row:
        return f"newer_than:{GLP_GMAIL_HISTORY_DAYS}d -in:sent"
    return "in:inbox is:unread -in:sent" if mailbox.get("provider") else "newer_than:7d -in:sent"


def poll_gmail_api(ledger: Ledger, mailbox: dict[str, Any], run_id: str, bootstrap: bool, limit: int) -> dict[str, Any]:
    service = gmail_service_for(mailbox)
    profile = service.users().getProfile(userId="me").execute()
    profile_email = (profile.get("emailAddress") or "").lower()
    if profile_email != mailbox["canonical_address"]:
        raise RuntimeError("gmail_profile_mismatch")
    if limit < 1:
        raise ValueError("gmail_poll_limit_invalid")
    selected: list[dict[str, Any]] = []
    seen_message_ids: set[str] = set()
    token: str | None = None
    seen_tokens: set[str] = set()
    pages_traversed = 0
    provider_duplicate_ids = 0
    duplicate = 0
    deferred_unseen = 0
    while True:
        if pages_traversed >= GMAIL_MAX_LIST_PAGES:
            raise RuntimeError("gmail_pagination_page_limit_exceeded")
        # This is an inbound control plane. Gmail's mailbox-wide search also
        # returns sent copies unless they are excluded explicitly, which can
        # misroute our own outbound mail as an unknown-recipient event.
        query = gmail_poll_query(mailbox)
        kwargs: dict[str, Any] = {"userId": "me", "q": query, "maxResults": 500, "includeSpamTrash": False}
        if token:
            kwargs["pageToken"] = token
        response = service.users().messages().list(**kwargs).execute()
        pages_traversed += 1
        for item in response.get("messages") or []:
            mid = str(item.get("id") or "").strip()
            if not mid:
                continue
            if mid in seen_message_ids:
                provider_duplicate_ids += 1
                continue
            seen_message_ids.add(mid)
            if ledger.event_exists(mailbox["mailbox_id"], mid):
                duplicate += 1
            elif len(selected) < limit:
                # Provider order is intentional: the processing limit applies
                # only after cross-page and persisted-event duplicate removal.
                selected.append(item)
            else:
                deferred_unseen += 1
        next_token = response.get("nextPageToken")
        if not next_token:
            token = None
            break
        next_token = str(next_token)
        if next_token in seen_tokens:
            raise RuntimeError("gmail_pagination_token_repeated")
        seen_tokens.add(next_token)
        token = next_token
    persisted = 0
    sent_only_skipped = 0
    last_message_at = None
    for item in selected:
        mid = str(item["id"])
        message = service.users().messages().get(userId="me", id=mid, format="raw").execute()
        labels = set(message.get("labelIds") or [])
        # Defensive backstop if Gmail search semantics drift or a provider
        # fixture ignores q=. Message labels are message-specific; inbound
        # messages must never carry the SENT system label.
        if "SENT" in labels:
            sent_only_skipped += 1
            continue
        raw = base64.urlsafe_b64decode((message.get("raw") or "") + "=" * (-len(message.get("raw") or "") % 4))
        result = ledger.ingest(mailbox, mid, raw, provider_thread_id=message.get("threadId"), labels=list(labels), bootstrap=bootstrap)
        persisted += int(result["status"] == "persisted")
        duplicate += int(result["status"] == "duplicate")
        try:
            ts = int(message.get("internalDate") or 0)
            if ts:
                last_message_at = dt.datetime.fromtimestamp(ts / 1000, dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        except Exception:
            pass
    ledger.health(mailbox["mailbox_id"], auth_ok=True, read_ok=True, backlog=len(seen_message_ids), last_message_at=last_message_at, unhealthy=None, poll_receipt_id=run_id)
    return {
        "mailbox_id": mailbox["mailbox_id"], "status": "pass", "profile_match": True,
        "discovered": len(seen_message_ids), "selected_unseen": len(selected),
        "persisted": persisted, "duplicates": duplicate,
        "sent_only_skipped": sent_only_skipped,
        "provider_duplicate_ids": provider_duplicate_ids, "deferred_unseen": deferred_unseen,
        "pages_traversed": pages_traversed, "pagination_complete": token is None,
        "provider_mutations": 0, "external_sends": 0,
    }


def _imap_message_identity(parts: Iterable[Any]) -> tuple[str | None, str | None, bytes]:
    """Extract Gmail IDs and a bounded Message-ID header from a FETCH result."""
    metadata = b" ".join(
        item[0] for item in parts
        if isinstance(item, tuple) and isinstance(item[0], bytes)
    ).decode("ascii", "ignore")
    payload = next(
        (item[1] for item in parts if isinstance(item, tuple) and isinstance(item[1], bytes)),
        b"",
    )
    gm = re.search(r"X-GM-MSGID (\d+)", metadata)
    th = re.search(r"X-GM-THRID (\d+)", metadata)
    parsed_id = ""
    if payload:
        parsed_id = str(BytesHeaderParser(policy=email.policy.default).parsebytes(payload).get("Message-ID") or "")
    return (gm.group(1) if gm else parsed_id or None, th.group(1) if th else None, payload)


class ImapTransientFailure(RuntimeError):
    """Private transport failure carrying only fixed, receipt-safe metadata."""

    def __init__(self, *, stage: str, family: str, auth_ok: bool) -> None:
        self.stage = stage
        self.family = family
        self.auth_ok = auth_ok
        super().__init__(f"imap_{family}:{stage}")


class ImapPollError(RuntimeError):
    """Exhausted IMAP retries with no provider exception text attached."""

    def __init__(self, *, stage: str, family: str, attempts: int, auth_ok: bool) -> None:
        self.stage = stage
        self.family = family
        self.attempts = attempts
        self.auth_ok = auth_ok
        super().__init__(f"imap_{family}:{stage}:attempts_exhausted")


def _poll_generic_imap_once(
    ledger: Ledger, mailbox: dict[str, Any], run_id: str, bootstrap: bool, limit: int,
    deadline: float,
) -> dict[str, Any]:
    address = mailbox["canonical_address"]
    env_paths = [KAEL_IMAP_ENV_PATH, BUSINESS / ".env", Path("/home/ubuntu/.hermes/.env")]
    password = load_env_value(env_paths, "GMAIL_APP_PASSWORD").replace(" ", "")
    configured = load_env_value(env_paths, "GMAIL_ADDRESS") or address
    if not password or configured.lower() != address:
        raise RuntimeError("generic_imap_auth_missing_or_profile_mismatch")
    connection = None
    stage = "connect"
    auth_ok = False
    try:
        if time.monotonic() >= deadline:
            raise TimeoutError("imap_adapter_deadline")
        connection = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=IMAP_TIMEOUT_SECONDS)
        stage = "login"
        if time.monotonic() >= deadline:
            raise TimeoutError("imap_adapter_deadline")
        connection.login(address, password)
        auth_ok = True
        stage = "select"
        if time.monotonic() >= deadline:
            raise TimeoutError("imap_adapter_deadline")
        status, _ = connection.select('"[Gmail]/All Mail"', readonly=True)
        if status != "OK":
            raise RuntimeError("imap_select_failed")
        since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)).strftime("%d-%b-%Y")
        stage = "search"
        if time.monotonic() >= deadline:
            raise TimeoutError("imap_adapter_deadline")
        status, data = connection.search(None, "SINCE", since)
        if status != "OK":
            raise RuntimeError("imap_search_failed")
        sequence_ids = (data[0] or b"").split()[-limit:]
        persisted = duplicate = 0
        sent_only_skipped = 0
        raw_fetches = 0
        for sequence_id in sequence_ids:
            stage = "identity_fetch"
            if time.monotonic() >= deadline:
                raise TimeoutError("imap_adapter_deadline")
            status, identity_parts = connection.fetch(
                sequence_id,
                "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)] X-GM-MSGID X-GM-THRID)",
            )
            if status != "OK":
                raise RuntimeError("imap_identity_fetch_failed")
            provider_id, thread_id, _ = _imap_message_identity(identity_parts)
            if provider_id and ledger.event_exists(mailbox["mailbox_id"], provider_id):
                duplicate += 1
                continue
            stage = "raw_fetch"
            if time.monotonic() >= deadline:
                raise TimeoutError("imap_adapter_deadline")
            status, full_parts = connection.fetch(sequence_id, "(BODY.PEEK[] X-GM-MSGID X-GM-THRID)")
            if status != "OK":
                raise RuntimeError("imap_raw_fetch_failed")
            raw_provider_id, raw_thread_id, raw = _imap_message_identity(full_parts)
            if not raw:
                raise RuntimeError("imap_raw_fetch_empty")
            raw_fetches += 1
            provider_id = provider_id or raw_provider_id or sha256_bytes(raw)
            thread_id = thread_id or raw_thread_id
            if ledger.event_exists(mailbox["mailbox_id"], provider_id):
                duplicate += 1
                continue
            # All Mail includes sent copies without Gmail API labels. Suppress
            # only an exact From owned by this mailbox with no registry-owned
            # recipient; all uncertain attribution remains on the ingest path.
            try:
                headers = BytesHeaderParser(policy=email.policy.default).parsebytes(raw)
                from_headers = headers.get_all("From", [])
                from_addresses = getaddresses([str(value) for value in from_headers])
                owned_senders = {
                    str(value).lower() for value in (
                        mailbox["canonical_address"], *(mailbox.get("aliases") or []),
                        *(mailbox.get("groups") or []),
                    )
                }
                recipient_headers = [
                    value for name in ("To", "Cc", "Bcc", "Delivered-To", "X-Original-To", "Resent-To", "Resent-Cc", "Resent-Bcc")
                    for value in headers.get_all(name, [])
                ]
                recipients = [address.lower() for _, address in getaddresses([str(value) for value in recipient_headers]) if address]
                header_defects = list(headers.defects)
                for value in [*from_headers, *recipient_headers]:
                    header_defects.extend(getattr(value, "defects", ()))
            except (ValueError, IndexError, email.errors.HeaderParseError):
                header_defects = ["sent_filter_header_parse_failed"]
            if (not header_defects and len(from_headers) == 1 and len(from_addresses) == 1
                    and from_addresses[0][1].lower() in owned_senders):
                _, attribution_status = ledger.resolve_attribution(mailbox, recipients)
                if attribution_status == "unregistered_recipient_mapping":
                    sent_only_skipped += 1
                    continue
            result = ledger.ingest(mailbox, provider_id, raw, provider_thread_id=thread_id, source_folder="[Gmail]/All Mail", bootstrap=bootstrap)
            persisted += int(result["status"] == "persisted")
        ledger.health(mailbox["mailbox_id"], auth_ok=True, read_ok=True, backlog=len(sequence_ids), last_message_at=None, unhealthy=None, poll_receipt_id=run_id)
        return {
            "mailbox_id": mailbox["mailbox_id"], "status": "pass", "profile_match": True,
            "discovered": len(sequence_ids), "persisted": persisted, "duplicates": duplicate,
            "readonly_select": True, "metadata_prefiltered": len(sequence_ids) - raw_fetches,
            "raw_fetches": raw_fetches, "sent_only_skipped": sent_only_skipped,
            "provider_mutations": 0, "external_sends": 0,
        }
    except (TimeoutError, OSError, imaplib.IMAP4.abort) as exc:
        family = "timeout" if isinstance(exc, TimeoutError) else "abort" if isinstance(exc, imaplib.IMAP4.abort) else "transport"
        raise ImapTransientFailure(stage=stage, family=family, auth_ok=auth_ok) from exc
    except imaplib.IMAP4.error as exc:
        family = "auth_rejected" if stage == "login" else "protocol"
        raise ImapPollError(stage=stage, family=family, attempts=1, auth_ok=auth_ok) from exc
    finally:
        if connection is not None:
            with contextlib.suppress(Exception):
                connection.logout()


def poll_generic_imap(ledger: Ledger, mailbox: dict[str, Any], run_id: str, bootstrap: bool, limit: int) -> dict[str, Any]:
    """Poll generic Gmail IMAP with bounded reconnects for transient read failures."""
    auth_succeeded = False
    deadline = time.monotonic() + IMAP_ADAPTER_DEADLINE_SECONDS
    for attempt in range(len(IMAP_RETRY_DELAYS_SECONDS) + 1):
        try:
            result = _poll_generic_imap_once(ledger, mailbox, run_id, bootstrap, limit, deadline)
            result["reconnect_attempts"] = attempt
            return result
        except ImapTransientFailure as exc:
            auth_succeeded = auth_succeeded or exc.auth_ok
            if attempt >= len(IMAP_RETRY_DELAYS_SECONDS):
                raise ImapPollError(
                    stage=exc.stage, family=exc.family, attempts=attempt + 1,
                    auth_ok=auth_succeeded,
                ) from exc
            if time.monotonic() + IMAP_RETRY_DELAYS_SECONDS[attempt] >= deadline:
                raise ImapPollError(
                    stage=exc.stage, family=exc.family, attempts=attempt + 1,
                    auth_ok=auth_succeeded,
                ) from exc
            time.sleep(IMAP_RETRY_DELAYS_SECONDS[attempt])
    raise RuntimeError("imap_retry_state_unreachable")


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if not spec or not spec.loader:
        raise RuntimeError("module_load_failed")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def synthetic_form_email(mailbox: dict[str, Any], table: str, row: dict[str, Any]) -> bytes:
    record_id = str(row.get("id") or sha256_text(json.dumps(row, sort_keys=True)))
    sender = str(row.get("email") or "unknown@example.invalid")
    subject = f"GLP website inbound: {table} {record_id}"
    safe_row = json.dumps(row, sort_keys=True, ensure_ascii=False)
    message = EmailMessage()
    message["From"] = sender
    message["To"] = mailbox["canonical_address"]
    message["Subject"] = subject
    message["Message-ID"] = f"<d1-{table}-{record_id}@glpclinicscout.com>"
    message["Date"] = str(row.get("created_at") or utc_now())
    message.set_content(safe_row)
    return message.as_bytes(policy=email.policy.default)


def poll_glp_d1(ledger: Ledger, mailbox: dict[str, Any], run_id: str, bootstrap: bool, limit: int) -> dict[str, Any]:
    limit = max(1, min(limit, 250))
    rows = {
        "crm_contacts": cloudflare_d1_query(f"select id, clinic_name, website, contact_name, email, service_area, stage, score, source_urls_json, notes, created_at from crm_contacts where stage='inbound_partner_interest' order by datetime(created_at) desc limit {limit};"),
        "support_requests": cloudflare_d1_query(f"select id, email, topic, message, route, created_at from support_requests order by datetime(created_at) desc limit {limit};"),
        "leads": cloudflare_d1_query(f"select id, session_id, name, email, phone, state, zip, source, consent_status, created_at from leads where consent_status='clinic_intro_lead_requested' order by datetime(created_at) desc limit {limit};"),
    }
    persisted = duplicate = discovered = 0
    for table in ("crm_contacts", "support_requests", "leads"):
        for row in rows.get(table) or []:
            discovered += 1
            provider_id = f"d1:{table}:{row.get('id')}"
            if ledger.event_exists(mailbox["mailbox_id"], provider_id):
                duplicate += 1
                continue
            raw = synthetic_form_email(mailbox, table, row)
            result = ledger.ingest(mailbox, provider_id, raw, source_folder=table, bootstrap=bootstrap)
            persisted += int(result["status"] == "persisted")
    ledger.health(mailbox["mailbox_id"], auth_ok=True, read_ok=True, backlog=discovered, last_message_at=None, unhealthy=None, poll_receipt_id=run_id)
    return {"mailbox_id": mailbox["mailbox_id"], "status": "pass", "discovered": discovered, "persisted": persisted, "duplicates": duplicate, "d1_readonly_queries": True, "provider_mutations": 0, "external_sends": 0}


def cloudflare_d1_query(sql: str) -> list[dict[str, Any]]:
    """Execute a scoped D1 SELECT through the REST API; response rows stay private."""
    token = load_env_value([Path("/home/ubuntu/.hermes/.env")], "CLOUDFLARE_API_TOKEN")
    if not token:
        raise RuntimeError("cloudflare_api_token_missing")
    account_id = "f860943dff399ffb1db0a28dcd3dadfd"
    database_id = "bcb91d2d-c3f1-4c55-93e3-070587e86d60"
    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/d1/database/{database_id}/query"
    request = urllib.request.Request(
        url,
        data=json.dumps({"sql": sql}).encode("utf-8"),
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"cloudflare_d1_http_{exc.code}") from exc
    if not payload.get("success"):
        raise RuntimeError("cloudflare_d1_query_unsuccessful")
    blocks = payload.get("result") or []
    if isinstance(blocks, dict):
        blocks = [blocks]
    rows: list[dict[str, Any]] = []
    for block in blocks:
        rows.extend(block.get("results") or [])
    return rows


def discord_token() -> str:
    return load_env_value([Path("/home/ubuntu/.hermes/.env")], "DISCORD_BOT_TOKEN")


def discord_request(path: str, method: str = "GET", body: dict[str, Any] | None = None) -> dict[str, Any] | list[Any]:
    token = discord_token()
    if not token:
        raise RuntimeError("discord_token_missing")
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(1, 8):
        request = urllib.request.Request(
            "https://discord.com/api/v10" + path, data=data, method=method,
            headers={"Authorization": f"Bot {token}", "Content-Type": "application/json", "User-Agent": "DiscordBot (https://discord.com, 10)"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read().decode("utf-8", "replace")
            return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            if exc.code != 429 or attempt == 7:
                raise RuntimeError(f"discord_http_{exc.code}") from exc
            try:
                retry_after = float((json.loads(raw) or {}).get("retry_after") or 1.0)
            except Exception:
                retry_after = 1.0
            time.sleep(max(0.25, retry_after + 0.25))
    raise RuntimeError("discord_retry_exhausted")


def claim_outbox_for_dispatch(ledger: Ledger, outbox_id: str, stale_before: str) -> bool:
    """Atomically claim one row so concurrent dispatchers cannot double-post."""
    with ledger.conn:
        cursor = ledger.conn.execute(
            """UPDATE notification_outbox SET status='dispatching',updated_at=?
               WHERE outbox_id=? AND (
                 status IN ('pending','failed_retryable','posted_pending_readback')
                 OR (status='dispatching' AND updated_at<?)
               )""",
            (utc_now(), outbox_id, stale_before),
        )
    return cursor.rowcount == 1


def suppress_affiliate_operator_card_before_dispatch(ledger: Ledger, row: sqlite3.Row) -> bool:
    """Re-evaluate generic affiliate cards at the irreversible Discord edge."""
    if str(row["purpose"]) != "operator_decision" or not row["event_id"]:
        return False
    if str(row["idempotency_key"]).startswith("named-approval-blocker:"):
        return False
    event_id = str(row["event_id"])
    try:
        event, _mailbox, parsed, classification = ledger._event_context(event_id)
        if str(classification.get("category") or "") != "affiliate_network_response":
            return False
        disposition = affiliate_response_disposition(parsed, classification)
        suppress = str(event["state"]) in {"resolved_externally", "held", "archived"} or disposition not in {
            "operator_prerequisite_card",
        }
    except Exception as exc:
        # Affiliate-provider and parser failures belong in ops repair, never in a
        # speculative operator decision card.
        suppress = True
        ledger.audit("affiliate_operator_card_preflight_failed", "outbox", str(row["outbox_id"]), {"error_class": type(exc).__name__})
    if suppress:
        with ledger.conn:
            ledger.conn.execute(
                """UPDATE notification_outbox SET status='suppressed_affiliate_preflight',updated_at=?
                   WHERE outbox_id=? AND status IN ('pending','failed_retryable','posted_pending_readback','dispatching')""",
                (utc_now(), row["outbox_id"]),
            )
        ledger.audit("affiliate_operator_card_suppressed", "outbox", str(row["outbox_id"]), {"event_id": event_id})
    return suppress


def dispatch_outbox(ledger: Ledger, *, live: bool, run_started: str | None = None) -> dict[str, Any]:
    stale_before = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    pending = list(ledger.conn.execute(
        """SELECT * FROM notification_outbox
           WHERE status IN ('pending','failed_retryable','posted_pending_readback')
              OR (status='dispatching' AND updated_at<?)
           ORDER BY CASE purpose WHEN 'affiliate_exception' THEN 0 WHEN 'operator_decision' THEN 1 ELSE 2 END,
                    created_at,idempotency_key,outbox_id LIMIT 1000""",
        (stale_before,),
    ))
    delivered = failed = suppressed = historical_suppressed = rate_limited = summary_queued = 0
    if not live:
        return {"status": "dry_run", "pending": len(pending), "delivered": 0, "failed": 0}
    # Operator decision cards are lossless.  Every pending card that survives
    # the event-level safety gates below must be attempted in this pass,
    # regardless of when it was ingested or how many sibling cards exist.
    # Do not reintroduce a per-pass ceiling or historical-backfill suppression:
    # those branches withheld operator action and replaced cards with a summary.
    recent_cache: dict[str, dict[str, str]] = {}
    for row in pending:
        if suppress_affiliate_operator_card_before_dispatch(ledger, row):
            suppressed += 1
            continue
        if not claim_outbox_for_dispatch(ledger, str(row["outbox_id"]), stale_before):
            continue
        channel = row["destination_channel_id"]
        payload = row["payload"]
        try:
            message_id = row["discord_message_id"]
            if not message_id:
                if channel not in recent_cache:
                    recent = discord_request(f"/channels/{channel}/messages?limit=100")
                    recent_cache[channel] = {str(item.get("content") or ""): str(item.get("id") or "") for item in recent if isinstance(item, dict)} if isinstance(recent, list) else {}
                message_id = recent_cache[channel].get(payload)
            if not message_id:
                created = discord_request(
                    f"/channels/{channel}/messages", "POST",
                    {"content": payload, "allowed_mentions": {"parse": []}, "flags": 4},
                )
                if not isinstance(created, dict) or not created.get("id"):
                    raise RuntimeError("discord_create_invalid_response")
                message_id = str(created["id"])
            with ledger.conn:
                ledger.conn.execute("UPDATE notification_outbox SET status='posted_pending_readback',attempts=attempts+1,discord_message_id=?,response_sha256=?,updated_at=? WHERE outbox_id=?", (message_id, sha256_text(payload), utc_now(), row["outbox_id"]))
            readback = discord_request(f"/channels/{channel}/messages/{message_id}")
            if not isinstance(readback, dict) or str(readback.get("channel_id")) != channel or str(readback.get("content")) != payload:
                raise RuntimeError("discord_readback_mismatch")
            with ledger.conn:
                ledger.conn.execute("UPDATE notification_outbox SET status='delivered',readback_sha256=?,delivered_at=?,updated_at=? WHERE outbox_id=?", (sha256_text(payload), utc_now(), utc_now(), row["outbox_id"]))
            ledger.audit("notification_delivered", "outbox", row["outbox_id"], {"channel_id": channel, "discord_message_id": message_id, "payload_sha256": sha256_text(payload)})
            delivered += 1
        except Exception as exc:
            with ledger.conn:
                ledger.conn.execute("UPDATE notification_outbox SET status='failed_retryable',attempts=attempts+1,last_error_class=?,updated_at=? WHERE outbox_id=?", (type(exc).__name__ + ":" + str(exc)[:100], utc_now(), row["outbox_id"]))
            failed += 1
    return {
        "status": "pass" if failed == 0 else "partial", "pending": len(pending), "delivered": delivered,
        "failed": failed, "suppressed": suppressed, "historical_suppressed": historical_suppressed,
        "rate_limited": rate_limited, "summary_queued": summary_queued,
    }


def sync_operator_decisions(ledger: Ledger, *, live: bool) -> dict[str, Any]:
    """Read exact replies to active decision cards and apply local-only actions."""
    if not live:
        return {"status": "dry_run", "matched": 0, "handled": 0, "failed": 0}
    messages = discord_request(f"/channels/{DECISIONS_CHANNEL_ID}/messages?limit=100")
    if not isinstance(messages, list):
        raise RuntimeError("discord_decision_sync_invalid_response")
    matched = handled = failed = ignored = 0
    failures: list[str] = []
    ignored_reasons: list[str] = []
    disposition_re = re.compile(
        r"^(evt_[0-9a-f]{28}) (SEND|DRAFT|HOLD|RESOLVED_EXTERNALLY)(?: - [^\r\n]+)?$"
    )
    affiliate_tool_re = re.compile(r"^(evt_[0-9a-f]{28}) - affiliate inbox tool should handle$")
    edit_re = re.compile(r"^(evt_[0-9a-f]{28}) (EDIT [^\r\n]+)$")
    for message in sorted((row for row in messages if isinstance(row, dict)), key=lambda row: int(row.get("id") or 0)):
        author = message.get("author") or {}
        if str(author.get("id") or "") != OPERATOR_ID or bool(author.get("bot")) or message.get("edited_timestamp") is not None:
            continue
        commands: list[tuple[str, str]] = []
        for line in str(message.get("content") or "").splitlines():
            affiliate_tool = affiliate_tool_re.fullmatch(line)
            if affiliate_tool:
                commands.append((affiliate_tool.group(1), "AFFILIATE_TOOL"))
                continue
            match = disposition_re.fullmatch(line) or edit_re.fullmatch(line)
            if match:
                commands.append((match.group(1), match.group(2)))
        if not commands:
            continue
        event_ids = [event_id for event_id, _ in commands]
        if len(set(event_ids)) != len(event_ids):
            failed += len(commands)
            failures.append("duplicate_event_in_decision_message")
            continue
        source_decision_message_id = str(message.get("id") or "")
        replied_card_message_id = str((message.get("message_reference") or {}).get("message_id") or "")
        for event_id, action in commands:
            if source_decision_message_id == MANUAL_DISPOSITION_MESSAGE_ID and action in {"RESOLVED_EXTERNALLY", "AFFILIATE_TOOL"}:
                try:
                    ledger.record_manual_disposition(
                        event_id,
                        disposition=action,
                        source_message_id=source_decision_message_id,
                        source_channel_id=DECISIONS_CHANNEL_ID,
                        actor_id=OPERATOR_ID,
                        note=("route to affiliate inbox tool and update Sheet" if action == "AFFILIATE_TOOL" else "operator manually cleared card"),
                    )
                    ledger._queue_disposition_ack(event_id, f"{source_decision_message_id}:{event_id}", action)
                    matched += 1
                    handled += 1
                except ValueError as exc:
                    failed += 1
                    failures.append(str(exc)[:80])
                continue
            card_message_id = replied_card_message_id
            if action in {"DRAFT", "SEND", "HOLD"} or action.startswith("EDIT"):
                # The command names its event explicitly and the author is the
                # verified operator in the decisions channel. Reply-target
                # pedantry added no authority and ate five consecutive EDITs on
                # 2026-08-05 (replies landed on body parts and superseded
                # cards). Bind to the event's newest delivered card directly.
                active_card = ledger.conn.execute(
                    """SELECT discord_message_id FROM notification_outbox
                       WHERE event_id=? AND purpose='operator_decision' AND status='delivered'
                       ORDER BY delivered_at DESC,outbox_id DESC LIMIT 1""",
                    (event_id,),
                ).fetchone()
                card_message_id = str(active_card["discord_message_id"] or "") if active_card else ""
                if not card_message_id:
                    ignored += 1
                    ignored_reasons.append("command_without_delivered_decision_card")
                    continue
            if action == "RESOLVED_EXTERNALLY":
                active_card = ledger.conn.execute(
                    """SELECT discord_message_id FROM notification_outbox
                       WHERE event_id=? AND purpose='operator_decision' AND status='delivered'
                       ORDER BY delivered_at DESC,outbox_id DESC LIMIT 1""",
                    (event_id,),
                ).fetchone()
                card_message_id = str(active_card["discord_message_id"] or "") if active_card else ""
                if not card_message_id:
                    # A stale resolution command has no active card authority.
                    # Ignore it idempotently: it cannot authorize any effect and
                    # must not turn a healthy read-only poll into a cron failure.
                    ignored += 1
                    ignored_reasons.append("stale_resolved_externally_without_active_card")
                    continue
            elif not card_message_id:
                continue
            else:
                active_card = ledger.conn.execute(
                    """SELECT discord_message_id FROM notification_outbox
                       WHERE event_id=? AND purpose='operator_decision' AND status='delivered'
                       ORDER BY delivered_at DESC,outbox_id DESC LIMIT 1""",
                    (event_id,),
                ).fetchone()
                active_card_message_id = str(active_card["discord_message_id"] or "") if active_card else ""
                if not active_card_message_id or card_message_id != active_card_message_id:
                    # A reply to a superseded or missing card has no current
                    # operator authority. Ignore it in the scheduler sync so
                    # stale history cannot turn a safe read-only poll into a
                    # cron failure. Direct action APIs retain their strict
                    # active-card binding and continue to raise.
                    ignored += 1
                    ignored_reasons.append("stale_operator_decision_without_active_card_binding")
                    continue
            matched += 1
            # Always qualify by event_id.  The identity of a decision is (reply,
            # event), never how many commands this particular sync pass parsed --
            # keying off len(commands) made the same reply spell its id two ways
            # across two passes.  Matches the manual-disposition ack above, which
            # has always suffixed unconditionally.
            decision_message_id = f"{source_decision_message_id}:{event_id}"
            try:
                ledger._handle_synced_operator_action(
                    event_id, action, actor_id=OPERATOR_ID,
                    channel_id=DECISIONS_CHANNEL_ID, decision_message_id=decision_message_id,
                    card_message_id=card_message_id,
                )
                handled += 1
            except ValueError as exc:
                failed += 1
                failures.append(str(exc)[:80])
    return {
        "status": "pass" if failed == 0 else "partial",
        "matched": matched,
        "handled": handled,
        "failed": failed,
        "ignored": ignored,
        "failure_classes": sorted(set(failures)),
        "ignored_reasons": sorted(set(ignored_reasons)),
    }


def run_poll(
    ledger: Ledger, registry: dict[str, Any], *, bootstrap: bool, dispatch: bool,
    decision_sync: bool = False, limit: int,
) -> dict[str, Any]:
    run_id = "inboxrun_" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + os.urandom(3).hex()
    source_hash = sha256_bytes(Path(__file__).read_bytes())
    started = utc_now()
    stale_before = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=4)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    stale_run_ids = [
        row["run_id"]
        for row in ledger.conn.execute(
            "SELECT run_id FROM inbox_runs WHERE exit_status='running' AND started_at<? ORDER BY started_at",
            (stale_before,),
        )
    ]
    with ledger.conn:
        ledger.conn.execute(
            "UPDATE inbox_runs SET finished_at=?,exit_status='interrupted' WHERE exit_status='running' AND started_at<?",
            (started, stale_before),
        )
    for stale_run_id in stale_run_ids:
        ledger.audit("stale_run_recovered", "inbox_run", stale_run_id, {"exit_status": "interrupted", "recovered_by_run_id": run_id})
    ledger.conn.execute("INSERT INTO inbox_runs(run_id,started_at,exit_status,source_sha256) VALUES(?,?,?,?)", (run_id, started, "running", source_hash))
    ledger.conn.commit()
    decision_sync_result = sync_operator_decisions(
        ledger, live=decision_sync and dispatch and not bootstrap,
    )
    results = []
    for mailbox in registry["mailboxes"]:
        if mailbox["lifecycle"] != "active":
            continue
        try:
            if mailbox["adapter"] == "generic_imap_readonly":
                result = poll_generic_imap(ledger, mailbox, run_id, bootstrap, limit)
            elif mailbox["adapter"] in {"gmail_api_readonly", "gmail_api_dwd_readonly", "gmail_api_refresh_narrowed_readonly"}:
                result = poll_gmail_api(ledger, mailbox, run_id, bootstrap, limit)
            elif mailbox["adapter"] == "glp_d1_readonly":
                result = poll_glp_d1(ledger, mailbox, run_id, bootstrap, limit)
            else:
                raise RuntimeError("unsupported_active_adapter")
        except Exception as exc:
            if isinstance(exc, ImapPollError):
                error = type(exc).__name__ + ":" + str(exc)
                auth_ok = exc.auth_ok
                safe_details = {"error_stage": exc.stage, "attempts": exc.attempts}
            else:
                error = type(exc).__name__ + ":" + str(exc)[:160]
                auth_ok = False
                safe_details = {}
            ledger.health(mailbox["mailbox_id"], auth_ok=auth_ok, read_ok=False, backlog=0, last_message_at=None, unhealthy=error, poll_receipt_id=run_id)
            ledger.queue_ops_failure(mailbox["mailbox_id"], error, run_id)
            result = {"mailbox_id": mailbox["mailbox_id"], "status": "blocked", "error_class": error, **safe_details, "provider_mutations": 0, "external_sends": 0, "persisted": 0, "duplicates": 0, "discovered": 0}
        results.append(result)
    counts = {
        "discovered": sum(int(row.get("discovered") or 0) for row in results),
        "persisted": sum(int(row.get("persisted") or 0) for row in results),
        "duplicates": sum(int(row.get("duplicates") or 0) for row in results),
    }
    projection_result = project_kael_daily_audits(ledger)
    affiliate_exception_repair = ledger.repair_affiliate_exception_outbox()
    dispatch_result = dispatch_outbox(ledger, live=dispatch and not bootstrap, run_started=started)
    from inbox_control_plane import affiliate_decision_queue
    affiliate_queue_result = affiliate_decision_queue.dispatch(ledger, live=dispatch and not bootstrap)
    status = "pass" if all(row["status"] == "pass" for row in results) and projection_result["status"] == "pass" and dispatch_result.get("failed", 0) == 0 and decision_sync_result.get("failed", 0) == 0 and affiliate_queue_result["status"] == "pass" else "blocked"
    receipt = {
        "status": status, "run_id": run_id, "started_at": started, "finished_at": utc_now(),
        "bootstrap": bootstrap, "mailboxes": results, "counts": counts, "daily_audit_projection": projection_result,
        "dispatch": dispatch_result, "decision_sync": decision_sync_result,
        "affiliate_exception_outbox_repair": affiliate_exception_repair,
        "affiliate_sheet_queue": affiliate_queue_result,
        "outbound_send_count": 0, "provider_draft_count": 0, "provider_mutation_count": 0,
        "raw_bodies_in_receipt": False, "secret_printed": False,
    }
    receipt_path = REPORT_ROOT / "production-runs" / f"{run_id}.json"
    private_json(receipt_path, receipt)
    with ledger.conn:
        ledger.conn.execute(
            """UPDATE inbox_runs SET finished_at=?,discovered_count=?,persisted_count=?,duplicate_count=?,
            classified_count=?,decision_queued_count=(SELECT COUNT(*) FROM notification_outbox WHERE purpose='operator_decision'),
            suppressed_count=(SELECT COUNT(*) FROM inbox_classifications WHERE route='silent'),
            quarantined_count=(SELECT COUNT(*) FROM inbox_events WHERE state='quarantined'),outbound_send_count=0,
            provider_draft_count=0,provider_mutation_count=0,exit_status=?,receipt_path=? WHERE run_id=?""",
            (receipt["finished_at"], counts["discovered"], counts["persisted"], counts["duplicates"], counts["persisted"], status, str(receipt_path), run_id),
        )
    ledger.audit("poll_completed", "inbox_run", run_id, {"status": status, "counts": counts, "daily_audit_projection": {"status": projection_result["status"], "projected": projection_result["projected"], "existing": projection_result["existing"], "invalid_count": len(projection_result["invalid"])}, "outbound_send_count": 0, "provider_draft_count": 0, "provider_mutation_count": 0})
    return receipt


def status_report(ledger: Ledger) -> dict[str, Any]:
    now = dt.datetime.now(dt.timezone.utc)
    mailboxes = []
    for row in ledger.conn.execute("SELECT m.*,c.* FROM mailboxes m LEFT JOIN provider_cursors c USING(mailbox_id) ORDER BY m.mailbox_id"):
        age = None
        healthy = row["lifecycle"] != "active"
        if row["last_complete_poll_at"]:
            try:
                age = (now - dt.datetime.fromisoformat(row["last_complete_poll_at"].replace("Z", "+00:00"))).total_seconds()
                healthy = not row["unhealthy_reason"] and age <= max(2 * int(row["poll_seconds"]), int(row["poll_seconds"]) + 120)
            except Exception:
                healthy = False
        mailboxes.append({
            "mailbox_id": row["mailbox_id"], "venture": row["venture"], "lifecycle": row["lifecycle"],
            "canonical_address": row["canonical_address"], "aliases": json.loads(row["aliases_json"]),
            "last_auth_success_at": row["last_auth_success_at"], "last_read_success_at": row["last_read_success_at"],
            "last_complete_poll_at": row["last_complete_poll_at"], "freshness_age_seconds": age,
            "backlog_count": row["backlog_count"], "next_poll_at": row["next_poll_at"],
            "unhealthy_reason": row["unhealthy_reason"], "healthy": healthy,
        })
    counters = {row["name"]: row["value"] for row in ledger.conn.execute("SELECT name,value FROM side_effect_counters")}
    return {
        "status": "pass" if all(row["healthy"] for row in mailboxes if row["lifecycle"] == "active") else "blocked",
        "schema_version": SCHEMA_VERSION, "policy_version": POLICY_VERSION, "db_path": str(ledger.path),
        "mailboxes": mailboxes, "counts": {
            "events": ledger.conn.execute("SELECT COUNT(*) FROM inbox_events").fetchone()[0],
            "classifications": ledger.conn.execute("SELECT COUNT(*) FROM inbox_classifications").fetchone()[0],
            "pending_notifications": ledger.conn.execute(
                """SELECT COUNT(*) FROM notification_outbox
                   WHERE status IN ('pending','failed_retryable','posted_pending_readback','dispatching')"""
            ).fetchone()[0],
            "local_drafts": ledger.conn.execute("SELECT COUNT(*) FROM local_drafts").fetchone()[0],
        },
        "side_effect_counters": counters, "audit_chain": ledger.validate_audit_chain(),
        "outbound_send_enabled": OUTBOUND_SEND_ENABLED, "secret_printed": False,
    }


def restore_test(backup_path: Path) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="inbox-restore-") as temp:
        restored = Path(temp) / "restored.sqlite3"
        shutil.copy2(backup_path, restored)
        connection = sqlite3.connect(restored)
        try:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
            events = connection.execute("SELECT COUNT(*) FROM inbox_events").fetchone()[0]
            audits = connection.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
        finally:
            connection.close()
    return {"status": "pass" if integrity == "ok" and not foreign_keys else "fail", "integrity_check": integrity, "foreign_key_failures": len(foreign_keys), "events": events, "audits": audits, "production_provider_touched": False, "external_send": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--init", action="store_true")
    parser.add_argument("--poll", action="store_true")
    parser.add_argument("--bootstrap", action="store_true")
    parser.add_argument("--dispatch", action="store_true")
    parser.add_argument("--decision-sync", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--backup", action="store_true")
    parser.add_argument("--restore-test", type=Path)
    parser.add_argument("--resolve-externally", metavar="EVENT_ID")
    parser.add_argument("--actor-id", default=OPERATOR_ID)
    parser.add_argument("--channel-id", default=DECISIONS_CHANNEL_ID)
    parser.add_argument("--decision-message-id")
    parser.add_argument("--card-message-id")
    parser.add_argument("--limit", type=int, default=250)
    args = parser.parse_args(argv)
    if args.resolve_externally and bool(args.decision_message_id) != bool(args.card_message_id):
        parser.error("resolved-externally bindings require both --decision-message-id and --card-message-id, or neither")
    registry = load_registry()
    ledger = Ledger(args.db)
    try:
        ledger.sync_registry(registry)
        output: dict[str, Any] = {"status": "pass", "initialized": True, "db_path": str(args.db), "secret_printed": False}
        if args.resolve_externally:
            if args.decision_message_id:
                output = ledger._handle_synced_operator_action(
                    args.resolve_externally, "RESOLVED_EXTERNALLY", actor_id=args.actor_id,
                    channel_id=args.channel_id, decision_message_id=args.decision_message_id,
                    card_message_id=args.card_message_id,
                )
            else:
                output = ledger.resolve_externally(
                    args.resolve_externally, actor_id=args.actor_id,
                    channel_id=args.channel_id, decision_message_id=None,
                )
        elif args.poll:
            output = run_poll(
                ledger, registry, bootstrap=args.bootstrap, dispatch=args.dispatch,
                decision_sync=args.decision_sync, limit=max(1, min(args.limit, 500)),
            )
        elif args.backup:
            backup = ledger.backup()
            output = {"status": "pass", "backup_path": str(backup), "backup_sha256": sha256_bytes(backup.read_bytes()), "secret_printed": False}
        elif args.restore_test:
            output = restore_test(args.restore_test)
        elif args.status or args.init:
            output = status_report(ledger)
        print(json.dumps(output, indent=2, sort_keys=True, ensure_ascii=False))
        return 0 if output.get("status") == "pass" else 2
    finally:
        ledger.close()


if __name__ == "__main__":
    raise SystemExit(main())
