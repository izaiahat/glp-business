#!/usr/bin/env python3
"""Zero-service Loom intake, view-alert, transcript, and reporting rails.

All owner/investor/patient sends are out of scope. Discord writes are internal
operating receipts only. Loom video metadata comes from the public oEmbed route;
view notifications come from Loom email or an explicit Engagement CSV import.
"""
from __future__ import annotations

import argparse
import csv
import fcntl
import hashlib
import html
import json
import os
import re
import shutil
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, "/home/ubuntu/business/ops")
from discord_fmt import kvline as fmt_kvline, lint as fmt_lint, meaning as fmt_meaning, render as fmt_render, subtext as fmt_subtext, title as fmt_title

BORING_SCRIPT_DIR = Path("/home/ubuntu/business/ventures/boring-ai/holistic-machine/scripts")
if str(BORING_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(BORING_SCRIPT_DIR))
import boring_ai_loom_recording_order as recording_order

ROOT = Path("/home/ubuntu/business")
CONFIG_PATH = ROOT / "ops/loom/rails.json"
REGISTRY_PATH = ROOT / "ops/loom/video-registry.jsonl"
EVENTS_PATH = ROOT / "ops/loom/events.jsonl"
STATE_PATH = ROOT / "ops/loom/watcher-state.json"
GUARD_STATE_PATH = ROOT / "ops/loom/guard-events-state.json"
GUARD_EVENTS_PATH = ROOT / "ops/loom/guard-events.jsonl"
GUARD_SUMMARY_STATE_PATH = ROOT / "ops/loom/guard-summary-state.json"
LOCK_PATH = ROOT / "ops/loom/watcher.lock"
OUTCOMES_PATH = ROOT / "ops/outcomes.jsonl"
TRANSCRIPT_DIR = ROOT / "ops/loom/transcripts"
REPORT_DIR = ROOT / "reports/loom-automation-20260709"
TEMPLATE_PATH = ROOT / "site-system/flagship-v2/templates/loom-hero.html"
ENV_PATHS = [ROOT / "secrets/loom/loom.env", ROOT / ".env", Path("/home/ubuntu/.hermes/.env")]
LOOM_URL_RE = re.compile(r"https?://(?:www\.)?loom\.com/(?:share|embed)/([A-Za-z0-9_-]+)(?:[^\s<>)]*)?", re.I)
OPP_RE = re.compile(r"\bopp_[a-z0-9]+\b", re.I)
WATCH_RE = re.compile(r"\b(?:watched|viewed|saw|opened)\b", re.I)
PERCENT_RE = re.compile(r"\b(100|[1-9]?\d)\s*%")
LOOM_SENDER_RE = re.compile(r"(?:^|[.@])loom\.com$|atlassian\.com$", re.I)
GUARD_ACTIONS = (
    "loom_video_created",
    "loom_video_viewed",
    "loom_video_downloaded",
    "loom_video_comment",
    "loom_video_reaction",
    "loom_video_direct_share",
)
GUARD_RETENTION_DAYS = 180
GUARD_INITIAL_LOOKBACK_DAYS = 179
GUARD_ARM_ENV = "LOOM_GUARD_EVENTS_ARMED"
ENGAGEMENT_LEDGER_PATH = ROOT / "ops/loom/engagement-ledger.jsonl"
BORING_DB_PATH = ROOT / "ventures/boring-ai/holistic-machine/data/opportunities.sqlite3"
BORING_EXPORT_PATH = ROOT / "ventures/boring-ai/holistic-machine/exports/manual_delivery_queue.csv"
READY_MANUAL_STATUS_BASE = "ready_manual_only"


def ready_manual_status_predicate(column: str = "status") -> tuple[str, tuple[str, str, str]]:
    """Return a bound SQL predicate for legacy and numeric-version manual packages."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", column):
        raise ValueError("invalid delivery status SQL column")
    predicate = f"({column}=? OR ({column} GLOB ? AND {column} NOT GLOB ?))"
    return predicate, (
        READY_MANUAL_STATUS_BASE,
        READY_MANUAL_STATUS_BASE + "_v[0-9]*",
        READY_MANUAL_STATUS_BASE + "_v*[^0-9]*",
    )


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def load_config(path: Path | None = None) -> dict[str, Any]:
    path = path or CONFIG_PATH
    data = load_json(path, {})
    if not isinstance(data, dict):
        raise RuntimeError(f"invalid Loom rails config: {path}")
    return data


def load_env() -> None:
    for path in ENV_PATHS:
        if not path.exists():
            continue
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def first_env(*names: str) -> str:
    load_env()
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def load_loom_admin_credentials(path: Path | None = None) -> tuple[str, str]:
    """Read admin auth from the approved 0600 Loom secret only."""
    path = path or (ROOT / "secrets/loom/loom.env")
    if not path.is_file():
        raise RuntimeError("Loom admin credential file unavailable")
    if path.stat().st_mode & 0o077:
        raise RuntimeError("Loom admin credential file must be mode 0600")
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() in {"LOOM_ORG_ID", "LOOM_ADMIN_API_KEY"}:
            values[key.strip()] = value.strip().strip('"').strip("'")
    if not values.get("LOOM_ORG_ID") or not values.get("LOOM_ADMIN_API_KEY"):
        raise RuntimeError("Loom admin credentials incomplete")
    return values["LOOM_ORG_ID"], values["LOOM_ADMIN_API_KEY"]


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.write(json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            item = json.loads(line)
            if isinstance(item, dict):
                rows.append(item)
        except Exception:
            continue
    return rows


def canonical_loom_url(value: str) -> tuple[str, str]:
    match = LOOM_URL_RE.search(value or "")
    if not match:
        raise ValueError("no valid loom.com/share or loom.com/embed URL found")
    video_id = match.group(1)
    return f"https://www.loom.com/share/{video_id}", video_id


def fetch_oembed(url: str, *, timeout: int = 30, opener=urllib.request.urlopen) -> dict[str, Any]:
    canonical, video_id = canonical_loom_url(url)
    endpoint = "https://www.loom.com/v1/oembed?" + urllib.parse.urlencode({"format": "json", "url": canonical})
    req = urllib.request.Request(endpoint, headers={"User-Agent": "Kael-Loom-Rails/1.0"})
    with opener(req, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict) or not payload.get("html"):
        raise RuntimeError("Loom oEmbed returned no embed HTML")
    return {
        "video_id": video_id,
        "share_url": canonical,
        "title": str(payload.get("title") or "Untitled Loom").strip(),
        "author_name": str(payload.get("author_name") or "").strip(),
        "thumbnail_url": str(payload.get("thumbnail_url") or "").strip(),
        "duration_seconds": int(payload.get("duration") or 0),
        "width": int(payload.get("width") or 0),
        "height": int(payload.get("height") or 0),
        "embed_html": str(payload.get("html") or "").strip(),
        "provider_name": str(payload.get("provider_name") or "Loom"),
    }


def send_sheet_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def update_send_sheet(
    path: Path,
    opportunity_id: str,
    metadata: dict[str, Any],
    transcript_path: str = "",
    *,
    package: dict[str, Any] | None = None,
) -> dict[str, str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("w") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            fields = list(reader.fieldnames or [])
            rows = list(reader)
        matches = [row for row in rows if row.get("opportunity_id") == opportunity_id]
        if len(matches) > 1:
            raise RuntimeError(f"expected one send-sheet row for {opportunity_id}; found {len(matches)}")
        if matches:
            target = matches[0]
        elif package is not None:
            target = {field: "" for field in fields}
            target.update({
                "opportunity_id": opportunity_id,
                "owner_name": str(package.get("owner_name") or ""),
                "business_name": str(package.get("business_name") or ""),
                "verbatim_ask": str(package.get("verbatim_ask") or ""),
                "public_preview_url": str(package.get("preview_url") or ""),
                "recommended_channel": str(package.get("recommended_channel") or ""),
                "contact_route": str(package.get("contact_route") or ""),
                "paste_ready_message": str(package.get("delivery_copy") or ""),
                "package_pitch": str(package.get("package_pitch") or ""),
                "apollo_status": "canonical_v2_guard_capture",
            })
            rows.append(target)
        else:
            raise RuntimeError(f"expected one send-sheet row for {opportunity_id}; found 0")
        extra = [
            "loom_url", "loom_video_id", "loom_title", "loom_duration_seconds",
            "loom_thumbnail_url", "loom_captured_at", "loom_transcript_path",
        ]
        for field in extra:
            if field not in fields:
                fields.append(field)
        target.update({
            "loom_url": str(metadata.get("share_url") or target.get("loom_url") or ""),
            "loom_video_id": str(metadata.get("video_id") or target.get("loom_video_id") or ""),
            "loom_title": str(metadata.get("title") or target.get("loom_title") or ""),
            "loom_duration_seconds": str(metadata.get("duration_seconds") or target.get("loom_duration_seconds") or ""),
            "loom_thumbnail_url": str(metadata.get("thumbnail_url") or target.get("loom_thumbnail_url") or ""),
            "loom_captured_at": utc_now(),
            "loom_transcript_path": transcript_path or target.get("loom_transcript_path") or "",
        })
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        os.replace(tmp, path)
        return dict(target)


def _update_boring_export(
    path: Path,
    opportunity_id: str,
    metadata: dict[str, Any],
    *,
    package: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Wire a Loom into the operator-facing CSV without authorizing a send."""
    if not path.is_file():
        return {}
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("w") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            fields = list(reader.fieldnames or [])
            rows = list(reader)
        matches = [row for row in rows if row.get("opportunity_id") == opportunity_id]
        if len(matches) > 1:
            raise RuntimeError(f"expected one manual-delivery row for {opportunity_id}; found {len(matches)}")
        if matches:
            target = matches[0]
        elif package is not None:
            target = {field: "" for field in fields}
            target.update({
                "opportunity_id": opportunity_id,
                "business_name": str(package.get("business_name") or ""),
                "source_url": str(package.get("source_url") or ""),
                "original_ask": str(package.get("original_ask") or ""),
                "preview_url_or_path": str(package.get("preview_url_or_path") or ""),
                "manual_delivery_copy": str(package.get("manual_delivery_copy") or ""),
                "operator_decision": str(package.get("operator_decision") or "pending"),
            })
            rows.append(target)
        else:
            raise RuntimeError(f"expected one manual-delivery row for {opportunity_id}; found 0")
        for field in ["loom_url", "loom_video_id", "loom_title", "loom_duration_seconds", "loom_captured_at"]:
            if field not in fields:
                fields.append(field)
        loom_url = str(metadata.get("share_url") or "")
        copy = str(
            target.get("manual_delivery_copy")
            or ((package or {}).get("manual_delivery_copy"))
            or ""
        )
        if "[PASTE LOOM URL]" in copy:
            copy = copy.replace("[PASTE LOOM URL]", loom_url)
        target.update({
            "manual_delivery_copy": copy,
            "loom_url": loom_url,
            "loom_video_id": str(metadata.get("video_id") or ""),
            "loom_title": str(metadata.get("title") or ""),
            "loom_duration_seconds": str(metadata.get("duration_seconds") or ""),
            "loom_captured_at": utc_now(),
        })
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        os.replace(tmp, path)
        return dict(target)


def _payload_loom(payload: dict[str, Any]) -> dict[str, Any]:
    value = payload.get("loom")
    if isinstance(value, dict):
        return value
    if payload.get("loom_video_id"):
        return {"video_id": payload.get("loom_video_id"), "share_url": payload.get("loom_url")}
    return {}


def boring_delivery_for_video(video_id: str, db_path: Path | None = None) -> str:
    """Resolve an already-attached video for idempotent Guard event retries."""
    db_path = db_path or BORING_DB_PATH
    if not video_id or not db_path.is_file():
        return ""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    try:
        status_sql, status_params = ready_manual_status_predicate()
        rows = conn.execute(
            f"""
            SELECT opportunity_id,instructions_json
            FROM delivery_instructions
            WHERE {status_sql}
            """,
            status_params,
        ).fetchall()
    finally:
        conn.close()
    matches: list[str] = []
    for row in rows:
        try:
            payload = json.loads(row["instructions_json"] or "{}")
        except Exception:
            continue
        if str(_payload_loom(payload).get("video_id") or "") == video_id:
            matches.append(str(row["opportunity_id"]))
    unique = list(dict.fromkeys(matches))
    if len(unique) > 1:
        raise RuntimeError(f"Loom video is attached to multiple Boring AI packages: {video_id}")
    return unique[0] if unique else ""


def attach_loom_to_boring_queue(opportunity_id: str, metadata: dict[str, Any], db_path: Path | None = None) -> dict[str, Any]:
    """Attach a detected Loom to the canonical manual-send package and queue surfaces."""
    db_path = db_path or BORING_DB_PATH
    if not db_path.is_file():
        raise RuntimeError("Boring AI opportunity database unavailable")
    video_id = str(metadata.get("video_id") or "")
    loom_url = str(metadata.get("share_url") or "")
    if not video_id or not loom_url:
        raise RuntimeError("Loom metadata incomplete for Boring AI queue attachment")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        status_sql, status_params = ready_manual_status_predicate("di.status")
        rows = conn.execute(
            f"""
            SELECT di.*,o.business_name AS business_name
            FROM delivery_instructions di
            JOIN opportunities o ON o.id=di.opportunity_id
            WHERE di.opportunity_id=?
              AND {status_sql}
            ORDER BY di.created_at DESC
            """,
            (opportunity_id, *status_params),
        ).fetchall()
        if len(rows) != 1:
            raise RuntimeError(f"expected one ready manual package for {opportunity_id}; found {len(rows)}")
        row = rows[0]
        try:
            payload = json.loads(row["instructions_json"] or "{}")
        except Exception as exc:
            raise RuntimeError(f"invalid delivery package JSON for {opportunity_id}") from exc
        primary_method = str(payload.get("primary_method") or "")
        delivery_columns = {
            str(info[1]) for info in conn.execute("PRAGMA table_info(delivery_log)").fetchall()
        }
        channel_clause = " AND channel=?" if "channel" in delivery_columns else ""
        order_parts = [name for name in ("created_at", "id") if name in delivery_columns]
        order_clause = " ORDER BY " + ",".join(name + " DESC" for name in order_parts) if order_parts else ""
        delivery_params: tuple[Any, ...] = (opportunity_id, primary_method) if channel_clause else (opportunity_id,)
        delivery_rows = conn.execute(
            "SELECT * FROM delivery_log WHERE opportunity_id=? AND status='not_contacted'"
            + channel_clause + order_clause,
            delivery_params,
        ).fetchall()
        if len(delivery_rows) != 1:
            raise RuntimeError(
                f"expected one pending {primary_method or 'primary'} delivery log "
                f"for {opportunity_id}; found {len(delivery_rows)}"
            )
        delivery_row = delivery_rows[0]
        existing = _payload_loom(payload)
        if existing.get("video_id") and str(existing.get("video_id")) != video_id:
            raise RuntimeError(f"different Loom already attached to {opportunity_id}")
        template = str(
            payload.get("delivery_copy_template")
            or payload.get("delivery_copy")
            or payload.get("dm_copy")
            or ""
        )
        rendered = template.replace("[PASTE LOOM URL]", loom_url).replace("{LOOM_URL}", loom_url)
        loom_payload = {
            "video_id": video_id,
            "share_url": loom_url,
            "title": str(metadata.get("title") or ""),
            "duration_seconds": int(metadata.get("duration_seconds") or 0),
            "thumbnail_url": str(metadata.get("thumbnail_url") or ""),
            "captured_at": utc_now(),
            "capture_source": "atlassian_guard_auto",
        }
        payload.update({
            "delivery_copy_template": template,
            "delivery_copy": rendered,
            "loom": loom_payload,
            "loom_url": loom_url,
            "loom_video_id": video_id,
            "loom_capture_mode": "atlassian_guard_auto",
            "operator_posts_loom_in_delivery_channel": False,
            "external_owner_send_executed": False,
        })
        ts = utc_now()
        conn.execute(
            "UPDATE delivery_instructions SET instructions_json=?,updated_at=? WHERE id=?",
            (json.dumps(payload, sort_keys=True), ts, row["id"]),
        )
        conn.execute(
            "UPDATE delivery_log SET delivery_copy=?,operator_action_required=? WHERE id=?",
            (rendered, "Loom auto-captured; Izaiah verifies the source post and sends manually. Machine never sends.", delivery_row["id"]),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    export_row = _update_boring_export(
        BORING_EXPORT_PATH,
        opportunity_id,
        metadata,
        package={
            "business_name": str(row["business_name"] or ""),
            "source_url": str(payload.get("source_url") or ""),
            "original_ask": str(payload.get("need") or ""),
            "preview_url_or_path": str(payload.get("preview_url") or row["approved_preview_url"] or ""),
            "manual_delivery_copy": rendered,
            "operator_decision": "pending",
        },
    )
    return {
        "status": "attached",
        "opportunity_id": opportunity_id,
        "video_id": video_id,
        "delivery_instruction_id": str(row["id"]),
        "manual_export_updated": bool(export_row),
        "operator_card_message_id": str(payload.get("loom_operator_card_message_id") or ""),
        "operator_package": {
            "owner_name": str(payload.get("prospect_name") or ""),
            "business_name": str(row["business_name"] or "Boring AI prospect"),
            "source_url": str(payload.get("source_url") or ""),
            "preview_url": str(payload.get("preview_url") or row["approved_preview_url"] or ""),
            "loom_url": loom_url,
            "delivery_copy": rendered,
            "verbatim_ask": str(payload.get("need") or ""),
            "recommended_channel": str(payload.get("primary_method") or ""),
            "contact_route": str(payload.get("primary_destination") or ""),
            "package_pitch": str(payload.get("pricing_url") or ""),
        },
        "external_owner_send_executed": False,
    }


def render_boring_capture_card(package: dict[str, Any]) -> str:
    business = re.sub(r"\s+", " ", str(package.get("business_name") or "Boring AI prospect")).strip()[:100]
    source = str(package.get("source_url") or "").strip()
    preview = str(package.get("preview_url") or "").strip()
    loom_url = str(package.get("loom_url") or "").strip()
    copy = str(package.get("delivery_copy") or "").strip()
    method = str(package.get("recommended_channel") or "").strip()
    route_label = {
        "manual_linkedin_dm": "Manual LinkedIn DM",
        "manual_meta_dm_or_best_route": "Manual Meta DM",
        "manual_meta_dm": "Manual Meta DM",
        "manual_email": "Manual email",
    }.get(method, "Manual route")
    contact_route = str(package.get("contact_route") or source).strip()
    if not preview.startswith("https://") or not loom_url.startswith("https://") or not copy:
        raise RuntimeError("captured Loom package is missing preview, Loom, or delivery copy")
    source_line = source if source.startswith("https://") else "Source route missing — do not send; reply BLOCKED."
    next_step = (
        "→ Verify the source post is still live and matches, then send manually. Reply `SENT` or `BLOCKED`."
        if source.startswith("https://")
        else "→ Do not send. Reply `BLOCKED` so the missing source route can be repaired."
    )
    card = "\n".join([
        f"🕵️ **Boring Delivery** — {business} Loom ready",
        "",
        f"**Send here:** {route_label}",
        contact_route,
        source_line,
        "**Approved preview:** " + preview,
        "**Recorded Loom:** " + loom_url,
        "",
        "**Paste-ready message:**",
        f"“{copy}”",
        "",
        next_step,
        "-# No prospect message was sent automatically.",
    ])
    if len(card) > 1900:
        raise RuntimeError(f"captured Loom delivery card exceeds Discord cap: {len(card)}")
    return card


def registry_key(video_id: str, venture: str, opportunity_id: str) -> str:
    return hashlib.sha256(f"{video_id}|{venture}|{opportunity_id}".encode()).hexdigest()


def upsert_registry(record: dict[str, Any], path: Path | None = None) -> tuple[dict[str, Any], bool]:
    path = path or REGISTRY_PATH
    existing = read_jsonl(path)
    key = registry_key(str(record["video_id"]), str(record["venture"]), str(record["opportunity_id"]))
    for item in reversed(existing):
        if item.get("registry_key") == key:
            return item, False
    record = {"registry_key": key, "captured_at": utc_now(), "no_external_send": True, **record}
    append_jsonl(path, record)
    return record, True


def discord_token() -> str:
    load_env()
    token = os.environ.get("DISCORD_BOT_TOKEN", "")
    if not token:
        raise RuntimeError("Discord bot credential unavailable")
    return token


def discord_api(method: str, path: str, payload: dict[str, Any] | None = None, *, attempts: int = 4) -> Any:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    for attempt in range(attempts):
        req = urllib.request.Request(
            "https://discord.com/api/v10" + path,
            data=data,
            method=method,
            headers={
                "Authorization": "Bot " + discord_token(),
                "Content-Type": "application/json",
                "User-Agent": "DiscordBot (https://tryboringai.com, 1.0)",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=25) as response:
                raw = response.read().decode("utf-8", "replace")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            body = exc.read(1200).decode("utf-8", "replace")
            if exc.code == 429 and attempt < attempts - 1:
                try:
                    delay = float(json.loads(body).get("retry_after", 1))
                except Exception:
                    delay = 1
                time.sleep(delay + 0.2)
                continue
            raise RuntimeError(f"Discord HTTP {exc.code}: {body[:240]}") from exc
    raise RuntimeError("Discord retry exhausted")


def discord_send(channel_id: str, content: str) -> list[str]:
    message = content.strip()
    lint_result = fmt_lint(message, output_class="actionable")
    if lint_result.get("status") != "pass":
        raise RuntimeError("Discord house-style lint rejected Loom output: " + ",".join(lint_result.get("errors") or []))
    result = discord_api(
        "POST", f"/channels/{channel_id}/messages",
        {"content": message, "allowed_mentions": {"parse": []}},
    )
    return [str(result.get("id") or "")]


def _persist_boring_capture_card_message(opportunity_id: str, video_id: str, message_id: str) -> None:
    conn = sqlite3.connect(BORING_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        status_sql, status_params = ready_manual_status_predicate()
        row = conn.execute(
            f"SELECT id,instructions_json FROM delivery_instructions WHERE opportunity_id=? AND {status_sql}",
            (opportunity_id, *status_params),
        ).fetchone()
        if not row:
            raise RuntimeError("captured Loom delivery instruction disappeared")
        payload = json.loads(row["instructions_json"] or "{}")
        if str(_payload_loom(payload).get("video_id") or "") != video_id:
            raise RuntimeError("captured Loom card persistence video mismatch")
        existing = str(payload.get("loom_operator_card_message_id") or "")
        if existing and existing != message_id:
            raise RuntimeError("different captured Loom operator card already recorded")
        payload["loom_operator_card_message_id"] = message_id
        payload["loom_operator_card_verified_at"] = utc_now()
        with conn:
            conn.execute(
                "UPDATE delivery_instructions SET instructions_json=?,updated_at=? WHERE id=?",
                (json.dumps(payload, sort_keys=True), utc_now(), row["id"]),
            )
    finally:
        conn.close()


def ensure_boring_capture_card(queue_attachment: dict[str, Any], channel_id: str) -> str:
    package = queue_attachment.get("operator_package") or {}
    content = render_boring_capture_card(package)
    message_id = str(queue_attachment.get("operator_card_message_id") or "")
    if message_id:
        live = discord_api("GET", f"/channels/{channel_id}/messages/{message_id}")
        if str(live.get("content") or "") != content:
            raise RuntimeError("captured Loom operator card readback mismatch")
        return message_id
    recent = discord_api("GET", f"/channels/{channel_id}/messages?limit=100")
    exact = [str(item.get("id") or "") for item in recent if str(item.get("content") or "") == content]
    if len(exact) > 1:
        raise RuntimeError("duplicate captured Loom operator cards found")
    message_id = exact[0] if exact else discord_send(channel_id, content)[0]
    live = discord_api("GET", f"/channels/{channel_id}/messages/{message_id}")
    if str(live.get("content") or "") != content:
        raise RuntimeError("captured Loom operator card exact readback failed")
    _persist_boring_capture_card_message(
        str(queue_attachment.get("opportunity_id") or ""),
        str(queue_attachment.get("video_id") or ""),
        message_id,
    )
    return message_id


def render_hero(metadata: dict[str, Any], *, booking_html: str = "") -> str:
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    replacements = {
        "{{LOOM_TITLE}}": html.escape(str(metadata.get("title") or "Video walkthrough")),
        "{{LOOM_SHARE_URL}}": html.escape(str(metadata.get("share_url") or ""), quote=True),
        "{{LOOM_EMBED_URL}}": html.escape(f"https://www.loom.com/embed/{metadata.get('video_id', '')}", quote=True),
        "{{LOOM_THUMBNAIL_URL}}": html.escape(str(metadata.get("thumbnail_url") or ""), quote=True),
        "{{LOOM_DURATION}}": html.escape(str(metadata.get("duration_seconds") or "")),
        "{{BOOKING_EMBED}}": booking_html,
    }
    for needle, value in replacements.items():
        template = template.replace(needle, value)
    return template


def ingest_url(url: str, *, venture: str, opportunity_id: str, source: str, no_discord: bool = False, oembed_fetcher=fetch_oembed) -> dict[str, Any]:
    cfg = load_config()
    rail = (cfg.get("ventures") or {}).get(venture)
    if not isinstance(rail, dict):
        raise RuntimeError(f"unknown Loom rail venture={venture}")
    if venture == "glp" and bool(rail.get("armed")):
        raise RuntimeError("GLP Loom rail must remain build-not-arm until a receipted C5 GO")
    metadata = oembed_fetcher(url)
    max_seconds = int(rail.get("max_duration_seconds") or 0)
    if max_seconds and metadata.get("duration_seconds") and int(metadata["duration_seconds"]) > max_seconds:
        raise RuntimeError(f"Loom duration {metadata['duration_seconds']}s exceeds {max_seconds}s rail limit")
    sheet_value = str(rail.get("send_sheet") or "")
    sheet = Path(sheet_value) if sheet_value else None
    row: dict[str, str] = {}
    queue_attachment: dict[str, Any] = {}
    if venture == "boring_ai":
        queue_attachment = attach_loom_to_boring_queue(opportunity_id, metadata)
    if sheet:
        row = update_send_sheet(
            sheet,
            opportunity_id,
            metadata,
            package=queue_attachment.get("operator_package") if venture == "boring_ai" else None,
        )
    record, created = upsert_registry({
        **metadata,
        "venture": venture,
        "opportunity_id": opportunity_id,
        "source": source,
        "send_sheet": str(sheet) if sheet else "",
        "business_name": row.get("business_name", ""),
        "owner_name": row.get("owner_name", ""),
        "rail_armed": bool(rail.get("armed")),
    })
    delivery_ledger = Path(str(rail.get("delivery_ledger") or REGISTRY_PATH))
    if created and delivery_ledger != REGISTRY_PATH:
        append_jsonl(delivery_ledger, {"event": "loom_link_captured", **record})
    hero_path = REPORT_DIR / "rendered-heros" / f"{opportunity_id}.html"
    hero_path.parent.mkdir(parents=True, exist_ok=True)
    hero_path.write_text(render_hero(metadata), encoding="utf-8")
    message_ids: list[str] = []
    channel_id = str(rail.get("channel_id") or "")
    if venture == "boring_ai" and channel_id and not no_discord:
        message_ids = [ensure_boring_capture_card(queue_attachment, channel_id)]
    elif created and channel_id and not no_discord:
        message_ids = discord_send(channel_id, fmt_render(
            fmt_title("watch", "Loom Intake", f"1 {venture.replace('_', ' ')} video filed"),
            fmt_kvline(("opportunity ID shown", False), ("duration", f"{metadata['duration_seconds']}s"), ("external sends", 0)),
            fmt_meaning("no action — the internal registry and send sheet were updated; no owner, investor, or patient was contacted"),
            fmt_subtext(f"artifact directory: {hero_path.parent}"),
        ))
    return {
        "status": "captured" if created else "duplicate",
        "venture": venture,
        "opportunity_id": opportunity_id,
        "video_id": metadata["video_id"],
        "title": metadata["title"],
        "duration_seconds": metadata["duration_seconds"],
        "hero_path": str(hero_path),
        "discord_message_ids": message_ids,
        "queue_attachment": queue_attachment,
        "no_external_send": True,
        "secret_printed": False,
    }


def find_opportunity(content: str, referenced_content: str = "") -> str:
    matches = OPP_RE.findall((content or "") + "\n" + (referenced_content or ""))
    unique = list(dict.fromkeys(x.lower() for x in matches))
    if len(unique) != 1:
        raise RuntimeError(f"link capture requires exactly one opp_id; found {len(unique)}")
    return unique[0]


def watch_discord(*, no_discord: bool = False, dry_run: bool = False) -> dict[str, Any]:
    cfg = load_config()
    rail = cfg["ventures"]["boring_ai"]
    channel_id = str(rail["channel_id"])
    state = load_json(STATE_PATH, {"discord_after": "0"})
    after = str(state.get("discord_after") or "0")
    messages = discord_api("GET", f"/channels/{channel_id}/messages?after={after}&limit=100")
    messages = sorted(messages, key=lambda item: int(item.get("id") or 0))
    results: list[dict[str, Any]] = []
    newest = after
    for message in messages:
        newest = max(newest, str(message.get("id") or "0"), key=int)
        if message.get("author", {}).get("bot"):
            continue
        content = str(message.get("content") or "")
        if not LOOM_URL_RE.search(content):
            continue
        referenced = ""
        ref_id = str((message.get("message_reference") or {}).get("message_id") or "")
        if ref_id:
            try:
                referenced = str(discord_api("GET", f"/channels/{channel_id}/messages/{ref_id}").get("content") or "")
            except Exception:
                referenced = ""
        try:
            opportunity_id = find_opportunity(content, referenced)
            if dry_run:
                results.append({"status": "would_capture", "opportunity_id": opportunity_id, "message_id": message.get("id")})
            else:
                results.append(ingest_url(content, venture="boring_ai", opportunity_id=opportunity_id, source=f"discord:{message.get('id')}", no_discord=no_discord))
        except Exception as exc:
            results.append({"status": "blocked", "message_id": message.get("id"), "error": f"{type(exc).__name__}: {exc}"[:300]})
    if not dry_run:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps({**state, "discord_after": newest, "updated_at": utc_now()}, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, STATE_PATH)
    return {"status": "pass", "checked": len(messages), "results": results, "no_external_send": True, "secret_printed": False}


def mail_record_dict(record: Any) -> dict[str, str]:
    raw = asdict(record) if is_dataclass(record) else dict(record)
    return {key: str(raw.get(key) or "") for key in ["from_email", "from_header", "subject", "body", "message_id", "gmail_msgid"]}


def match_registered_video(subject: str, body: str) -> dict[str, Any] | None:
    text = f"{subject}\n{body}"
    url_match = LOOM_URL_RE.search(text)
    registry = read_jsonl(REGISTRY_PATH)
    if url_match:
        video_id = url_match.group(1)
        for item in reversed(registry):
            if str(item.get("video_id")) == video_id:
                return item
    low = text.lower()
    candidates = [item for item in registry if str(item.get("title") or "").lower() in low and item.get("title")]
    return candidates[-1] if len(candidates) == 1 else None


def parse_view_notification(record: Any) -> dict[str, Any] | None:
    mail = mail_record_dict(record)
    sender = mail["from_email"].lower().strip()
    if not LOOM_SENDER_RE.search(sender):
        return None
    text = f"{mail['subject']}\n{mail['body']}"
    if not WATCH_RE.search(text):
        return None
    video = match_registered_video(mail["subject"], mail["body"])
    title = str((video or {}).get("title") or "")
    if not title:
        quoted = re.search(r"(?:video|recording)\s+[‘'\"]([^’'\"\n]{2,160})[’'\"]", text, re.I)
        title = quoted.group(1).strip() if quoted else "Unknown Loom video"
    viewer = "anonymous"
    patterns = [
        r"(?im)^\s*(?:viewer|watched by|viewed by)\s*:\s*([^\n<]{2,160})",
        r"(?i)([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})\s+(?:watched|viewed)",
        r"(?im)^\s*([^\n<]{2,100}?)\s+(?:watched|viewed)\s+(?:your|the)\s+(?:video|recording)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            viewer = re.sub(r"\s+", " ", match.group(1)).strip(" .:-")
            break
    percent_match = PERCENT_RE.search(text)
    watch_percent = int(percent_match.group(1)) if percent_match else None
    return {
        "event": "loom_video_first_view_email",
        "ts_utc": utc_now(),
        "viewer": viewer,
        "watch_percent": watch_percent,
        "watch_percent_available": watch_percent is not None,
        "video_id": str((video or {}).get("video_id") or ""),
        "title": title,
        "venture": str((video or {}).get("venture") or "unmatched"),
        "opportunity_id": str((video or {}).get("opportunity_id") or ""),
        "message_key": mail["gmail_msgid"] or mail["message_id"] or hashlib.sha256(text.encode()).hexdigest(),
        "source": "loom_email_first_view",
        "no_external_send": True,
    }


def append_outcome(event: dict[str, Any]) -> None:
    append_jsonl(OUTCOMES_PATH, {
        "ts_utc": event.get("ts_utc") or utc_now(),
        "event_type": "outcome_metric",
        "payload": {
            "venture": event.get("venture"),
            "metric": "loom_video_view",
            "viewer": event.get("viewer"),
            "video_id": event.get("video_id"),
            "opportunity_id": event.get("opportunity_id"),
            "watch_percent": event.get("watch_percent"),
            "source": event.get("source"),
            "location": event.get("location") or {},
        },
        "secret_printed": False,
    })


def handle_view_event(event: dict[str, Any], *, no_discord: bool = False) -> dict[str, Any]:
    prior = read_jsonl(EVENTS_PATH)
    if any(item.get("message_key") == event.get("message_key") for item in prior):
        return {"handled": True, "duplicate": True, "event": event, "no_external_send": True}
    append_jsonl(EVENTS_PATH, event)
    append_outcome(event)
    cfg = load_config()
    venture_cfg = (cfg.get("ventures") or {}).get(event.get("venture"), {})
    channel_id = str(venture_cfg.get("channel_id") or cfg.get("default_alert_channel_id") or "")
    watch = f"{event['watch_percent']}%" if event.get("watch_percent") is not None else "not included in first-view email"
    message_ids: list[str] = []
    if channel_id and not no_discord:
        viewer_class = "identified privately" if event.get("viewer") not in {None, "", "anonymous", "someone"} else "anonymous"
        message_ids = discord_send(channel_id, fmt_render(
            fmt_title("watch", "Loom View", f"1 first view captured for {str(event.get('venture')).replace('_', ' ')}"),
            fmt_kvline(("viewer", viewer_class), ("watch depth", watch), ("opportunity matched", bool(event.get("opportunity_id")))),
            fmt_meaning("review Loom Engagement Insights for depth; the viewer identity remains only in the private outcome ledger"),
            fmt_subtext(f"artifact: {EVENTS_PATH}"),
        ))
    return {"handled": True, "duplicate": False, "event": event, "discord_message_ids": message_ids, "no_external_send": True}


def maybe_handle_mail_record(record: Any, *, no_discord: bool = False) -> dict[str, Any]:
    event = parse_view_notification(record)
    if not event:
        return {"handled": False}
    return handle_view_event(event, no_discord=no_discord)


def import_transcript(file_path: Path, *, video_id: str) -> dict[str, Any]:
    registry = read_jsonl(REGISTRY_PATH)
    matches = [item for item in registry if str(item.get("video_id")) == video_id]
    if len(matches) != 1:
        raise RuntimeError(f"expected one registry row for video_id={video_id}; found {len(matches)}")
    source = file_path.resolve()
    if not source.is_file() or source.stat().st_size == 0:
        raise RuntimeError("transcript file missing or empty")
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    suffix = source.suffix.lower() if source.suffix.lower() in {".srt", ".vtt", ".txt"} else ".txt"
    destination = TRANSCRIPT_DIR / f"{video_id}{suffix}"
    shutil.copyfile(source, destination)
    record = matches[0]
    sheet_value = str(record.get("send_sheet") or "")
    if sheet_value:
        try:
            transcript_ref = str(destination.relative_to(ROOT))
        except ValueError:
            transcript_ref = str(destination)
        update_send_sheet(Path(sheet_value), str(record["opportunity_id"]), record, transcript_ref)
    event = {
        "event": "loom_transcript_filed",
        "ts_utc": utc_now(),
        "video_id": video_id,
        "venture": record.get("venture"),
        "opportunity_id": record.get("opportunity_id"),
        "transcript_path": str(destination),
        "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
        "bytes": destination.stat().st_size,
        "source": "manual_official_loom_download",
        "no_external_send": True,
    }
    append_jsonl(EVENTS_PATH, event)
    return {"status": "filed", **event, "secret_printed": False}


def import_engagement_csv(path: Path, *, venture: str, no_discord: bool = False) -> dict[str, Any]:
    rows = send_sheet_rows(path)
    registry = read_jsonl(REGISTRY_PATH)
    events: list[dict[str, Any]] = []
    for index, row in enumerate(rows, 2):
        title = next((row.get(key, "") for key in row if key.lower() in {"video title", "title", "video"}), "")
        viewer = next((row.get(key, "") for key in row if key.lower() in {"viewer", "viewer email", "email", "name"}), "") or "anonymous"
        percent_raw = next((row.get(key, "") for key in row if "percent" in key.lower() or "completion" in key.lower()), "")
        matched = [item for item in registry if str(item.get("venture")) == venture and str(item.get("title", "")).strip().lower() == title.strip().lower()]
        if len(matched) != 1:
            continue
        percent_match = PERCENT_RE.search(percent_raw)
        event = {
            "event": "loom_engagement_csv_view",
            "ts_utc": utc_now(),
            "viewer": viewer,
            "watch_percent": int(percent_match.group(1)) if percent_match else None,
            "watch_percent_available": bool(percent_match),
            "video_id": matched[0].get("video_id"),
            "title": matched[0].get("title"),
            "venture": venture,
            "opportunity_id": matched[0].get("opportunity_id"),
            "message_key": hashlib.sha256(f"{path.resolve()}|{index}|{viewer}|{title}|{percent_raw}".encode()).hexdigest(),
            "source": "manual_engagement_csv_export",
            "no_external_send": True,
        }
        events.append(handle_view_event(event, no_discord=no_discord))
    return {"status": "pass", "rows": len(rows), "matched_events": len(events), "events": events, "no_external_send": True, "secret_printed": False}


def parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def recursive_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        result: list[str] = []
        for item in value.values():
            result.extend(recursive_strings(item))
        return result
    if isinstance(value, list):
        result = []
        for item in value:
            result.extend(recursive_strings(item))
        return result
    return []


def guard_event_identity(event: dict[str, Any]) -> dict[str, Any]:
    attributes = event.get("attributes") if isinstance(event.get("attributes"), dict) else {}
    actor = attributes.get("actor") if isinstance(attributes.get("actor"), dict) else {}
    location = attributes.get("location") if isinstance(attributes.get("location"), dict) else {}
    joined = "\n".join(recursive_strings(event))
    loom_match = LOOM_URL_RE.search(joined)
    video_id = loom_match.group(1) if loom_match else ""
    if not video_id:
        context = attributes.get("context") if isinstance(attributes.get("context"), list) else []
        container = attributes.get("container") if isinstance(attributes.get("container"), list) else []
        for item in context + container:
            if not isinstance(item, dict):
                continue
            candidate = str(item.get("id") or "")
            if "video" in str(item.get("type") or "").lower() and re.fullmatch(r"[A-Za-z0-9_-]{8,}", candidate):
                video_id = candidate
                break
            blob = "\n".join(recursive_strings(item))
            for pattern in (
                r"(?:loom[_:/.-]?video|/video/)[/:=]([A-Za-z0-9_-]{8,})",
                r"ari:cloud:loom(?::[^\s/]*)*/(?:video|recording)/([A-Za-z0-9_-]{8,})",
            ):
                match = re.search(pattern, blob, re.I)
                if match:
                    video_id = match.group(1)
                    break
            if video_id:
                break
    opportunity_ids = list(dict.fromkeys(value.lower() for value in OPP_RE.findall(joined)))
    return {
        "event_id": str(event.get("id") or ""),
        "action": str(attributes.get("action") or ""),
        "event_time": str(attributes.get("time") or ""),
        "video_id": video_id,
        "share_url": f"https://www.loom.com/share/{video_id}" if video_id else "",
        "opportunity_id": opportunity_ids[0] if len(opportunity_ids) == 1 else "",
        "actor_id": str(actor.get("id") or ""),
        "actor_name": str(actor.get("name") or ""),
        "actor_email": str(actor.get("email") or ""),
        "location": {
            key: str(location.get(key) or "")
            for key in ("geo", "countryName", "regionName", "city")
            if location.get(key)
        },
    }


def atlassian_events_request(
    *, org_id: str, api_key: str, action: str, cursor: str = "", from_time: str = "",
    limit: int = 100, opener=urllib.request.urlopen,
) -> dict[str, Any]:
    params: dict[str, Any] = {"action": action, "limit": limit}
    if cursor:
        params["cursor"] = cursor
    elif from_time:
        # Atlassian admin events API requires from/to as Unix epoch milliseconds (ADMIN-400-5 on ISO 8601)
        params["from"] = str(int(parse_utc(from_time).timestamp() * 1000))
    endpoint = f"https://api.atlassian.com/admin/v1/orgs/{urllib.parse.quote(org_id, safe='')}/events?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(endpoint, headers={
        "Authorization": "Bearer " + api_key,
        "Accept": "application/json",
        "User-Agent": "Kael-Loom-Guard-Poller/1.0",
    })
    with opener(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("data", []), list):
        raise RuntimeError("Atlassian events response has invalid shape")
    return payload


def guard_seen_event(event_id: str) -> bool:
    return bool(event_id) and any(str(row.get("event_id") or "") == event_id for row in read_jsonl(GUARD_EVENTS_PATH))


def match_guard_video(identity: dict[str, Any]) -> dict[str, Any] | None:
    video_id = str(identity.get("video_id") or "")
    if not video_id:
        return None
    matches = [row for row in read_jsonl(REGISTRY_PATH) if str(row.get("video_id") or "") == video_id]
    return matches[-1] if matches else None


def resolve_created_metadata(event: dict[str, Any], identity: dict[str, Any], *, oembed_fetcher=fetch_oembed) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if identity.get("share_url"):
        return oembed_fetcher(str(identity["share_url"])), None
    registry = read_jsonl(REGISTRY_PATH)
    opportunity_id = str(identity.get("opportunity_id") or "")
    if opportunity_id:
        matches = [row for row in registry if str(row.get("opportunity_id") or "") == opportunity_id and row.get("share_url")]
        if len(matches) == 1:
            return oembed_fetcher(str(matches[0]["share_url"])), matches[0]
    text = "\n".join(recursive_strings(event)).lower()
    matches = [
        row for row in registry
        if row.get("share_url") and (
            str(row.get("video_id") or "").lower() in text
            or (str(row.get("title") or "").strip() and str(row.get("title") or "").lower() in text)
        )
    ]
    if len(matches) == 1:
        return oembed_fetcher(str(matches[0]["share_url"])), matches[0]
    raise RuntimeError("created Loom event has no resolvable URL or unique Phase-1 intake match")


def pending_guard_opportunity(
    metadata: dict[str, Any],
    explicit: str = "",
    *,
    event_id: str = "",
    event_time: str = "",
) -> str:
    cfg = load_config()
    venture = str(cfg.get("guard_created_venture") or "boring_ai")
    rail = (cfg.get("ventures") or {}).get(venture) or {}
    if venture == "boring_ai":
        if str(rail.get("guard_created_assignment") or "") != "active_recording_session":
            raise RuntimeError("Boring AI Guard assignment must be active_recording_session")
        conn = sqlite3.connect(BORING_DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            assignment = recording_order.claim_next_guard_item(
                conn,
                event_id=event_id,
                event_time=event_time,
                video_id=str(metadata.get("video_id") or ""),
            )
        finally:
            conn.close()
        return str(assignment["opportunity_id"])
    sheet_value = str(rail.get("send_sheet") or "")
    if not sheet_value:
        if explicit:
            return explicit
        raise RuntimeError("Guard-created venture has no send sheet")
    rows = send_sheet_rows(Path(sheet_value))
    if explicit and any(str(row.get("opportunity_id") or "") == explicit for row in rows):
        return explicit
    pending = [row for row in rows if not str(row.get("loom_video_id") or "").strip()]
    title = str(metadata.get("title") or "").lower()
    title_matches = [
        row for row in pending
        if str(row.get("opportunity_id") or "").lower() in title
        or (
            str(row.get("business_name") or "").strip().lower() not in {"", "public asker"}
            and str(row.get("business_name") or "").strip().lower() in title
        )
    ]
    if len(title_matches) == 1:
        return str(title_matches[0]["opportunity_id"])
    raise RuntimeError("created Loom could not be reconciled to one pending send-sheet row")


def _find_exact_order_message(channel_id: str, content: str) -> str | None:
    messages = discord_api("GET", f"/channels/{channel_id}/messages?limit=100")
    matches = [str(item.get("id") or "") for item in messages if str(item.get("content") or "") == content]
    if len(matches) > 1:
        raise RuntimeError("duplicate_exact_loom_order_messages_found")
    return matches[0] if matches else None


def _refresh_next_recording_order(conn: sqlite3.Connection) -> dict[str, Any]:
    return recording_order.refresh_recording_order(
        conn,
        send_message=lambda channel_id, content: discord_send(channel_id, content)[0],
        get_message=lambda channel_id, message_id: discord_api("GET", f"/channels/{channel_id}/messages/{message_id}"),
        find_exact_message=_find_exact_order_message,
    )


def process_guard_event(event: dict[str, Any], *, no_discord: bool = False, oembed_fetcher=fetch_oembed) -> dict[str, Any]:
    identity = guard_event_identity(event)
    event_id = str(identity.get("event_id") or "")
    if not event_id:
        raise RuntimeError("Guard event missing id")
    if guard_seen_event(event_id):
        return {"status": "duplicate", "event_id": event_id}
    action = str(identity.get("action") or "")
    result: dict[str, Any] = {"status": "recorded", **identity, "no_external_send": True}
    if action == "loom_video_created":
        metadata, phase1_match = resolve_created_metadata(event, identity, oembed_fetcher=oembed_fetcher)
        metadata["guard_event_time"] = str(identity.get("event_time") or "")
        metadata["guard_event_id"] = event_id
        try:
            opportunity_id = pending_guard_opportunity(
                metadata,
                str(identity.get("opportunity_id") or (phase1_match or {}).get("opportunity_id") or ""),
                event_id=event_id,
                event_time=str(identity.get("event_time") or ""),
            )
        except RuntimeError as exc:
            reason = str(exc)
            if reason.startswith("guard_created_requires_exactly_one_armed_session:") or reason == "guard_created_event_predates_armed_session":
                result.update({"status": "ignored_created_event", "reason": reason})
                append_jsonl(GUARD_EVENTS_PATH, {"recorded_at": utc_now(), **result})
                return result
            raise
        venture = str(load_config().get("guard_created_venture") or "boring_ai")
        result["intake"] = ingest_url(
            str(metadata["share_url"]), venture=venture,
            opportunity_id=opportunity_id, source=f"guard_event:{event_id}",
            no_discord=no_discord, oembed_fetcher=lambda _url: metadata,
        )
        if venture == "boring_ai":
            conn = sqlite3.connect(BORING_DB_PATH)
            conn.row_factory = sqlite3.Row
            try:
                assigned = conn.execute(
                    "SELECT * FROM loom_recording_session_items WHERE assigned_guard_event_id=?",
                    (event_id,),
                ).fetchone()
                if not assigned:
                    raise RuntimeError("Guard assignment disappeared before capture completion")
                queue_attachment = result["intake"].get("queue_attachment") or {}
                result["recording_session"] = recording_order.complete_guard_capture(
                    conn,
                    item_id=str(assigned["id"]),
                    event_id=event_id,
                    video_id=str(metadata.get("video_id") or ""),
                    canonical_update_succeeded=str(queue_attachment.get("status") or "") == "attached",
                    export_update_succeeded=bool(queue_attachment.get("manual_export_updated")),
                )
                if result["recording_session"].get("session_completed") and not no_discord:
                    result["next_recording_order"] = _refresh_next_recording_order(conn)
            finally:
                conn.close()
        result["status"] = "intake_captured"
    elif action == "loom_video_viewed":
        matched = match_guard_video(identity)
        outcome = {
            "event": "loom_video_viewed_guard",
            "ts_utc": identity.get("event_time") or utc_now(),
            "viewer": identity.get("actor_email") or identity.get("actor_name") or identity.get("actor_id") or "anonymous",
            "watch_percent": None,
            "watch_percent_available": False,
            "video_id": identity.get("video_id") or str((matched or {}).get("video_id") or ""),
            "title": str((matched or {}).get("title") or "Unknown Loom video"),
            "venture": str((matched or {}).get("venture") or "unmatched"),
            "opportunity_id": str((matched or {}).get("opportunity_id") or identity.get("opportunity_id") or ""),
            "message_key": "guard:" + event_id,
            "source": "atlassian_guard_audit_event",
            "location": identity.get("location") or {},
            "no_external_send": True,
        }
        result["outcome"] = handle_view_event(outcome, no_discord=no_discord)
        result["status"] = "outcome_captured"
    else:
        result["status"] = "ignored_action"
    append_jsonl(GUARD_EVENTS_PATH, {"recorded_at": utc_now(), **result})
    return result


def guard_initial_state(now: datetime) -> dict[str, Any]:
    watermark = (now - timedelta(days=GUARD_INITIAL_LOOKBACK_DAYS)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return {
        "schema": "loom_guard_cursor_v1",
        "actions": {action: {"cursor": "", "watermark": watermark} for action in GUARD_ACTIONS},
    }


def write_guard_state(state: dict[str, Any]) -> None:
    GUARD_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = GUARD_STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps({**state, "updated_at": utc_now()}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, GUARD_STATE_PATH)


def poll_guard_events(
    *, no_discord: bool = False, now: datetime | None = None,
    request_fn=atlassian_events_request, process_event_fn=process_guard_event,
) -> dict[str, Any]:
    cfg = load_config()
    guard_cfg = cfg.get("guard_events") if isinstance(cfg.get("guard_events"), dict) else {}
    if not bool(guard_cfg.get("armed")) or first_env(GUARD_ARM_ENV).upper() != "YES":
        return {
            "status": "disarmed", "armed": False,
            "reason": f"requires guard_events.armed=true and {GUARD_ARM_ENV}=YES",
            "requests": 0, "no_external_send": True, "secret_printed": False,
        }
    org_id = first_env("LOOM_ORG_ID", "ATLASSIAN_ORG_ID")
    api_key = first_env("LOOM_ADMIN_API_KEY", "LOOM_API_KEY", "ATLASSIAN_ADMIN_API_KEY")
    if not org_id or not api_key:
        raise RuntimeError("Guard poller armed but Loom organization/API credential is unavailable")
    now = now or datetime.now(timezone.utc)
    state = load_json(GUARD_STATE_PATH, guard_initial_state(now))
    actions_state = state.setdefault("actions", {})
    configured_actions = tuple(guard_cfg.get("actions") or GUARD_ACTIONS)
    if any(action not in GUARD_ACTIONS for action in configured_actions):
        raise RuntimeError("Guard poller action allowlist contains unsupported action")
    max_requests = min(int(guard_cfg.get("max_requests_per_run") or 8), 8)
    requests = 0
    processed: list[dict[str, Any]] = []
    for action in configured_actions:
        action_state = actions_state.setdefault(action, guard_initial_state(now)["actions"][action])
        watermark = str(action_state.get("watermark") or "")
        if not watermark or now - parse_utc(watermark) >= timedelta(days=GUARD_RETENTION_DAYS):
            raise RuntimeError(f"Guard cursor stale for {action}; watermark is outside the 180-day retention window")

        # Atlassian documents pagination and event timestamps, but no response-order
        # guarantee. Collect a complete created-event page chain and sort it before
        # attribution so a newest-first response cannot reverse the operator's order.
        rows_for_action: list[dict[str, Any]] = []
        cursor = str(action_state.get("cursor") or "")
        pagination_complete = False
        while requests < max_requests:
            payload = request_fn(
                org_id=org_id, api_key=api_key, action=action,
                cursor=cursor, from_time=watermark,
                limit=int(guard_cfg.get("page_limit") or 100),
            )
            requests += 1
            rows_for_action.extend(event for event in (payload.get("data") or []) if isinstance(event, dict))
            cursor = str((payload.get("meta") or {}).get("next") or "")
            if not cursor:
                pagination_complete = True
                break
        if action == "loom_video_created" and not pagination_complete:
            raise RuntimeError("Guard created-event page chain exceeded request ceiling; no Loom was attributed")

        def chronological_key(event: dict[str, Any]) -> tuple[datetime, str]:
            attributes = event.get("attributes") if isinstance(event.get("attributes"), dict) else {}
            event_time = str(attributes.get("time") or "")
            if not event_time:
                raise RuntimeError("Guard event is missing the time required for deterministic ordering")
            return parse_utc(event_time), str(event.get("id") or "")

        ordered_rows = sorted(rows_for_action, key=chronological_key)
        for event in ordered_rows:
            processed.append(process_event_fn(event, no_discord=no_discord))

        event_times = [str((event.get("attributes") or {}).get("time") or "") for event in ordered_rows]
        if event_times:
            action_state["watermark"] = max(event_times, key=parse_utc)
        elif pagination_complete:
            # A successful quiet poll proves the interval was empty. Keep five
            # minutes of overlap for delayed ingestion while preventing the
            # watermark from aging past Guard's 180-day retention boundary.
            overlap = (now - timedelta(minutes=5)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            action_state["watermark"] = max((watermark, overlap), key=parse_utc)
        action_state["cursor"] = "" if pagination_complete else cursor
        action_state["updated_at"] = utc_now()
        write_guard_state(state)
    return {
        "status": "pass", "armed": True, "requests": requests,
        "processed": processed, "request_ceiling": max_requests,
        "retention_days": GUARD_RETENTION_DAYS,
        "created_event_ordering": "event_time_ascending_after_complete_page_chain",
        "no_external_send": True, "secret_printed": False,
    }


def weekly_raise_report() -> dict[str, Any]:
    events = [item for item in read_jsonl(EVENTS_PATH) if item.get("venture") == "allocant" and "view" in str(item.get("event"))]
    latest_by_key: dict[str, dict[str, Any]] = {}
    for item in events:
        latest_by_key[str(item.get("message_key") or item.get("ts_utc"))] = item
    report = {
        "generated_at": utc_now(),
        "venture": "allocant",
        "loom_demo_views": len(latest_by_key),
        "identified_viewers": sum(1 for item in latest_by_key.values() if item.get("viewer") not in {"", "anonymous", "someone"}),
        "papermark_opens": None,
        "papermark_status": "separate existing tracked-asset rail; no fabricated merge when source unavailable",
        "events": list(latest_by_key.values())[-25:],
        "no_external_send": True,
        "secret_printed": False,
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / "allocant-weekly-loom-views.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"status": "pass", "report_path": str(path), **report}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Loom zero-service delivery rails")
    sub = parser.add_subparsers(dest="command", required=True)
    ingest = sub.add_parser("ingest-url")
    ingest.add_argument("url")
    ingest.add_argument("--venture", required=True, choices=["boring_ai", "allocant", "glp"])
    ingest.add_argument("--opportunity-id", required=True)
    ingest.add_argument("--source", default="operator_cli")
    ingest.add_argument("--no-discord", action="store_true")
    watch = sub.add_parser("watch-discord")
    watch.add_argument("--no-discord", action="store_true")
    watch.add_argument("--dry-run", action="store_true")
    transcript = sub.add_parser("import-transcript")
    transcript.add_argument("--file", required=True, type=Path)
    transcript.add_argument("--video-id", required=True)
    engagement = sub.add_parser("import-engagement-csv")
    engagement.add_argument("--file", required=True, type=Path)
    engagement.add_argument("--venture", required=True, choices=["boring_ai", "allocant", "glp"])
    engagement.add_argument("--no-discord", action="store_true")
    guard = sub.add_parser("poll-guard-events")
    guard.add_argument("--no-discord", action="store_true")
    sub.add_parser("weekly-raise-report")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("w") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if args.command == "ingest-url":
            result = ingest_url(args.url, venture=args.venture, opportunity_id=args.opportunity_id, source=args.source, no_discord=args.no_discord)
        elif args.command == "watch-discord":
            result = watch_discord(no_discord=args.no_discord, dry_run=args.dry_run)
        elif args.command == "import-transcript":
            result = import_transcript(args.file, video_id=args.video_id)
        elif args.command == "import-engagement-csv":
            result = import_engagement_csv(args.file, venture=args.venture, no_discord=args.no_discord)
        elif args.command == "poll-guard-events":
            result = poll_guard_events(no_discord=args.no_discord)
        else:
            result = weekly_raise_report()
    print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
