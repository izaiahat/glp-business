#!/usr/bin/env python3
"""Deterministic weekly digital PR control plane for the GLP Price Index.

The ordinary weekly command is provider free. It resolves the immutable active
content release, evaluates mechanical news gates, revalidates target evidence,
and records either local drafts or an explicit no news receipt. A separate
library/CLI send path stays fail closed behind an exact live Discord approval,
a transactional weekly quota claim, and Gmail Sent readback.
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
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from email.utils import parseaddr
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

BUSINESS = Path("/home/ubuntu/business")
if str(BUSINESS) not in sys.path:
    sys.path.insert(0, str(BUSINESS))
from scripts.glp_current_content_base_schema import validate_pointer_shape  # noqa: E402

CONTENT_BASE_POINTER = BUSINESS / "reports/glp-clinic-scout/seo-machine/current-content-base.json"
TARGETS_PATH = BUSINESS / "reports/glp-clinic-scout/growth/build-2-digital-pr-targets-20260808.json"
OPS_ROOT = BUSINESS / "ops/glp-digital-pr"
DB_PATH = OPS_ROOT / "glp-digital-pr.sqlite3"
REPORT_ROOT = BUSINESS / "reports/glp-clinic-scout/growth/build-2"
CURRENT_RECEIPT_PATH = REPORT_ROOT / "build-2-current-zero-send-20260808.json"
SEND_RECEIPT_ROOT = OPS_ROOT / "send-receipts"
PRICE_INDEX_URL = "https://glpclinicscout.com/glp-1-price-index/"
PRICE_INDEX_ROUTE = "/glp-1-price-index/"
SENDER = "izaiah@glpclinicscout.com"
MAILBOX_ID = "glp_izaiah_gmail"
DECISIONS_CHANNEL_ID = "1524254375453786294"
OPERATOR_ID = "392067741500309517"
LA = ZoneInfo("America/Los_Angeles")
MAX_TARGET_AGE_DAYS = 90
WEEKLY_SEND_CAP = 10
SCHEMA_VERSION = "glp-digital-pr-control-plane-v1"
GATE_VERSION = "glp-price-index-news-gate-v2"
TARGET_POLICY_VERSION = "glp-digital-pr-target-policy-v1"
APPROVAL_SCHEMA = "glp-digital-pr-pitch-approval-v1"
EXPLICIT_CONTACT_METHODS = {
    "explicit_first_party",
    "explicit_article_byline",
    "explicit_author_profile",
    "explicit_outlet_profile",
}
COUNTED_QUOTA_STATES = ("claimed", "submission_started", "reconcile_required", "sent_verified")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
SAFE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,127}$")
EMAIL_RE = re.compile(r"^[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?(?:\.[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?)+$", re.I)
OBSERVATION_ROW_KEYS_V2 = {
    "id", "provider_id", "medication", "formulation", "billing_basis",
    "price_phase", "selection_basis", "amount_usd",
}
MEDICATIONS = ("semaglutide", "tirzepatide")


class DigitalPRError(RuntimeError):
    """Fail closed without exposing credentials or message bodies in the reason."""


class WeeklyQuotaExceeded(DigitalPRError):
    pass


@dataclass(frozen=True)
class ContentBase:
    pointer_path: Path
    pointer_sha256: str
    release_root: Path
    release_tree_sha256: str
    price_tracker_path: Path
    price_tracker_sha256: str
    pointer: dict[str, Any]
    tracker: dict[str, Any]


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def pretty_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def read_json(path: Path) -> tuple[dict[str, Any], bytes]:
    if path.is_symlink() or not path.is_file():
        raise DigitalPRError(f"source_not_regular:{path}")
    raw = path.read_bytes()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DigitalPRError(f"source_json_invalid:{path.name}") from exc
    if not isinstance(value, dict):
        raise DigitalPRError(f"source_json_not_object:{path.name}")
    return value, raw


def atomic_write(path: Path, raw: bytes, *, mode: int = 0o644, conflict_safe: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700 if mode == 0o600 else 0o755)
    if conflict_safe and path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != raw:
            raise DigitalPRError(f"artifact_conflict:{path}")
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


def framed_tree_sha256(root: Path) -> tuple[str, int]:
    if root.is_symlink() or not root.is_dir():
        raise DigitalPRError("content_release_root_invalid")
    rows: list[tuple[str, str, int]] = []
    for path in root.rglob("*"):
        if path.is_file():
            relative = path.relative_to(root).as_posix()
            raw = path.read_bytes()
            rows.append((relative, sha256_bytes(raw), len(raw)))
    digest = hashlib.sha256()
    for relative, file_hash, size in sorted(rows, key=lambda row: row[0]):
        digest.update(f"{relative}\0{file_hash}\0{size}\n".encode("utf-8"))
    return digest.hexdigest(), len(rows)


def parse_now(value: str | dt.date | dt.datetime | None = None) -> dt.datetime:
    if value is None:
        return dt.datetime.now(dt.timezone.utc).astimezone(LA)
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            raise DigitalPRError("now_timezone_required")
        return value.astimezone(LA)
    if isinstance(value, dt.date):
        return dt.datetime.combine(value, dt.time(12, 0), LA)
    text = str(value).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return dt.datetime.combine(dt.date.fromisoformat(text), dt.time(12, 0), LA)
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DigitalPRError("now_invalid") from exc
    if parsed.tzinfo is None:
        raise DigitalPRError("now_timezone_required")
    return parsed.astimezone(LA)


def canonical_as_of(now: dt.datetime) -> str:
    return dt.datetime.combine(now.date(), dt.time(12, 0), LA).isoformat()


def week_bounds(now: dt.datetime) -> tuple[str, str]:
    start = now.date() - dt.timedelta(days=now.weekday())
    return start.isoformat(), (start + dt.timedelta(days=6)).isoformat()


def age_days(value: str, today: dt.date, *, code: str) -> int:
    try:
        observed = dt.date.fromisoformat(str(value))
    except ValueError as exc:
        raise DigitalPRError(code) from exc
    age = (today - observed).days
    if age < 0:
        raise DigitalPRError(code + "_future")
    return age


def https_url(value: Any) -> bool:
    try:
        parsed = urlparse(str(value))
    except ValueError:
        return False
    return parsed.scheme == "https" and bool(parsed.hostname) and not parsed.username and not parsed.password


def resolve_content_base(
    pointer_path: Path = CONTENT_BASE_POINTER,
    *,
    releases_root: Path = BUSINESS / "site-system/flagship-v2/releases",
) -> ContentBase:
    pointer, pointer_raw = read_json(pointer_path)
    try:
        validate_pointer_shape(
            pointer,
            releases_root=releases_root,
            require_release_dirs=True,
        )
    except RuntimeError as exc:
        raise DigitalPRError(f"content_base_pointer_invalid:{exc}") from exc
    release_root = Path(str(pointer.get("release_root") or ""))
    releases_root = releases_root.resolve()
    try:
        release_root.resolve().relative_to(releases_root)
    except (ValueError, OSError) as exc:
        raise DigitalPRError("content_base_release_outside_releases") from exc
    measured_tree, _file_count = framed_tree_sha256(release_root)
    if not HASH_RE.fullmatch(str(pointer.get("release_tree_sha256") or "")) or measured_tree != pointer["release_tree_sha256"]:
        raise DigitalPRError("content_base_release_tree_hash_mismatch")
    for key in ("manifest", "final_receipt"):
        row = pointer.get(key) or {}
        source = Path(str(row.get("path") or ""))
        if not HASH_RE.fullmatch(str(row.get("sha256") or "")) or not source.is_file():
            raise DigitalPRError(f"content_base_{key}_binding_invalid")
        if sha256_bytes(source.read_bytes()) != row["sha256"]:
            raise DigitalPRError(f"content_base_{key}_hash_mismatch")
    tracker_path = release_root / "data/price-tracker.json"
    tracker, tracker_raw = read_json(tracker_path)
    return ContentBase(
        pointer_path=pointer_path,
        pointer_sha256=sha256_bytes(pointer_raw),
        release_root=release_root,
        release_tree_sha256=measured_tree,
        price_tracker_path=tracker_path,
        price_tracker_sha256=sha256_bytes(tracker_raw),
        pointer=pointer,
        tracker=tracker,
    )


def _snapshot_row(record: dict[str, Any], amount: Any | None = None) -> dict[str, Any]:
    return {
        "id": record["id"],
        "provider_id": record["provider_id"],
        "medication": record["medication"],
        "formulation": record["formulation"],
        "billing_basis": record["billing_basis"],
        "price_phase": record["price_phase"],
        "selection_basis": record["selection_basis"],
        "amount_usd": record["amount_usd"] if amount is None else amount,
    }


def comparison_identity(row: dict[str, Any]) -> str:
    return f"{row['provider_id']}|{row['medication']}|{row['id']}"


def normalize_price_observations(tracker: dict[str, Any]) -> dict[str, Any]:
    config = tracker.get("price_index")
    if not isinstance(config, dict) or config.get("route") != PRICE_INDEX_ROUTE or config.get("publication_state") != "PUBLIC_INDEXABLE":
        raise DigitalPRError("price_index_config_invalid")
    records = config.get("records")
    observations = config.get("observations")
    if not isinstance(records, list) or not records or not isinstance(observations, list) or not observations:
        raise DigitalPRError("price_index_history_missing")
    current_by_id: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict) or set(record) != OBSERVATION_ROW_KEYS_V2:
            raise DigitalPRError("price_index_current_record_schema_invalid")
        if not SAFE_ID_RE.fullmatch(str(record.get("id") or "")) or not SAFE_ID_RE.fullmatch(str(record.get("provider_id") or "")):
            raise DigitalPRError("price_index_current_identity_invalid")
        if record.get("medication") not in MEDICATIONS or not isinstance(record.get("amount_usd"), (int, float)) or record["amount_usd"] <= 0:
            raise DigitalPRError("price_index_current_amount_invalid")
        if record["id"] in current_by_id:
            raise DigitalPRError("price_index_current_id_duplicate")
        current_by_id[record["id"]] = record

    schema = str(config.get("schema") or "")
    legacy_single_observation = schema == "glp-clinic-scout-price-index-v1"
    if schema not in {"glp-clinic-scout-price-index-v1", "glp-clinic-scout-price-index-v2"}:
        raise DigitalPRError("price_index_schema_unsupported")
    if legacy_single_observation and len(observations) != 1:
        raise DigitalPRError("legacy_price_index_multiple_observations_unrepresentable")

    normalized: list[dict[str, Any]] = []
    previous_date = ""
    for observation in observations:
        if not isinstance(observation, dict) or set(observation) != {"observed_on", "data_as_of", "source_files", "record_set_sha256", "records"}:
            raise DigitalPRError("price_index_observation_schema_invalid")
        observed_on = str(observation.get("observed_on") or "")
        try:
            dt.date.fromisoformat(observed_on)
            dt.date.fromisoformat(str(observation.get("data_as_of") or ""))
        except ValueError as exc:
            raise DigitalPRError("price_index_observation_date_invalid") from exc
        if observed_on <= previous_date:
            raise DigitalPRError("price_index_observation_order_invalid")
        previous_date = observed_on
        rows = observation.get("records")
        if not isinstance(rows, list) or not rows:
            raise DigitalPRError("price_index_observation_records_invalid")
        if sha256_bytes(canonical_bytes(rows)) != observation.get("record_set_sha256"):
            raise DigitalPRError("price_index_observation_hash_invalid")
        normalized_rows: list[dict[str, Any]] = []
        identities: set[str] = set()
        ids: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                raise DigitalPRError("price_index_observation_row_invalid")
            if legacy_single_observation:
                if set(row) != {"id", "amount_usd"} or row.get("id") not in current_by_id:
                    raise DigitalPRError("legacy_price_index_identity_unavailable")
                normalized_row = _snapshot_row(current_by_id[row["id"]], row.get("amount_usd"))
            else:
                if set(row) != OBSERVATION_ROW_KEYS_V2:
                    raise DigitalPRError("price_index_observation_row_schema_invalid")
                normalized_row = dict(row)
            if normalized_row.get("medication") not in MEDICATIONS or not isinstance(normalized_row.get("amount_usd"), (int, float)) or normalized_row["amount_usd"] <= 0:
                raise DigitalPRError("price_index_observation_row_amount_invalid")
            identity = comparison_identity(normalized_row)
            if identity in identities or normalized_row["id"] in ids:
                raise DigitalPRError("price_index_observation_identity_duplicate")
            identities.add(identity)
            ids.add(normalized_row["id"])
            normalized_rows.append(normalized_row)
        normalized.append({
            "observed_on": observed_on,
            "data_as_of": observation["data_as_of"],
            "source_files": observation["source_files"],
            "record_set_sha256": observation["record_set_sha256"],
            "records": normalized_rows,
        })

    latest_expected = [_snapshot_row(record) for record in records]
    if normalized[-1]["records"] != latest_expected:
        raise DigitalPRError("price_index_latest_observation_not_current")
    return {
        "schema": schema,
        "normalization": "legacy_single_real_observation_joined_to_current_identity" if legacy_single_observation else "native_variable_cardinality_v2",
        "records": records,
        "observations": normalized,
    }


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def _decimal_text(value: Decimal, places: int | None = None) -> str:
    if places is not None:
        value = value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _median(values: Iterable[Any]) -> Decimal:
    rows = sorted(_decimal(value) for value in values)
    if not rows:
        raise DigitalPRError("price_index_medication_empty")
    middle = len(rows) // 2
    return rows[middle] if len(rows) % 2 else (rows[middle - 1] + rows[middle]) / Decimal(2)


def _snapshot_metrics(observation: dict[str, Any]) -> dict[str, dict[str, Any]]:
    metrics: dict[str, dict[str, Any]] = {}
    for medication in MEDICATIONS:
        rows = [row for row in observation["records"] if row["medication"] == medication]
        values = [_decimal(row["amount_usd"]) for row in rows]
        if not values:
            raise DigitalPRError(f"price_index_medication_empty:{medication}")
        minimum = min(values)
        maximum = max(values)
        median = _median(values)
        metrics[medication] = {
            "provider_record_count": len(values),
            "median_usd": _decimal_text(median),
            "min_usd": _decimal_text(minimum),
            "max_usd": _decimal_text(maximum),
            "spread_multiple": _decimal_text(maximum / minimum, 12),
        }
    return metrics


def evaluate_newsworthiness(normalized: dict[str, Any]) -> dict[str, Any]:
    observations = normalized["observations"]
    current = observations[-1]
    current_metrics = _snapshot_metrics(current)
    reasons: list[dict[str, Any]] = []
    for medication in MEDICATIONS:
        ratio = _decimal(current_metrics[medication]["spread_multiple"])
        if ratio >= Decimal("3.0"):
            reasons.append({
                "gate": "spread_multiple_at_least_3x",
                "medication": medication,
                "spread_multiple": current_metrics[medication]["spread_multiple"],
                "threshold": "3",
            })

    prior_metrics: dict[str, dict[str, Any]] | None = None
    if len(observations) >= 2:
        prior = observations[-2]
        prior_metrics = _snapshot_metrics(prior)
        for medication in MEDICATIONS:
            previous_median = _decimal(prior_metrics[medication]["median_usd"])
            current_median = _decimal(current_metrics[medication]["median_usd"])
            absolute_percent = abs((current_median - previous_median) / previous_median) * Decimal(100)
            if absolute_percent >= Decimal("5.00"):
                reasons.append({
                    "gate": "absolute_median_change_at_least_5_percent",
                    "medication": medication,
                    "prior_median_usd": prior_metrics[medication]["median_usd"],
                    "current_median_usd": current_metrics[medication]["median_usd"],
                    "absolute_change_percent": _decimal_text(absolute_percent, 6),
                    "threshold_percent": "5",
                })
        prior_provider_ids = {row["provider_id"] for row in prior["records"]}
        for row in current["records"]:
            current_median = _decimal(current_metrics[row["medication"]]["median_usd"])
            if row["provider_id"] not in prior_provider_ids and _decimal(row["amount_usd"]) < current_median:
                reasons.append({
                    "gate": "new_provider_below_current_medication_median",
                    "provider_id": row["provider_id"],
                    "record_identity": comparison_identity(row),
                    "medication": row["medication"],
                    "amount_usd": _decimal_text(_decimal(row["amount_usd"])),
                    "current_median_usd": _decimal_text(current_median),
                    "provider_absent_from_prior_snapshot": True,
                    "strictly_below_current_median": True,
                })
    else:
        prior = None

    reasons.sort(key=lambda row: canonical_bytes(row))
    observation_binding = {
        "observed_on": current["observed_on"],
        "data_as_of": current["data_as_of"],
        "records": current["records"],
        "source_files": current["source_files"],
    }
    return {
        "gate_version": GATE_VERSION,
        "decision": "NEWSWORTHY" if reasons else "NO_NEWS_NO_PITCH",
        "observation_count": len(observations),
        "latest_observed_on": current["observed_on"],
        "prior_observed_on": prior["observed_on"] if prior else None,
        "observation_sha256": sha256_bytes(canonical_bytes(observation_binding)),
        "current_metrics": current_metrics,
        "prior_metrics": prior_metrics,
        "matched_reasons": reasons,
        "thresholds": {
            "absolute_median_change_percent_min": "5",
            "spread_multiple_min": "3",
            "new_provider_below_current_medication_median": True,
        },
    }


def validate_target_packet(document: dict[str, Any], packet_sha256: str, now: dt.datetime) -> list[dict[str, Any]]:
    if document.get("schema") != "glp-clinic-scout-growth-build-2-research-v1" or document.get("window_days") != 90:
        raise DigitalPRError("target_packet_schema_invalid")
    targets = document.get("targets")
    if not isinstance(targets, list):
        raise DigitalPRError("target_packet_targets_invalid")
    results: list[dict[str, Any]] = []
    for target in targets:
        if not isinstance(target, dict):
            raise DigitalPRError("target_row_invalid")
        required = {"name", "outlet", "beat", "article_url", "article_title", "evidence_date", "evidence", "contact_state"}
        if not required <= set(target) or not https_url(target.get("article_url")):
            raise DigitalPRError("target_evidence_invalid")
        evidence_age = age_days(str(target["evidence_date"]), now.date(), code="target_evidence_date_invalid")
        evidence_fresh = evidence_age <= MAX_TARGET_AGE_DAYS
        target_id = "target_" + sha256_text("|".join([
            str(target["name"]), str(target["outlet"]), str(target["article_url"]),
        ]))[:24]
        contact = target.get("contact")
        contact_address: str | None = None
        contact_evidence_hash: str | None = None
        contact_age: int | None = None
        contact_method: str | None = None
        contact_reason = "no_explicit_evidenced_contact"
        if isinstance(contact, dict):
            expected = {"address", "evidence_url", "evidence_date", "evidence_sha256", "method"}
            if set(contact) != expected:
                contact_reason = "contact_evidence_schema_invalid"
            else:
                address = parseaddr(str(contact.get("address") or ""))[1].strip().lower()
                method = str(contact.get("method") or "")
                evidence_hash = str(contact.get("evidence_sha256") or "")
                try:
                    measured_contact_age = age_days(str(contact.get("evidence_date") or ""), now.date(), code="contact_evidence_date_invalid")
                except DigitalPRError:
                    measured_contact_age = None
                if method not in EXPLICIT_CONTACT_METHODS or str(target.get("contact_state")) != "verified_explicit":
                    contact_reason = "contact_not_explicit"
                elif not EMAIL_RE.fullmatch(address) or address != str(contact.get("address") or "").strip().lower():
                    contact_reason = "contact_address_invalid"
                elif not https_url(contact.get("evidence_url")) or not HASH_RE.fullmatch(evidence_hash):
                    contact_reason = "contact_evidence_invalid"
                elif measured_contact_age is None or measured_contact_age > MAX_TARGET_AGE_DAYS:
                    contact_reason = "contact_evidence_expired"
                else:
                    contact_address = address
                    contact_evidence_hash = evidence_hash
                    contact_age = measured_contact_age
                    contact_method = method
                    contact_reason = "eligible_explicit_evidenced_contact"
        eligible = evidence_fresh and contact_address is not None
        reasons = []
        if not evidence_fresh:
            reasons.append("target_article_evidence_expired")
        if contact_reason != "eligible_explicit_evidenced_contact":
            reasons.append(contact_reason)
        target_version_id = "tv_" + sha256_bytes(canonical_bytes({
            "target": target,
            "packet_sha256": packet_sha256,
            "validated_on": now.date().isoformat(),
        }))[:28]
        results.append({
            "target_id": target_id,
            "target_version_id": target_version_id,
            "name": str(target["name"]),
            "outlet": str(target["outlet"]),
            "beat": list(target["beat"]) if isinstance(target["beat"], list) else [],
            "article_url": str(target["article_url"]),
            "article_title": str(target["article_title"]),
            "evidence_date": str(target["evidence_date"]),
            "evidence_age_days": evidence_age,
            "evidence_fresh": evidence_fresh,
            "contact_address": contact_address,
            "contact_evidence_sha256": contact_evidence_hash,
            "contact_evidence_age_days": contact_age,
            "contact_method": contact_method,
            "eligible": eligible,
            "ineligible_reasons": reasons,
            "source_packet_sha256": packet_sha256,
            "target_payload_sha256": sha256_bytes(canonical_bytes(target)),
            "target_payload": target,
        })
    return sorted(results, key=lambda row: (row["outlet"], row["name"], row["target_id"]))


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS gate_runs(
  run_id TEXT PRIMARY KEY,as_of_local TEXT NOT NULL,week_start TEXT NOT NULL,week_end TEXT NOT NULL,
  gate_version TEXT NOT NULL,status TEXT NOT NULL,content_base_path TEXT NOT NULL,
  content_base_sha256 TEXT NOT NULL,release_root TEXT NOT NULL,release_tree_sha256 TEXT NOT NULL,
  price_tracker_path TEXT NOT NULL,price_tracker_sha256 TEXT NOT NULL,target_path TEXT NOT NULL,
  target_sha256 TEXT NOT NULL,observation_sha256 TEXT NOT NULL,latest_observed_on TEXT NOT NULL,
  prior_observed_on TEXT,gate_inputs_json TEXT NOT NULL,matched_reasons_json TEXT NOT NULL,
  run_payload_json TEXT NOT NULL,run_payload_sha256 TEXT NOT NULL,created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS target_evidence(
  target_version_id TEXT PRIMARY KEY,target_id TEXT NOT NULL,run_id TEXT NOT NULL REFERENCES gate_runs(run_id),
  name TEXT NOT NULL,outlet TEXT NOT NULL,article_url TEXT NOT NULL,article_title TEXT NOT NULL,
  evidence_date TEXT NOT NULL,evidence_age_days INTEGER NOT NULL,evidence_fresh INTEGER NOT NULL,
  contact_address TEXT,contact_evidence_sha256 TEXT,contact_evidence_age_days INTEGER,contact_method TEXT,
  eligible INTEGER NOT NULL,ineligible_reasons_json TEXT NOT NULL,source_packet_sha256 TEXT NOT NULL,
  target_payload_sha256 TEXT NOT NULL,target_payload_json TEXT NOT NULL,validated_on TEXT NOT NULL,
  UNIQUE(run_id,target_id)
);
CREATE TABLE IF NOT EXISTS pitch_drafts(
  pitch_id TEXT PRIMARY KEY,run_id TEXT NOT NULL REFERENCES gate_runs(run_id),
  target_version_id TEXT NOT NULL REFERENCES target_evidence(target_version_id),observation_sha256 TEXT NOT NULL,
  sender TEXT NOT NULL,recipient TEXT NOT NULL,recipient_sha256 TEXT NOT NULL,subject TEXT NOT NULL,
  subject_sha256 TEXT NOT NULL,body TEXT NOT NULL,body_sha256 TEXT NOT NULL,claims_json TEXT NOT NULL,
  claims_sha256 TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
  UNIQUE(run_id,target_version_id),UNIQUE(recipient,observation_sha256)
);
CREATE TABLE IF NOT EXISTS operator_approvals(
  pitch_id TEXT PRIMARY KEY REFERENCES pitch_drafts(pitch_id),binding_sha256 TEXT NOT NULL,
  card_message_id TEXT NOT NULL,decision_message_id TEXT NOT NULL UNIQUE,channel_id TEXT NOT NULL,
  actor_id TEXT NOT NULL,card_payload_sha256 TEXT NOT NULL,decision_payload_sha256 TEXT NOT NULL,
  validated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quota_reservations(
  reservation_id TEXT PRIMARY KEY,pitch_id TEXT NOT NULL UNIQUE REFERENCES pitch_drafts(pitch_id),
  week_start TEXT NOT NULL,week_end TEXT NOT NULL,recipient TEXT NOT NULL,observation_sha256 TEXT NOT NULL,
  status TEXT NOT NULL,claimed_at TEXT NOT NULL,submission_started_at TEXT,reconciled_at TEXT,
  UNIQUE(week_start,recipient,observation_sha256)
);
CREATE TABLE IF NOT EXISTS send_reconciliations(
  pitch_id TEXT PRIMARY KEY REFERENCES pitch_drafts(pitch_id),reservation_id TEXT NOT NULL UNIQUE REFERENCES quota_reservations(reservation_id),
  status TEXT NOT NULL,sender TEXT NOT NULL,recipient TEXT NOT NULL,provider_message_id TEXT NOT NULL UNIQUE,
  provider_thread_id TEXT NOT NULL,outbound_rfc_message_id TEXT NOT NULL,provider_internal_date TEXT NOT NULL,
  sent_receipt_path TEXT NOT NULL,sent_receipt_sha256 TEXT NOT NULL,receipt_json TEXT NOT NULL,reconciled_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS no_news_receipts(
  run_id TEXT PRIMARY KEY REFERENCES gate_runs(run_id),receipt_path TEXT NOT NULL,receipt_sha256 TEXT NOT NULL,
  receipt_json TEXT NOT NULL,created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS effect_counters(
  name TEXT PRIMARY KEY,value INTEGER NOT NULL CHECK(value>=0),updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS quota_week_status_idx ON quota_reservations(week_start,status);
CREATE INDEX IF NOT EXISTS reply_thread_sender_idx ON send_reconciliations(provider_thread_id,recipient,status);
"""


class DigitalPRLedger:
    def __init__(self, path: Path = DB_PATH):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.parent.chmod(0o700)
        self.conn = sqlite3.connect(path, timeout=15)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=15000")
        self.conn.executescript(SCHEMA_SQL)
        now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        schema_row = self.conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
        if schema_row is None:
            self.conn.execute("INSERT INTO schema_meta(key,value) VALUES('schema_version',?)", (SCHEMA_VERSION,))
        elif schema_row["value"] != SCHEMA_VERSION:
            raise DigitalPRError("digital_pr_schema_version_mismatch")
        counter_names = {str(row["name"]) for row in self.conn.execute("SELECT name FROM effect_counters")}
        for name in ("external_sends", "provider_mutations", "discord_posts", "spend_cents"):
            if name not in counter_names:
                self.conn.execute("INSERT INTO effect_counters(name,value,updated_at) VALUES(?,0,?)", (name, now))
        self.conn.commit()
        self._chmod()

    def _chmod(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(self.path) + suffix)
            if path.exists():
                path.chmod(0o600)

    def close(self) -> None:
        self.conn.commit()
        self._chmod()
        self.conn.close()

    def __enter__(self) -> "DigitalPRLedger":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def effects(self) -> dict[str, int]:
        return {str(row["name"]): int(row["value"]) for row in self.conn.execute("SELECT name,value FROM effect_counters ORDER BY name")}

    def record_run(self, payload: dict[str, Any], targets: list[dict[str, Any]]) -> str:
        run_payload_json = canonical_bytes(payload).decode("utf-8")
        run_payload_sha256 = sha256_text(run_payload_json)
        run_id = "run_" + run_payload_sha256[:28]
        now = payload["as_of_local"]
        existing = self.conn.execute(
            "SELECT run_payload_json,run_payload_sha256 FROM gate_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if existing is not None:
            if existing["run_payload_json"] != run_payload_json or existing["run_payload_sha256"] != run_payload_sha256:
                raise DigitalPRError("gate_run_conflict")
            stored_target_versions = {
                str(row["target_version_id"])
                for row in self.conn.execute("SELECT target_version_id FROM target_evidence WHERE run_id=?", (run_id,))
            }
            expected_target_versions = {str(target["target_version_id"]) for target in targets}
            if stored_target_versions != expected_target_versions:
                raise DigitalPRError("gate_run_target_evidence_conflict")
            return run_id
        with self.conn:
            self.conn.execute(
                """INSERT OR IGNORE INTO gate_runs(
                   run_id,as_of_local,week_start,week_end,gate_version,status,content_base_path,
                   content_base_sha256,release_root,release_tree_sha256,price_tracker_path,price_tracker_sha256,
                   target_path,target_sha256,observation_sha256,latest_observed_on,prior_observed_on,
                   gate_inputs_json,matched_reasons_json,run_payload_json,run_payload_sha256,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id, payload["as_of_local"], payload["week_start"], payload["week_end"],
                    payload["news_gate"]["gate_version"], payload["news_gate"]["decision"],
                    payload["sources"]["content_base"]["path"], payload["sources"]["content_base"]["sha256"],
                    payload["sources"]["release"]["path"], payload["sources"]["release"]["tree_sha256"],
                    payload["sources"]["price_tracker"]["path"], payload["sources"]["price_tracker"]["sha256"],
                    payload["sources"]["targets"]["path"], payload["sources"]["targets"]["sha256"],
                    payload["news_gate"]["observation_sha256"], payload["news_gate"]["latest_observed_on"],
                    payload["news_gate"]["prior_observed_on"], json.dumps(payload["news_gate"], sort_keys=True),
                    json.dumps(payload["news_gate"]["matched_reasons"], sort_keys=True), run_payload_json,
                    run_payload_sha256, now,
                ),
            )
            existing = self.conn.execute("SELECT run_payload_sha256 FROM gate_runs WHERE run_id=?", (run_id,)).fetchone()
            if existing is None or existing[0] != run_payload_sha256:
                raise DigitalPRError("gate_run_identity_conflict")
            for target in targets:
                self.conn.execute(
                    """INSERT OR IGNORE INTO target_evidence(
                       target_version_id,target_id,run_id,name,outlet,article_url,article_title,evidence_date,
                       evidence_age_days,evidence_fresh,contact_address,contact_evidence_sha256,
                       contact_evidence_age_days,contact_method,eligible,ineligible_reasons_json,
                       source_packet_sha256,target_payload_sha256,target_payload_json,validated_on
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        target["target_version_id"], target["target_id"], run_id, target["name"], target["outlet"],
                        target["article_url"], target["article_title"], target["evidence_date"], target["evidence_age_days"],
                        int(target["evidence_fresh"]), target["contact_address"], target["contact_evidence_sha256"],
                        target["contact_evidence_age_days"], target["contact_method"], int(target["eligible"]),
                        json.dumps(target["ineligible_reasons"], sort_keys=True), target["source_packet_sha256"],
                        target["target_payload_sha256"], json.dumps(target["target_payload"], sort_keys=True),
                        payload["as_of_date"],
                    ),
                )
        return run_id

    def record_no_news_receipt(self, run_id: str, path: Path, receipt: dict[str, Any]) -> str:
        raw = pretty_bytes(receipt)
        digest = sha256_bytes(raw)
        receipt_json = canonical_bytes(receipt).decode("utf-8")
        existing = self.conn.execute(
            "SELECT receipt_path,receipt_sha256,receipt_json FROM no_news_receipts WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if existing is not None:
            if (
                existing["receipt_path"] != str(path)
                or existing["receipt_sha256"] != digest
                or existing["receipt_json"] != receipt_json
            ):
                raise DigitalPRError("no_news_receipt_conflict")
            atomic_write(path, raw, mode=0o644, conflict_safe=True)
            return digest
        atomic_write(path, raw, mode=0o644, conflict_safe=True)
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO no_news_receipts(run_id,receipt_path,receipt_sha256,receipt_json,created_at) VALUES(?,?,?,?,?)",
                (run_id, str(path), digest, receipt_json, receipt["as_of_local"]),
            )
            row = self.conn.execute("SELECT receipt_sha256,receipt_json FROM no_news_receipts WHERE run_id=?", (run_id,)).fetchone()
            if row is None or row["receipt_sha256"] != digest or row["receipt_json"] != receipt_json:
                raise DigitalPRError("no_news_receipt_conflict")
        return digest


def _finding_sentence(reason: dict[str, Any]) -> str:
    if reason["gate"] == "absolute_median_change_at_least_5_percent":
        return (
            f"The observed {reason['medication']} median moved {reason['absolute_change_percent']} percent "
            f"from ${reason['prior_median_usd']} to ${reason['current_median_usd']}."
        )
    if reason["gate"] == "spread_multiple_at_least_3x":
        return f"The current observed {reason['medication']} price range spans {reason['spread_multiple']} times from low to high."
    return (
        f"A newly observed provider entered at ${reason['amount_usd']} for {reason['medication']}, "
        f"below the current ${reason['current_median_usd']} median."
    )


def create_pitch_draft(ledger: DigitalPRLedger, run_id: str, target_version_id: str) -> dict[str, Any]:
    run = ledger.conn.execute("SELECT * FROM gate_runs WHERE run_id=?", (run_id,)).fetchone()
    target = ledger.conn.execute("SELECT * FROM target_evidence WHERE target_version_id=? AND run_id=?", (target_version_id, run_id)).fetchone()
    if run is None or target is None:
        raise DigitalPRError("pitch_source_not_found")
    if run["status"] != "NEWSWORTHY":
        raise DigitalPRError("pitch_refused_no_news")
    if int(target["eligible"]) != 1 or not target["contact_address"]:
        raise DigitalPRError("pitch_refused_no_explicit_contact")
    reasons = json.loads(run["matched_reasons_json"])
    if not reasons:
        raise DigitalPRError("pitch_refused_no_matched_reason")
    reason = reasons[0]
    claims = {
        "schema": "glp-digital-pr-pitch-claims-v1",
        "observation_sha256": run["observation_sha256"],
        "latest_observed_on": run["latest_observed_on"],
        "mechanical_finding": reason,
        "price_index_url": PRICE_INDEX_URL,
        "dataset_offer": True,
    }
    recipient = str(target["contact_address"]).lower()
    subject = f"New GLP price data for {target['outlet']}"
    first_name = str(target["name"]).split()[0]
    observed_date = dt.date.fromisoformat(str(run["latest_observed_on"]))
    observed_label = f"{observed_date.strftime('%B')} {observed_date.day}, {observed_date.year}"
    body = (
        f"Hi {first_name},\n\n"
        f"We just updated the Clinic Scout GLP 1 Price Index with provider level prices observed on {observed_label}. "
        f"{_finding_sentence(reason)}\n\n"
        f"The methodology and current data are here: {PRICE_INDEX_URL}\n\n"
        "I can send the underlying dataset if it would be useful for your coverage.\n\n"
        "Izaiah\nGLP Clinic Scout"
    )
    core = {
        "run_id": run_id,
        "target_version_id": target_version_id,
        "observation_sha256": run["observation_sha256"],
        "sender": SENDER,
        "recipient": recipient,
        "subject_sha256": sha256_text(subject),
        "body_sha256": sha256_text(body),
        "claims_sha256": sha256_bytes(canonical_bytes(claims)),
    }
    pitch_id = "pitch_" + sha256_bytes(canonical_bytes(core))[:28]
    now = run["as_of_local"]
    with ledger.conn:
        ledger.conn.execute(
            """INSERT OR IGNORE INTO pitch_drafts(
               pitch_id,run_id,target_version_id,observation_sha256,sender,recipient,recipient_sha256,
               subject,subject_sha256,body,body_sha256,claims_json,claims_sha256,status,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                pitch_id, run_id, target_version_id, run["observation_sha256"], SENDER, recipient,
                sha256_text(recipient), subject, sha256_text(subject), body, sha256_text(body),
                json.dumps(claims, sort_keys=True), sha256_bytes(canonical_bytes(claims)), "draft", now, now,
            ),
        )
        row = ledger.conn.execute("SELECT * FROM pitch_drafts WHERE pitch_id=?", (pitch_id,)).fetchone()
        if row is None or row["body_sha256"] != sha256_text(body) or row["recipient"] != recipient:
            raise DigitalPRError("pitch_draft_identity_conflict")
    return dict(row)


def approval_binding(pitch: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    if str(pitch["sender"]).lower() != SENDER:
        raise DigitalPRError("pitch_sender_binding_invalid")
    if sha256_text(str(pitch["recipient"])) != str(pitch["recipient_sha256"]):
        raise DigitalPRError("pitch_recipient_hash_mismatch")
    if sha256_text(str(pitch["subject"])) != str(pitch["subject_sha256"]):
        raise DigitalPRError("pitch_subject_hash_mismatch")
    if sha256_text(str(pitch["body"])) != str(pitch["body_sha256"]):
        raise DigitalPRError("pitch_body_hash_mismatch")
    if sha256_text(str(pitch["claims_json"])) != str(pitch["claims_sha256"]):
        # Claims are stored as normalized JSON text. Reparse before hashing so
        # harmless SQLite spacing never weakens the immutable value binding.
        try:
            claims_hash = sha256_bytes(canonical_bytes(json.loads(str(pitch["claims_json"]))))
        except json.JSONDecodeError as exc:
            raise DigitalPRError("pitch_claims_json_invalid") from exc
        if claims_hash != str(pitch["claims_sha256"]):
            raise DigitalPRError("pitch_claims_hash_mismatch")
    return {
        "schema": APPROVAL_SCHEMA,
        "pitch_id": str(pitch["pitch_id"]),
        "sender": str(pitch["sender"]),
        "recipient": str(pitch["recipient"]),
        "recipient_sha256": str(pitch["recipient_sha256"]),
        "subject": str(pitch["subject"]),
        "subject_sha256": str(pitch["subject_sha256"]),
        "body_sha256": str(pitch["body_sha256"]),
        "observation_sha256": str(pitch["observation_sha256"]),
        "claims_sha256": str(pitch["claims_sha256"]),
    }


def render_approval_card(pitch: sqlite3.Row | dict[str, Any]) -> tuple[str, str]:
    binding = approval_binding(pitch)
    digest = sha256_bytes(canonical_bytes(binding))
    content = (
        "GLP DIGITAL PR PITCH APPROVAL\n"
        f"Pitch: {binding['pitch_id']}\n"
        f"Recipient: {binding['recipient']}\n"
        f"Subject: {binding['subject']}\n"
        f"Recipient SHA256: {binding['recipient_sha256']}\n"
        f"Subject SHA256: {binding['subject_sha256']}\n"
        f"Body SHA256: {binding['body_sha256']}\n"
        f"Price Index observation SHA256: {binding['observation_sha256']}\n"
        f"Claims SHA256: {binding['claims_sha256']}\n"
        f"Approval binding SHA256: {digest}\n"
        f"Reply exactly: APPROVE GLP_PR_PITCH {binding['pitch_id']} {digest}"
    )
    return content, digest


def _message_reference_id(message: dict[str, Any]) -> str:
    reference = message.get("message_reference") or {}
    return str(reference.get("message_id") or "") if isinstance(reference, dict) else ""


def validate_operator_approval(
    ledger: DigitalPRLedger,
    pitch_id: str,
    decision_message_id: str,
    *,
    fetch_message: Callable[[str, str], dict[str, Any]],
    now: str | dt.date | dt.datetime | None = None,
) -> dict[str, Any]:
    pitch = ledger.conn.execute("SELECT * FROM pitch_drafts WHERE pitch_id=?", (pitch_id,)).fetchone()
    if pitch is None or pitch["status"] not in {"draft", "approved", "send_claimed"}:
        raise DigitalPRError("approval_pitch_not_active")
    card_content, binding_sha256 = render_approval_card(pitch)
    decision = fetch_message(DECISIONS_CHANNEL_ID, decision_message_id)
    if str(decision.get("id") or "") != decision_message_id or str(decision.get("channel_id") or "") != DECISIONS_CHANNEL_ID:
        raise DigitalPRError("approval_decision_channel_mismatch")
    author = decision.get("author") or {}
    if str(author.get("id") or "") != OPERATOR_ID or author.get("bot") is True:
        raise DigitalPRError("approval_actor_mismatch")
    if decision.get("edited_timestamp") is not None:
        raise DigitalPRError("approval_decision_edited")
    card_message_id = _message_reference_id(decision)
    if not card_message_id:
        raise DigitalPRError("approval_card_reference_missing")
    expected_decision = f"APPROVE GLP_PR_PITCH {pitch_id} {binding_sha256}"
    if str(decision.get("content") or "").strip() != expected_decision:
        raise DigitalPRError("approval_decision_payload_mismatch")
    card = fetch_message(DECISIONS_CHANNEL_ID, card_message_id)
    if str(card.get("id") or "") != card_message_id or str(card.get("channel_id") or "") != DECISIONS_CHANNEL_ID:
        raise DigitalPRError("approval_card_channel_mismatch")
    if card.get("edited_timestamp") is not None or str(card.get("content") or "") != card_content:
        raise DigitalPRError("approval_card_not_exact_active")
    validated_at = canonical_as_of(parse_now(now))
    with ledger.conn:
        ledger.conn.execute(
            """INSERT OR IGNORE INTO operator_approvals(
               pitch_id,binding_sha256,card_message_id,decision_message_id,channel_id,actor_id,
               card_payload_sha256,decision_payload_sha256,validated_at
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                pitch_id, binding_sha256, card_message_id, decision_message_id, DECISIONS_CHANNEL_ID,
                OPERATOR_ID, sha256_text(card_content), sha256_text(expected_decision), validated_at,
            ),
        )
        existing = ledger.conn.execute("SELECT * FROM operator_approvals WHERE pitch_id=?", (pitch_id,)).fetchone()
        if existing is None or any(str(existing[key]) != value for key, value in (
            ("binding_sha256", binding_sha256), ("card_message_id", card_message_id),
            ("decision_message_id", decision_message_id), ("actor_id", OPERATOR_ID),
        )):
            raise DigitalPRError("approval_identity_conflict")
        ledger.conn.execute("UPDATE pitch_drafts SET status='approved',updated_at=? WHERE pitch_id=?", (validated_at, pitch_id))
    return dict(existing)


def reserve_weekly_quota(
    ledger: DigitalPRLedger,
    pitch_id: str,
    *,
    now: str | dt.date | dt.datetime | None = None,
) -> dict[str, Any]:
    local_now = parse_now(now)
    start, end = week_bounds(local_now)
    claimed_at = canonical_as_of(local_now)
    ledger.conn.execute("BEGIN IMMEDIATE")
    try:
        pitch = ledger.conn.execute("SELECT * FROM pitch_drafts WHERE pitch_id=?", (pitch_id,)).fetchone()
        if pitch is None:
            raise DigitalPRError("quota_pitch_missing")
        existing = ledger.conn.execute("SELECT * FROM quota_reservations WHERE pitch_id=?", (pitch_id,)).fetchone()
        if existing is not None:
            # A provider outcome may remain unresolved across the LA week boundary.
            # Reuse the original claim so recovery can only reconcile that exact
            # pitch; never consume a second week's slot or permit a second send.
            if existing["recipient"] != pitch["recipient"] or existing["observation_sha256"] != pitch["observation_sha256"]:
                raise DigitalPRError("quota_reservation_binding_conflict")
            ledger.conn.commit()
            return {**dict(existing), "idempotent_replay": True}
        placeholders = ",".join("?" for _ in COUNTED_QUOTA_STATES)
        count = int(ledger.conn.execute(
            f"SELECT COUNT(*) FROM quota_reservations WHERE week_start=? AND status IN ({placeholders})",
            (start, *COUNTED_QUOTA_STATES),
        ).fetchone()[0])
        if count >= WEEKLY_SEND_CAP:
            raise WeeklyQuotaExceeded("weekly_quota_exceeded")
        reservation_id = "qres_" + sha256_text(f"{pitch_id}|{start}")[:28]
        ledger.conn.execute(
            """INSERT INTO quota_reservations(
               reservation_id,pitch_id,week_start,week_end,recipient,observation_sha256,status,claimed_at
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (reservation_id, pitch_id, start, end, pitch["recipient"], pitch["observation_sha256"], "claimed", claimed_at),
        )
        ledger.conn.execute("UPDATE pitch_drafts SET status='send_claimed',updated_at=? WHERE pitch_id=?", (claimed_at, pitch_id))
        row = ledger.conn.execute("SELECT * FROM quota_reservations WHERE reservation_id=?", (reservation_id,)).fetchone()
        ledger.conn.commit()
        return {**dict(row), "idempotent_replay": False}
    except Exception:
        ledger.conn.rollback()
        raise


def _ensure_target_fresh_for_send(ledger: DigitalPRLedger, pitch: sqlite3.Row, now: dt.datetime) -> None:
    target = ledger.conn.execute("SELECT * FROM target_evidence WHERE target_version_id=?", (pitch["target_version_id"],)).fetchone()
    if target is None or int(target["eligible"]) != 1 or target["contact_address"] != pitch["recipient"]:
        raise DigitalPRError("send_target_not_eligible")
    if age_days(target["evidence_date"], now.date(), code="send_target_evidence_date_invalid") > MAX_TARGET_AGE_DAYS:
        raise DigitalPRError("send_target_evidence_expired")
    contact_age = target["contact_evidence_age_days"]
    validated_on = dt.date.fromisoformat(str(target["validated_on"]))
    if contact_age is None:
        raise DigitalPRError("send_contact_evidence_missing")
    original_contact_date = validated_on - dt.timedelta(days=int(contact_age))
    if (now.date() - original_contact_date).days > MAX_TARGET_AGE_DAYS:
        raise DigitalPRError("send_contact_evidence_expired")


def _default_mailbox() -> dict[str, Any]:
    from inbox_control_plane.control_plane import load_registry
    registry = load_registry()
    mailbox = next((row for row in registry["mailboxes"] if row["mailbox_id"] == MAILBOX_ID), None)
    if mailbox is None or mailbox.get("lifecycle") != "active" or str(mailbox.get("canonical_address") or "").lower() != SENDER:
        raise DigitalPRError("send_mailbox_not_active")
    return mailbox


def send_approved_pitch(
    ledger: DigitalPRLedger,
    pitch_id: str,
    decision_message_id: str,
    *,
    fetch_message: Callable[[str, str], dict[str, Any]],
    transport: Callable[..., dict[str, Any]] | None = None,
    mailbox: dict[str, Any] | None = None,
    now: str | dt.date | dt.datetime | None = None,
    receipt_root: Path = SEND_RECEIPT_ROOT,
) -> dict[str, Any]:
    existing = ledger.conn.execute("SELECT receipt_json FROM send_reconciliations WHERE pitch_id=?", (pitch_id,)).fetchone()
    if existing is not None:
        receipt = json.loads(existing["receipt_json"])
        return {**receipt, "idempotent_replay": True, "send_executed_now": False}
    local_now = parse_now(now)
    pitch = ledger.conn.execute("SELECT * FROM pitch_drafts WHERE pitch_id=?", (pitch_id,)).fetchone()
    if pitch is None:
        raise DigitalPRError("send_pitch_missing")
    run = ledger.conn.execute("SELECT status FROM gate_runs WHERE run_id=?", (pitch["run_id"],)).fetchone()
    if run is None or run["status"] != "NEWSWORTHY":
        raise DigitalPRError("send_gate_not_newsworthy")
    _ensure_target_fresh_for_send(ledger, pitch, local_now)
    approval = validate_operator_approval(
        ledger, pitch_id, decision_message_id, fetch_message=fetch_message, now=local_now,
    )
    reservation = reserve_weekly_quota(ledger, pitch_id, now=local_now)
    reservation_row = ledger.conn.execute("SELECT * FROM quota_reservations WHERE pitch_id=?", (pitch_id,)).fetchone()
    if reservation_row["status"] == "sent_verified":
        raise DigitalPRError("send_reconciliation_missing_for_verified_quota")
    allow_new_submission = reservation_row["status"] == "claimed"
    if transport is None:
        from inbox_control_plane.approved_gmail_sender import run_approved_gmail_new_thread_transport
        transport = run_approved_gmail_new_thread_transport
    mailbox = mailbox or _default_mailbox()
    binding = {
        "pitch_id": pitch_id,
        "body": pitch["body"],
        "body_sha256": pitch["body_sha256"],
        "subject": pitch["subject"],
        "subject_sha256": pitch["subject_sha256"],
        "recipient": pitch["recipient"],
        "recipient_sha256": pitch["recipient_sha256"],
        "recipient_display": "",
        "from_identity": SENDER,
        "observation_sha256": pitch["observation_sha256"],
        "claims_sha256": pitch["claims_sha256"],
        "approval_binding_sha256": approval["binding_sha256"],
        "decision_message_id": decision_message_id,
        "mailbox": mailbox,
    }

    def submission_started() -> None:
        stamp = canonical_as_of(local_now)
        ledger.conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = ledger.conn.execute(
                """UPDATE quota_reservations SET status='submission_started',submission_started_at=?
                   WHERE reservation_id=? AND status='claimed'""",
                (stamp, reservation["reservation_id"]),
            )
            if cursor.rowcount != 1:
                raise DigitalPRError("quota_submission_start_conflict")
            ledger.conn.commit()
        except Exception:
            ledger.conn.rollback()
            raise

    try:
        result = transport(
            binding,
            allow_new_submission=allow_new_submission,
            on_submission_start=submission_started,
        )
    except Exception as exc:
        if bool(getattr(exc, "submission_started", False)):
            with ledger.conn:
                ledger.conn.execute(
                    "UPDATE quota_reservations SET status='reconcile_required' WHERE reservation_id=? AND status!='sent_verified'",
                    (reservation["reservation_id"],),
                )
        raise
    checks = result.get("checks") or {}
    readback = result.get("readback") or {}
    provider_message_id = str(result.get("provider_message_id") or "")
    provider_thread_id = str(result.get("provider_thread_id") or "")
    outbound_rfc_message_id = str(readback.get("message_id") or "")
    if checks.get("all_passed") is not True or not provider_message_id or not provider_thread_id or not outbound_rfc_message_id:
        with ledger.conn:
            ledger.conn.execute(
                "UPDATE quota_reservations SET status='reconcile_required' WHERE reservation_id=? AND status!='sent_verified'",
                (reservation["reservation_id"],),
            )
        raise DigitalPRError("gmail_new_thread_readback_incomplete")
    reconciled_at = canonical_as_of(local_now)
    receipt = {
        "schema": "glp-digital-pr-send-receipt-v1",
        "status": "SENT_VERIFIED",
        "pitch_id": pitch_id,
        "reservation_id": reservation["reservation_id"],
        "week_start": reservation["week_start"],
        "week_end": reservation["week_end"],
        "approval": {
            "actor_id": OPERATOR_ID,
            "channel_id": DECISIONS_CHANNEL_ID,
            "decision_message_id": decision_message_id,
            "card_message_id": approval["card_message_id"],
            "binding_sha256": approval["binding_sha256"],
        },
        "immutable_pitch": approval_binding(pitch),
        "provider": result,
        "sender": SENDER,
        "recipient": pitch["recipient"],
        "provider_message_id": provider_message_id,
        "provider_thread_id": provider_thread_id,
        "outbound_rfc_message_id": outbound_rfc_message_id,
        "reconciled_at": reconciled_at,
        "effects": {"external_sends": 1, "provider_mutations": 1, "discord_posts": 0, "spend_cents": 0},
        "secret_printed": False,
    }
    receipt_path = receipt_root / f"{pitch_id}.json"
    receipt_raw = pretty_bytes(receipt)
    receipt_sha256 = sha256_bytes(receipt_raw)
    atomic_write(receipt_path, receipt_raw, mode=0o600, conflict_safe=True)
    ledger.conn.execute("BEGIN IMMEDIATE")
    try:
        ledger.conn.execute(
            """INSERT OR IGNORE INTO send_reconciliations(
               pitch_id,reservation_id,status,sender,recipient,provider_message_id,provider_thread_id,
               outbound_rfc_message_id,provider_internal_date,sent_receipt_path,sent_receipt_sha256,
               receipt_json,reconciled_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                pitch_id, reservation["reservation_id"], "sent_verified", SENDER, pitch["recipient"],
                provider_message_id, provider_thread_id, outbound_rfc_message_id,
                str(result.get("provider_internal_date") or ""), str(receipt_path), receipt_sha256,
                json.dumps(receipt, sort_keys=True), reconciled_at,
            ),
        )
        inserted = int(ledger.conn.execute("SELECT changes()").fetchone()[0])
        stored = ledger.conn.execute("SELECT provider_message_id,sent_receipt_sha256 FROM send_reconciliations WHERE pitch_id=?", (pitch_id,)).fetchone()
        if stored is None or stored["provider_message_id"] != provider_message_id or stored["sent_receipt_sha256"] != receipt_sha256:
            raise DigitalPRError("send_reconciliation_conflict")
        ledger.conn.execute(
            "UPDATE quota_reservations SET status='sent_verified',reconciled_at=? WHERE reservation_id=?",
            (reconciled_at, reservation["reservation_id"]),
        )
        ledger.conn.execute("UPDATE pitch_drafts SET status='sent_verified',updated_at=? WHERE pitch_id=?", (reconciled_at, pitch_id))
        if inserted:
            for name in ("external_sends", "provider_mutations"):
                ledger.conn.execute("UPDATE effect_counters SET value=value+1,updated_at=? WHERE name=?", (reconciled_at, name))
        ledger.conn.commit()
    except Exception:
        ledger.conn.rollback()
        raise
    return {**receipt, "idempotent_replay": False, "send_executed_now": bool(result.get("send_executed_now"))}


def run_weekly(
    *,
    now: str | dt.date | dt.datetime | None = None,
    pointer_path: Path = CONTENT_BASE_POINTER,
    targets_path: Path = TARGETS_PATH,
    db_path: Path = DB_PATH,
    receipt_path: Path = CURRENT_RECEIPT_PATH,
    releases_root: Path = BUSINESS / "site-system/flagship-v2/releases",
) -> dict[str, Any]:
    local_now = parse_now(now)
    content_base = resolve_content_base(pointer_path, releases_root=releases_root)
    normalized = normalize_price_observations(content_base.tracker)
    news_gate = evaluate_newsworthiness(normalized)
    target_document, target_raw = read_json(targets_path)
    target_sha256 = sha256_bytes(target_raw)
    targets = validate_target_packet(target_document, target_sha256, local_now)
    start, end = week_bounds(local_now)
    run_payload = {
        "schema": "glp-digital-pr-weekly-run-v1",
        "as_of_date": local_now.date().isoformat(),
        "as_of_local": canonical_as_of(local_now),
        "week_start": start,
        "week_end": end,
        "timezone": "America/Los_Angeles",
        "sources": {
            "content_base": {"path": str(pointer_path), "sha256": content_base.pointer_sha256},
            "release": {"path": str(content_base.release_root), "tree_sha256": content_base.release_tree_sha256},
            "price_tracker": {"path": str(content_base.price_tracker_path), "sha256": content_base.price_tracker_sha256},
            "targets": {"path": str(targets_path), "sha256": target_sha256},
        },
        "price_index_schema": normalized["schema"],
        "history_normalization": normalized["normalization"],
        "news_gate": news_gate,
        "target_policy_version": TARGET_POLICY_VERSION,
        "target_facts": {
            "researched": len(targets),
            "article_evidence_fresh": sum(bool(row["evidence_fresh"]) for row in targets),
            "addresses_eligible": sum(bool(row["eligible"]) for row in targets),
            # Contact-evidence hashes bind validator-accepted derived evidence tuples;
            # they are not hashes of locally retained raw HTTP response bytes.
            "contact_evidence_hashes_available": sum(
                bool(row["eligible"])
                and bool(HASH_RE.fullmatch(str(row["contact_evidence_sha256"] or "")))
                for row in targets
            ),
            "raw_capture_hashes_available": 0,
            "packet_hash_bound": True,
        },
    }
    with DigitalPRLedger(db_path) as ledger:
        run_id = ledger.record_run(run_payload, targets)
        drafts: list[dict[str, Any]] = []
        if news_gate["decision"] == "NEWSWORTHY":
            for target in [row for row in targets if row["eligible"]][:WEEKLY_SEND_CAP]:
                drafts.append(create_pitch_draft(ledger, run_id, target["target_version_id"]))
        status = news_gate["decision"]
        if status == "NEWSWORTHY" and not drafts:
            status = "NEWSWORTHY_NO_ELIGIBLE_CONTACTS"
        elif status == "NEWSWORTHY":
            status = "NEWSWORTHY_DRAFTS_READY"
        no_news_facts = []
        if news_gate["observation_count"] < 2:
            no_news_facts.append("no_prior_real_snapshot")
        for medication in MEDICATIONS:
            ratio = news_gate["current_metrics"][medication]["spread_multiple"]
            if _decimal(ratio) < Decimal("3"):
                no_news_facts.append(f"{medication}_spread_{ratio}x_below_3x")
        receipt = {
            "schema": "glp-digital-pr-current-zero-send-receipt-v1",
            "status": status,
            "run_id": run_id,
            "as_of_date": local_now.date().isoformat(),
            "as_of_local": canonical_as_of(local_now),
            "timezone": "America/Los_Angeles",
            "week": {"start": start, "end": end, "send_cap": WEEKLY_SEND_CAP},
            "sources": run_payload["sources"],
            "price_index": {
                "schema": normalized["schema"],
                "history_normalization": normalized["normalization"],
                "observation_count": news_gate["observation_count"],
                "latest_observed_on": news_gate["latest_observed_on"],
                "prior_observed_on": news_gate["prior_observed_on"],
                "observation_sha256": news_gate["observation_sha256"],
                "current_metrics": news_gate["current_metrics"],
            },
            "news_gate": {
                "version": GATE_VERSION,
                "decision": news_gate["decision"],
                "matched_reasons": news_gate["matched_reasons"],
                "matched_facts": no_news_facts if not news_gate["matched_reasons"] else news_gate["matched_reasons"],
                "thresholds": news_gate["thresholds"],
            },
            "targets": {
                **run_payload["target_facts"],
                "eligible_target_ids": [row["target_id"] for row in targets if row["eligible"]],
                "ineligible_reason_counts": {
                    reason: sum(reason in row["ineligible_reasons"] for row in targets)
                    for reason in sorted({reason for row in targets for reason in row["ineligible_reasons"]})
                },
            },
            "drafts": {"created": len(drafts), "sent": 0},
            "send_receipts_created": 0,
            "effects_this_run": {
                "external_sends": 0,
                "provider_mutations": 0,
                "discord_posts": 0,
                "deployments": 0,
                "release_pointer_mutations": 0,
                "cron_registrations": 0,
                "spend_cents": 0,
                "secret_reads": 0,
            },
            "cumulative_effect_counters": ledger.effects(),
            "sender_if_future_approved": SENDER,
            "operator_approval_required_per_pitch": True,
            "send_receipt_fabricated": False,
            "production_deployed": False,
        }
        if news_gate["decision"] == "NO_NEWS_NO_PITCH":
            receipt_sha256 = ledger.record_no_news_receipt(run_id, receipt_path, receipt)
        else:
            receipt_raw = pretty_bytes(receipt)
            atomic_write(receipt_path, receipt_raw, mode=0o644, conflict_safe=True)
            receipt_sha256 = sha256_bytes(receipt_raw)
        return {**receipt, "receipt_path": str(receipt_path), "receipt_sha256": receipt_sha256, "db_path": str(db_path)}


def _live_discord_fetch(channel_id: str, message_id: str) -> dict[str, Any]:
    from inbox_control_plane.control_plane import discord_request
    result = discord_request(f"/channels/{channel_id}/messages/{message_id}")
    if not isinstance(result, dict):
        raise DigitalPRError("discord_message_readback_invalid")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command")
    weekly = subparsers.add_parser("weekly", help="evaluate the provider-free weekly gate")
    weekly.add_argument("--now")
    weekly.add_argument("--pointer", type=Path, default=CONTENT_BASE_POINTER)
    weekly.add_argument("--targets", type=Path, default=TARGETS_PATH)
    weekly.add_argument("--db", type=Path, default=DB_PATH)
    weekly.add_argument("--receipt", type=Path, default=CURRENT_RECEIPT_PATH)
    card = subparsers.add_parser("approval-card", help="render a local exact approval card without posting")
    card.add_argument("--pitch-id", required=True)
    card.add_argument("--db", type=Path, default=DB_PATH)
    send = subparsers.add_parser("send", help="send one exact approved pitch after live approval readback")
    send.add_argument("--pitch-id", required=True)
    send.add_argument("--decision-message-id", required=True)
    send.add_argument("--db", type=Path, default=DB_PATH)
    send.add_argument("--now")
    args = parser.parse_args(argv)
    command = args.command or "weekly"
    if command == "weekly":
        result = run_weekly(
            now=getattr(args, "now", None), pointer_path=getattr(args, "pointer", CONTENT_BASE_POINTER),
            targets_path=getattr(args, "targets", TARGETS_PATH), db_path=getattr(args, "db", DB_PATH),
            receipt_path=getattr(args, "receipt", CURRENT_RECEIPT_PATH),
        )
    elif command == "approval-card":
        with DigitalPRLedger(args.db) as ledger:
            pitch = ledger.conn.execute("SELECT * FROM pitch_drafts WHERE pitch_id=?", (args.pitch_id,)).fetchone()
            if pitch is None:
                raise DigitalPRError("pitch_not_found")
            content, digest = render_approval_card(pitch)
            result = {"status": "LOCAL_CARD_ONLY", "pitch_id": args.pitch_id, "binding_sha256": digest, "content": content, "discord_posts": 0}
    else:
        with DigitalPRLedger(args.db) as ledger:
            result = send_approved_pitch(
                ledger, args.pitch_id, args.decision_message_id,
                fetch_message=_live_discord_fetch, now=args.now,
            )
    print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DigitalPRError as exc:
        print(json.dumps({"status": "BLOCKED", "reason": str(exc), "effects": {"external_sends": 0}}, sort_keys=True), file=sys.stderr)
        raise SystemExit(2)
