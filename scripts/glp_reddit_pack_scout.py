#!/usr/bin/env python3
"""Deterministic, provider-free Reddit discovery qualifier for GLP Clinic Scout.

The program consumes exact, already-saved Firecrawl JSON bytes. It performs no
network I/O and contains no Reddit, browser, Discord, cron, credential, or account
transport. Outputs are local normalized evidence, manual-review packs (only when
every gate passes), a scheduler-deliverable Discord ops-alert artifact, and a
SQLite audit ledger.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlparse
from zoneinfo import ZoneInfo

BUSINESS = Path("/home/ubuntu/business")
BUILD3 = BUSINESS / "reports/glp-clinic-scout/growth/build-3"
DEFAULT_CAPTURE_PATH = BUILD3 / "reddit-firecrawl-search-captures-20260811-r5.json"
DEFAULT_CAPTURE_SHA256 = "e682ce558f405a4dae83d3a1fa625cd5e3b752041982042231bbc22dcbf38673"
DEFAULT_DB_PATH = BUSINESS / "ops/glp-reddit-packs/glp-reddit-packs-v3.sqlite3"
DEFAULT_OUTPUT_ROOT = BUILD3 / "reddit-scout-runs"

OPS_ALERTS_CHANNEL_ID = "1487300132054765629"
OPS_ALERTS_DELIVER = f"discord:{OPS_ALERTS_CHANNEL_ID}"
DAILY_PACK_CAP = 2
MAX_CAPTURE_AGE_HOURS = 24.0
MAX_THREAD_AGE_HOURS = 24.0
MAX_RULE_AGE_DAYS = 7.0
MAX_ACCOUNT_AGE_DAYS = 7.0
MAX_PUBLICATION_EVIDENCE_CAPTURE_AGE_HOURS = 24.0
MAX_AS_OF_WALL_CLOCK_AGE_HOURS = 24.0
FUTURE_TOLERANCE_SECONDS = 300.0
SCHEMA_VERSION = "glp-reddit-firecrawl-discovery-ledger-v3"
PREVIOUS_SCHEMA_VERSION = "glp-reddit-firecrawl-discovery-ledger-v2"
POLICY_VERSION = "glp-reddit-scout-policy-v3-p1-001-006"
PT = ZoneInfo("America/Los_Angeles")

EXACT_FIRECRAWL_STACKS = {
    "Firecrawl MCP via mcporter; web search only; no Reddit transport",
    "Firecrawl MCP web search only; no Reddit transport",
}

HASH_RE = re.compile(r"^[0-9a-f]{64}$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
EXACT_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$"
)
THREAD_PATH_RE = re.compile(
    r"^/r/([^/]+)/comments/([a-z0-9]+)(?:/([^/]*))?(?:/[^/]*)?/?$", re.I
)
RULE_PATH_RE = re.compile(r"^/r/([^/]+)/about/rules/?$", re.I)
PROFILE_PATH_RE = re.compile(r"^/user/([A-Za-z0-9_-]{3,20})/?$", re.I)
ACCOUNT_ID_RE = re.compile(r"^t2_[a-z0-9]{5,32}$", re.I)
RELATIVE_TIME_RE = re.compile(
    r"\b(?:just now|today|yesterday|tomorrow|an?\s+(?:minute|hour|day|week)\s+ago|"
    r"\d+\s*(?:seconds?|minutes?|hours?|days?|weeks?)\s+ago)\b",
    re.I,
)
TOPIC_RE = re.compile(
    r"\b(price|prices|pricing|cost|costs|afford|provider|providers|clinic|clinics|"
    r"coverage|insurance|access|available|availability|switch|switching|recommend|"
    r"recommendation|compare|comparing|membership|shipping|fee|fees|telehealth)\b|"
    r"where\s+to\s+(?:go|get|start)",
    re.I,
)
PROMPT_INJECTION_RE = re.compile(
    r"ignore\s+(?:all\s+)?(?:previous|prior|above)\s+instructions|system\s+prompt|"
    r"developer\s+message|<\/?(?:tool|system|assistant)|jailbreak|do\s+not\s+follow\s+the\s+rules|"
    r"(?:post|comment|vote|dm|message)\s+(?:this|it)\s+(?:to|on)\s+reddit",
    re.I,
)
AFFILIATE_RE = re.compile(
    r"awin1\.com|clickbank|hop\.clickbank|go2cloud|impact\.com/.*(?:aff|click)|"
    r"[?&](?:aff(?:iliate)?|ref(?:erral)?|subid|clickref|coupon|promo(?:code)?)=|"
    r"\b(?:affiliate link|referral link|coupon code|referral code|use my code)\b",
    re.I,
)
PROMOTIONAL_RE = re.compile(
    r"\b(?:buy now|sign up (?:now|here|with)|click here|dm me|message me|our deal|"
    r"limited time|today only|use code|sponsored by|i work (?:for|with)|my company|my clinic)\b",
    re.I,
)
MEDICAL_RISK_RE = re.compile(
    r"\b(?:what dose|increase (?:my|the) dose|dosage advice|diagnos(?:e|is|ed)|"
    r"cure|treat my|safe for me|should i take|suicid(?:e|al)|overdose|emergency|"
    r"severe (?:pain|vomiting|depression)|medical advice|prescribe|prescription advice)\b",
    re.I,
)
DRAFT_MEDICAL_CLAIM_RE = re.compile(
    r"\b(?:cure[sd]?|treats?|diagnos(?:e|es|ed|is)|clinically proven|doctor recommended|"
    r"safe for (?:you|everyone)|guarantee(?:d|s)?|lose \d+ (?:lb|lbs|pounds?))\b",
    re.I,
)
DRAFT_CTA_RE = re.compile(
    r"\b(?:buy now|sign up|click here|dm me|message me|use my code|limited time|today only)\b",
    re.I,
)

ZERO_EFFECTS = {
    "reddit_posts": 0,
    "reddit_comments": 0,
    "reddit_votes": 0,
    "reddit_direct_messages": 0,
    "reddit_account_operations": 0,
    "reddit_logins": 0,
    "reddit_browser_actions": 0,
    "reddit_reads_outside_firecrawl": 0,
    "discord_direct_writes": 0,
    "cron_mutations": 0,
    "provider_mutations": 0,
    "spend_cents": 0,
}
FORBIDDEN_EFFECT_KEYS = set(ZERO_EFFECTS) | {
    "reddit_writes",
    "discord_writes",
    "discord_posts",
    "reddit_account_automation",
}


class PackGateError(RuntimeError):
    """A typed deterministic fail-closed admission or persistence error."""


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def pretty_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def require_hash(value: Any, code: str) -> str:
    text = str(value or "")
    if not HASH_RE.fullmatch(text):
        raise PackGateError(code)
    return text


def read_bound_json(path: Path, expected_sha256: str, label: str) -> tuple[dict[str, Any], bytes, str]:
    expected = require_hash(expected_sha256, f"{label}_expected_sha256_invalid")
    if path.is_symlink() or not path.is_file():
        raise PackGateError(f"{label}_not_regular")
    raw = path.read_bytes()
    measured = sha256_bytes(raw)
    if measured != expected:
        raise PackGateError(f"{label}_sha256_mismatch")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PackGateError(f"{label}_json_invalid") from exc
    if not isinstance(value, dict):
        raise PackGateError(f"{label}_not_object")
    return value, raw, measured


def atomic_write(path: Path, raw: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_symlink() or not path.is_file():
            raise PackGateError(f"artifact_not_regular:{path}")
        if path.read_bytes() != raw:
            raise PackGateError(f"artifact_conflict:{path}")
        path.chmod(mode)
        return
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(mode)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def parse_exact_timestamp(value: Any, code: str) -> dt.datetime:
    text = str(value or "").strip()
    if not EXACT_TIMESTAMP_RE.fullmatch(text):
        raise PackGateError(code)
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PackGateError(code) from exc
    if parsed.tzinfo is None:
        raise PackGateError(code)
    return parsed.astimezone(dt.timezone.utc)


def utc_text(value: dt.datetime) -> str:
    normalized = value.astimezone(dt.timezone.utc)
    return normalized.isoformat(timespec="seconds").replace("+00:00", "Z")


def clean_untrusted_text(value: Any, *, maximum: int) -> str:
    text = str(value or "").replace("\x00", " ")
    text = " ".join(text.split())
    return text[:maximum]


def canonicalize_reddit_thread_url(value: Any) -> tuple[str, str, str]:
    text = str(value or "").strip()
    try:
        parsed = urlparse(text)
    except ValueError as exc:
        raise PackGateError("thread_url_malformed") from exc
    host = (parsed.hostname or "").lower()
    if parsed.scheme.lower() not in {"http", "https"} or host not in {
        "reddit.com",
        "www.reddit.com",
        "old.reddit.com",
        "np.reddit.com",
    }:
        raise PackGateError("thread_url_not_reddit")
    if parsed.username or parsed.password:
        raise PackGateError("thread_url_credentials_forbidden")
    path = unquote(parsed.path)
    match = THREAD_PATH_RE.fullmatch(path)
    if not match:
        raise PackGateError("thread_url_not_thread")
    subreddit = match.group(1).lower()
    post_id = match.group(2).lower()
    if not re.fullmatch(r"[a-z0-9]{5,12}", post_id):
        raise PackGateError("thread_post_id_invalid")
    canonical = f"https://www.reddit.com/r/{subreddit}/comments/{post_id}/"
    return canonical, subreddit, post_id


def canonicalize_rules_url(value: Any) -> tuple[str, str]:
    text = str(value or "").strip()
    try:
        parsed = urlparse(text)
    except ValueError as exc:
        raise PackGateError("rules_url_malformed") from exc
    if parsed.scheme.lower() != "https" or (parsed.hostname or "").lower() not in {
        "reddit.com",
        "www.reddit.com",
        "old.reddit.com",
    }:
        raise PackGateError("rules_url_not_authoritative_reddit")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise PackGateError("rules_url_not_canonical")
    match = RULE_PATH_RE.fullmatch(unquote(parsed.path))
    if not match:
        raise PackGateError("rules_url_not_about_rules")
    subreddit = match.group(1).lower()
    return f"https://www.reddit.com/r/{subreddit}/about/rules/", subreddit


def canonicalize_reddit_profile_url(value: Any) -> tuple[str, str]:
    text = str(value or "").strip()
    try:
        parsed = urlparse(text)
    except ValueError as exc:
        raise PackGateError("account_source_url_malformed") from exc
    if parsed.scheme.lower() != "https" or (parsed.hostname or "").lower() not in {
        "reddit.com",
        "www.reddit.com",
        "old.reddit.com",
    }:
        raise PackGateError("account_source_url_not_authoritative_reddit")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise PackGateError("account_source_url_not_canonical")
    match = PROFILE_PATH_RE.fullmatch(unquote(parsed.path))
    if not match:
        raise PackGateError("account_source_url_not_profile")
    username = match.group(1)
    return f"https://www.reddit.com/user/{username}/", username


def _consistent_uuid(*values: Any) -> tuple[str | None, str]:
    """Resolve one identifier only when every returned representation agrees."""
    nonempty = [str(value).strip() for value in values if value not in (None, "")]
    if not nonempty:
        return None, "NOT_RETURNED_FAIL_CLOSED"
    if any(not UUID_RE.fullmatch(value) for value in nonempty):
        return None, "RETURNED_INVALID_FAIL_CLOSED"
    unique = {value.lower() for value in nonempty}
    if len(unique) != 1:
        return None, "RETURNED_CONFLICT_FAIL_CLOSED"
    return next(iter(unique)), "RETURNED_CONSISTENT"


def _trusted_now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _assert_as_of_near_wall_clock(as_of: dt.datetime, trusted_now: dt.datetime) -> None:
    age_hours = (trusted_now - as_of).total_seconds() / 3600.0
    if age_hours < -FUTURE_TOLERANCE_SECONDS / 3600.0 or age_hours > MAX_AS_OF_WALL_CLOCK_AGE_HOURS:
        raise PackGateError("as_of_outside_trusted_wall_clock")


def pt_day(value: dt.datetime) -> str:
    return value.astimezone(PT).date().isoformat()


def derive_run_id(
    *,
    capture_sha256: str,
    evidence_sha256: str | None,
    as_of_utc: str,
    policy: dict[str, Any],
) -> str:
    """Derive the immutable run identity from the complete normalized run subject."""
    subject = {
        "schema": "glp-reddit-firecrawl-run-subject-v3",
        "policy_version": POLICY_VERSION,
        "capture_sha256": capture_sha256,
        "evidence_sha256": evidence_sha256,
        "as_of_utc": as_of_utc,
        "policy": policy,
    }
    return "run_" + sha256_bytes(canonical_bytes(subject))[:28]


def enforce_zero_effects(document: dict[str, Any], label: str) -> None:
    effects = document.get("effects")
    if effects is None:
        return
    if not isinstance(effects, dict):
        raise PackGateError(f"{label}_effects_invalid")
    for key, value in effects.items():
        if key in FORBIDDEN_EFFECT_KEYS:
            if isinstance(value, bool) or not isinstance(value, int) or value != 0:
                raise PackGateError(f"{label}_nonzero_effect:{key}")


def _request_lineage(capture: dict[str, Any], group: str) -> dict[str, Any]:
    request = capture.get("request")
    if not isinstance(request, dict):
        raise PackGateError(f"capture_request_invalid:{group}")
    domains = request.get("includeDomains")
    if not isinstance(domains, list) or not domains or any(str(value).lower() not in {"reddit.com", "www.reddit.com"} for value in domains):
        raise PackGateError(f"capture_domain_not_reddit_only:{group}")
    sources = request.get("sources")
    if not isinstance(sources, list) or not sources or any(not isinstance(row, dict) or row.get("type") != "web" for row in sources):
        raise PackGateError(f"capture_source_not_web:{group}")
    query = str(request.get("query") or "").strip()
    if not query:
        raise PackGateError(f"capture_query_missing:{group}")
    return {
        "query_group": group,
        "query": query,
        "query_sha256": sha256_text(query),
        "request_sha256": sha256_bytes(canonical_bytes(request)),
        "tbs": request.get("tbs"),
        "limit": request.get("limit"),
        "include_domains": [str(value) for value in domains],
        "sources": sources,
    }


def normalize_firecrawl_capture(document: dict[str, Any], capture_sha256: str, capture_path: Path) -> dict[str, Any]:
    if document.get("schema") != "glp-reddit-firecrawl-search-captures-v1":
        raise PackGateError("capture_schema_invalid")
    # A free-form string that merely contains “Firecrawl” is not provenance.
    if document.get("research_stack") not in EXACT_FIRECRAWL_STACKS:
        raise PackGateError("capture_firecrawl_provider_claim_invalid")
    enforce_zero_effects(document, "capture")
    captured_at = parse_exact_timestamp(document.get("captured_at_utc"), "capture_timestamp_invalid")
    captures = document.get("captures")
    if not isinstance(captures, list) or not captures:
        raise PackGateError("captures_missing")

    thread_rows: list[dict[str, Any]] = []
    rule_rows: list[dict[str, Any]] = []
    account_rows: list[dict[str, Any]] = []
    lineage: list[dict[str, Any]] = []
    groups_seen: set[str] = set()
    for capture_index, capture_value in enumerate(captures, start=1):
        if not isinstance(capture_value, dict):
            raise PackGateError("capture_group_not_object")
        capture = capture_value
        group = str(capture.get("group") or "").strip()
        if not group or group in groups_seen:
            raise PackGateError("capture_group_missing_or_duplicate")
        groups_seen.add(group)
        request_lineage = _request_lineage(capture, group)
        response = capture.get("response")
        if not isinstance(response, dict) or response.get("success") is not True:
            raise PackGateError(f"capture_response_not_success:{group}")
        web = ((response.get("data") or {}).get("web") if isinstance(response.get("data"), dict) else None)
        if not isinstance(web, list):
            raise PackGateError(f"capture_web_rows_missing:{group}")

        search_id, search_id_status = _consistent_uuid(
            capture.get("firecrawl_search_id"),
            capture.get("search_id"),
            response.get("firecrawl_search_id"),
            response.get("search_id"),
            response.get("id"),
        )
        if search_id_status == "RETURNED_CONSISTENT" and not (
            capture.get("firecrawl_search_id") and response.get("firecrawl_search_id")
        ):
            search_id = None
            search_id_status = "RETURNED_INCOMPLETE_FAIL_CLOSED"

        purpose_raw = str(capture.get("purpose") or "")
        if group == "rule_page_discovery" or purpose_raw == "rule_discovery":
            purpose = "rule_discovery"
            target = rule_rows
        elif purpose_raw == "account_discovery":
            purpose = "account_discovery"
            target = account_rows
        else:
            purpose = "thread_discovery"
            target = thread_rows
        lineage.append(
            {
                **request_lineage,
                "capture_index": capture_index,
                "purpose": purpose,
                "raw_hits": len(web),
                "firecrawl_search_id": search_id,
                "firecrawl_search_id_status": search_id_status,
            }
        )

        for fallback_rank, value in enumerate(web, start=1):
            if not isinstance(value, dict):
                raise PackGateError(f"capture_result_not_object:{group}")
            rank = value.get("position", fallback_rank)
            if isinstance(rank, bool) or not isinstance(rank, int) or rank < 1:
                raise PackGateError(f"capture_rank_invalid:{group}")
            result_id, result_status = _consistent_uuid(
                search_id,
                value.get("firecrawl_search_id"),
                value.get("search_id"),
            )
            if search_id_status != "RETURNED_CONSISTENT":
                result_id, result_status = None, search_id_status
            elif not value.get("firecrawl_search_id"):
                result_id, result_status = None, "RESULT_ID_NOT_RETURNED_FAIL_CLOSED"
            exact_url = str(value.get("url") or "").strip()
            title = clean_untrusted_text(value.get("title"), maximum=500)
            snippet = clean_untrusted_text(value.get("description"), maximum=2000)
            source_subject = {
                "capture_timestamp_utc": utc_text(captured_at),
                "query_group": group,
                "query_sha256": request_lineage["query_sha256"],
                "rank": rank,
                "exact_url": exact_url,
                "title": title,
                "snippet": snippet,
                "firecrawl_search_id": result_id,
                "firecrawl_search_id_status": result_status,
            }
            target.append(
                {
                    **source_subject,
                    "source_row_sha256": sha256_bytes(canonical_bytes(source_subject)),
                }
            )

    raw_rejections: list[dict[str, Any]] = []
    by_url: dict[str, dict[str, Any]] = {}
    for row in thread_rows:
        try:
            canonical_url, subreddit, post_id = canonicalize_reddit_thread_url(row["exact_url"])
        except PackGateError as exc:
            raw_rejections.append(
                {
                    "query_group": row["query_group"],
                    "rank": row["rank"],
                    "exact_url": row["exact_url"],
                    "source_row_sha256": row["source_row_sha256"],
                    "reason": str(exc),
                }
            )
            row["canonical_url"] = None
            row["canonicalization_status"] = "REJECTED_FAIL_CLOSED"
            row["canonicalization_reason"] = str(exc)
            continue
        row["canonical_url"] = canonical_url
        row["canonicalization_status"] = "CANONICALIZED"
        row["canonicalization_reason"] = None
        candidate = by_url.setdefault(
            canonical_url,
            {
                "canonical_url": canonical_url,
                "subreddit": subreddit,
                "post_id": post_id,
                "occurrences": [],
                "source_row_sha256s": [],
            },
        )
        content_subject = {"title": row["title"], "snippet": row["snippet"]}
        candidate["occurrences"].append(
            {
                "query_group": row["query_group"],
                "rank": row["rank"],
                "exact_url": row["exact_url"],
                "title": row["title"],
                "snippet": row["snippet"],
                "content_sha256": sha256_bytes(canonical_bytes(content_subject)),
                "firecrawl_search_id": row["firecrawl_search_id"],
                "firecrawl_search_id_status": row["firecrawl_search_id_status"],
                "source_row_sha256": row["source_row_sha256"],
            }
        )
        candidate["source_row_sha256s"].append(row["source_row_sha256"])

    candidates: list[dict[str, Any]] = []
    duplicate_groups: list[dict[str, Any]] = []
    for canonical_url in sorted(by_url):
        candidate = by_url[canonical_url]
        candidate["occurrences"] = sorted(
            candidate["occurrences"],
            key=lambda row: (row["query_group"], row["rank"], row["source_row_sha256"]),
        )
        candidate["source_row_sha256s"] = sorted(candidate["source_row_sha256s"])
        candidate["title"] = candidate["occurrences"][0]["title"]
        candidate["snippet"] = candidate["occurrences"][0]["snippet"]
        candidate["duplicate_occurrences"] = len(candidate["occurrences"]) - 1
        candidate["duplicate_content_conflict"] = len(
            {row["content_sha256"] for row in candidate["occurrences"]}
        ) > 1
        if candidate["duplicate_occurrences"]:
            duplicate_groups.append(
                {
                    "canonical_url": canonical_url,
                    "occurrence_count": len(candidate["occurrences"]),
                    "duplicate_count": candidate["duplicate_occurrences"],
                    "content_conflict": candidate["duplicate_content_conflict"],
                    "source_row_sha256s": candidate["source_row_sha256s"],
                }
            )
        candidates.append(candidate)

    normalized_rule_rows: list[dict[str, Any]] = []
    for row in rule_rows:
        canonical_rules_url = None
        subreddit = None
        with contextlib.suppress(PackGateError):
            canonical_rules_url, subreddit = canonicalize_rules_url(row["exact_url"])
        normalized_rule_rows.append(
            {
                **row,
                "canonical_rules_url": canonical_rules_url,
                "subreddit": subreddit,
                "is_authoritative_rules_url_lead": canonical_rules_url is not None,
                "qualification": "DISCOVERY_LEAD_ONLY_NOT_RULE_EVIDENCE",
            }
        )

    normalized_account_rows: list[dict[str, Any]] = []
    for row in account_rows:
        canonical_profile_url = None
        username = None
        with contextlib.suppress(PackGateError):
            canonical_profile_url, username = canonicalize_reddit_profile_url(row["exact_url"])
        normalized_account_rows.append(
            {
                **row,
                "canonical_profile_url": canonical_profile_url,
                "username": username,
                "is_authoritative_profile_url_lead": canonical_profile_url is not None,
                "qualification": "DISCOVERY_LEAD_ONLY_NOT_ACCOUNT_EVIDENCE",
            }
        )

    return {
        "capture_path": str(capture_path),
        "capture_sha256": capture_sha256,
        "capture_timestamp_utc": utc_text(captured_at),
        "lineage": sorted(lineage, key=lambda row: row["capture_index"]),
        "raw_thread_rows": thread_rows,
        "rule_query_rows": normalized_rule_rows,
        "account_query_rows": normalized_account_rows,
        "raw_rejections": raw_rejections,
        "candidates": candidates,
        "duplicate_groups": duplicate_groups,
    }


def load_evidence(path: Path | None, expected_sha256: str | None) -> tuple[dict[str, Any], str | None, str | None]:
    if path is None:
        if expected_sha256 not in (None, ""):
            raise PackGateError("evidence_path_missing")
        return {
            "thread_publication_evidence": [],
            "rule_evidence": [],
            "account_eligibility": None,
        }, None, None
    if not expected_sha256:
        raise PackGateError("evidence_expected_sha256_required")
    document, _raw, measured = read_bound_json(path, expected_sha256, "evidence")
    if document.get("schema") != "glp-reddit-firecrawl-qualification-evidence-v3":
        raise PackGateError("evidence_schema_invalid")
    if document.get("web_provider") != "firecrawl":
        raise PackGateError("evidence_web_provider_not_firecrawl")
    require_hash(document.get("source_capture_sha256"), "evidence_source_capture_sha256_invalid")
    enforce_zero_effects(document, "evidence")
    for key in ("thread_publication_evidence", "rule_evidence"):
        if not isinstance(document.get(key), list):
            raise PackGateError(f"evidence_{key}_not_list")
    return document, str(path), measured


def _index_thread_evidence(document: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    indexed: dict[str, list[dict[str, Any]]] = {}
    for value in document.get("thread_publication_evidence") or []:
        if not isinstance(value, dict):
            raise PackGateError("thread_publication_evidence_not_object")
        canonical_url, _subreddit, _post_id = canonicalize_reddit_thread_url(
            value.get("canonical_url") or value.get("source_url")
        )
        indexed.setdefault(canonical_url, []).append(value)
    return indexed


def _index_rule_evidence(document: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    indexed: dict[str, list[dict[str, Any]]] = {}
    for value in document.get("rule_evidence") or []:
        if not isinstance(value, dict):
            raise PackGateError("rule_evidence_not_object")
        _canonical_url, subreddit = canonicalize_rules_url(value.get("rules_url"))
        indexed.setdefault(subreddit, []).append(value)
    return indexed


def _validate_firecrawl_lineage(
    row: dict[str, Any],
    *,
    source_capture_sha256: str,
    discovery_rows: list[dict[str, Any]],
    code: str,
) -> dict[str, Any]:
    lineage = row.get("firecrawl_lineage")
    required_keys = {
        "provider",
        "source_capture_sha256",
        "discovery_source_row_sha256",
        "search_id",
        "search_response_id",
        "result_search_id",
        "scrape_id",
        "scrape_response_id",
    }
    if not isinstance(lineage, dict) or set(lineage) != required_keys:
        raise PackGateError(code)
    if lineage.get("provider") != "firecrawl":
        raise PackGateError(code)
    if require_hash(lineage.get("source_capture_sha256"), code) != source_capture_sha256:
        raise PackGateError(code)
    discovery_sha = require_hash(lineage.get("discovery_source_row_sha256"), code)
    matching = [item for item in discovery_rows if item.get("source_row_sha256") == discovery_sha]
    if len(matching) != 1 or matching[0].get("firecrawl_search_id_status") != "RETURNED_CONSISTENT":
        raise PackGateError(code)
    search_id, search_status = _consistent_uuid(
        row.get("firecrawl_search_id"),
        lineage.get("search_id"),
        lineage.get("search_response_id"),
        lineage.get("result_search_id"),
        matching[0].get("firecrawl_search_id"),
    )
    scrape_id, scrape_status = _consistent_uuid(
        row.get("firecrawl_scrape_id"),
        lineage.get("scrape_id"),
        lineage.get("scrape_response_id"),
    )
    if search_status != "RETURNED_CONSISTENT" or scrape_status != "RETURNED_CONSISTENT":
        raise PackGateError(code)
    if search_id == scrape_id:
        raise PackGateError(code)
    return {
        "provider": "firecrawl",
        "source_capture_sha256": source_capture_sha256,
        "discovery_source_row_sha256": discovery_sha,
        "firecrawl_search_id": search_id,
        "firecrawl_scrape_id": scrape_id,
    }


def _publication_gate(
    rows: list[dict[str, Any]],
    candidate: dict[str, Any],
    as_of: dt.datetime,
    *,
    source_capture_sha256: str,
) -> tuple[dict[str, Any] | None, list[str], bool, bool]:
    if not rows:
        return None, ["exact_publication_timestamp_missing"], False, False
    normalized: list[dict[str, Any]] = []
    invalid = False
    for row in rows:
        try:
            published = parse_exact_timestamp(row.get("published_at_utc"), "invalid")
            captured = parse_exact_timestamp(row.get("captured_at_utc"), "invalid")
            source_url, _subreddit, _post_id = canonicalize_reddit_thread_url(row.get("source_url"))
            source_sha = require_hash(row.get("source_sha256"), "invalid")
            quote = clean_untrusted_text(row.get("timestamp_quote"), maximum=1000)
            lineage = _validate_firecrawl_lineage(
                row,
                source_capture_sha256=source_capture_sha256,
                discovery_rows=candidate["occurrences"],
                code="invalid",
            )
            published_text = utc_text(published)
            captured_age_hours = (as_of - captured).total_seconds() / 3600.0
            if (
                source_url != candidate["canonical_url"]
                or not quote
                or RELATIVE_TIME_RE.search(quote)
                or published_text not in quote
                or captured_age_hours < -FUTURE_TOLERANCE_SECONDS / 3600.0
                or captured_age_hours > MAX_PUBLICATION_EVIDENCE_CAPTURE_AGE_HOURS
                or published > captured + dt.timedelta(seconds=FUTURE_TOLERANCE_SECONDS)
            ):
                raise PackGateError("invalid")
            normalized.append(
                {
                    "published_at_utc": published_text,
                    "captured_at_utc": utc_text(captured),
                    "capture_age_hours_at_run": round(captured_age_hours, 6),
                    "source_url": source_url,
                    "source_sha256": source_sha,
                    "firecrawl_lineage": lineage,
                    "timestamp_quote": quote,
                }
            )
        except PackGateError:
            invalid = True
    if not normalized:
        return None, ["exact_publication_evidence_invalid"], False, False
    values = {row["published_at_utc"] for row in normalized}
    identities = {sha256_bytes(canonical_bytes(row)) for row in normalized}
    if len(values) != 1 or len(identities) != 1:
        return None, ["exact_publication_timestamp_conflict"], True, False
    chosen = normalized[0]
    published = parse_exact_timestamp(chosen["published_at_utc"], "publication_timestamp_invalid")
    age_hours = (as_of - published).total_seconds() / 3600.0
    chosen["age_hours_at_run"] = round(age_hours, 6)
    reasons: list[str] = []
    if invalid:
        reasons.append("exact_publication_evidence_conflict_or_invalid_extra")
    if age_hours < -FUTURE_TOLERANCE_SECONDS / 3600.0:
        reasons.append("publication_timestamp_future")
    elif age_hours > MAX_THREAD_AGE_HOURS:
        reasons.append("publication_timestamp_stale")
    return chosen, sorted(set(reasons)), True, not reasons


def _constraint_semantics_consistent(kind: str, value: str, basis_quote: str, exact_quote: str) -> bool:
    lowered_basis = basis_quote.casefold()
    lowered_all = exact_quote.casefold()
    prohibition_patterns = {
        "promotion": r"(?:no|prohibit(?:ed|s)?|not allowed|forbid(?:den|s)?)\s+(?:self[- ]?)?promot|(?:self[- ]?promotion|promotional posts?)\s+(?:is|are)\s+(?:prohibited|not allowed|forbidden)",
        "affiliate_links": r"(?:affiliate|referral)\s+(?:links?|codes?)\s+(?:are|is)\s+(?:prohibited|not allowed|forbidden)|no\s+(?:affiliate|referral)\s+(?:links?|codes?)",
        "medical_advice": r"medical advice\s+(?:is|are)\s+(?:prohibited|not allowed|forbidden)|no\s+medical advice",
    }
    prohibited_anywhere = bool(re.search(prohibition_patterns[kind], lowered_all, re.I))
    if value == "PROHIBITED":
        return prohibited_anywhere and bool(re.search(r"prohibit|not allowed|forbid|\bno\b", lowered_basis, re.I))
    if value in {"HELPFUL_DISCLOSED_NON_PROMOTIONAL_REPLY_ALLOWED", "ALLOWED_WITH_DISCLOSURE", "ALLOWED"}:
        return "allow" in lowered_basis and not prohibited_anywhere
    return value == "UNKNOWN"


def _rule_gate(
    rows: list[dict[str, Any]],
    subreddit: str,
    as_of: dt.datetime,
    *,
    source_capture_sha256: str,
    discovery_rows: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, list[str], bool]:
    if not rows:
        return None, ["authoritative_current_subreddit_rules_missing"], False
    normalized: list[dict[str, Any]] = []
    reasons: list[str] = []
    for row in rows:
        try:
            rules_url, rules_subreddit = canonicalize_rules_url(row.get("rules_url"))
            captured = parse_exact_timestamp(row.get("captured_at_utc"), "invalid")
            source_sha = require_hash(row.get("source_sha256"), "invalid")
            quote = clean_untrusted_text(row.get("exact_quote"), maximum=8000)
            constraints = row.get("constraints")
            basis = row.get("constraint_quotes")
            lineage = _validate_firecrawl_lineage(
                row,
                source_capture_sha256=source_capture_sha256,
                discovery_rows=discovery_rows,
                code="invalid",
            )
            age_days = (as_of - captured).total_seconds() / 86400.0
            if (
                rules_subreddit != subreddit
                or row.get("is_authoritative_current_rules_page") is not True
                or len(quote) < 20
                or re.search(r"recaptcha|captcha requires|access denied|query answer was empty", quote, re.I)
                or not isinstance(constraints, dict)
                or not isinstance(basis, dict)
                or age_days < -FUTURE_TOLERANCE_SECONDS / 86400.0
                or age_days > MAX_RULE_AGE_DAYS
            ):
                raise PackGateError("invalid")
            allowed_values = {
                "promotion": {"HELPFUL_DISCLOSED_NON_PROMOTIONAL_REPLY_ALLOWED", "PROHIBITED", "UNKNOWN"},
                "affiliate_links": {"PROHIBITED", "ALLOWED_WITH_DISCLOSURE", "UNKNOWN"},
                "medical_advice": {"PROHIBITED", "ALLOWED", "UNKNOWN"},
            }
            normalized_constraints: dict[str, str] = {}
            normalized_basis: dict[str, str] = {}
            for key, values in allowed_values.items():
                policy_value = str(constraints.get(key) or "")
                basis_quote = clean_untrusted_text(basis.get(key), maximum=1000)
                if (
                    policy_value not in values
                    or not basis_quote
                    or basis_quote.casefold() not in quote.casefold()
                    or not _constraint_semantics_consistent(key, policy_value, basis_quote, quote)
                ):
                    raise PackGateError("invalid")
                normalized_constraints[key] = policy_value
                normalized_basis[key] = basis_quote
            normalized.append(
                {
                    "subreddit": subreddit,
                    "rules_url": rules_url,
                    "captured_at_utc": utc_text(captured),
                    "age_days_at_run": round(age_days, 6),
                    "firecrawl_lineage": lineage,
                    "source_sha256": source_sha,
                    "exact_quote": quote,
                    "constraints": normalized_constraints,
                    "constraint_quotes": normalized_basis,
                }
            )
        except PackGateError:
            reasons.append("authoritative_subreddit_rule_evidence_invalid")
    if not normalized:
        return None, sorted(set(reasons or ["authoritative_current_subreddit_rules_missing"])), False
    identities = {sha256_bytes(canonical_bytes(row)) for row in normalized}
    if len(identities) != 1:
        return None, ["authoritative_subreddit_rule_evidence_conflict"], False
    chosen = normalized[0]
    constraints = chosen["constraints"]
    if constraints["promotion"] == "PROHIBITED":
        reasons.append("subreddit_rule_prohibits_disclosed_reply")
    elif constraints["promotion"] == "UNKNOWN":
        reasons.append("subreddit_promotion_rule_unknown")
    if constraints["affiliate_links"] == "UNKNOWN":
        reasons.append("subreddit_affiliate_rule_unknown")
    if constraints["medical_advice"] == "UNKNOWN":
        reasons.append("subreddit_medical_rule_unknown")
    return chosen, sorted(set(reasons)), True


def _account_gate(
    document: dict[str, Any],
    subreddit: str,
    as_of: dt.datetime,
    *,
    source_capture_sha256: str,
    discovery_rows: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, list[str]]:
    account = document.get("account_eligibility")
    if not isinstance(account, dict):
        return None, ["account_eligibility_unknown"]
    try:
        identity = account.get("identity")
        receipt = account.get("authoritative_source_receipt")
        if not isinstance(identity, dict) or not isinstance(receipt, dict):
            raise PackGateError("invalid")
        if set(identity) != {"platform", "username", "account_id", "profile_url", "identity_sha256"}:
            raise PackGateError("invalid")
        username = str(identity.get("username") or "")
        account_id = str(identity.get("account_id") or "").lower()
        profile_url, profile_username = canonicalize_reddit_profile_url(identity.get("profile_url"))
        identity_subject = {
            "platform": "reddit",
            "username": username,
            "account_id": account_id,
            "profile_url": profile_url,
        }
        if (
            identity.get("platform") != "reddit"
            or username.casefold() != profile_username.casefold()
            or not ACCOUNT_ID_RE.fullmatch(account_id)
            or require_hash(identity.get("identity_sha256"), "invalid")
            != sha256_bytes(canonical_bytes(identity_subject))
        ):
            raise PackGateError("invalid")
        if receipt.get("schema") != "reddit-account-authoritative-source-receipt-v1":
            raise PackGateError("invalid")
        source_url, receipt_username = canonicalize_reddit_profile_url(receipt.get("source_url"))
        verified = parse_exact_timestamp(account.get("verified_at_utc"), "invalid")
        receipt_captured = parse_exact_timestamp(receipt.get("captured_at_utc"), "invalid")
        source_sha = require_hash(receipt.get("source_sha256"), "invalid")
        quote = clean_untrusted_text(receipt.get("exact_quote"), maximum=2000)
        lineage = _validate_firecrawl_lineage(
            receipt,
            source_capture_sha256=source_capture_sha256,
            discovery_rows=discovery_rows,
            code="invalid",
        )
        if (
            receipt.get("source_type") != "REDDIT_FIRST_PARTY_ACCOUNT_PROFILE"
            or receipt.get("account_id") != account_id
            or str(receipt.get("username") or "").casefold() != username.casefold()
            or receipt_username.casefold() != username.casefold()
            or source_url != profile_url
            or verified != receipt_captured
            or username.casefold() not in quote.casefold()
            or account_id.casefold() not in quote.casefold()
        ):
            raise PackGateError("invalid")
        receipt_sha = sha256_bytes(canonical_bytes(receipt))
    except PackGateError:
        return None, ["account_eligibility_evidence_invalid"]

    reasons: list[str] = []
    age_days = (as_of - verified).total_seconds() / 86400.0
    if age_days < -FUTURE_TOLERANCE_SECONDS / 86400.0:
        reasons.append("account_eligibility_future")
    elif age_days > MAX_ACCOUNT_AGE_DAYS:
        reasons.append("account_eligibility_stale")
    if account.get("status") != "VERIFIED_ELIGIBLE":
        reasons.append("account_eligibility_unknown")
    communities = account.get("communities")
    row = communities.get(subreddit) if isinstance(communities, dict) else None
    if not isinstance(row, dict):
        reasons.append("account_community_eligibility_unknown")
    else:
        if row.get("eligible") is not True or row.get("requirements_checked") is not True:
            reasons.append("account_community_not_eligible")
        if row.get("authority_receipt_sha256") != receipt_sha:
            reasons.append("account_community_authority_unbound")
        if isinstance(row.get("account_age_days"), bool) or not isinstance(row.get("account_age_days"), int) or row["account_age_days"] < 0:
            reasons.append("account_age_not_explicit")
        if isinstance(row.get("combined_karma"), bool) or not isinstance(row.get("combined_karma"), int) or row["combined_karma"] < 0:
            reasons.append("account_karma_not_explicit")
    return {
        "status": account.get("status"),
        "verified_at_utc": utc_text(verified),
        "age_days_at_run": round(age_days, 6),
        "identity": identity_subject,
        "identity_sha256": identity["identity_sha256"],
        "authoritative_source_receipt": receipt,
        "authoritative_source_receipt_sha256": receipt_sha,
        "source_sha256": source_sha,
        "firecrawl_lineage": lineage,
        "community": row,
    }, sorted(set(reasons))


def _candidate_content_reasons(candidate: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    occurrence_reason_sets: list[tuple[str, ...]] = []
    for occurrence in candidate.get("occurrences") or []:
        text = f"{occurrence.get('title', '')} {occurrence.get('snippet', '')}"
        occurrence_reasons: list[str] = []
        if not TOPIC_RE.search(text):
            occurrence_reasons.append("thread_topic_not_relevant")
        if PROMPT_INJECTION_RE.search(text):
            occurrence_reasons.append("prompt_injection_suspected")
        if AFFILIATE_RE.search(text):
            occurrence_reasons.append("affiliate_or_referral_content")
        if PROMOTIONAL_RE.search(text):
            occurrence_reasons.append("promotional_content")
        if MEDICAL_RISK_RE.search(text):
            occurrence_reasons.append("medical_advice_or_crisis_content")
        occurrence_reason_sets.append(tuple(sorted(set(occurrence_reasons))))
        reasons.extend(occurrence_reasons)
    if candidate.get("duplicate_content_conflict"):
        reasons.append("duplicate_occurrence_content_conflict")
    if len(set(occurrence_reason_sets)) > 1:
        reasons.append("duplicate_occurrence_safety_conflict")
    return sorted(set(reasons))


def lint_help_first_draft(value: str) -> list[str]:
    text = str(value or "").strip()
    reasons: list[str] = []
    if len(text.split()) < 45:
        reasons.append("draft_not_help_first_or_self_contained")
    if len(text) > 1600:
        reasons.append("draft_overflow")
    if "affiliated with glp clinic scout" not in text.lower():
        reasons.append("affiliation_disclosure_missing")
    if "http://" in text.lower() or "https://" in text.lower() or AFFILIATE_RE.search(text):
        reasons.append("draft_link_or_affiliate_surface_present")
    if DRAFT_MEDICAL_CLAIM_RE.search(text):
        reasons.append("draft_medical_claim_present")
    if DRAFT_CTA_RE.search(text):
        reasons.append("draft_promotional_cta_present")
    if "operator posts manually" not in text.lower():
        reasons.append("manual_post_boundary_missing")
    return sorted(set(reasons))


def build_help_first_draft() -> str:
    draft = (
        "Disclosure: I’m affiliated with GLP Clinic Scout. A practical comparison starts with the ongoing "
        "monthly price rather than an introductory number. Check whether medication, membership, labs, "
        "shipping, visits, dose-related price changes, cancellation terms, and minimum commitments are included. "
        "Write the answers in the same columns and verify current terms directly with each provider before paying. "
        "This is general comparison guidance, not individualized medical advice. OPERATOR POSTS MANUALLY; this "
        "local pack never posts, comments, votes, messages, or logs in to Reddit."
    )
    lint = lint_help_first_draft(draft)
    if lint:
        raise PackGateError("generated_draft_lint_failed:" + ",".join(lint))
    return draft


def _reason_counts(candidates: Iterable[dict[str, Any]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for candidate in candidates:
        counts.update(candidate.get("rejection_reasons") or [])
    return dict(sorted(counts.items()))


def _build_manual_pack(
    candidate: dict[str, Any],
    *,
    run_id: str,
    capture_sha256: str,
    evidence_sha256: str | None,
    as_of_utc: str,
) -> dict[str, Any]:
    publication = candidate.get("publication_evidence")
    rules = candidate.get("rule_evidence")
    account = candidate.get("account_evidence")
    if not isinstance(publication, dict) or not isinstance(rules, dict) or not isinstance(account, dict):
        raise PackGateError("manual_pack_incomplete_evidence")
    draft = build_help_first_draft()
    pack_subject = {
        "run_id": run_id,
        "canonical_url": candidate["canonical_url"],
        "capture_sha256": capture_sha256,
        "evidence_sha256": evidence_sha256,
    }
    pack_id = "pack_" + sha256_bytes(canonical_bytes(pack_subject))[:24]
    pack = {
        "schema": "glp-reddit-manual-review-pack-v2",
        "status": "HELD_MANUAL_OPERATOR_REVIEW",
        "pack_id": pack_id,
        "as_of_utc": as_of_utc,
        "thread": {
            "canonical_url": candidate["canonical_url"],
            "title": candidate["title"],
            "subreddit": candidate["subreddit"],
            "publication_evidence": publication,
        },
        "rule_capture": rules,
        "qualification_reason": "Complete Firecrawl discovery lineage, exact fresh publication evidence, exact current authoritative rules, explicit rule constraints, safe thread content, and current account eligibility all passed.",
        "compliance_notes": [
            "Help-first and self-contained; no site, affiliate, referral, coupon, or tracking link.",
            "No medical advice, diagnosis, dosage, safety, outcome, superlative, urgency, or guarantee claim.",
            "Affiliation is disclosed; exact subreddit rule constraints are attached for operator recheck.",
            "OPERATOR POSTS MANUALLY from their own account only after rechecking the live thread and rules.",
        ],
        "source_hashes": {
            "firecrawl_capture_sha256": capture_sha256,
            "qualification_evidence_sha256": evidence_sha256,
            "discovery_source_row_sha256s": candidate["source_row_sha256s"],
            "publication_source_sha256": publication["source_sha256"],
            "rule_source_sha256": rules["source_sha256"],
            "account_source_sha256": account["source_sha256"],
        },
        "help_first_draft": draft,
        "draft_sha256": sha256_text(draft),
        "capabilities": {
            "reddit_post": False,
            "reddit_comment": False,
            "reddit_vote": False,
            "reddit_direct_message": False,
            "reddit_login": False,
            "reddit_account_operation": False,
            "browser_automation": False,
        },
    }
    if len(pretty_bytes(pack)) > 24_000:
        raise PackGateError("manual_pack_card_overflow")
    return pack


def _refresh_packet_summary(packet: dict[str, Any]) -> None:
    evaluated = packet["candidate_dispositions"]
    reason_counts = _reason_counts(evaluated)
    eligible_count = sum(bool(candidate["eligible"]) for candidate in evaluated)
    packet["denominators"]["eligible_threads"] = eligible_count
    packet["denominators"]["rejected_unique_threads"] = len(evaluated) - eligible_count
    packet["denominators"]["manual_review_packs_created"] = len(packet["manual_review_packs"])
    packet["denominators"]["rejection_reason_counts"] = reason_counts
    conflict = any("conflict" in reason for reason in reason_counts)
    missing_rule_or_time = any(
        reason in reason_counts
        for reason in {
            "authoritative_current_subreddit_rules_missing",
            "authoritative_current_subreddit_rules_stale",
            "authoritative_subreddit_rule_evidence_invalid",
            "authoritative_subreddit_rule_capture_future",
            "exact_publication_timestamp_missing",
            "exact_publication_evidence_invalid",
            "exact_publication_evidence_conflict_or_invalid_extra",
            "publication_timestamp_stale",
            "publication_timestamp_future",
        }
    )
    provenance_incomplete = any(
        reason in reason_counts
        for reason in {
            "firecrawl_search_id_missing",
            "firecrawl_search_id_invalid",
            "firecrawl_lineage_conflict_or_incomplete",
            "firecrawl_capture_stale",
            "firecrawl_capture_timestamp_future",
        }
    )
    if conflict:
        disposition = "BLOCKED_CONFLICTING_RULE_OR_FRESHNESS_EVIDENCE"
    elif missing_rule_or_time:
        disposition = "BLOCKED_NO_EXACT_RULE_OR_FRESHNESS_EVIDENCE"
    elif provenance_incomplete:
        disposition = "BLOCKED_FIRECRAWL_PROVENANCE_INCOMPLETE"
    elif eligible_count:
        disposition = "ELIGIBLE_MANUAL_REVIEW_PACKS_READY"
    else:
        disposition = "NO_ELIGIBLE_TARGET"
    packet["status"] = disposition
    if disposition == "BLOCKED_NO_EXACT_RULE_OR_FRESHNESS_EVIDENCE":
        packet["remediation"] = (
            "Run new Firecrawl-only searches and exact maxAge=0 scrapes; save one consistent returned "
            "search/scrape lineage. Bind exact original publication timestamp quotes and fresh authoritative "
            "/about/rules/ quotes. Do not infer dates or rules from search windows, ranks, snippets, Reddit IDs, "
            "relative-age text, or caller-controlled historical as-of values."
        )
    else:
        packet["remediation"] = None


def qualify_capture(
    source: dict[str, Any],
    evidence: dict[str, Any],
    *,
    as_of: dt.datetime,
    evidence_path: str | None,
    evidence_sha256: str | None,
) -> dict[str, Any]:
    capture_time = parse_exact_timestamp(source["capture_timestamp_utc"], "capture_timestamp_invalid")
    capture_age_hours = (as_of - capture_time).total_seconds() / 3600.0
    if evidence_sha256 and evidence.get("source_capture_sha256") != source["capture_sha256"]:
        raise PackGateError("evidence_source_capture_sha256_mismatch")
    thread_evidence = _index_thread_evidence(evidence)
    rule_evidence = _index_rule_evidence(evidence)
    rule_discovery: dict[str, list[dict[str, Any]]] = {}
    for row in source["rule_query_rows"]:
        if row.get("subreddit"):
            rule_discovery.setdefault(row["subreddit"], []).append(row)
    evaluated: list[dict[str, Any]] = []
    exact_timestamp_count = 0
    fresh_timestamp_count = 0
    qualified_count = 0

    for source_candidate in source["candidates"]:
        candidate = dict(source_candidate)
        reasons: list[str] = []
        statuses = {row.get("firecrawl_search_id_status") for row in candidate["occurrences"]}
        if statuses == {"NOT_RETURNED_FAIL_CLOSED"}:
            reasons.append("firecrawl_search_id_missing")
        elif statuses != {"RETURNED_CONSISTENT"}:
            reasons.append("firecrawl_lineage_conflict_or_incomplete")
        if capture_age_hours < -FUTURE_TOLERANCE_SECONDS / 3600.0:
            reasons.append("firecrawl_capture_timestamp_future")
        elif capture_age_hours > MAX_CAPTURE_AGE_HOURS:
            reasons.append("firecrawl_capture_stale")

        publication, publication_reasons, has_exact_timestamp, fresh_timestamp = _publication_gate(
            thread_evidence.get(candidate["canonical_url"], []),
            candidate,
            as_of,
            source_capture_sha256=source["capture_sha256"],
        )
        reasons.extend(publication_reasons)
        exact_timestamp_count += int(has_exact_timestamp)
        fresh_timestamp_count += int(fresh_timestamp)

        rules, rule_reasons, _has_authoritative = _rule_gate(
            rule_evidence.get(candidate["subreddit"], []),
            candidate["subreddit"],
            as_of,
            source_capture_sha256=source["capture_sha256"],
            discovery_rows=rule_discovery.get(candidate["subreddit"], []),
        )
        reasons.extend(rule_reasons)
        account: dict[str, Any] | None = None

        if not reasons:
            qualified_count += 1
            reasons.extend(_candidate_content_reasons(candidate))
            account, account_reasons = _account_gate(
                evidence,
                candidate["subreddit"],
                as_of,
                source_capture_sha256=source["capture_sha256"],
                discovery_rows=source["account_query_rows"],
            )
            reasons.extend(account_reasons)

        candidate["publication_evidence"] = publication
        candidate["rule_evidence"] = rules
        candidate["account_evidence"] = account
        candidate["rejection_reasons"] = sorted(set(reasons))
        candidate["pre_state_eligible"] = not candidate["rejection_reasons"]
        candidate["eligibility"] = (
            "ELIGIBLE_PENDING_DURABLE_STATE" if candidate["pre_state_eligible"] else "REJECTED_FAIL_CLOSED"
        )
        candidate["eligible"] = candidate["pre_state_eligible"]
        candidate["pack_id"] = None
        evaluated.append(candidate)

    policy = {
        "thread_max_age_hours": MAX_THREAD_AGE_HOURS,
        "publication_evidence_capture_max_age_hours": MAX_PUBLICATION_EVIDENCE_CAPTURE_AGE_HOURS,
        "rule_max_age_days": MAX_RULE_AGE_DAYS,
        "account_max_age_days": MAX_ACCOUNT_AGE_DAYS,
        "as_of_wall_clock_max_age_hours": MAX_AS_OF_WALL_CLOCK_AGE_HOURS,
        "daily_pack_cap": DAILY_PACK_CAP,
        "daily_timezone": "America/Los_Angeles",
        "cross_run_repeat": "PROHIBITED",
    }
    # Replay recomputes fractional freshness ages from this exact clock. Keep
    # subsecond precision here; historical whole-second clocks stay byte-identical.
    as_of_utc = as_of.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    run_id = derive_run_id(
        capture_sha256=source["capture_sha256"],
        evidence_sha256=evidence_sha256,
        as_of_utc=as_of_utc,
        policy=policy,
    )

    authoritative_rule_pages = 0
    for subreddit, rows in rule_evidence.items():
        _chosen, _reasons, has_authoritative = _rule_gate(
            rows,
            subreddit,
            as_of,
            source_capture_sha256=source["capture_sha256"],
            discovery_rows=rule_discovery.get(subreddit, []),
        )
        authoritative_rule_pages += int(has_authoritative)

    raw_count = len(source["raw_thread_rows"])
    invalid_count = len(source["raw_rejections"])
    unique_count = len(evaluated)
    duplicate_count = sum(int(row["duplicate_count"]) for row in source["duplicate_groups"])
    if raw_count != unique_count + duplicate_count + invalid_count:
        raise PackGateError("denominator_accounting_mismatch")

    packet = {
        "schema": "glp-reddit-firecrawl-discovery-packet-v3",
        "status": "PENDING_DURABLE_STATE",
        "run_id": run_id,
        "policy_version": POLICY_VERSION,
        "as_of_utc": as_of_utc,
        "pt_day": pt_day(as_of),
        "source_capture": {
            "path": source["capture_path"],
            "sha256": source["capture_sha256"],
            "captured_at_utc": source["capture_timestamp_utc"],
            "age_hours_at_run": round(capture_age_hours, 6),
            "provider": "firecrawl",
        },
        "qualification_evidence": {
            "path": evidence_path,
            "sha256": evidence_sha256,
            "status": "BOUND" if evidence_sha256 else "ABSENT_FAIL_CLOSED",
        },
        "policy": policy,
        "query_lineage": source["lineage"],
        "denominators": {
            "thread_query_groups": sum(row["purpose"] == "thread_discovery" for row in source["lineage"]),
            "thread_raw_hits": raw_count,
            "thread_canonical_unique": unique_count,
            "thread_duplicates_removed": duplicate_count,
            "thread_malformed_rejected": invalid_count,
            "rule_query_raw_hits": len(source["rule_query_rows"]),
            "authoritative_rule_pages": authoritative_rule_pages,
            "exact_publication_timestamps": exact_timestamp_count,
            "fresh_publication_timestamps": fresh_timestamp_count,
            "qualified_threads": qualified_count,
            "evidence_eligible_threads": sum(row["pre_state_eligible"] for row in evaluated),
            "eligible_threads": sum(row["pre_state_eligible"] for row in evaluated),
            "rejected_unique_threads": unique_count - sum(row["pre_state_eligible"] for row in evaluated),
            "manual_review_packs_created": 0,
            "daily_pack_cap": DAILY_PACK_CAP,
            "daily_packs_preexisting": 0,
            "daily_packs_after_run": 0,
            "rejection_reason_counts": {},
        },
        "duplicate_groups": source["duplicate_groups"],
        "raw_rejections": source["raw_rejections"],
        "raw_thread_rows": source["raw_thread_rows"],
        "rule_query_rows": source["rule_query_rows"],
        "account_query_rows": source["account_query_rows"],
        "candidate_dispositions": evaluated,
        "manual_review_packs": [],
        "remediation": None,
        "capabilities": {
            "local_saved_firecrawl_bytes_read": True,
            "live_network_transport": False,
            "reddit_read_transport": False,
            "reddit_write_transport": False,
            "reddit_credentials": False,
            "reddit_login": False,
            "browser_automation": False,
            "discord_direct_transport": False,
            "cron_mutation_transport": False,
            "operator_manual_post_only": True,
        },
        "effects_this_run": dict(ZERO_EFFECTS),
    }
    _refresh_packet_summary(packet)
    return packet


def finalize_packet_for_durable_state(
    packet: dict[str, Any],
    *,
    seen_urls: set[str],
    daily_packs_preexisting: int,
    capture_sha256: str,
    evidence_sha256: str | None,
) -> None:
    if daily_packs_preexisting < 0 or daily_packs_preexisting > DAILY_PACK_CAP:
        raise PackGateError("daily_pack_state_invalid")
    available = DAILY_PACK_CAP - daily_packs_preexisting
    manual_packs: list[dict[str, Any]] = []
    for candidate in packet["candidate_dispositions"]:
        if not candidate["pre_state_eligible"]:
            continue
        if candidate["canonical_url"] in seen_urls:
            candidate["rejection_reasons"] = ["cross_run_opportunity_already_queued"]
            candidate["eligibility"] = "REJECTED_FAIL_CLOSED"
            candidate["eligible"] = False
            continue
        if available <= 0:
            candidate["rejection_reasons"] = ["pt_daily_pack_cap_reached"]
            candidate["eligibility"] = "REJECTED_FAIL_CLOSED"
            candidate["eligible"] = False
            continue
        pack = _build_manual_pack(
            candidate,
            run_id=packet["run_id"],
            capture_sha256=capture_sha256,
            evidence_sha256=evidence_sha256,
            as_of_utc=packet["as_of_utc"],
        )
        candidate["pack_id"] = pack["pack_id"]
        candidate["eligibility"] = "ELIGIBLE_MANUAL_REVIEW"
        candidate["eligible"] = True
        manual_packs.append(pack)
        seen_urls.add(candidate["canonical_url"])
        available -= 1
    packet["manual_review_packs"] = manual_packs
    packet["denominators"]["daily_packs_preexisting"] = daily_packs_preexisting
    packet["denominators"]["daily_packs_after_run"] = daily_packs_preexisting + len(manual_packs)
    _refresh_packet_summary(packet)


def derive_output_cursor(
    input_cursor: str,
    run_id: str,
    candidate_state_sha256: str,
    pack_ids: list[str],
) -> str:
    if not re.fullmatch(r"(?:ROOT|cursor_[0-9a-f]{24})", input_cursor):
        raise PackGateError("queue_cursor_invalid")
    subject = {
        "input_cursor": input_cursor,
        "run_id": run_id,
        "candidate_state_sha256": candidate_state_sha256,
        "pack_ids": pack_ids,
    }
    return "cursor_" + sha256_bytes(canonical_bytes(subject))[:24]


def build_ops_alert_pack(
    packet: dict[str, Any],
    normalized_sha256: str,
    *,
    queue_cursor: str,
    output_cursor: str,
    candidate_state_sha256: str,
) -> dict[str, Any]:
    packs = packet["manual_review_packs"]
    expected_cursor = derive_output_cursor(
        queue_cursor,
        packet["run_id"],
        candidate_state_sha256,
        [row["pack_id"] for row in packs],
    )
    if output_cursor != expected_cursor:
        raise PackGateError("queue_output_cursor_invalid")
    d = packet["denominators"]
    content_lines = [
        (
            f"GLP Reddit | {packet['status']} | PT {d['daily_packs_after_run']}/{DAILY_PACK_CAP} | "
            f"run packs {len(packs)} | {d['thread_raw_hits']} raw/{d['thread_canonical_unique']} unique/"
            f"{d['thread_duplicates_removed']} dup/{d['eligible_threads']} eligible"
        ),
        (
            f"Rules {d['authoritative_rule_pages']}/{d['rule_query_raw_hits']}; "
            f"exact timestamps {d['exact_publication_timestamps']}."
        ),
    ]
    if packet.get("remediation"):
        content_lines.append("BLOCKER: " + packet["remediation"])
    for index, pack in enumerate(packs, start=1):
        thread = pack["thread"]
        publication = thread["publication_evidence"]
        rules = pack["rule_capture"]
        account = next(
            row["account_evidence"]
            for row in packet["candidate_dispositions"]
            if row.get("pack_id") == pack["pack_id"]
        )
        username = account["identity"]["username"]
        title = clean_untrusted_text(thread["title"], maximum=96)
        content_lines.extend(
            [
                f"PACK {index} | {title}",
                thread["canonical_url"],
                (
                    f"PASS: evidence/rule/account/timestamp | u/{username} | "
                    f"published {publication['published_at_utc']}"
                ),
                "DRAFT: " + pack["help_first_draft"],
            ]
        )
    content_lines.extend(
        [
            "ZERO AUTOMATED REDDIT WRITES: posts/comments/votes/DMs/account ops/logins all 0.",
            "OPERATOR POSTS MANUALLY. Scheduler-only; local Discord calls 0.",
        ]
    )
    content = "\n".join(content_lines)
    if len(content) > 1900:
        raise PackGateError("ops_alert_content_overflow")
    dispositions = [
        {
            "canonical_url": row["canonical_url"],
            "eligibility": row["eligibility"],
            "rejection_reasons": row["rejection_reasons"],
            "pack_id": row["pack_id"],
        }
        for row in packet["candidate_dispositions"]
    ]
    return {
        "schema": "glp-reddit-discord-ops-alert-pack-v3",
        "status": packet["status"],
        "run_id": packet["run_id"],
        "delivery": {
            "mode": "SCHEDULER_ONLY_NOT_SENT_LOCALLY",
            "deliver": OPS_ALERTS_DELIVER,
            "channel_id": OPS_ALERTS_CHANNEL_ID,
            "direct_discord_transport_invoked": False,
            "exact_scheduler_readback_required_after_parent_activation": True,
            "readback": None,
        },
        "capacity": {
            "daily_pack_cap": DAILY_PACK_CAP,
            "pt_day": packet["pt_day"],
            "daily_packs_preexisting": d["daily_packs_preexisting"],
            "daily_packs_after_run": d["daily_packs_after_run"],
            "eligible_threads": d["eligible_threads"],
            "manual_review_packs": len(packs),
            "remaining_capacity": DAILY_PACK_CAP - d["daily_packs_after_run"],
        },
        "denominators": d,
        "hashes": {
            "firecrawl_capture_sha256": packet["source_capture"]["sha256"],
            "qualification_evidence_sha256": packet["qualification_evidence"]["sha256"],
            "normalized_packet_sha256": normalized_sha256,
            "candidate_dispositions_sha256": sha256_bytes(canonical_bytes(dispositions)),
            "candidate_state_sha256": candidate_state_sha256,
        },
        "candidate_dispositions": dispositions,
        "queue": {
            "input_cursor": queue_cursor,
            "output_cursor": output_cursor,
            "cursor_advanced": True,
            "queued_count": len(packs),
            "queued_pack_ids": [row["pack_id"] for row in packs],
            "idempotency_key": f"glp-reddit-ops-alert:{packet['run_id']}",
        },
        "manual_review_packs": packs,
        "remediation": packet.get("remediation"),
        "effects": dict(ZERO_EFFECTS),
        "automated_reddit_writes": {
            "posts": 0,
            "comments": 0,
            "votes": 0,
            "direct_messages": 0,
            "account_operations": 0,
            "logins": 0,
            "browser_actions": 0,
        },
        "content": content,
    }


def build_run_receipt(
    packet: dict[str, Any],
    ops_alert: dict[str, Any],
    *,
    capture_path: Path,
    capture_sha256: str,
    evidence_path: str | None,
    evidence_sha256: str | None,
    artifact_paths: tuple[Path, Path, Path],
    normalized_sha256: str,
    ops_alert_sha256: str,
    db_path: Path,
    sequence: int,
    predecessor_run_id: str | None,
    candidate_state_sha256: str,
    queue_state_sha256: str,
) -> dict[str, Any]:
    """Build the exact self-hash-excluding durable receipt for one run."""
    candidates = packet["candidate_dispositions"]
    packs = packet["manual_review_packs"]
    return {
        "schema": "glp-reddit-firecrawl-discovery-run-receipt-v3",
        "status": packet["status"],
        "run_id": packet["run_id"],
        "as_of_utc": packet["as_of_utc"],
        "pt_day": packet["pt_day"],
        "sources": {
            "firecrawl_capture": {"path": str(capture_path), "sha256": capture_sha256},
            "qualification_evidence": {
                "path": evidence_path,
                "sha256": evidence_sha256,
            },
        },
        "artifacts": {
            "normalized_packet": {"path": str(artifact_paths[0]), "sha256": normalized_sha256},
            "discord_ops_alert_pack": {"path": str(artifact_paths[1]), "sha256": ops_alert_sha256},
            "run_receipt": {"path": str(artifact_paths[2]), "self_hash_excluded": True},
        },
        "denominators": packet["denominators"],
        "queue": ops_alert["queue"],
        "intended_scheduler_delivery": {
            "deliver": OPS_ALERTS_DELIVER,
            "channel_id": OPS_ALERTS_CHANNEL_ID,
            "local_send_executed": False,
            "exact_readback": None,
            "operational_pass_claimed": False,
        },
        "ledger": {
            "path": str(db_path),
            "schema_version": SCHEMA_VERSION,
            "run_sequence": sequence,
            "predecessor_run_id": predecessor_run_id,
            "candidate_rows_for_run": len(candidates),
            "candidate_state_sha256": candidate_state_sha256,
            "queue_rows_for_run": len(packs),
            "queue_state_sha256": queue_state_sha256,
            "cursor_before_run": ops_alert["queue"]["input_cursor"],
            "cursor_after_run": ops_alert["queue"]["output_cursor"],
            "integrity_check": "ok",
            "serial_transaction": "BEGIN IMMEDIATE",
        },
        "capabilities": packet["capabilities"],
        "effects": dict(ZERO_EFFECTS),
        "ops_alert_message": ops_alert["content"],
    }


class DiscoveryLedger:
    """Serial, append-only v3 ledger for one linear durable cursor chain."""

    SCHEMA_SQL = """
CREATE TABLE reddit_discovery_metadata (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE reddit_discovery_runs (
  run_id TEXT PRIMARY KEY,
  sequence INTEGER NOT NULL UNIQUE CHECK(sequence > 0),
  predecessor_run_id TEXT REFERENCES reddit_discovery_runs(run_id),
  as_of_utc TEXT NOT NULL,
  pt_day TEXT NOT NULL,
  status TEXT NOT NULL,
  capture_path TEXT NOT NULL,
  capture_sha256 TEXT NOT NULL,
  evidence_path TEXT,
  evidence_sha256 TEXT,
  normalized_path TEXT NOT NULL UNIQUE,
  normalized_sha256 TEXT NOT NULL,
  ops_alert_path TEXT NOT NULL UNIQUE,
  ops_alert_sha256 TEXT NOT NULL,
  receipt_path TEXT NOT NULL UNIQUE,
  receipt_sha256 TEXT NOT NULL,
  queue_input_cursor TEXT NOT NULL,
  queue_output_cursor TEXT NOT NULL UNIQUE,
  candidate_count INTEGER NOT NULL CHECK(candidate_count >= 0),
  candidate_state_sha256 TEXT NOT NULL,
  queue_count INTEGER NOT NULL CHECK(queue_count >= 0),
  queue_state_sha256 TEXT NOT NULL
);
CREATE TABLE reddit_candidate_dispositions (
  run_id TEXT NOT NULL REFERENCES reddit_discovery_runs(run_id) ON DELETE RESTRICT,
  canonical_url TEXT NOT NULL,
  eligibility TEXT NOT NULL,
  reasons_json TEXT NOT NULL,
  pack_id TEXT,
  candidate_json TEXT NOT NULL,
  candidate_sha256 TEXT NOT NULL,
  PRIMARY KEY (run_id, canonical_url)
);
CREATE TABLE reddit_manual_pack_queue (
  pack_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES reddit_discovery_runs(run_id) ON DELETE RESTRICT,
  pt_day TEXT NOT NULL,
  ordinal INTEGER NOT NULL CHECK(ordinal > 0),
  canonical_url TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK(status='HELD_MANUAL_OPERATOR_REVIEW'),
  payload_json TEXT NOT NULL,
  payload_sha256 TEXT NOT NULL,
  UNIQUE(run_id, ordinal)
);
CREATE INDEX reddit_manual_pack_queue_pt_day_idx ON reddit_manual_pack_queue(pt_day);
CREATE TABLE reddit_discovery_effect_counters (
  name TEXT PRIMARY KEY,
  value INTEGER NOT NULL CHECK(value >= 0)
);
""".strip()
    # This is the only legacy shape eligible for the deliberately narrow empty-v2
    # migration.  It is exact so a drifted legacy database cannot be normalized by
    # migration before the drift is observed.
    V2_SCHEMA_SQL = """
CREATE TABLE reddit_discovery_metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE reddit_discovery_runs(run_id TEXT PRIMARY KEY);
CREATE TABLE reddit_candidate_dispositions(run_id TEXT,canonical_url TEXT);
CREATE TABLE reddit_manual_pack_queue(pack_id TEXT,run_id TEXT);
CREATE TABLE reddit_discovery_effect_counters(name TEXT PRIMARY KEY,value INTEGER);
""".strip()
    # Updated only when SCHEMA_SQL intentionally changes.  Runtime recomputes the
    # complete sqlite_master/PRAGMA record and compares this exact digest before
    # replay or any append.
    SCHEMA_FINGERPRINT_SHA256 = "e6a3162efaa5eae62b0dc7adfc0d7742070f102ec67d22827d94a9e46f5f3f3d"
    V2_SCHEMA_FINGERPRINT_SHA256 = "ab43c567b38c201dcf89d389a35beef0c20113943b5379e1ccf8c302a2f85b96"
    EMPTY_V2_MIGRATION = (
        "Only an exact canonical v2 ledger with zero runs, zero candidate rows, zero queue rows, "
        "the exact metadata key set, and the exact all-zero effect-counter set may migrate in place. "
        "The five v2 tables are transactionally dropped and recreated from SCHEMA_SQL as v3. Any "
        "non-empty or drifted v2 ledger fails closed because v2 did not retain full candidate JSON, "
        "PT-day capacity, sequence, or complete replay state."
    )

    def __init__(self, path: Path):
        self.path = Path(path)
        self.conn: sqlite3.Connection
        self._path_existed_before_open = False

    def __enter__(self) -> "DiscoveryLedger":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._path_existed_before_open = self.path.exists()
        if self._path_existed_before_open and (self.path.is_symlink() or not self.path.is_file()):
            raise PackGateError("state_db_not_regular")
        try:
            self.conn = sqlite3.connect(self.path, timeout=30)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.execute("PRAGMA busy_timeout=30000")
            self._initialize()
            check = self.conn.execute("PRAGMA integrity_check").fetchone()[0]
        except PackGateError:
            with contextlib.suppress(Exception):
                self.conn.close()
            raise
        except sqlite3.DatabaseError as exc:
            with contextlib.suppress(Exception):
                self.conn.close()
            raise PackGateError("state_db_corrupt_or_invalid") from exc
        if check != "ok":
            self.conn.close()
            raise PackGateError("state_db_integrity_check_failed")
        self.path.chmod(0o600)
        self._assert_schema_fingerprint(
            self.SCHEMA_FINGERPRINT_SHA256, "state_db_v3_schema_fingerprint_mismatch"
        )
        self._assert_zero_effects()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self.conn.in_transaction:
            self.conn.rollback()
        self.conn.close()
        self.path.chmod(0o600)

    def _table_names(self) -> set[str]:
        return {
            str(row[0])
            for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }

    @staticmethod
    def _schema_record(conn: sqlite3.Connection) -> dict[str, Any]:
        """Return an exact, root-page-independent SQLite schema record.

        sqlite_master SQL binds declared constraints and explicit objects.  The
        PRAGMA projections additionally bind every column, foreign key, implicit
        and explicit index, collation/order bit, trigger/view absence or presence,
        and user_version.
        """

        object_rows = conn.execute(
            """SELECT type,name,tbl_name,sql FROM sqlite_master
               WHERE type IN ('table','index','trigger','view')
               ORDER BY type,name"""
        ).fetchall()
        objects = [
            {
                "type": str(row[0]),
                "name": str(row[1]),
                "table": str(row[2]),
                "sql": row[3] if row[3] is None else str(row[3]),
            }
            for row in object_rows
        ]
        table_names = sorted(
            str(row[1])
            for row in object_rows
            if str(row[0]) == "table" and not str(row[1]).startswith("sqlite_")
        )
        tables: dict[str, Any] = {}
        for table_name in table_names:
            columns = [
                {
                    "cid": int(row[0]),
                    "name": str(row[1]),
                    "type": str(row[2]),
                    "notnull": int(row[3]),
                    "default": row[4] if row[4] is None else str(row[4]),
                    "pk": int(row[5]),
                    "hidden": int(row[6]),
                }
                for row in conn.execute(
                    'SELECT cid,name,type,"notnull",dflt_value,pk,hidden '
                    "FROM pragma_table_xinfo(?) ORDER BY cid",
                    (table_name,),
                )
            ]
            foreign_keys = [
                {
                    "id": int(row[0]),
                    "seq": int(row[1]),
                    "table": str(row[2]),
                    "from": str(row[3]),
                    "to": None if row[4] is None else str(row[4]),
                    "on_update": str(row[5]),
                    "on_delete": str(row[6]),
                    "match": str(row[7]),
                }
                for row in conn.execute(
                    'SELECT id,seq,"table","from","to",on_update,on_delete,match '
                    "FROM pragma_foreign_key_list(?) ORDER BY id,seq",
                    (table_name,),
                )
            ]
            indexes: list[dict[str, Any]] = []
            index_rows = conn.execute(
                'SELECT name,"unique",origin,partial FROM pragma_index_list(?) ORDER BY name',
                (table_name,),
            ).fetchall()
            for index_row in index_rows:
                index_name = str(index_row[0])
                indexes.append(
                    {
                        "name": index_name,
                        "unique": int(index_row[1]),
                        "origin": str(index_row[2]),
                        "partial": int(index_row[3]),
                        "columns": [
                            {
                                "seqno": int(row[0]),
                                "cid": int(row[1]),
                                "name": None if row[2] is None else str(row[2]),
                                "desc": int(row[3]),
                                "coll": None if row[4] is None else str(row[4]),
                                "key": int(row[5]),
                            }
                            for row in conn.execute(
                                "SELECT seqno,cid,name,desc,coll,key "
                                "FROM pragma_index_xinfo(?) ORDER BY seqno",
                                (index_name,),
                            )
                        ],
                    }
                )
            tables[table_name] = {
                "columns": columns,
                "foreign_keys": foreign_keys,
                "indexes": indexes,
            }
        return {
            "user_version": int(conn.execute("PRAGMA user_version").fetchone()[0]),
            "objects": objects,
            "tables": tables,
        }

    @classmethod
    def _schema_fingerprint(cls, conn: sqlite3.Connection) -> str:
        return sha256_bytes(canonical_bytes(cls._schema_record(conn)))

    def _assert_schema_fingerprint(self, expected: str, code: str) -> None:
        if self._schema_fingerprint(self.conn) != expected:
            raise PackGateError(code)

    def _execute_schema(self, schema_sql: str) -> None:
        for statement in schema_sql.split(";"):
            if statement.strip():
                self.conn.execute(statement)

    def _seed_v3_singletons(self) -> None:
        self.conn.executemany(
            "INSERT INTO reddit_discovery_metadata(key,value) VALUES(?,?)",
            (
                ("schema_version", SCHEMA_VERSION),
                ("current_cursor", "ROOT"),
                ("last_run_id", ""),
            ),
        )
        self.conn.executemany(
            "INSERT INTO reddit_discovery_effect_counters(name,value) VALUES(?,0)",
            ((name,) for name in ZERO_EFFECTS),
        )

    def _initialize(self) -> None:
        tables = self._table_names()
        known = {
            "reddit_discovery_metadata",
            "reddit_discovery_runs",
            "reddit_candidate_dispositions",
            "reddit_manual_pack_queue",
            "reddit_discovery_effect_counters",
        }
        if not tables:
            if self._path_existed_before_open:
                raise PackGateError("state_db_existing_empty_invalid")
            self.conn.execute("BEGIN IMMEDIATE")
            self._execute_schema(self.SCHEMA_SQL)
            self._seed_v3_singletons()
            self._assert_schema_fingerprint(
                self.SCHEMA_FINGERPRINT_SHA256, "state_db_v3_schema_fingerprint_mismatch"
            )
            self._assert_zero_effects()
            self.conn.commit()
            return
        if "reddit_discovery_metadata" not in tables:
            raise PackGateError("state_db_unversioned_tables_invalid")
        try:
            row = self.conn.execute(
                "SELECT value FROM reddit_discovery_metadata WHERE key='schema_version'"
            ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise PackGateError("state_db_schema_fingerprint_invalid") from exc
        version = str(row[0]) if row else None
        if version == SCHEMA_VERSION:
            # Existing v3 is a verification path, never a repair path.  Validate
            # schema, singleton/effect rows, artifacts, and the complete cursor
            # chain before any INSERT, migration, or COMMIT can occur.
            self._assert_schema_fingerprint(
                self.SCHEMA_FINGERPRINT_SHA256, "state_db_v3_schema_fingerprint_mismatch"
            )
            self.validate_all_state()
            return
        if version != PREVIOUS_SCHEMA_VERSION:
            raise PackGateError("state_db_schema_version_invalid")
        if tables != known:
            raise PackGateError("state_db_v2_table_set_invalid")
        self._assert_schema_fingerprint(
            self.V2_SCHEMA_FINGERPRINT_SHA256, "state_db_v2_schema_fingerprint_mismatch"
        )
        metadata_rows = {
            str(item[0]): str(item[1])
            for item in self.conn.execute("SELECT key,value FROM reddit_discovery_metadata")
        }
        if metadata_rows != {"schema_version": PREVIOUS_SCHEMA_VERSION}:
            raise PackGateError("state_db_v2_metadata_invalid")
        self._assert_zero_effects()
        counts = [
            int(self.conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])
            for name in (
                "reddit_discovery_runs",
                "reddit_candidate_dispositions",
                "reddit_manual_pack_queue",
            )
        ]
        if any(counts):
            raise PackGateError("state_db_v2_nonempty_migration_requires_verified_offline_rebuild")
        self.conn.execute("BEGIN IMMEDIATE")
        for name in (
            "reddit_discovery_effect_counters",
            "reddit_manual_pack_queue",
            "reddit_candidate_dispositions",
            "reddit_discovery_runs",
            "reddit_discovery_metadata",
        ):
            self.conn.execute(f"DROP TABLE {name}")
        self._execute_schema(self.SCHEMA_SQL)
        self._seed_v3_singletons()
        self._assert_schema_fingerprint(
            self.SCHEMA_FINGERPRINT_SHA256, "state_db_v3_schema_fingerprint_mismatch"
        )
        self._assert_zero_effects()
        self.conn.commit()

    def _assert_zero_effects(self) -> None:
        rows = {
            str(row[0]): row[1]
            for row in self.conn.execute("SELECT name,value FROM reddit_discovery_effect_counters")
        }
        if set(rows) != set(ZERO_EFFECTS):
            raise PackGateError("effect_counter_set_invalid")
        for name, value in rows.items():
            if isinstance(value, bool) or not isinstance(value, int) or value != 0:
                raise PackGateError(f"nonzero_effect_counter:{name}")

    def metadata(self, key: str) -> str:
        row = self.conn.execute(
            "SELECT value FROM reddit_discovery_metadata WHERE key=?", (key,)
        ).fetchone()
        if row is None:
            raise PackGateError(f"state_db_metadata_missing:{key}")
        return str(row[0])

    def existing(self, run_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM reddit_discovery_runs WHERE run_id=?", (run_id,)
        ).fetchone()

    @staticmethod
    def _read_exact_artifact(path_text: str, expected_sha256: str, code: str) -> dict[str, Any]:
        path = Path(path_text)
        if path.is_symlink() or not path.is_file():
            raise PackGateError(f"{code}_missing")
        raw = path.read_bytes()
        if sha256_bytes(raw) != expected_sha256:
            raise PackGateError(f"{code}_sha256_mismatch")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PackGateError(f"{code}_json_invalid") from exc
        if not isinstance(value, dict):
            raise PackGateError(f"{code}_not_object")
        return value

    def validate_all_state(self) -> None:
        """Rebuild and validate the complete durable chain from its bound source bytes."""
        self._assert_schema_fingerprint(
            self.SCHEMA_FINGERPRINT_SHA256, "state_db_v3_schema_fingerprint_mismatch"
        )
        self._assert_zero_effects()
        check = self.conn.execute("PRAGMA integrity_check").fetchone()[0]
        if check != "ok":
            raise PackGateError("state_db_integrity_check_failed")
        fk = self.conn.execute("PRAGMA foreign_key_check").fetchall()
        if fk:
            raise PackGateError("state_db_foreign_key_check_failed")
        metadata_rows = {
            str(row[0]): str(row[1])
            for row in self.conn.execute("SELECT key,value FROM reddit_discovery_metadata")
        }
        if set(metadata_rows) != {"schema_version", "current_cursor", "last_run_id"}:
            raise PackGateError("state_db_metadata_key_set_invalid")
        if metadata_rows["schema_version"] != SCHEMA_VERSION:
            raise PackGateError("state_db_schema_version_invalid")
        runs = self.conn.execute(
            "SELECT * FROM reddit_discovery_runs ORDER BY sequence"
        ).fetchall()
        expected_cursor = "ROOT"
        expected_predecessor: str | None = None
        artifact_paths: set[str] = set()
        expected_seen_urls: set[str] = set()
        expected_daily_counts: dict[str, int] = {}
        for sequence, row in enumerate(runs, start=1):
            if int(row["sequence"]) != sequence:
                raise PackGateError("state_db_run_sequence_not_contiguous")
            if row["predecessor_run_id"] != expected_predecessor:
                raise PackGateError("state_db_cursor_predecessor_conflict")
            if row["queue_input_cursor"] != expected_cursor:
                raise PackGateError("state_db_cursor_chain_branched")
            run_id = str(row["run_id"])
            current_artifact_paths = (
                Path(str(row["normalized_path"])),
                Path(str(row["ops_alert_path"])),
                Path(str(row["receipt_path"])),
            )
            for path in current_artifact_paths:
                path_text = str(path)
                if path_text in artifact_paths:
                    raise PackGateError("state_db_artifact_path_reused")
                artifact_paths.add(path_text)
                if run_id not in path.parts and run_id not in path.name:
                    raise PackGateError("state_db_artifact_path_not_run_qualified")
            normalized = self._read_exact_artifact(
                row["normalized_path"], row["normalized_sha256"], "replay_normalized_artifact"
            )
            ops_alert = self._read_exact_artifact(
                row["ops_alert_path"], row["ops_alert_sha256"], "replay_ops_alert_artifact"
            )
            receipt = self._read_exact_artifact(
                row["receipt_path"], row["receipt_sha256"], "replay_receipt_artifact"
            )

            capture, _capture_raw, capture_sha = read_bound_json(
                Path(str(row["capture_path"])), str(row["capture_sha256"]), "state_capture"
            )
            source = normalize_firecrawl_capture(
                capture, capture_sha, Path(str(row["capture_path"]))
            )
            evidence_path = Path(str(row["evidence_path"])) if row["evidence_path"] is not None else None
            evidence, evidence_path_text, evidence_sha = load_evidence(
                evidence_path,
                str(row["evidence_sha256"]) if row["evidence_sha256"] is not None else None,
            )
            as_of = parse_exact_timestamp(str(row["as_of_utc"]), "state_db_as_of_invalid")
            derived_pt_day = pt_day(as_of)
            expected_packet = qualify_capture(
                source,
                evidence,
                as_of=as_of,
                evidence_path=evidence_path_text,
                evidence_sha256=evidence_sha,
            )
            expected_preexisting = expected_daily_counts.get(derived_pt_day, 0)
            finalize_packet_for_durable_state(
                expected_packet,
                seen_urls=expected_seen_urls,
                daily_packs_preexisting=expected_preexisting,
                capture_sha256=capture_sha,
                evidence_sha256=evidence_sha,
            )
            if normalized != expected_packet:
                raise PackGateError("state_db_normalized_packet_not_source_derived")
            expected_normalized_sha = sha256_bytes(pretty_bytes(expected_packet))

            candidates = normalized.get("candidate_dispositions")
            packs = normalized.get("manual_review_packs")
            if not isinstance(candidates, list) or not isinstance(packs, list):
                raise PackGateError("state_db_normalized_state_incomplete")
            candidate_state_sha = sha256_bytes(canonical_bytes(candidates))
            queue_state_sha = sha256_bytes(canonical_bytes(packs))
            expected_output = derive_output_cursor(
                expected_cursor,
                run_id,
                candidate_state_sha,
                [str(pack["pack_id"]) for pack in packs],
            )
            expected_ops_alert = build_ops_alert_pack(
                expected_packet,
                expected_normalized_sha,
                queue_cursor=expected_cursor,
                output_cursor=expected_output,
                candidate_state_sha256=candidate_state_sha,
            )
            if ops_alert != expected_ops_alert:
                raise PackGateError("state_db_ops_alert_not_packet_derived")
            expected_ops_sha = sha256_bytes(pretty_bytes(expected_ops_alert))
            expected_receipt = build_run_receipt(
                expected_packet,
                expected_ops_alert,
                capture_path=Path(str(row["capture_path"])),
                capture_sha256=capture_sha,
                evidence_path=evidence_path_text,
                evidence_sha256=evidence_sha,
                artifact_paths=current_artifact_paths,
                normalized_sha256=expected_normalized_sha,
                ops_alert_sha256=expected_ops_sha,
                db_path=self.path,
                sequence=sequence,
                predecessor_run_id=expected_predecessor,
                candidate_state_sha256=candidate_state_sha,
                queue_state_sha256=queue_state_sha,
            )
            if receipt != expected_receipt:
                raise PackGateError("state_db_receipt_not_state_derived")
            expected_receipt_sha = sha256_bytes(pretty_bytes(expected_receipt))
            expected_run_fields: dict[str, Any] = {
                "run_id": expected_packet["run_id"],
                "sequence": sequence,
                "predecessor_run_id": expected_predecessor,
                "as_of_utc": expected_packet["as_of_utc"],
                "pt_day": derived_pt_day,
                "status": expected_packet["status"],
                "capture_path": expected_packet["source_capture"]["path"],
                "capture_sha256": expected_packet["source_capture"]["sha256"],
                "evidence_path": expected_packet["qualification_evidence"]["path"],
                "evidence_sha256": expected_packet["qualification_evidence"]["sha256"],
                "normalized_path": str(current_artifact_paths[0]),
                "normalized_sha256": expected_normalized_sha,
                "ops_alert_path": str(current_artifact_paths[1]),
                "ops_alert_sha256": expected_ops_sha,
                "receipt_path": str(current_artifact_paths[2]),
                "receipt_sha256": expected_receipt_sha,
                "queue_input_cursor": expected_cursor,
                "queue_output_cursor": expected_output,
                "candidate_count": len(candidates),
                "candidate_state_sha256": candidate_state_sha,
                "queue_count": len(packs),
                "queue_state_sha256": queue_state_sha,
            }
            for field, expected_value in expected_run_fields.items():
                if row[field] != expected_value:
                    raise PackGateError(f"state_db_run_field_mismatch:{field}")

            candidate_rows = self.conn.execute(
                "SELECT * FROM reddit_candidate_dispositions WHERE run_id=? ORDER BY canonical_url",
                (run_id,),
            ).fetchall()
            if len(candidate_rows) != len(candidates):
                raise PackGateError("state_db_candidate_rows_incomplete")
            expected_candidates = {
                str(candidate["canonical_url"]): candidate for candidate in candidates
            }
            if len(expected_candidates) != len(candidates):
                raise PackGateError("state_db_candidate_urls_duplicate")
            for candidate_row in candidate_rows:
                candidate = expected_candidates.get(str(candidate_row["canonical_url"]))
                if candidate is None:
                    raise PackGateError("state_db_candidate_row_unbound")
                payload = canonical_bytes(candidate).decode("utf-8")
                if candidate_row["candidate_json"] != payload:
                    raise PackGateError("state_db_candidate_payload_mismatch")
                if candidate_row["candidate_sha256"] != sha256_text(payload):
                    raise PackGateError("state_db_candidate_payload_sha256_mismatch")
                if candidate_row["eligibility"] != candidate.get("eligibility"):
                    raise PackGateError("state_db_candidate_eligibility_mismatch")
                if json.loads(candidate_row["reasons_json"]) != candidate.get("rejection_reasons"):
                    raise PackGateError("state_db_candidate_reasons_mismatch")
                if candidate_row["pack_id"] != candidate.get("pack_id"):
                    raise PackGateError("state_db_candidate_pack_id_mismatch")
            queue_rows = self.conn.execute(
                "SELECT * FROM reddit_manual_pack_queue WHERE run_id=? ORDER BY ordinal",
                (run_id,),
            ).fetchall()
            if len(queue_rows) != len(packs):
                raise PackGateError("state_db_queue_rows_incomplete")
            for ordinal, (queue_row, pack) in enumerate(zip(queue_rows, packs), start=1):
                payload = canonical_bytes(pack).decode("utf-8")
                if int(queue_row["ordinal"]) != ordinal:
                    raise PackGateError("state_db_queue_ordinal_mismatch")
                if queue_row["pack_id"] != pack.get("pack_id"):
                    raise PackGateError("state_db_queue_pack_id_mismatch")
                if queue_row["canonical_url"] != pack.get("thread", {}).get("canonical_url"):
                    raise PackGateError("state_db_queue_url_mismatch")
                if queue_row["pt_day"] != derived_pt_day:
                    raise PackGateError("state_db_queue_pt_day_mismatch")
                if queue_row["status"] != pack.get("status"):
                    raise PackGateError("state_db_queue_status_mismatch")
                if queue_row["payload_json"] != payload or queue_row["payload_sha256"] != sha256_text(payload):
                    raise PackGateError("state_db_queue_payload_mismatch")
            expected_daily_counts[derived_pt_day] = expected_preexisting + len(packs)
            expected_cursor = expected_output
            expected_predecessor = run_id
        current_cursor = metadata_rows["current_cursor"]
        last_run_id = metadata_rows["last_run_id"]
        if current_cursor != expected_cursor:
            raise PackGateError("state_db_current_cursor_mismatch")
        if last_run_id != (expected_predecessor or ""):
            raise PackGateError("state_db_last_run_id_mismatch")
        over_cap = self.conn.execute(
            "SELECT pt_day,COUNT(*) FROM reddit_manual_pack_queue GROUP BY pt_day HAVING COUNT(*) > ?",
            (DAILY_PACK_CAP,),
        ).fetchall()
        if over_cap:
            raise PackGateError("state_db_pt_daily_pack_cap_exceeded")
        duplicates = self.conn.execute(
            "SELECT canonical_url,COUNT(*) FROM reddit_manual_pack_queue GROUP BY canonical_url HAVING COUNT(*) > 1"
        ).fetchall()
        if duplicates:
            raise PackGateError("state_db_cross_run_repeat_detected")


def _unique_artifact_path(
    *,
    output_root: Path,
    run_id: str,
    basename: str,
    explicit: Path | None,
) -> Path:
    if explicit is None:
        return Path(output_root) / run_id / basename
    path = Path(explicit)
    if run_id in path.parts or run_id in path.name:
        return path
    suffix = "".join(path.suffixes)
    stem = path.name[: -len(suffix)] if suffix else path.name
    return path.with_name(f"{stem}.{run_id}{suffix}")


def _remove_new_artifacts(paths: Iterable[Path]) -> None:
    for path in paths:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        with contextlib.suppress(OSError):
            path.parent.rmdir()


def _verify_replay(
    ledger: DiscoveryLedger,
    row: sqlite3.Row,
    *,
    capture_path: Path,
    evidence_path: Path | None,
    queue_cursor: str,
    artifact_paths: tuple[Path, Path, Path],
) -> dict[str, Any]:
    if str(row["capture_path"]) != str(capture_path):
        raise PackGateError("replay_capture_path_mismatch")
    expected_evidence_path = str(evidence_path) if evidence_path else None
    if row["evidence_path"] != expected_evidence_path:
        raise PackGateError("replay_evidence_path_mismatch")
    if artifact_paths != (
        Path(str(row["normalized_path"])),
        Path(str(row["ops_alert_path"])),
        Path(str(row["receipt_path"])),
    ):
        raise PackGateError("replay_artifact_path_mismatch")
    if queue_cursor not in ("AUTO", row["queue_input_cursor"]):
        raise PackGateError("replay_queue_cursor_mismatch")
    ledger.validate_all_state()
    receipt = DiscoveryLedger._read_exact_artifact(
        str(row["receipt_path"]), str(row["receipt_sha256"]), "replay_receipt_artifact"
    )
    return {
        **receipt,
        "receipt_sha256": row["receipt_sha256"],
        "idempotent_replay": True,
    }


def run_discovery(
    *,
    capture_path: Path,
    expected_capture_sha256: str,
    as_of: str,
    evidence_path: Path | None = None,
    expected_evidence_sha256: str | None = None,
    db_path: Path = DEFAULT_DB_PATH,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    normalized_path: Path | None = None,
    ops_alert_path: Path | None = None,
    receipt_path: Path | None = None,
    queue_cursor: str = "AUTO",
) -> dict[str, Any]:
    as_of_dt = parse_exact_timestamp(as_of, "as_of_timestamp_invalid")
    _assert_as_of_near_wall_clock(as_of_dt, _trusted_now_utc())
    capture, _capture_raw, capture_sha = read_bound_json(
        capture_path, expected_capture_sha256, "capture"
    )
    source = normalize_firecrawl_capture(capture, capture_sha, capture_path)
    evidence, evidence_path_text, evidence_sha = load_evidence(
        evidence_path, expected_evidence_sha256
    )
    packet = qualify_capture(
        source,
        evidence,
        as_of=as_of_dt,
        evidence_path=evidence_path_text,
        evidence_sha256=evidence_sha,
    )
    artifact_paths = (
        _unique_artifact_path(
            output_root=output_root,
            run_id=packet["run_id"],
            basename="normalized-packet.json",
            explicit=normalized_path,
        ),
        _unique_artifact_path(
            output_root=output_root,
            run_id=packet["run_id"],
            basename="discord-ops-alert-pack.json",
            explicit=ops_alert_path,
        ),
        _unique_artifact_path(
            output_root=output_root,
            run_id=packet["run_id"],
            basename="run-receipt.json",
            explicit=receipt_path,
        ),
    )
    if len({str(path) for path in artifact_paths}) != 3:
        raise PackGateError("run_artifact_paths_not_unique")

    created_paths: list[Path] = []
    with DiscoveryLedger(db_path) as ledger:
        try:
            ledger.conn.execute("BEGIN IMMEDIATE")
            # Revalidate under the same write-reserving transaction that would
            # append a new run.  This closes the check/write race without ever
            # normalizing an existing v3 store.
            ledger.validate_all_state()
            existing = ledger.existing(packet["run_id"])
            if existing is not None:
                if existing["capture_sha256"] != capture_sha or existing["evidence_sha256"] != evidence_sha:
                    raise PackGateError("replay_source_hash_mismatch")
                if existing["as_of_utc"] != packet["as_of_utc"]:
                    raise PackGateError("replay_as_of_mismatch")
                result = _verify_replay(
                    ledger,
                    existing,
                    capture_path=capture_path,
                    evidence_path=evidence_path,
                    queue_cursor=queue_cursor,
                    artifact_paths=artifact_paths,
                )
                ledger.conn.commit()
                return result

            current_cursor = ledger.metadata("current_cursor")
            last_run_id = ledger.metadata("last_run_id")
            if queue_cursor == "AUTO":
                input_cursor = current_cursor
            elif queue_cursor == current_cursor:
                input_cursor = queue_cursor
            else:
                raise PackGateError("queue_cursor_not_current_linear_head")
            seen_urls = {
                str(row[0])
                for row in ledger.conn.execute("SELECT canonical_url FROM reddit_manual_pack_queue")
            }
            daily_packs = int(
                ledger.conn.execute(
                    "SELECT COUNT(*) FROM reddit_manual_pack_queue WHERE pt_day=?",
                    (packet["pt_day"],),
                ).fetchone()[0]
            )
            finalize_packet_for_durable_state(
                packet,
                seen_urls=seen_urls,
                daily_packs_preexisting=daily_packs,
                capture_sha256=capture_sha,
                evidence_sha256=evidence_sha,
            )
            candidates = packet["candidate_dispositions"]
            packs = packet["manual_review_packs"]
            candidate_state_sha = sha256_bytes(canonical_bytes(candidates))
            queue_state_sha = sha256_bytes(canonical_bytes(packs))
            output_cursor = derive_output_cursor(
                input_cursor,
                packet["run_id"],
                candidate_state_sha,
                [str(row["pack_id"]) for row in packs],
            )
            normalized_raw = pretty_bytes(packet)
            normalized_sha = sha256_bytes(normalized_raw)
            ops_alert = build_ops_alert_pack(
                packet,
                normalized_sha,
                queue_cursor=input_cursor,
                output_cursor=output_cursor,
                candidate_state_sha256=candidate_state_sha,
            )
            ops_raw = pretty_bytes(ops_alert)
            ops_sha = sha256_bytes(ops_raw)
            sequence = int(
                ledger.conn.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM reddit_discovery_runs").fetchone()[0]
            )
            receipt = build_run_receipt(
                packet,
                ops_alert,
                capture_path=capture_path,
                capture_sha256=capture_sha,
                evidence_path=evidence_path_text,
                evidence_sha256=evidence_sha,
                artifact_paths=artifact_paths,
                normalized_sha256=normalized_sha,
                ops_alert_sha256=ops_sha,
                db_path=db_path,
                sequence=sequence,
                predecessor_run_id=last_run_id or None,
                candidate_state_sha256=candidate_state_sha,
                queue_state_sha256=queue_state_sha,
            )
            receipt_raw = pretty_bytes(receipt)
            receipt_sha = sha256_bytes(receipt_raw)
            for path, raw in zip(artifact_paths, (normalized_raw, ops_raw, receipt_raw)):
                existed = path.exists()
                atomic_write(path, raw)
                if not existed:
                    created_paths.append(path)
            ledger.conn.execute(
                """INSERT INTO reddit_discovery_runs(
                     run_id,sequence,predecessor_run_id,as_of_utc,pt_day,status,
                     capture_path,capture_sha256,evidence_path,evidence_sha256,
                     normalized_path,normalized_sha256,ops_alert_path,ops_alert_sha256,
                     receipt_path,receipt_sha256,queue_input_cursor,queue_output_cursor,
                     candidate_count,candidate_state_sha256,queue_count,queue_state_sha256
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    packet["run_id"], sequence, last_run_id or None, packet["as_of_utc"],
                    packet["pt_day"], packet["status"], str(capture_path), capture_sha,
                    evidence_path_text, evidence_sha, str(artifact_paths[0]), normalized_sha,
                    str(artifact_paths[1]), ops_sha, str(artifact_paths[2]), receipt_sha,
                    input_cursor, output_cursor, len(candidates), candidate_state_sha,
                    len(packs), queue_state_sha,
                ),
            )
            ledger.conn.executemany(
                """INSERT INTO reddit_candidate_dispositions(
                     run_id,canonical_url,eligibility,reasons_json,pack_id,candidate_json,candidate_sha256
                   ) VALUES(?,?,?,?,?,?,?)""",
                [
                    (
                        packet["run_id"], candidate["canonical_url"], candidate["eligibility"],
                        canonical_bytes(candidate["rejection_reasons"]).decode("utf-8"),
                        candidate["pack_id"], canonical_bytes(candidate).decode("utf-8"),
                        sha256_bytes(canonical_bytes(candidate)),
                    )
                    for candidate in candidates
                ],
            )
            ledger.conn.executemany(
                """INSERT INTO reddit_manual_pack_queue(
                     pack_id,run_id,pt_day,ordinal,canonical_url,status,payload_json,payload_sha256
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                [
                    (
                        pack["pack_id"], packet["run_id"], packet["pt_day"], ordinal,
                        pack["thread"]["canonical_url"], pack["status"],
                        canonical_bytes(pack).decode("utf-8"), sha256_bytes(canonical_bytes(pack)),
                    )
                    for ordinal, pack in enumerate(packs, start=1)
                ],
            )
            ledger.conn.execute(
                "UPDATE reddit_discovery_metadata SET value=? WHERE key='current_cursor'",
                (output_cursor,),
            )
            ledger.conn.execute(
                "UPDATE reddit_discovery_metadata SET value=? WHERE key='last_run_id'",
                (packet["run_id"],),
            )
            ledger.validate_all_state()
            ledger.conn.commit()
        except PackGateError:
            ledger.conn.rollback()
            _remove_new_artifacts(created_paths)
            raise
        except sqlite3.DatabaseError as exc:
            ledger.conn.rollback()
            _remove_new_artifacts(created_paths)
            raise PackGateError("state_db_write_failed") from exc

    return {
        **receipt,
        "receipt_sha256": receipt_sha,
        "idempotent_replay": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", default="weekly", choices=("weekly",))
    parser.add_argument("--capture", type=Path, default=DEFAULT_CAPTURE_PATH)
    parser.add_argument("--capture-sha256", default=DEFAULT_CAPTURE_SHA256)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--evidence-sha256")
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--normalized-packet", type=Path)
    parser.add_argument("--ops-alert-pack", type=Path)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--queue-cursor", default="AUTO")
    args = parser.parse_args(argv)
    result = run_discovery(
        capture_path=args.capture,
        expected_capture_sha256=args.capture_sha256,
        as_of=args.as_of,
        evidence_path=args.evidence,
        expected_evidence_sha256=args.evidence_sha256,
        db_path=args.db,
        output_root=args.output_root,
        normalized_path=args.normalized_packet,
        ops_alert_path=args.ops_alert_pack,
        receipt_path=args.receipt,
        queue_cursor=args.queue_cursor,
    )
    print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PackGateError as exc:
        print(
            json.dumps(
                {"status": "BLOCKED_FAIL_CLOSED", "reason": str(exc), "effects": ZERO_EFFECTS},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        raise SystemExit(2)
