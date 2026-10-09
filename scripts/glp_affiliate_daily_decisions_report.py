#!/usr/bin/env python3
"""Daily consolidated GLP affiliate ops report to #decisions, with DONE-ack round-trip.

Operator order (2026-08-07): after the affiliate application chain runs each morning
(reconciliation 7:15 → supply sweep 7:35 → portal drain 9:05 → supply discovery 9:20 PT),
post ONE well-formatted #decisions card: how many/which applications went out, what is
blocked, and for each blocker EXACTLY what the operator must do. When the operator
replies that they did it (`DONE <id>`), the machine ingests the reply, updates the
blocker store, and the affiliate machine stays on current state. GLP Clinic Scout
affiliates only — clones for future sites read their own site config.

Architecture:
  ground truth  applications-ledger.json (lanes already record last_blocker +
                operator_step per row) + api-key-minting-queue.json + the approval
                facts DB. The report never invents state.
  blocker store reports/glp-clinic-scout/affiliate-machine/operator-blockers.json —
                a PROJECTION of ground truth (derived entries re-sync every run,
                so any lane that writes the ledger is covered automatically) plus
                seeded manual items; ack status fields are preserved across syncs.
  posting       #decisions is dispatcher-only: cards are queued into the inbox
                control plane's notification_outbox and delivered by its dispatch
                loop (readback-verified, idempotent by key). This script never
                POSTs to #decisions directly.
  acks          conservative reader of operator messages in #decisions:
                `DONE <id>` exact, or free text matching exactly ONE open
                blocker's keywords. Ambiguity queues a card asking for the id —
                never guess-clear. Machine verification (not the ack) is what
                moves a blocker to verified_cleared.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import os
import re
import sqlite3
import stat
import sys
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

BUSINESS = Path("/home/ubuntu/business")
MACHINE_DIR = BUSINESS / "reports/glp-clinic-scout/affiliate-machine"
LEDGER_PATH = MACHINE_DIR / "applications-ledger.json"
MINT_QUEUE_PATH = MACHINE_DIR / "api-key-minting-queue.json"
BLOCKER_STORE = MACHINE_DIR / "operator-blockers.json"
INTAKE_DIR = MACHINE_DIR / "operator-blockers-intake"
STATE_PATH = MACHINE_DIR / "daily-report-state.json"
REPORT_DIR = MACHINE_DIR / "daily-reports"
ITEM_E_INVENTORY = MACHINE_DIR / "fable-fix-order-closure-20260808/item-e-47-program-inventory-20260810.json"
APPLY_RUN_QUEUE_DIR = MACHINE_DIR / "apply-run-queue"
RECONCILER_DIR = BUSINESS / "reports/glp-affiliate-auto-reconciler"
RESOLUTIONS_DIR = RECONCILER_DIR / "portal-access-resolutions"
OPERATOR_ACTIONS_DIR = RECONCILER_DIR / "operator-actions"
AWIN_TERMS_RESEARCH_PATH = MACHINE_DIR / "bridge-finish-1-20260812/awin-41-stage1-5-research.json"
AWIN_TERMS_RESEARCH_SHA256 = "7eb6660110af1b7f64149036217723c1095398b9d2016dfb7da32135dfc6ae73"
AWIN_HELD_TERMS_IDS = ("80963", "115383", "126727")
#: a portal access resolution for this network suppresses these ledger blocker classes
_ACCESS_RESOLUTION_CLASSES = {"awin": ["AWIN_PORTAL_TOTP"], "rakuten": ["RAKUTEN_PORTAL_SESSION_MISSING"]}
SITE_CONFIG = BUSINESS / "config/affiliate-sites/glp-clinic-scout.json"
INBOX_DB = Path("/home/ubuntu/.hermes/state/email-monitor.sqlite3")
PT = ZoneInfo("America/Los_Angeles")
POST_AFTER_PT = (9, 50)
# v3 intentionally remints the daily card identity after the held Awin terms
# cards became part of the canonical payload.  The v2/v3 keys may already have
# delivered payloads for the same PT day; reusing either would silently preserve
# stale bytes because notification_outbox is insert-once by idempotency key.
# v4 adds exact dual ACCEPT/REJECT terms decisions and current-decision cards.
REPORT_KEY = "glp-affiliate-daily-report-v4"
PURPOSE = "affiliate_daily_report"


sys.path.insert(0, str(BUSINESS / "ops"))
sys.path.insert(0, str(BUSINESS / "scripts"))
from discord_fmt import lint, render, subtext, title  # noqa: E402
from affiliate_blocker_completeness import (  # noqa: E402
    canonical_program_key, completeness_proof, exact_program_identity, proof_line, validate_completeness,
    validate_rendered_chunks,
)

#: last_blocker values that are MACHINE defects, never operator work. The operator
#: must never be told to fix playwright; these roll up into one repair count.
_MACHINE_DEFECT_RE = re.compile(
    r"^(?:ModuleNotFoundError|AttributeError|TypeError|KeyError|ImportError|"
    r"submit_control_not_exact|TimeoutError|playwright)"
)
_NON_OPERATOR_LEDGER_CLASSES = {
    "AWIN_PORTAL_TOTP": {
        "status": "machine_owned",
        "cause": "legacy Awin TOTP state is invalid because Awin 2FA is disabled; the machine must use the stored password session and retry",
    },
    "AWIN_PORTAL_SESSION_REQUIRED": {
        "status": "machine_owned",
        "cause": "the Awin API identity is healthy but the authenticated publisher UI session must be restored by the machine before terms capture or Join",
    },
    "AWIN_PUBLISHER_IDENTITY_UNVERIFIED": {
        "status": "machine_owned",
        "cause": "Awin API publisher identity is verified; the remaining authenticated UI observation or session repair belongs to the machine",
    },
    "AWIN_UI_SESSION_OR_IDENTITY_OBSERVATION_UNAVAILABLE": {
        "status": "machine_owned",
        "cause": "the Awin authenticated UI session or independent website observation is unavailable and must be repaired by the machine",
    },
    "incomplete_awaiting_external": {
        "status": "machine_research",
        "cause": "these rows need machine-side evidence, access, or provider-state reconciliation; they are not an operator action list",
    },
}
_NON_OPERATOR_MINT_NETWORK_STATES = {
    "awin": "machine_owned",
    "katalys": "machine_owned",
    "sovrn": "awaiting_external",
}
_PARKED_RE = re.compile(r"^(?:lane_not_authorized|operator_ruling_hold):")
_HARD_CONTRACT_TRIGGER_RE = re.compile(
    r"^(?:legal_terms_hard_trigger|hard_contract_trigger|BLOCKED_HARD_CONTRACT_TRIGGER)(?::|$)", re.I)
_ROUTINE_LEGAL_COPY_RE = re.compile(
    r"^(?:routine_(?:legal|terms|copy|compliance)|"
    r"(?:legal|terms|contract|copy|disclosure)_(?:review|gap|incomplete|missing|capture)|"
    r"terms_capture_incomplete_nonblocking)(?::|$)", re.I)
_TERMINAL_PROGRAM_STATUSES = frozenset({"terminal_provider_unavailable"})

#: blocker class -> (stable id, operator step template). Classes not listed fall
#: back to BLK-<slug> with the ledger rows' own operator_step lines as steps.
_CLASS_IDS = {
    "AWIN_PORTAL_TOTP": "BLK-awin-totp",
    "RAKUTEN_PORTAL_SESSION_MISSING": "BLK-rakuten-session",
    "application_form_not_found": "BLK-manual-apply",
    "account_password_required": "BLK-account-password",
}
#: plain-words WHAT'S WRONG per ledger blocker class (operator ruling 2026-08-07:
#: cards never show a bare reason code).
_CLASS_CAUSES = {
    "AWIN_PORTAL_TOTP": "a legacy ledger row still names an invalid Awin TOTP blocker; Awin 2FA is not enabled, so the lane must re-run API-first with exact reviewed authority",
    "RAKUTEN_PORTAL_SESSION_MISSING": "Rakuten Advertising has no live publisher session for ops@, so the lane cannot open the application forms",
    "application_form_not_found": "these programs have no application URL the lane can reach — each needs its form found and completed by hand",
    "account_password_required": "the portal asks for an account password at login — creating/storing one is an operator step the lane may not do alone",
    "credential_or_finance_field": "the application form asks for a credential or finance field the lane is not authorized to fill",
}
_KEYWORDS = {
    "BLK-awin-totp": {"awin", "totp", "authenticator"},
    "BLK-rakuten-session": {"rakuten"},
    "BLK-manual-apply": {"manual", "form", "forms"},
    "BLK-account-password": {"password"},
    "BLK-docusign-telewellnessmd": {"docusign", "telewellness", "telewellnessmd", "signed", "agreement"},
    "BLK-refersion-portal-access": {"refersion"},
    "BLK-katalys-w9": {"katalys", "deposit", "bank", "payment", "forms"},
    "BLK-plexusdx-partner-call": {"plexusdx", "partner", "call", "calendly", "terms"},
    "BLK-t2d-live-application": {"t2d", "agent", "application", "mock", "tax", "payout"},
    "BLK-coreage-katalys-terms": {"coreage", "katalys", "1632", "terms", "legal"},
}


def _now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pt_today(now: dt.datetime | None = None) -> str:
    return (now or dt.datetime.now(dt.timezone.utc)).astimezone(PT).strftime("%Y-%m-%d")


def _pt_date_of(ts: str) -> str:
    try:
        parsed = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return ""
    return parsed.astimezone(PT).strftime("%Y-%m-%d")


def _slug(value: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", value.lower())).strip("-")[:40]


def _load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def _atomic_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_control_plane():
    return _load_module("cp_daily_report", BUSINESS / "inbox_control_plane/control_plane.py")


def load_operator_rulings():
    return _load_module("affiliate_operator_rulings_daily", BUSINESS / "scripts/affiliate_operator_rulings.py")


def portal_card_steps() -> dict[str, tuple[str, str]]:
    """Single source of truth for portal blocker step text (portal card composer)."""
    try:
        mod = _load_module("portal_card_steps", BUSINESS / "scripts/affiliate_portal_apply_card.py")
        return dict(getattr(mod, "BLOCKER_STEPS", {}))
    except Exception:
        return {}


# --------------------------------------------------------------------- ground truth
def blocker_projection_sources(ledger: dict[str, Any],
                               mint_queue: dict[str, Any]) -> dict[str, Any]:
    """Derive blocker-relevant synchronizer inputs without reading state.

    Item E's execution gate reuses this pure boundary to prove that a newer
    blocker store is the deterministic daily projection of exact ledger and
    mint-queue evidence. Keep every blocker-relevant classification here so the
    live collector and the offline authenticator cannot drift apart.
    """
    apps = ledger.get("applications") or []
    submitted_today, in_review, machine_defects, parked, machine_research = [], [], [], [], []
    operator_rows: dict[str, list[dict[str, Any]]] = {}
    for row in apps:
        status = str(row.get("status") or "")
        if (status in _TERMINAL_PROGRAM_STATUSES
                or row.get("exclude_from_application_execution") is True
                or row.get("exclude_from_action_cards") is True):
            # Keep the historical ledger identity, but never turn a terminal,
            # non-executable programme into operator work or a waiting row.
            continue
        blocker = str(row.get("last_blocker") or "")
        entry = {
            "name": str(row.get("name") or row.get("program_id") or "?"),
            "network": str(row.get("network") or "?"),
            "program_id": str(row.get("program_id") or ""),
            "lane": str(row.get("lane") or ""),
            "blocker": blocker,
            "operator_step": str(row.get("operator_step") or ""),
            "observation_cursor": str(row.get("last_blocked_at_utc") or row.get("planned_at_utc") or ""),
        }
        if status == "SUBMITTED_VERIFIED":
            in_review.append(entry)
            continue
        if _PARKED_RE.match(blocker):
            parked.append(entry)
        elif blocker and _MACHINE_DEFECT_RE.match(blocker):
            machine_defects.append(entry)
        elif (blocker and _ROUTINE_LEGAL_COPY_RE.match(blocker)
              and not _HARD_CONTRACT_TRIGGER_RE.match(blocker)):
            entry["operator_action_required"] = False
            machine_research.append(entry)
        elif blocker:
            operator_rows.setdefault(blocker.split(":", 1)[0], []).append(entry)
    mint_pending = [t for t in (mint_queue.get("tasks") or []) if not t.get("resolved_at_utc")]
    return {
        "submitted_today": submitted_today,
        "in_review": in_review,
        "operator_rows": operator_rows,
        "machine_defects": machine_defects,
        "machine_research": machine_research,
        "parked": parked,
        "mint_pending": mint_pending,
    }


def collect_snapshot(now: dt.datetime | None = None) -> dict[str, Any]:
    """Read every ground-truth store for the current PT day. Pure read."""
    today = _pt_today(now)
    ledger = _load_json(LEDGER_PATH, {"applications": []})
    sources = blocker_projection_sources(
        ledger, _load_json(MINT_QUEUE_PATH, {"tasks": []}))
    # Date bucketing is report-only; the blocker synchronizer ignores both
    # submitted buckets, but preserve the established public snapshot behavior.
    sources["submitted_today"] = []
    sources["in_review"] = []
    for row in ledger.get("applications") or []:
        if str(row.get("status") or "") != "SUBMITTED_VERIFIED":
            continue
        entry = {
            "name": str(row.get("name") or row.get("program_id") or "?"),
            "network": str(row.get("network") or "?"),
            "program_id": str(row.get("program_id") or ""),
            "lane": str(row.get("lane") or ""),
            "blocker": str(row.get("last_blocker") or ""),
            "operator_step": str(row.get("operator_step") or ""),
            "observation_cursor": str(row.get("last_blocked_at_utc") or row.get("planned_at_utc") or ""),
        }
        when = str(row.get("submitted_at_utc") or "")
        sources["submitted_today" if _pt_date_of(when) == today else "in_review"].append(entry)
    approvals_today = _approvals_today(today)
    return {
        "schema": "glp-affiliate-daily-report-snapshot-v1",
        "date_pt": today,
        "collected_at_utc": _now_utc(),
        **sources,
        "approvals_today": approvals_today,
    }


def _approvals_today(today: str) -> int:
    if not INBOX_DB.is_file():
        return 0
    try:
        conn = sqlite3.connect(f"file:{INBOX_DB}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT first_verified_at_utc FROM affiliate_approval_facts "
                "WHERE classification_portfolio='glp'").fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return 0
    return sum(1 for (ts,) in rows if _pt_date_of(str(ts or "")) == today)


def _observation_binding(entry: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, str]:
    """Stable local evidence binding for an awaiting-external assertion."""
    source = str(entry.get("source") or "")
    cls = str(entry.get("class") or "")
    if source == "ledger":
        observed = list((snapshot.get("operator_rows") or {}).get(cls) or [])
        cursor = max((str(row.get("observation_cursor") or row.get("last_blocked_at_utc")
                          or row.get("planned_at_utc") or "") for row in observed), default="")
    elif source == "mint_queue":
        network = _slug(str(entry.get("id") or "").replace("BLK-", "").replace("-portal-access", ""))
        observed = [task for task in snapshot.get("mint_pending", [])
                    if _slug(str(task.get("network") or "")) == network]
        cursor = max((str(task.get("updated_at_utc") or task.get("queued_at_utc") or "") for task in observed), default="")
    else:
        observed = [{"event_id": entry.get("event_id"), "class": cls,
                     "programs": entry.get("programs") or []}]
        cursor = ""
    canonical = json.dumps(observed, sort_keys=True, separators=(",", ":"), default=str)
    return {"fingerprint": hashlib.sha256(canonical.encode()).hexdigest(), "cursor": cursor, "source": source}


def _preserve_awaiting_external(entry: dict[str, Any], previous: dict[str, Any],
                                snapshot: dict[str, Any], *, now_utc: str | None = None
                                ) -> dict[str, Any]:
    """Keep a wait only while its machine observation bytes remain identical."""
    prior = previous.get("awaiting_external") or {}
    if previous.get("status") != "awaiting_external" or not prior:
        return entry
    observed = _observation_binding(entry, snapshot)
    bound = prior.get("observation") or {}
    if bound.get("fingerprint") == observed["fingerprint"] and bound.get("cursor") == observed["cursor"]:
        entry["status"] = "awaiting_external"
        entry["awaiting_external"] = prior
        return entry
    entry["status"] = "open"
    entry["reopened_from_awaiting_external"] = {
        "at_utc": now_utc or _now_utc(),
        "prior_observation": bound, "new_observation": observed,
    }
    return entry


def _preserve_operator_declined(entry: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
    """Terminal operator decline survives every ordinary projection/sync."""
    if previous.get("status") == "operator_declined" and previous.get("operator_declined"):
        entry["status"] = "operator_declined"
        entry["operator_declined"] = previous["operator_declined"]
    return entry


def apply_awaiting_external_updates(store: dict[str, Any], snapshot: dict[str, Any], *,
                                    message_id: str, state_updates: list[dict[str, str]],
                                    captured_at_utc: str | None = None) -> list[str]:
    """Persist explicit operator state, conflict-safe and observation-bound.

    ``operator_declined`` is terminal. Only an explicit ``reopen`` instruction
    may move it back to open; ordinary awaiting/DONE/sync operations cannot.
    """
    applied: list[str] = []
    captured_at_utc = captured_at_utc or _now_utc()
    entries = store.get("blockers") or {}
    for update in state_updates:
        blocker_id = str(update.get("blocker_id") or "")
        entry = entries.get(blocker_id)
        if entry is None:
            continue
        kind = str(update.get("state_kind") or "awaiting_external")
        words = _sanitize_operator_words(str(update.get("operator_words") or ""))
        words_sha = hashlib.sha256(words.encode("utf-8")).hexdigest()
        prior_decline = entry.get("operator_declined") or {}
        if entry.get("status") == "operator_declined" and kind != "reopen":
            if (str(prior_decline.get("source_message_id")) == str(message_id)
                    and prior_decline.get("operator_words_sha256") != words_sha):
                raise RuntimeError(f"operator_state_message_conflict:{blocker_id}:{message_id}")
            continue
        if kind == "reopen":
            if entry.get("status") != "operator_declined":
                continue
            entry["status"] = "open"
            entry["reopened_by_operator"] = {
                "source_message_id": str(message_id), "operator_words": words,
                "operator_words_sha256": words_sha, "captured_at_utc": captured_at_utc,
                "prior_decline": prior_decline,
            }
            entry.pop("operator_declined", None)
            entry["updated_at_utc"] = captured_at_utc
            applied.append(blocker_id)
            continue
        state = {
            "source_message_id": str(message_id), "operator_words": words,
            "operator_words_sha256": words_sha, "state_kind": kind,
            "observation": _observation_binding(entry, snapshot),
            "captured_at_utc": captured_at_utc,
        }
        state_field = "operator_declined" if kind == "operator_declined" else "awaiting_external"
        target_status = state_field
        prior = entry.get(state_field) or {}
        if str(prior.get("source_message_id")) == str(message_id):
            if prior != state:
                raise RuntimeError(f"operator_state_message_conflict:{blocker_id}:{message_id}")
            continue
        entry["status"] = target_status
        entry[state_field] = state
        entry["updated_at_utc"] = captured_at_utc
        applied.append(blocker_id)
    return applied


# --------------------------------------------------------------------- blocker store
def sync_blockers(snapshot: dict[str, Any], store: dict[str, Any] | None = None, *,
                  synchronizer_now_utc: str | None = None,
                  source_ledger: dict[str, Any] | None = None,
                  resolution_documents: list[dict[str, Any]] | None = None,
                  intake_documents: list[dict[str, Any]] | None = None,
                  resolved_event_ids: set[str] | None = None,
                  manually_verified_ids: set[str] | None = None) -> dict[str, Any]:
    """Project ground truth into the durable blocker store, preserving ack state.

    Derived entries whose ground truth vanished are verified_cleared with evidence.
    Seeded/manual entries are only cleared by their own verification rule.

    The keyword-only evidence arguments form a deterministic, zero-write replay
    boundary for Item E authentication. Normal live callers omit them and retain
    the existing canonical reads and verification behavior.
    """
    def sync_now() -> str:
        return synchronizer_now_utc or _now_utc()

    store = store if store is not None else _load_json(BLOCKER_STORE, {})
    entries: dict[str, Any] = dict(store.get("blockers") or {})
    terminal_entries = {
        key: value for key, value in entries.items()
        if isinstance(value, dict) and value.get("status") in _TERMINAL_PROGRAM_STATUSES
    }
    steps_map = (portal_card_steps() if any(
        cls not in _NON_OPERATOR_LEDGER_CLASSES
        for cls in (snapshot.get("operator_rows") or {})) else {})
    seen_derived: set[str] = set()
    cleared_by_machine: list[dict[str, Any]] = list(store.get("cleared_by_machine") or [])

    # Portal access RESOLUTIONS recorded by the reconciler suppress the matching
    # ledger blocker classes: the wall came down, the lane retries at next drain.
    # A row blocked AGAIN after the resolution re-opens the class (evidence wins).
    suppressed = dict(store.get("suppressed_classes") or {})
    if resolution_documents is None:
        resolutions = ([_load_json(path, {})
                        for path in sorted(RESOLUTIONS_DIR.glob("*.json"))]
                       if RESOLUTIONS_DIR.is_dir() else [])
    else:
        resolutions = resolution_documents
    for resolution in resolutions:
        network = _slug(str(resolution.get("network") or ""))
        for cls in _ACCESS_RESOLUTION_CLASSES.get(network, []):
            if cls not in suppressed:
                suppressed[cls] = {
                    "created_at_utc": str(resolution.get("recorded_at_utc") or sync_now()),
                    "evidence": f"{resolution.get('network')} case {resolution.get('case_id') or '?'}: "
                                f"{resolution.get('resolution')} (event {resolution.get('event_id')})",
                }
                cleared_by_machine.append({
                    "id": _CLASS_IDS.get(cls, f"BLK-{_slug(cls)}"),
                    "at_utc": sync_now(),
                    "note": f"{resolution.get('network')} removed the portal login wall "
                            f"(case {resolution.get('case_id') or '?'}); the portal lane retries at the next drain",
                })
    store["suppressed_classes"] = suppressed

    for cls, rows in sorted(snapshot["operator_rows"].items()):
        non_operator = _NON_OPERATOR_LEDGER_CLASSES.get(cls)
        suppression = suppressed.get(cls)
        if suppression:
            ledger_value = (source_ledger if source_ledger is not None
                            else _load_json(LEDGER_PATH, {"applications": []}))
            ledger_rows = ledger_value.get("applications") or []
            newer = [r for r in ledger_rows
                     if str(r.get("last_blocker") or "").split(":", 1)[0] == cls
                     and str(r.get("last_blocked_at_utc") or "") > str(suppression.get("created_at_utc") or "")]
            if not newer:
                seen_derived.add(_CLASS_IDS.get(cls, f"BLK-{_slug(cls)}"))
                continue
            suppressed.pop(cls, None)  # blocked again AFTER the resolution: re-open
        blocker_id = _CLASS_IDS.get(cls, f"BLK-{_slug(cls)}")
        seen_derived.add(blocker_id)
        full_step = "" if non_operator else (steps_map.get(cls) or ("", ""))[0]
        steps: list[str] = [full_step] if full_step else []
        if non_operator:
            steps = ["machine retries from canonical credentials and exact provider evidence; no operator reply is requested"]
        elif not steps:
            for row in rows[:6]:
                if row["operator_step"]:
                    steps.append(f"{row['name']}: {row['operator_step']}")
        if not steps:
            steps = [f"complete the {cls} action for the listed programs by hand"]
        previous = entries.get(blocker_id) or {}
        cause = ((non_operator or {}).get("cause")
                 or _CLASS_CAUSES.get(cls, f"{len(rows)} planned application(s) are held: {cls.replace('_', ' ')}"))
        entries[blocker_id] = {
            "id": blocker_id,
            "source": "ledger",
            "class": cls,
            "cause": f"{cause} ({len(rows)} application(s) waiting)",
            "programs": sorted(({
                "name": r["name"], "network": r["network"],
                "program_id": r["program_id"], "lane": r["lane"],
            } for r in rows), key=lambda row: (row["network"], row["program_id"], row["name"])),
            "steps": steps,
            "keywords": sorted(_KEYWORDS.get(blocker_id, set()) | {cls.split("_")[0].lower()}),
            "created_at_utc": previous.get("created_at_utc") or sync_now(),
            "updated_at_utc": sync_now(),
            "status": (non_operator or {}).get("status") or previous.get("status") or "open",
            "ack": None if non_operator else previous.get("ack"),
            "operator_action_required": False if non_operator else True,
        }
        if not non_operator:
            entries[blocker_id] = _preserve_awaiting_external(
                entries[blocker_id], previous, snapshot, now_utc=synchronizer_now_utc)
            entries[blocker_id] = _preserve_operator_declined(entries[blocker_id], previous)
        entries[blocker_id]["program_count"] = len(entries[blocker_id]["programs"])
        entries[blocker_id]["program_completeness"] = completeness_proof(
            entries[blocker_id]["programs"], expected_denominator=len(rows))

    for task in snapshot["mint_pending"]:
        network = _slug(str(task.get("network") or "network"))
        blocker_id = f"BLK-{network}-portal-access"
        non_operator_status = _NON_OPERATOR_MINT_NETWORK_STATES.get(network)
        if task.get("portal_supported") is False:
            # An unsupported mint adapter is machine work, not a failed login.
            # Keep the task/program pending; this is not an authentication grant.
            non_operator_status = "machine_owned"
        if network == "direct" and _slug(str(task.get("program_id") or "")) == "plexusdx":
            non_operator_status = "superseded_operator_declined"
        seen_derived.add(blocker_id)
        previous = entries.get(blocker_id) or {}
        if non_operator_status == "machine_owned" and task.get("portal_supported") is False:
            cause = f"the {network} API mint task has no supported portal adapter"
            steps = ["machine reconciles account-scoped API capability for the exact queued program; no operator sign-in requested"]
        elif non_operator_status == "machine_owned":
            cause = f"the {network} persistent machine session or provider access path needs machine repair"
            steps = ["machine repairs or refreshes the persistent provider session and resumes the exact queued program"]
        elif non_operator_status == "awaiting_external":
            cause = f"the {network} marketplace account is waiting for provider approval or enablement"
            steps = ["machine monitors provider state; no operator reply or repeated login is requested"]
        elif non_operator_status == "superseded_operator_declined":
            cause = "the only verified path is the separately deferred PlexusDx partner call"
            steps = ["no action; this duplicate access task is superseded by BLK-plexusdx-partner-call"]
        else:
            cause = f"the {task.get('network')} portal blocks the lane's own login"
            steps = [
                f"sign in once to the {task.get('network')} publisher portal as ops@glpclinicscout.com "
                "(solve any human check); the lane stores the session and resumes automatically",
            ]
        entries[blocker_id] = {
            "id": blocker_id,
            "source": "mint_queue",
            "class": "portal_access",
            "cause": cause,
            "programs": [str(task.get("program_id") or "")] if task.get("program_id") else [],
            "steps": steps,
            "detail": str(task.get("detail") or "")[:400],
            "keywords": sorted(_KEYWORDS.get(blocker_id, {network})),
            "created_at_utc": previous.get("created_at_utc") or str(task.get("queued_at_utc") or sync_now()),
            "updated_at_utc": sync_now(),
            "status": non_operator_status or previous.get("status") or "open",
            "ack": None if non_operator_status else previous.get("ack"),
            "operator_action_required": False if non_operator_status else True,
        }
        if not non_operator_status:
            entries[blocker_id] = _preserve_awaiting_external(
                entries[blocker_id], previous, snapshot, now_utc=synchronizer_now_utc)
            entries[blocker_id] = _preserve_operator_declined(entries[blocker_id], previous)

    # Exception cards registered by the reconciler lane (3-block cards carry a
    # blocker id; the ack round-trip works for them exactly like derived entries).
    if intake_documents is None:
        intakes = ([_load_json(path, {}) for path in sorted(INTAKE_DIR.glob("BLK-*.json"))]
                   if INTAKE_DIR.is_dir() else [])
    else:
        intakes = intake_documents
    for intake in intakes:
        blocker_id = str(intake.get("id") or "")
        if not blocker_id:
            continue
        event_id = str(intake.get("event_id") or "")
        event_resolved = (_event_resolved(event_id) if resolved_event_ids is None
                          else event_id in resolved_event_ids)
        if event_resolved:
            if blocker_id in entries and entries[blocker_id].get("status") not in {"verified_cleared", "operator_declined"}:
                entries[blocker_id]["status"] = "verified_cleared"
                entries[blocker_id]["cleared_at_utc"] = sync_now()
                entries[blocker_id]["clearing_evidence"] = "inbox event resolved"
            continue
        seen_derived.add(blocker_id)
        previous = entries.get(blocker_id) or {}
        entries[blocker_id] = {
            "id": blocker_id, "source": "exception_card",
            "class": str(intake.get("exception_type") or "exception"),
            "cause": str(intake.get("cause") or ""),
            "steps": list(intake.get("steps") or []),
            "programs": list(intake.get("programs") or []),
            "keywords": sorted(set(intake.get("keywords") or []) | _KEYWORDS.get(blocker_id, set())),
            "event_id": intake.get("event_id"),
            "created_at_utc": previous.get("created_at_utc") or str(intake.get("created_at_utc") or sync_now()),
            "updated_at_utc": sync_now(),
            "status": previous.get("status") or "open",
            "ack": previous.get("ack"),
            "on_ack": previous.get("on_ack") or {"kind": "resolve_event", "event_id": intake.get("event_id")},
        }
        entries[blocker_id] = _preserve_awaiting_external(
            entries[blocker_id], previous, snapshot, now_utc=synchronizer_now_utc)
        entries[blocker_id] = _preserve_operator_declined(entries[blocker_id], previous)

    for blocker_id, entry in entries.items():
        if entry.get("status") in _TERMINAL_PROGRAM_STATUSES:
            continue
        if entry.get("source") in {"ledger", "mint_queue", "exception_card"} and blocker_id not in seen_derived:
            if entry.get("status") not in {"verified_cleared", "operator_declined"}:
                entry["status"] = "verified_cleared"
                entry["cleared_at_utc"] = sync_now()
                entry["clearing_evidence"] = "ground truth no longer carries this blocker"
        if entry.get("source") == "manual" and entry.get("status") in {"operator_says_done", "awaiting_external"}:
            manually_verified = (_manual_verified(entry) if manually_verified_ids is None
                                 else blocker_id in manually_verified_ids)
            if manually_verified:
                entry["status"] = "verified_cleared"
                entry["cleared_at_utc"] = sync_now()

    out = {
        "schema": "glp-affiliate-operator-blockers-v1",
        "updated_at_utc": sync_now(),
        "blockers": entries,
        "suppressed_classes": suppressed,
        "cleared_by_machine": cleared_by_machine[-20:],
        "machine_followups": store.get("machine_followups") or _default_followups(suppressed),
        "new_operator_rulings": store.get("new_operator_rulings") or [],
    }
    # Projection/sync may refresh every other derived row, but terminal rows are
    # immutable until a two-part provider-restored + explicit-operator-reopen
    # transaction supersedes their ruling.
    out["blockers"].update(terminal_entries)
    return out


def _default_followups(suppressed: dict[str, Any]) -> list[dict[str, Any]]:
    # Fresh standing policy says Awin 2FA is not enabled. Never recreate the
    # invalid TOTP blocker or request a security-setting mutation as a follow-up.
    return []


def _event_resolved(event_id: str) -> bool:
    if not event_id or not INBOX_DB.is_file():
        return False
    try:
        conn = sqlite3.connect(f"file:{INBOX_DB}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT state FROM inbox_events WHERE event_id=?", (event_id,)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return False
    return bool(row) and str(row[0]) in {"resolved_externally", "held", "archived", "resolved"}


def _load_awin_held_terms() -> dict[str, dict[str, str]]:
    """Load the three exact held terms rows from the hash-pinned research bytes."""
    try:
        info = AWIN_TERMS_RESEARCH_PATH.lstat()
        body = AWIN_TERMS_RESEARCH_PATH.read_bytes()
    except OSError as error:
        raise RuntimeError("awin_terms_research_unavailable") from error
    if not stat.S_ISREG(info.st_mode) or AWIN_TERMS_RESEARCH_PATH.is_symlink():
        raise RuntimeError("awin_terms_research_not_regular")
    source_sha = hashlib.sha256(body).hexdigest()
    if source_sha != AWIN_TERMS_RESEARCH_SHA256:
        raise RuntimeError("awin_terms_research_hash_mismatch")
    try:
        document = json.loads(body)
    except ValueError as error:
        raise RuntimeError("awin_terms_research_json_invalid") from error
    holds = {str(row.get("program_id") or ""): str(row.get("reason") or "")
             for row in document.get("fit_activation_holds") or []}
    stages = {str(row.get("program_id") or ""): row
              for row in document.get("stage1_to_5") or []}
    if set(holds) != set(AWIN_HELD_TERMS_IDS) or not set(AWIN_HELD_TERMS_IDS) <= set(stages):
        raise RuntimeError("awin_terms_research_held_denominator_mismatch")
    out: dict[str, dict[str, str]] = {}
    for program_id in AWIN_HELD_TERMS_IDS:
        row = stages[program_id]
        terms = row.get("stage3_affiliate_terms") or {}
        if program_id == "115383":
            evidence = str(terms.get("capture_result") or "")
        else:
            hits = terms.get("mechanical_trigger_hits") or []
            if len(hits) != 1:
                raise RuntimeError(f"awin_terms_trigger_denominator_mismatch:{program_id}")
            evidence = str(hits[0].get("evidence") or "")
        name = str(row.get("name") or "")
        reason = holds[program_id]
        if not name or not evidence or not reason:
            raise RuntimeError(f"awin_terms_evidence_incomplete:{program_id}")
        evidence_sha = hashlib.sha256(evidence.encode("utf-8")).hexdigest()
        out[program_id] = {
            "program_id": program_id,
            "name": name,
            "hold_reason": reason,
            "terms_evidence": evidence,
            "terms_evidence_sha256": evidence_sha,
            "source_research_sha256": source_sha,
        }
    return out


def _terms_decision_receipts(entry: dict[str, Any]) -> list[Path]:
    program_id = str(entry.get("terms_program_id") or "")
    return sorted(OPERATOR_ACTIONS_DIR.glob(f"awin-terms-{program_id}-*.json")) if program_id else []


def _terms_decision_receipt_identity(*, source_message_id: str | None,
                                     provenance: str) -> str:
    message_id = str(source_message_id or "")
    if message_id:
        return message_id
    if provenance:
        return f"provenance:{provenance}"
    raise RuntimeError("awin_terms_decision_provenance_missing")


def _parse_utc_timestamp(value: Any) -> dt.datetime:
    text = str(value or "")
    if not text.endswith("Z"):
        raise ValueError("timestamp_not_utc_z")
    parsed = dt.datetime.fromisoformat(text[:-1] + "+00:00")
    if parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(0):
        raise ValueError("timestamp_not_utc")
    return parsed


def _validate_terms_decision_receipt(entry: dict[str, Any], receipt: dict[str, Any],
                                     *, path: Path | None = None) -> dict[str, Any]:
    """Validate v2 decisions and the two immutable v1 acceptance receipts.

    The normalized return shape is the only decision representation consumed by
    newest-wins reconciliation.  Any file claiming this program identity but not
    satisfying the exact evidence/provenance contract fails closed.
    """
    program_id = str(entry.get("terms_program_id") or "")
    authoritative = _load_awin_held_terms().get(program_id)
    if (not authoritative or entry.get("id") != f"BLK-awin-terms-{program_id}"
            or entry.get("terms_evidence_sha256") != authoritative["terms_evidence_sha256"]
            or entry.get("source_research_sha256") != authoritative["source_research_sha256"]):
        raise RuntimeError(f"awin_terms_decision_stale_evidence:{program_id}")
    if path is not None:
        try:
            info = path.lstat()
        except OSError as error:
            raise RuntimeError(f"awin_terms_decision_receipt_unavailable:{program_id}") from error
        if (not stat.S_ISREG(info.st_mode) or path.is_symlink()
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise RuntimeError(f"awin_terms_decision_receipt_mode_or_type_invalid:{program_id}")
    common = (
        receipt.get("blocker_id") == entry["id"]
        and str(receipt.get("program_id")) == program_id
        and receipt.get("program_name") == authoritative["name"]
        and receipt.get("terms_evidence_sha256") == authoritative["terms_evidence_sha256"]
        and receipt.get("source_research_sha256") == authoritative["source_research_sha256"]
        and receipt.get("provider_write") is False
        and receipt.get("outbound_send") is False
    )
    if not common:
        raise RuntimeError(f"awin_terms_decision_receipt_binding_invalid:{program_id}")
    schema = str(receipt.get("schema") or "")
    if schema == "glp-awin-terms-acceptance-v1":
        source_message_id = str(receipt.get("source_message_id") or "")
        operator_words = str(receipt.get("accepted_text") or "")
        if (receipt.get("status") != "ACCEPTED_EXACT_OPERATOR_ACTION"
                or not source_message_id
                or operator_words != f"ACCEPT-TERMS {program_id}"):
            raise RuntimeError(f"awin_terms_acceptance_v1_invalid:{program_id}")
        provenance = f"discord-message-{source_message_id}"
        decision = "operator_accepted_terms"
        recorded_at_utc = str(receipt.get("recorded_at_utc") or "")
    elif schema == "glp-awin-terms-decision-v2":
        decision = str(receipt.get("decision") or "")
        verb = {"operator_accepted_terms": "ACCEPT-TERMS",
                "operator_rejected_terms": "REJECT-TERMS"}.get(decision)
        decision_text = str(receipt.get("decision_text") or "")
        operator_words = str(receipt.get("operator_words") or "")
        provenance = str(receipt.get("provenance") or "")
        source_message_value = receipt.get("source_message_id")
        source_message_id = "" if source_message_value is None else str(source_message_value)
        recorded_at_utc = str(receipt.get("recorded_at_utc") or "")
        if (receipt.get("status") != "DECIDED_EXACT_OPERATOR_ACTION" or not verb
                or decision_text != f"{verb} {program_id}"
                or receipt.get("decision_verb") != verb
                or not operator_words
                or receipt.get("operator_words_sha256") != hashlib.sha256(
                    operator_words.encode("utf-8")).hexdigest()
                or not provenance
                or receipt.get("reversible_by") != (
                    f"REJECT-TERMS {program_id}" if decision == "operator_accepted_terms"
                    else f"ACCEPT-TERMS {program_id}")):
            raise RuntimeError(f"awin_terms_decision_v2_invalid:{program_id}")
        _terms_decision_receipt_identity(
            source_message_id=source_message_id or None, provenance=provenance)
    else:
        raise RuntimeError(f"awin_terms_decision_schema_invalid:{program_id}")
    try:
        timestamp = _parse_utc_timestamp(recorded_at_utc)
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"awin_terms_decision_timestamp_invalid:{program_id}") from error
    authority_identity = _terms_decision_receipt_identity(
        source_message_id=source_message_id or None, provenance=provenance)
    if (path is not None and path.name !=
            f"awin-terms-{program_id}-{hashlib.sha256(authority_identity.encode('utf-8')).hexdigest()[:16]}.json"):
        raise RuntimeError(f"awin_terms_decision_receipt_filename_invalid:{program_id}")
    return {
        "schema": schema,
        "decision": decision,
        "program_id": program_id,
        "program_name": authoritative["name"],
        "operator_words": operator_words,
        "provenance": provenance,
        "source_message_id": source_message_id or None,
        "recorded_at_utc": recorded_at_utc,
        "_timestamp": timestamp,
        "_authority_identity": authority_identity,
        "receipt_path": str(path) if path is not None else None,
        "receipt_sha256": hashlib.sha256(path.read_bytes()).hexdigest() if path is not None else None,
    }


def _write_terms_decision_receipt(entry: dict[str, Any], *, decision: str,
                                  operator_words: str, provenance: str,
                                  decision_text: str | None = None,
                                  source_message_id: str | None = None,
                                  recorded_at_utc: str | None = None,
                                  output_dir: Path | None = None) -> dict[str, Any]:
    """Persist one provider-free, evidence-bound exact terms decision receipt."""
    program_id = str(entry.get("terms_program_id") or "")
    verb = {"operator_accepted_terms": "ACCEPT-TERMS",
            "operator_rejected_terms": "REJECT-TERMS"}.get(decision)
    exact = f"{verb} {program_id}" if verb else ""
    decision_text = decision_text or exact
    if (not verb or decision_text != exact or not operator_words
            or entry.get("id") != f"BLK-awin-terms-{program_id}"):
        raise RuntimeError("awin_terms_decision_not_exact")
    authoritative = _load_awin_held_terms().get(program_id)
    if (not authoritative
            or entry.get("terms_evidence_sha256") != authoritative["terms_evidence_sha256"]
            or entry.get("source_research_sha256") != authoritative["source_research_sha256"]):
        raise RuntimeError("awin_terms_decision_stale_evidence")
    identity = _terms_decision_receipt_identity(
        source_message_id=source_message_id, provenance=provenance)
    receipt = {
        "schema": "glp-awin-terms-decision-v2",
        "status": "DECIDED_EXACT_OPERATOR_ACTION",
        "blocker_id": entry["id"],
        "program_id": program_id,
        "program_name": authoritative["name"],
        "decision": decision,
        "decision_verb": verb,
        "decision_text": exact,
        "operator_words": operator_words,
        "operator_words_sha256": hashlib.sha256(operator_words.encode("utf-8")).hexdigest(),
        "provenance": provenance,
        "source_message_id": str(source_message_id) if source_message_id is not None else None,
        "terms_evidence_sha256": authoritative["terms_evidence_sha256"],
        "source_research_sha256": authoritative["source_research_sha256"],
        "recorded_at_utc": recorded_at_utc or _now_utc(),
        "reversible_by": (f"REJECT-TERMS {program_id}"
                          if decision == "operator_accepted_terms"
                          else f"ACCEPT-TERMS {program_id}"),
        "provider_write": False,
        "outbound_send": False,
    }
    _validate_terms_decision_receipt(entry, receipt)
    body = (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode("utf-8")
    root = Path(output_dir) if output_dir is not None else OPERATOR_ACTIONS_DIR
    root.mkdir(parents=True, exist_ok=True)
    suffix = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    path = root / f"awin-terms-{program_id}-{suffix}.json"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        try:
            existing = json.loads(path.read_text())
        except (OSError, ValueError) as error:
            raise RuntimeError(f"awin_terms_decision_receipt_conflict:{program_id}:{identity}") from error
        # The two already-ingested 2026-08-12 ACCEPT decisions are immutable v1
        # receipts at the same message-derived path. Treat exact logical replay as
        # idempotent; every opposite or drifted replay remains a hard conflict.
        if existing.get("schema") == "glp-awin-terms-acceptance-v1":
            try:
                normalized = _validate_terms_decision_receipt(entry, existing, path=path)
            except RuntimeError as error:
                raise RuntimeError(
                    f"awin_terms_decision_receipt_conflict:{program_id}:{identity}") from error
            if (decision == "operator_accepted_terms"
                    and normalized["source_message_id"] == str(source_message_id or "")
                    and decision_text == normalized["operator_words"]):
                return {"receipt": str(path),
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        "created": False, "schema": existing["schema"]}
        stable = {key: value for key, value in receipt.items() if key != "recorded_at_utc"}
        existing_stable = {key: value for key, value in existing.items() if key != "recorded_at_utc"}
        if stable != existing_stable or stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise RuntimeError(f"awin_terms_decision_receipt_conflict:{program_id}:{identity}")
        _validate_terms_decision_receipt(entry, existing, path=path)
        return {"receipt": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "created": False, "schema": existing["schema"]}
    os.chmod(path, 0o600)
    _validate_terms_decision_receipt(entry, receipt, path=path)
    return {"receipt": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "created": True, "schema": receipt["schema"]}


def _write_terms_acceptance_receipt(entry: dict[str, Any], *, message_id: str,
                                    accepted_text: str,
                                    recorded_at_utc: str | None = None) -> dict[str, Any]:
    """Backward-compatible callable that now issues canonical v2 decisions."""
    return _write_terms_decision_receipt(
        entry, decision="operator_accepted_terms", operator_words=accepted_text,
        decision_text=accepted_text,
        provenance=f"discord-message-{message_id}", source_message_id=str(message_id),
        recorded_at_utc=recorded_at_utc,
    )


def _current_terms_decision(entry: dict[str, Any]) -> dict[str, Any] | None:
    decisions: list[dict[str, Any]] = []
    for path in _terms_decision_receipts(entry):
        try:
            receipt = json.loads(path.read_text())
        except (OSError, ValueError) as error:
            raise RuntimeError(
                f"awin_terms_decision_receipt_invalid_json:{entry.get('terms_program_id')}:{path.name}") from error
        decisions.append(_validate_terms_decision_receipt(entry, receipt, path=path))
    if not decisions:
        return None
    by_identity: dict[str, dict[str, Any]] = {}
    for row in decisions:
        identity = row["_authority_identity"]
        previous = by_identity.get(identity)
        if previous and previous["receipt_sha256"] != row["receipt_sha256"]:
            raise RuntimeError(
                f"awin_terms_decision_duplicate_identity_conflict:{row['program_id']}:{identity}")
        by_identity[identity] = row
    decisions = list(by_identity.values())
    # Timestamp is the primary authority clock. Immutable identity and receipt
    # hash make equal-timestamp ordering total and independent of filesystem order.
    return max(decisions, key=lambda row: (
        row["_timestamp"], row["_authority_identity"], row["receipt_sha256"] or ""))


def _manual_verified(entry: dict[str, Any]) -> bool:
    """Machine evidence that a manual blocker actually cleared."""
    rule = str(entry.get("verify_rule") or "")
    if rule in {"awin_terms_acceptance_receipt", "awin_terms_decision_receipt"}:
        program_id = str(entry.get("terms_program_id") or "")
        authoritative = _load_awin_held_terms().get(program_id)
        if (not authoritative or entry.get("id") != f"BLK-awin-terms-{program_id}"
                or entry.get("terms_evidence_sha256") != authoritative["terms_evidence_sha256"]
                or entry.get("source_research_sha256") != authoritative["source_research_sha256"]):
            return False
        decision = _current_terms_decision(entry)
        return bool(decision and decision["decision"] == "operator_accepted_terms")
    if rule == "approval_fact_program" and entry.get("verify_program_match"):
        if not INBOX_DB.is_file():
            return False
        try:
            conn = sqlite3.connect(f"file:{INBOX_DB}?mode=ro", uri=True)
            try:
                rows = conn.execute("SELECT program_id FROM affiliate_approval_facts").fetchall()
            finally:
                conn.close()
        except sqlite3.Error:
            return False
        needle = str(entry["verify_program_match"]).lower()
        return any(needle in str(pid).lower() for (pid,) in rows)
    return False


def seed_manual_blockers(store: dict[str, Any], *,
                         synchronizer_now_utc: str | None = None) -> dict[str, Any]:
    """Migrate currently-known operator items INTO the store (once; idempotent)."""
    def sync_now() -> str:
        return synchronizer_now_utc or _now_utc()

    entries = store.setdefault("blockers", {})
    seeds = {
        "BLK-katalys-w9": {
            "cause": 'Katalys cannot pay us: their AP team (ap@katalys.com, Jul 14, "Katalys Onboarding | '
                     'Next Steps - Payment Information", Application ID 46734157436) needs a W-9 and a '
                     "Direct Deposit form — two HubSpot form links inside that email",
            "steps": [
                "open that email in the ops@glpclinicscout.com inbox",
                "complete the W-9 form link as Tilton Group LLC",
                "complete the Direct Deposit form link with the business bank account",
                "reply here: DONE BLK-katalys-w9",
            ],
            "event_id": "evt_075fc289cce733d083c5afc9726a",
            "on_ack": {
                "kind": "resolve_event",
                "event_id": "evt_075fc289cce733d083c5afc9726a",
                "note": "payout prerequisites met for Katalys: operator completed the W-9 and Direct "
                        "Deposit forms (application 46734157436); account_prerequisite exception cleared",
            },
            "verify_rule": "approval_fact_program",
            "verify_program_match": "katalys",
        },
        "BLK-plexusdx-partner-call": {
            "cause": "PlexusDx has no self-serve application and its official terms URL currently serves unrelated content; the only verified partner CTA is Izaiah's Calendly booking",
            "steps": [
                "open https://calendly.com/avalentine-plexusdx/",
                "book a time using Izaiah's identity and real availability",
                "ask PlexusDx for the current written affiliate terms and application path; do not accept terms, provide tax/payment data, or complete KYC on the call",
                "reply here: DONE BLK-plexusdx-partner-call",
            ],
            "verify_rule": "approval_fact_program",
            "verify_program_match": "plexusdx",
        },
        "BLK-t2d-live-application": {
            "cause": "T2D's official agent signup says it is a mock demonstration form and asks for payout/tax identity fields, so the machine cannot lawfully submit it as a live application",
            "steps": [
                "open https://t2d.com/agent/sign-up",
                "obtain a live non-demo application from T2D or written confirmation that this form is production",
                "only if confirmed live, complete the payout/tax identity fields and submit from Izaiah's account",
                "reply here: DONE BLK-t2d-live-application",
            ],
            "verify_rule": "approval_fact_program",
            "verify_program_match": "t2d",
        },
    }
    held_terms = _load_awin_held_terms()
    for program_id in AWIN_HELD_TERMS_IDS:
        row = held_terms[program_id]
        blocker_id = f"BLK-awin-terms-{program_id}"
        seed = {
            "cause": (
                f"Awin program {row['name']} ({program_id}) is held for future material expansion. "
                f"Exact current terms evidence: {row['terms_evidence']} Existing live links are preserved; "
                "only future material expansion is held."
            ),
            "steps": [
                f"review the exact evidence above for Awin program {row['name']} ({program_id})",
                "accept only if you intentionally approve these exact current terms for future material expansion, or reject to keep future material expansion held; either choice records a local receipt and performs no provider write",
                f"reply exactly: ACCEPT-TERMS {program_id}",
                f"or reply exactly: REJECT-TERMS {program_id}",
            ],
            "reply_syntax": [f"ACCEPT-TERMS {program_id}", f"REJECT-TERMS {program_id}"],
            "terms_program_id": program_id,
            "terms_program_name": row["name"],
            "terms_evidence": row["terms_evidence"],
            "terms_evidence_sha256": row["terms_evidence_sha256"],
            "source_research_sha256": row["source_research_sha256"],
            "verify_rule": "awin_terms_decision_receipt",
            "operator_action_required": True,
        }
        current = _current_terms_decision({"id": blocker_id, **seed})
        if current:
            visible = {key: value for key, value in current.items()
                       if not key.startswith("_")}
            seed["current_terms_decision"] = visible
            seed["current_terms_decision_status"] = current["decision"]
            seed["operator_action_required"] = False
            if current["decision"] == "operator_accepted_terms":
                seed["seed_status"] = "verified_cleared"
            else:
                seed["seed_status"] = "operator_rejected_terms"
                seed["cause"] = (
                    f"Awin program {row['name']} ({program_id}) current decision is operator_rejected_terms. "
                    f"Exact current terms evidence: {row['terms_evidence']} Exact operator words: "
                    f"{current['operator_words']} Provenance: {current['provenance']}. Existing live links "
                    f"are preserved; future material expansion remains held and is reversible by "
                    f"ACCEPT-TERMS {program_id}."
                )
        seeds[blocker_id] = seed
    for blocker_id, seed in seeds.items():
        target_status = seed.pop("seed_status", "open")
        if blocker_id not in entries:
            entries[blocker_id] = {
                "id": blocker_id,
                "source": "manual",
                "class": "manual",
                "programs": [],
                "keywords": sorted(_KEYWORDS.get(blocker_id, set())),
                "created_at_utc": sync_now(),
                "updated_at_utc": sync_now(),
                "status": target_status,
                "ack": None,
                **seed,
            }
        elif blocker_id.startswith("BLK-awin-terms-"):
            existing = entries[blocker_id]
            stale = (existing.get("terms_evidence_sha256") != seed["terms_evidence_sha256"]
                     or existing.get("source_research_sha256") != seed["source_research_sha256"])
            if stale:
                existing.update({
                    **seed, "source": "manual", "class": "manual", "programs": [],
                    "status": target_status, "ack": None, "updated_at_utc": sync_now(),
                    "stale_acceptance_invalidated": True,
                })
            else:
                # Current receipt authority, not historical blocker status,
                # deterministically projects accepted/rejected/open state.
                existing.update(seed)
                existing["status"] = target_status
                existing["ack"] = None if target_status != "open" else existing.get("ack")
                existing["updated_at_utc"] = sync_now()
    # Historical versions seeded routine terms/copy review as operator work.
    # Migrate it to the machine research queue unless a separate hard-trigger
    # receipt is explicitly bound; ordinary legal/copy uncertainty is never a
    # human interruption by itself.
    legacy = entries.get("BLK-coreage-katalys-terms")
    if (legacy and legacy.get("status") != "verified_cleared"
            and not legacy.get("hard_contract_trigger_receipt")):
        legacy.update({"status": "machine_research", "operator_action_required": False,
                       "machine_research_reason": "routine_terms_review_no_hard_contract_trigger",
                       "updated_at_utc": sync_now()})
    telewellness = entries.get("BLK-docusign-telewellnessmd")
    if telewellness and telewellness.get("status") != "verified_cleared":
        telewellness.update({
            "status": "superseded_relationship_adverse", "operator_action_required": False,
            "superseded_by_event_id": "evt_e7740747bf24c940900f895f7156",
            "superseded_reason": "latest partner reply states the partnership will not move forward",
            "updated_at_utc": sync_now(),
        })
    return store


# ------------------------------------------------------------------------- compose
def _open_blockers(store: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted(
        (e for e in (store.get("blockers") or {}).values()
         if e.get("status") in {"open", "operator_says_done"}
         and e.get("operator_action_required") is not False),
        key=lambda e: (0 if e.get("status") == "open" else 1, str(e.get("id"))),
    )


def _blocker_action_chunks(entry: dict[str, Any], *, aggregate_proof: dict[str, Any] | None = None) -> list[str]:
    """Render one blocker into complete deterministic continuation chunks."""
    programs = list(entry.get("programs") or [])
    declared = int(entry.get("program_count") if entry.get("program_count") is not None else len(programs))
    if declared != len(programs):
        raise RuntimeError(f"blocker_program_denominator_mismatch:{entry.get('id')}")
    proof = entry.get("program_completeness") or completeness_proof(programs, expected_denominator=declared)
    validate_completeness(programs, proof, expected_denominator=declared)
    if aggregate_proof is not None:
        aggregate_rows = list(aggregate_proof.get("programs") or [])
        aggregate_denominator = int(aggregate_proof.get("denominator") or 0)
        validate_completeness(
            aggregate_rows, aggregate_proof["proof"],
            expected_denominator=aggregate_denominator,
        )
    identities = [exact_program_identity(program) for program in programs]
    cause = str(entry.get("cause") or "")
    steps = list(entry.get("steps") or [])
    if len(cause) < 25 or not steps:
        raise RuntimeError(f"blocker_card_missing_block:{entry.get('id')}")
    def render_chunk(start: int, end: int, part: int, total_parts: int) -> str:
        heading = title("action", "GLP Affiliate Ops",
                        f"{entry['id']} · part {part}/{total_parts} · exact programs {start + 1 if identities else 0}-{end}/{declared}")
        body = [f"**`{entry['id']}`** — {cause}" if part == 1 else
                f"**`{entry['id']}`** — continuation; same blocker and action"]
        if part == 1:
            body.extend(f"{index}. {step}" for index, step in enumerate(steps, 1))
        body.extend(f"• {identity}" for identity in identities[start:end])
        body.append(proof_line(proof, start=(start + 1 if identities else 0), end=end))
        if aggregate_proof is not None:
            body.append(
                "aggregate exact actionable denominator: "
                f"{aggregate_proof['denominator']} · sha256:{aggregate_proof['proof']['identity_sha256']}"
            )
        reply_syntax = entry.get("reply_syntax") or f"DONE {entry['id']}"
        replies = list(reply_syntax) if isinstance(reply_syntax, list) else [str(reply_syntax)]
        for reply in replies:
            body.append(f"reply: `{reply}`")
        body.append("→ Complete every listed identity; continuation parts are one exact denominator, not samples.")
        body.append(subtext("state: operator-blockers.json"))
        return render(heading, *body, output_class="actionable")

    def chunk_is_complete(chunk: str, start: int, end: int) -> bool:
        expected_here = identities[start:end]
        return (
            len(chunk) <= 1880
            and lint(chunk, output_class="actionable").get("status") == "pass"
            and all("• " + identity in chunk for identity in expected_here)
            and f"sha256:{proof['identity_sha256']}" in chunk
        )

    slices: list[tuple[int, int]] = []
    cursor = 0
    while cursor < len(identities):
        selected_end: int | None = None
        for end in range(min(cursor + 8, len(identities)), cursor, -1):
            provisional = render_chunk(cursor, end, len(slices) + 1, 999)
            if chunk_is_complete(provisional, cursor, end):
                selected_end = end
                break
        if selected_end is None:
            raise RuntimeError(f"blocker_chunk_cannot_fit_without_loss:{entry.get('id')}:{len(slices) + 1}")
        slices.append((cursor, selected_end))
        cursor = selected_end
    if not slices:
        slices = [(0, 0)]

    rendered: list[str] = []
    for part, (start, end) in enumerate(slices, 1):
        chunk = render_chunk(start, end, part, len(slices))
        if not chunk_is_complete(chunk, start, end):
            raise RuntimeError(f"blocker_chunk_cannot_fit_without_loss:{entry.get('id')}:{part}")
        rendered.append(chunk)
    validate_rendered_chunks(programs, proof, rendered)
    return rendered


def compose_report(snapshot: dict[str, Any], store: dict[str, Any]) -> list[str]:
    """Render the daily card as all required lint-clean messages ≤1900 chars.

    Discord delivery is chunked, but it is lossless: there is deliberately no
    maximum number of chunks because every open blocker must reach the operator.
    """
    date = snapshot["date_pt"]
    opens = [e for e in _open_blockers(store) if e.get("status") == "open"]
    says_done = [e for e in _open_blockers(store) if e.get("status") == "operator_says_done"]
    waiting = sorted((e for e in (store.get("blockers") or {}).values()
                      if e.get("status") == "awaiting_external"
                      and e.get("exclude_from_waiting_footer") is not True), key=lambda e: str(e.get("id")))
    aggregate_programs = [program for entry in opens for program in list(entry.get("programs") or [])]
    aggregate_denominator = len(aggregate_programs)
    aggregate = {
        "programs": aggregate_programs,
        "denominator": aggregate_denominator,
        "proof": completeness_proof(aggregate_programs, expected_denominator=aggregate_denominator),
    }
    lines = [
        f"awaiting network review: **{len(snapshot['in_review'])}** · "
        f"approvals recorded today: **{snapshot['approvals_today']}**"]
    housekeeping = []
    if snapshot["machine_defects"]:
        housekeeping.append(f"machine repairs queued (not yours): **{len(snapshot['machine_defects'])}**")
    if snapshot.get("machine_research"):
        housekeeping.append(f"machine research queued (not yours): **{len(snapshot['machine_research'])}**")
    if snapshot["parked"]:
        housekeeping.append(f"parked out-of-lane: **{len(snapshot['parked'])}**")
    hygiene_receipt = _load_json(
        Path("/home/ubuntu/business/reports/glp-clinic-scout/affiliate-machine/hygiene/hygiene-latest.json"), {})
    if str(hygiene_receipt.get("started_at_utc", ""))[:10] == str(snapshot.get("collected_at_utc", ""))[:10]:
        acts = hygiene_receipt.get("actions") or {}
        withdrawn = sum(1 for e in acts.get("withdrawal_attempts") or [] if e.get("withdrawn_verified"))
        housekeeping.append(
            f"legacy hygiene: **{withdrawn}** withdrawn · "
            f"**{acts.get('suppressed_new', 0)}** newly suppressed · "
            f"**{hygiene_receipt.get('ledger_entries_total', 0)}** suppressed total")
    if housekeeping:
        lines.append(" · ".join(housekeeping))
    if says_done:
        lines.append("done by you, machine verifying: " + ", ".join(f"`{e['id']}`" for e in says_done[:4]))
    if waiting:
        ids = ", ".join(f"`{entry['id']}`" for entry in waiting[:6])
        extra = f" +{len(waiting) - 6} more" if len(waiting) > 6 else ""
        lines.append(f"waiting on networks/system: **{len(waiting)}** — {ids}{extra} (machine watching; no reply needed)")
    cleared = [c for c in (store.get("cleared_by_machine") or [])
               if str(c.get("at_utc", ""))[:10] >= date]
    for item in cleared[:3]:
        lines.append(f"cleared by machine: `{item['id']}` — {item['note']}")
    new_rulings = [r for r in (store.get("new_operator_rulings") or [])
                   if str(r.get("captured_at_utc", ""))[:10] >= date]
    if new_rulings:
        for ruling in sorted(new_rulings, key=lambda row: str(row.get("instruction_key") or "")):
            key = str(ruling.get("instruction_key") or "")
            value = str(ruling.get("value") or ruling.get("verdict") or "")
            lines.append(f"new operator ruling ingested: `{key}` = `{value}`")
    rejected_terms = sorted(
        (e for e in (store.get("blockers") or {}).values()
         if e.get("current_terms_decision_status") == "operator_rejected_terms"),
        key=lambda e: str(e.get("terms_program_id") or ""),
    )
    for entry in rejected_terms:
        decision = entry["current_terms_decision"]
        lines.append(
            f"terms decision: {entry['terms_program_name']} ({entry['terms_program_id']}) = "
            f"`operator_rejected_terms` · exact words: {decision['operator_words']} · "
            f"provenance: {decision['provenance']} · reversible by "
            f"`ACCEPT-TERMS {entry['terms_program_id']}` · evidence: {entry['terms_evidence']}"
        )
    lines.append(f"blocked on you: **{len(opens)}**" + (" — cards follow" if opens else " — nothing needed today"))

    head = title("report", "GLP Affiliate Ops", f"daily report {date}")
    chunk_classes = ["report"]
    chunks = [render(
        head, *lines,
        "→ Use each blocker card's exact reply syntax; terms cards require `ACCEPT-TERMS <program_id>` or `REJECT-TERMS <program_id>` and never accept DONE or fuzzy text.",
        subtext("artifacts: reports/glp-clinic-scout/affiliate-machine/daily-reports"),
        output_class="report",
    )]

    for entry in opens:
        entry_chunks = _blocker_action_chunks(entry, aggregate_proof=aggregate)
        chunks.extend(entry_chunks)
        chunk_classes.extend(["actionable"] * len(entry_chunks))
    errors = []
    for chunk, cls in zip(chunks, chunk_classes):
        verdict = lint(chunk, output_class=cls)
        if verdict.get("status") != "pass":
            errors.extend(verdict.get("errors") or ["unknown_lint_error"])
    if errors:
        raise RuntimeError("daily_report_lint_failed:" + ";".join(errors[:4]))
    return chunks


# ---------------------------------------------------------------------------- post
def post_report(chunks: list[str], date: str, *, live: bool) -> list[str]:
    """Queue the card(s) through the canonical dispatcher (notification_outbox)."""
    cp = load_control_plane()
    ledger = cp.Ledger(INBOX_DB)
    try:
        outbox_ids = []
        for index, chunk in enumerate(chunks, 1):
            key = f"{REPORT_KEY}:{date}:p{index}"
            if live:
                outbox_ids.append(ledger.queue_notification(
                    None, PURPOSE, cp.DECISIONS_CHANNEL_ID, key, chunk,
                    verify_existing_payload=True,
                ))
            else:
                outbox_ids.append(f"dry:{key}")
        return outbox_ids
    finally:
        ledger.close()


# ---------------------------------------------------------------------------- acks
_DONE_RE = re.compile(r"^\s*done\s+(BLK-[a-z0-9-]+)\s*$", re.I)
_TERMS_DECISION_RE = re.compile(r"^(ACCEPT|REJECT)-TERMS ([0-9]+)$")
_ACCEPT_TERMS_RE = re.compile(r"^ACCEPT-TERMS ([0-9]+)$")  # compatibility alias
_ACK_NEGATION_RE = re.compile(
    r"(?:\bnot\b|n't\b|\bnever\b|\bstill\b|\byet\b|\bpending\b|\bwaiting\b|"
    r"\bawaiting\b|\bcannot\b|\bcan\s+not\b|\bno[ -]response\b|\bno[ -]url\b|\bmissing\b)", re.I)
_STATE_UPDATE_RE = re.compile(
    r"(?:\bpending\b|\bawaiting\b|\bwaiting\b|\bstill\b|\bno[ -]response\b|"
    r"\bcannot\b|\bcan['’]?t\b|\bno[ -]url\b|\bmissing[ -]row\b|\bmissing\b)", re.I)
_OPERATOR_CREDENTIAL_RE = re.compile(
    r"(?i)\b(api[ _-]?key|secret[ _-]?key|client[ _-]?secret|access[ _-]?token|"
    r"refresh[ _-]?token|password|passphrase)\b\s*(?::|=|\bis\b)\s*[^\s,;]+"
)
_OPERATOR_TOKEN_RE = re.compile(r"\b(?:kapi|sk|pk)[_-][A-Za-z0-9_-]{8,}\b", re.I)
_DECLINE_RE = re.compile(r"\b(?:decline|skip|not gonna|not going to|won['’]?t|do not want|don['’]?t feel like)\b", re.I)
_REOPEN_RE = re.compile(r"\b(?:reopen|resume|do this now|try this now|put .* back|unskip)\b", re.I)
_NEGATED_DECLINE_RE = re.compile(
    r"\b(?:do\s+not|don['’]?t|never|not)\s+(?:decline|skip)\b", re.I)
_NEGATED_REOPEN_RE = re.compile(
    r"\b(?:do\s+not|don['’]?t|never|not)\s+(?:reopen|resume|unskip|try\s+this\s+now|do\s+this\s+now)\b",
    re.I,
)
_EXACT_47_CONFIRM_RE = re.compile(r"\bconfirm\s+and\s+join\s+all\s+47\s+pending\s+programs\b", re.I)
_EXACT_47_COMPLETE_RE = re.compile(r"\b(?:only\s+included|included)\s+3\s+of\s+the\s+47\b", re.I)
_AWIN_2FA_DISABLED_RE = re.compile(r"\bawin\s+does\s+not\s+have\s+2fa\s+enabled\b", re.I)
_EXACT_47_NEGATED_JOIN_RE = re.compile(
    r"\b(?:do\s+not|don['’]?t|never|not)\s+(?:confirm\s+and\s+)?join\b[^.;\n]{0,80}\b47\b", re.I)
_EXACT_47_NEGATED_COMPLETE_RE = re.compile(
    r"\b(?:do\s+not|don['’]?t|never|not)\s+(?:list|include|show)\b[^.;\n]{0,80}\b47\b", re.I)
_AWIN_2FA_ENABLED_RE = re.compile(r"\bawin\s+(?:does\s+have|has|uses|requires|is)\b[^.;\n]{0,30}\b2fa\b|\bawin\s+2fa\s+is\s+enabled\b", re.I)
_DURABLE_INSTRUCTION_VERB_RE = re.compile(
    r"\b(?:use|require|keep|add|remove|route|load|find|email|apply|include|exclude|"
    r"wait|monitor|reply|draft|review|fix|group|show|list|ensure|follow|capture|persist|"
    r"send|record|bind|verify|check|update|change|move|hold|allow|block|stop|start)\b",
    re.I,
)
_BLK_TOKEN_CANDIDATE_RE = re.compile(
    r"(?<![A-Za-z0-9_])BLK(?:[-_][A-Za-z0-9_-]*|(?=\b))", re.I)
_BLK_TOKEN_VALID_RE = re.compile(r"BLK-[a-z0-9]+(?:-[a-z0-9]+)*", re.I)


def _sanitize_operator_words(text: str) -> str:
    value = _OPERATOR_CREDENTIAL_RE.sub(lambda match: f"{match.group(1)}: [REDACTED]", str(text or ""))
    return _OPERATOR_TOKEN_RE.sub("[REDACTED]", value)


def _mixed_invalid_blocker_tokens(line: str, known_ids: dict[str, set[str]]) -> list[str]:
    """Return unknown/malformed BLK tokens mixed with a known exact ID.

    An instruction line cannot use a known blocker as an authority anchor while
    smuggling a second, unresolvable blocker identity into the same command.
    Unknown-only prose retains the historical clarification behaviour; the
    fail-closed message poison applies when at least one exact known ID and at
    least one unknown or malformed BLK token share a line.
    """
    canonical = {str(blocker_id).lower() for blocker_id in known_ids}
    candidates = [match.group(0) for match in _BLK_TOKEN_CANDIDATE_RE.finditer(str(line or ""))]
    known = [token for token in candidates
             if _BLK_TOKEN_VALID_RE.fullmatch(token) and token.lower() in canonical]
    invalid = [token for token in candidates
               if not _BLK_TOKEN_VALID_RE.fullmatch(token) or token.lower() not in canonical]
    return list(dict.fromkeys(invalid)) if known and invalid else []


def _instruction_for_line(line: str, known_ids: dict[str, set[str]], *, message_id: str = "") -> dict[str, Any] | None:
    """Parse the explicit exact-one-known-BLK command grammar.

    Command words are interpreted only after their own negation scope is
    removed.  Unallowlisted one-word prose is not an instruction; concise
    allowlisted commands such as ``BLK-x - skip`` are durable.
    """
    canonical = {blocker_id.lower(): blocker_id for blocker_id in known_ids}
    hits = {canonical[token] for token in re.findall(r"blk-[a-z0-9-]+", line.lower()) if token in canonical}
    remainder = re.sub(r"blk-[a-z0-9-]+", " ", line, flags=re.I)
    if len(hits) != 1:
        return None
    blocker_id = next(iter(hits))
    safe = _sanitize_operator_words(line)
    words_sha = hashlib.sha256(safe.encode("utf-8")).hexdigest()
    out: dict[str, Any] = {"blocker_id": blocker_id, "operator_words": safe,
                           "operator_words_sha256": words_sha,
                           "state_updates": [], "operational_rulings": [], "queue_requests": [],
                           "intent_kinds": set(), "ambiguous": False}

    negated_decline = bool(_NEGATED_DECLINE_RE.search(safe))
    negated_reopen = bool(_NEGATED_REOPEN_RE.search(safe))
    decline_scope_removed = _NEGATED_DECLINE_RE.sub(" ", safe)
    reopen_scope_removed = _NEGATED_REOPEN_RE.sub(" ", safe)
    positive_decline = bool(_DECLINE_RE.search(decline_scope_removed))
    positive_reopen = bool(_REOPEN_RE.search(reopen_scope_removed))
    if negated_decline:
        out["intent_kinds"].add("forbid_decline")
    if negated_reopen:
        out["intent_kinds"].add("forbid_reopen")
    if positive_decline:
        out["intent_kinds"].add("decline")
    if positive_reopen:
        out["intent_kinds"].add("reopen")
    if positive_decline and positive_reopen:
        out["ambiguous"] = True
        return out
    if positive_decline:
        out["state_updates"].append({"blocker_id": blocker_id, "state_kind": "operator_declined",
                                     "operator_words": safe})
        return out
    if positive_reopen:
        out["state_updates"].append({"blocker_id": blocker_id, "state_kind": "reopen",
                                     "operator_words": safe})
        return out

    lower = safe.lower()
    if blocker_id == "BLK-incomplete-awaiting-external" and any(
            marker in lower for marker in ("47", "join", "awin", "2fa")):
        complete = bool(_EXACT_47_COMPLETE_RE.search(safe))
        no_complete = bool(_EXACT_47_NEGATED_COMPLETE_RE.search(safe))
        no_2fa = bool(_AWIN_2FA_DISABLED_RE.search(safe))
        has_2fa = bool(_AWIN_2FA_ENABLED_RE.search(safe))
        join = bool(_EXACT_47_CONFIRM_RE.search(safe))
        no_join = bool(_EXACT_47_NEGATED_JOIN_RE.search(safe))
        out["intent_kinds"].update(
            ({"require_complete"} if complete else set())
            | ({"forbid_complete"} if no_complete else set())
            | ({"awin_2fa_disabled"} if no_2fa else set())
            | ({"awin_2fa_enabled"} if has_2fa else set())
            | ({"queue_exact"} if join else set())
            | ({"forbid_exact_apply"} if no_join else set())
        )
        if ((complete and no_complete) or (no_2fa and has_2fa) or (join and no_join)
                or not (complete or no_2fa or join)):
            out["ambiguous"] = True
            return out
        if complete:
            out["operational_rulings"].append({
             "instruction_key": "blocker_cards.require_complete_program_denominator",
             "value": "REQUIRE_EXACT_COMPLETE_DENOMINATOR",
             "reason": "operator requires all exact blocker programs in actionable continuation chunks",
             "operator_words_sha256": words_sha})
        if no_2fa:
            out["operational_rulings"].append({
             "instruction_key": "provider.awin.two_factor", "value": "NOT_ENABLED",
             "reason": "operator states Awin 2FA is not enabled; invalid TOTP must not remain the blocker",
             "operator_words_sha256": words_sha})
        if join:
            out["operational_rulings"].append({
             "instruction_key": f"apply_run.message:{message_id}:exact_inventory",
             "value": "QUEUE_EXACT_CANONICAL_INVENTORY",
             "reason": "operator authorizes complete disposition of exact reviewed 47-program inventory",
             "operator_words_sha256": words_sha})
            out["queue_requests"] = [{"kind": "exact_canonical_inventory", "denominator": 47,
                                      "source_message_id": str(message_id)}]
        return out
    state = _state_update_for_line(safe, known_ids)
    if state:
        out["intent_kinds"].add("awaiting_external")
        out["state_updates"].append(state)
    else:
        tokens = re.findall(r"[a-z0-9]+", remainder, re.I)
        if ((len(tokens) < 2 or not _DURABLE_INSTRUCTION_VERB_RE.search(remainder))
                and not (negated_decline or negated_reopen)):
            return None
        out["operational_rulings"].append({
            # Multiple substantive lines for one blocker are independent
            # immutable instructions, not duplicate keys that invalidate the
            # whole document.  Exact replay remains byte-identical.
            "instruction_key": f"blocker:{blocker_id.lower()}:instruction:{words_sha[:16]}",
            "value": "DURABLE_BLOCKER_INSTRUCTION",
            "reason": safe[:400], "operator_words_sha256": words_sha,
        })
    return out


def _execute_on_ack(cp, ledger, entry: dict[str, Any], message_id: str) -> dict[str, Any]:
    """Run the blocker's machine follow-through: receipt the operator action and,
    when the blocker is bound to an inbox event, resolve it under the operator's
    own DONE message as authority so the reconciler completes it next pass."""
    hook = entry.get("on_ack") or {}
    receipt = {
        "schema": "glp-affiliate-operator-action-v1",
        "blocker_id": entry["id"],
        "operator_message_id": message_id,
        "note": str(hook.get("note") or f"operator confirmed {entry['id']} done"),
        "recorded_at_utc": _now_utc(),
        "provider_mutation": False,
        "outbound_send": False,
    }
    OPERATOR_ACTIONS_DIR.mkdir(parents=True, exist_ok=True)
    receipt_path = OPERATOR_ACTIONS_DIR / f"{entry['id']}-{_pt_today()}.json"
    _atomic_write(receipt_path, receipt)
    result: dict[str, Any] = {"receipt": str(receipt_path)}
    event_id = str(hook.get("event_id") or entry.get("event_id") or "")
    if hook.get("kind") == "resolve_event" and event_id:
        try:
            ledger.resolve_automatically(
                event_id,
                automation_id="glp_affiliate_daily_report_ack",
                authority_message_id=message_id,
                reconciliation_receipt=receipt,
            )
            result["event_resolved"] = event_id
        except Exception as error:
            result["event_resolve_error"] = str(error)[:200]
    return result


def _execute_terms_decision(entry: dict[str, Any], message_id: str,
                            decision: dict[str, str]) -> dict[str, Any]:
    return _write_terms_decision_receipt(
        entry,
        decision=("operator_accepted_terms" if decision["verb"] == "ACCEPT"
                  else "operator_rejected_terms"),
        decision_text=decision["line"], operator_words=decision["line"],
        provenance=f"discord-message-{message_id}", source_message_id=message_id,
    )


def parse_ack(text: str, open_ids: dict[str, set[str]]) -> tuple[str | None, str]:
    """Return (blocker_id, kind). kind: exact | terms_exact | inferred | ambiguous | none."""
    raw_text = str(text or "")
    terms = _TERMS_DECISION_RE.fullmatch(raw_text)
    text = raw_text.strip()
    if terms:
        blocker_id = f"BLK-awin-terms-{terms.group(2)}"
        return ((blocker_id, "terms_exact") if blocker_id in open_ids
                else (None, "ambiguous"))
    match = _DONE_RE.match(text)
    if match:
        blocker_id = match.group(1)
        canonical = {i.lower(): i for i in open_ids
                     if not i.startswith("BLK-awin-terms-")}
        if blocker_id.lower() in canonical:
            return canonical[blocker_id.lower()], "exact"
        return None, "ambiguous"
    non_terms_open = {blocker_id: words for blocker_id, words in open_ids.items()
                      if not blocker_id.startswith("BLK-awin-terms-")}
    if _ACK_NEGATION_RE.search(text):
        return None, "none"
    tokens = set(re.findall(r"[a-z0-9-]+", text.lower()))
    if not tokens or not ({"done", "did", "signed", "finished", "completed", "cleared"} & tokens):
        return None, "none"
    # Terms acceptance is never inferred. Other blocker classes retain their
    # established conservative exact-id/two-keyword behavior.
    id_hits = [blocker_id for blocker_id in non_terms_open if blocker_id.lower() in tokens]
    if len(id_hits) == 1:
        return id_hits[0], "inferred"
    if len(id_hits) > 1:
        return None, "ambiguous"
    hits = [blocker_id for blocker_id, words in non_terms_open.items()
            if len((words - {blocker_id.lower()}) & tokens) >= 2]
    if len(hits) == 1:
        return hits[0], "inferred"
    if len(hits) > 1:
        return None, "ambiguous"
    return None, "none"


_DONE_LINE_RE = re.compile(r"\bdone\b", re.I)
_ACK_VERBS = {"done", "did", "signed", "finished", "completed", "cleared", "submitted", "filled"}


def _state_update_for_line(line: str, known_ids: dict[str, set[str]]) -> dict[str, str] | None:
    """Recognize only an external-state assertion tied to exactly one blocker."""
    if not _STATE_UPDATE_RE.search(line):
        return None
    canonical = {blocker_id.lower(): blocker_id for blocker_id in known_ids}
    explicit = [canonical[token] for token in re.findall(r"blk-[a-z0-9-]+", line.lower())
                if token in canonical]
    if len(set(explicit)) == 1:
        blocker_id = explicit[0]
    elif explicit:
        return None
    else:
        tokens = set(re.findall(r"[a-z0-9-]+", line.lower()))
        matches = [blocker_id for blocker_id, words in known_ids.items()
                   if len((words - {blocker_id.lower()}) & tokens) >= 2]
        if len(matches) != 1:
            return None
        blocker_id = matches[0]
    return {"blocker_id": blocker_id, "state_kind": "awaiting_external", "operator_words": line}


def operator_feedback_capture_lines(text: str) -> list[str]:
    """Return every non-empty operator line for the immutable intake capture.

    `parse_acks` intentionally removes recognized commands from its narrative
    output so they are not interpreted twice.  That semantic projection must not
    also become the source capture: doing so dropped exact `BLK-*` instructions
    from operator-feedback-intake while their sibling prose remained.  Capture
    the complete sanitized message here and keep command parsing separate.
    """
    return [
        _sanitize_operator_words(raw_line.strip())
        for raw_line in str(text or "").splitlines()
        if raw_line.strip()
    ]


def parse_acks(text: str, open_ids: dict[str, set[str]], *,
               state_ids: dict[str, set[str]] | None = None,
               message_id: str = "") -> dict[str, Any]:
    """Conservative multi-ack plus exact-BLK instruction parser."""
    acks: dict[str, str] = {}
    ambiguous_lines: list[str] = []
    narrative_lines: list[str] = []
    state_update_lines: list[dict[str, str]] = []
    instruction_lines: list[dict[str, Any]] = []
    operational_rulings: list[dict[str, str]] = []
    queue_requests: list[dict[str, Any]] = []
    seen_instruction_identities: set[tuple[str, str]] = set()
    duplicate_instruction_line_count = 0
    intent_by_blocker: dict[str, set[str]] = {}
    invalid_blocker_token_lines: list[dict[str, Any]] = []
    canonical = {i.lower(): i for i in open_ids
                 if not i.startswith("BLK-awin-terms-")}
    known_state_ids = state_ids if state_ids is not None else open_ids
    terms_known = {i.removeprefix("BLK-awin-terms-"): i for i in known_state_ids
                   if i.startswith("BLK-awin-terms-")}
    terms_decision_lines: dict[str, dict[str, str]] = {}
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        safe_line = _sanitize_operator_words(line)
        terms = _TERMS_DECISION_RE.fullmatch(raw_line)
        if terms:
            verb, program_id = terms.groups()
            hit = terms_known.get(program_id)
            if hit:
                acks.setdefault(hit, "terms_exact")
                terms_decision_lines[hit] = {"verb": verb, "line": line}
                intent_by_blocker.setdefault(hit, set()).add(
                    "terms_acceptance" if verb == "ACCEPT" else "terms_rejection")
            else:
                ambiguous_lines.append(safe_line[:160])
            continue
        if ("accept-terms" in line.lower() or "reject-terms" in line.lower()
                or any(blocker_id.lower() in line.lower() for blocker_id in terms_known.values())):
            # Near-miss case/grammar, DONE, and fuzzy prose cannot become terms
            # acceptance or a generic durable blocker instruction.
            ambiguous_lines.append(safe_line[:160])
            continue
        invalid_tokens = _mixed_invalid_blocker_tokens(safe_line, known_state_ids)
        if invalid_tokens:
            invalid_blocker_token_lines.append({
                "line": safe_line[:160], "invalid_tokens": invalid_tokens,
            })
            continue
        strict = _DONE_RE.match(line)
        if strict:
            hit = canonical.get(strict.group(1).lower())
            if hit:
                acks.setdefault(hit, "exact")
                intent_by_blocker.setdefault(hit, set()).add("operator_done")
            else:
                ambiguous_lines.append(safe_line[:160])
            continue
        tokens = set(re.findall(r"[a-z0-9-]+", line.lower()))
        has_verb = bool(_ACK_VERBS & tokens)
        line_ids = [canonical[t] for t in tokens if t in canonical]
        if not _ACK_NEGATION_RE.search(line) and line_ids and has_verb:
            for hit in line_ids:
                acks.setdefault(hit, "inferred")
                intent_by_blocker.setdefault(hit, set()).add("operator_done")
            continue
        if not _ACK_NEGATION_RE.search(line) and has_verb and not line_ids:
            keyword_hits = [blocker_id for blocker_id, words in open_ids.items()
                            if not blocker_id.startswith("BLK-awin-terms-")
                            and len((words - {blocker_id.lower()}) & tokens) >= 2]
            if len(keyword_hits) == 1:
                acks.setdefault(keyword_hits[0], "inferred")
                intent_by_blocker.setdefault(keyword_hits[0], set()).add("operator_done")
                continue
            if len(keyword_hits) > 1:
                ambiguous_lines.append(safe_line[:160])
                continue
        instruction = _instruction_for_line(safe_line, known_state_ids, message_id=message_id)
        if instruction is not None:
            blocker_id = str(instruction["blocker_id"])
            intent_by_blocker.setdefault(blocker_id, set()).update(instruction.get("intent_kinds") or set())
            instruction_identity = (blocker_id, str(instruction["operator_words_sha256"]))
            if instruction_identity in seen_instruction_identities:
                duplicate_instruction_line_count += 1
                continue
            seen_instruction_identities.add(instruction_identity)
            instruction_lines.append({
                "blocker_id": blocker_id,
                "operator_words_sha256": instruction["operator_words_sha256"],
                "state_update_count": len(instruction["state_updates"]),
                "operational_ruling_count": len(instruction["operational_rulings"]),
                "queue_request_count": len(instruction["queue_requests"]),
            })
            if instruction.get("ambiguous"):
                ambiguous_lines.append(safe_line[:160])
                narrative_lines.append(safe_line)
                continue
            state_update_lines.extend(instruction["state_updates"])
            operational_rulings.extend(
                {**row, "_source_blocker_id": blocker_id} for row in instruction["operational_rulings"])
            queue_requests.extend(
                {**row, "_source_blocker_id": blocker_id} for row in instruction["queue_requests"])
            # Backward-compatible feedback copy for ordinary wait assertions.
            # It is not "pure" narrative: the same line already has a durable
            # state update. Decline/reopen/standing-ruling lines stay out.
            if (instruction["state_updates"]
                    and all(row.get("state_kind") == "awaiting_external"
                            for row in instruction["state_updates"])):
                narrative_lines.append(safe_line)
            continue
        state_update = _state_update_for_line(safe_line, known_state_ids)
        narrative_lines.append(safe_line)
        if state_update:
            state_update_lines.append(state_update)
    # No line order may choose policy.  Resolve the complete payload's semantic
    # intent graph before returning any effect: positive/negative command pairs,
    # done-vs-terminal state, and exact-47 allow/forbid pairs all fail closed.
    by_blocker: dict[str, set[str]] = {key: set(value) for key, value in intent_by_blocker.items()}
    for update in state_update_lines:
        by_blocker.setdefault(str(update.get("blocker_id") or ""), set()).add(
            str(update.get("state_kind") or "")
        )
    conflict_pairs = (
        ("decline", "reopen"), ("decline", "forbid_decline"),
        ("reopen", "forbid_reopen"), ("queue_exact", "forbid_exact_apply"),
        ("require_complete", "forbid_complete"),
        ("awin_2fa_disabled", "awin_2fa_enabled"),
        ("operator_done", "decline"),
        ("terms_acceptance", "terms_rejection"),
    )
    state_kinds = {"operator_declined", "reopen", "awaiting_external"}
    conflicts = {
        key for key, kinds in by_blocker.items()
        if any(left in kinds and right in kinds for left, right in conflict_pairs)
        or len(kinds & state_kinds) > 1
    }
    if conflicts:
        # A semantic contradiction poisons the complete message, not merely one
        # blocker row.  Returning any sibling effect would make line order an
        # authority boundary and violate same-message atomicity.
        state_update_lines = []
        operational_rulings = []
        queue_requests = []
        acks = {}
        terms_decision_lines = {}
        for blocker_id in sorted(conflicts):
            ambiguous_lines.append(f"conflicting instructions for {blocker_id}")
    if invalid_blocker_token_lines:
        # One mixed-identity line poisons the complete operator message.  Do not
        # preserve sibling acks, state changes, rulings, queue requests, or prose:
        # any of those would make an unresolved token a partial-authority seam.
        acks = {}
        terms_decision_lines = {}
        narrative_lines = []
        state_update_lines = []
        instruction_lines = []
        operational_rulings = []
        queue_requests = []
        duplicate_instruction_line_count = 0
        ambiguous_lines.extend(
            f"unknown or malformed blocker token(s): {', '.join(row['invalid_tokens'])}"
            for row in invalid_blocker_token_lines
        )
    operational_rulings = [{key: value for key, value in row.items() if key != "_source_blocker_id"}
                           for row in operational_rulings]
    queue_requests = [{key: value for key, value in row.items() if key != "_source_blocker_id"}
                      for row in queue_requests]
    return {"acks": acks, "terms_decision_lines": terms_decision_lines,
            "ambiguous_lines": ambiguous_lines,
            "narrative_lines": narrative_lines, "state_update_lines": state_update_lines,
            "instruction_lines": instruction_lines, "operational_rulings": operational_rulings,
            "queue_requests": queue_requests,
            "duplicate_instruction_line_count": duplicate_instruction_line_count,
            "conflicting_blocker_ids": sorted(conflicts),
            "invalid_blocker_token_lines": invalid_blocker_token_lines}


def replay_operator_message_source(messages: dict[str, Any] | list[dict[str, Any]], message_id: str,
                                   open_ids: dict[str, set[str]], *,
                                   state_ids: dict[str, set[str]] | None = None) -> dict[str, Any]:
    """Provider-free fixture seam. It selects one supplied message ID only.

    This intentionally does not call the Discord control plane or touch durable
    state; tests and parent-controlled fixtures may exercise parsing safely.
    """
    if isinstance(messages, dict):
        message = messages.get(str(message_id))
    else:
        selected = [m for m in messages if str(m.get("id") or "") == str(message_id)]
        message = selected[0] if len(selected) == 1 else None
    if not isinstance(message, dict):
        raise ValueError("replay_message_id_not_found_or_ambiguous")
    return parse_acks(str(message.get("content") or ""), open_ids, state_ids=state_ids,
                      message_id=str(message_id))


def _sha_path_or_none(path: Path) -> str | None:
    path = Path(path)
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def _capture_item_e_source(*, path: Path, message_id: str, captured_at_utc: str,
                           lines: list[str], acked_ids: list[str]) -> dict[str, Any]:
    """Exclusively capture sanitized operator bytes; conflicting replay stops."""
    path = Path(path)
    payload = {
        "message_id": str(message_id), "captured_at_utc": str(captured_at_utc),
        "narrative_lines": lines, "acked_ids": acked_ids,
        "sanitized": True, "sanitizer": "glp_affiliate_daily_decisions_report._sanitize_operator_words",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    body = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        return payload
    except FileExistsError:
        existing = _load_json(path, None)
        if (not isinstance(existing, dict)
                or str(existing.get("message_id") or "") != str(message_id)
                or list(existing.get("narrative_lines") or []) != lines
                or sorted(str(value) for value in (existing.get("acked_ids") or [])) != acked_ids
                or not str(existing.get("captured_at_utc") or "")):
            raise RuntimeError(f"item_e_operator_source_conflict:{message_id}")
        return existing


def promote_live_item_e_instruction(*, message_id: str, content: str, parsed: dict[str, Any],
                                    captured_at_utc: str,
                                    blocker_path: Path = BLOCKER_STORE,
                                    ledger_path: Path = LEDGER_PATH,
                                    state_path: Path = STATE_PATH,
                                    inventory_path: Path = ITEM_E_INVENTORY,
                                    med_ruling_path: Path | None = None,
                                    intake_root: Path | None = None,
                                    rules_root: Path | None = None,
                                    queue_root: Path = APPLY_RUN_QUEUE_DIR,
                                    transaction_root: Path | None = None,
                                    fault_at: str | None = None) -> dict[str, Any]:
    """Promote a live exact-47 instruction through the one six-target contract.

    This is the only live daily-lane path for Item E.  It captures the immutable
    source first, builds all six outputs provider-free, and then delegates to the
    same shared-lock/full-vector-CAS/journal transaction as the explicit intake
    CLI.  No blocker/ruling/queue/state subset is written outside that unit.
    """
    if (parsed.get("queue_requests") != [{"kind": "exact_canonical_inventory", "denominator": 47,
                                          "source_message_id": str(message_id)}]
            or parsed.get("ambiguous_lines") or parsed.get("narrative_lines")
            or parsed.get("invalid_blocker_token_lines")):
        raise RuntimeError("item_e_live_instruction_not_exact_or_unambiguous")
    lines = [_sanitize_operator_words(line.strip()) for line in str(content).splitlines() if line.strip()]
    if not lines or not parsed.get("instruction_lines"):
        raise RuntimeError("item_e_live_instruction_lines_missing")
    intake_root = Path(intake_root or (Path(blocker_path).parent / "operator-feedback-intake"))
    source_path = intake_root / f"{message_id}.json"
    source = _capture_item_e_source(
        path=source_path, message_id=message_id, captured_at_utc=captured_at_utc,
        lines=lines, acked_ids=sorted(str(value) for value in (parsed.get("acks") or {})))
    captured = str(source["captured_at_utc"])
    source_sha = _sha_path_or_none(source_path)
    if source_sha is None:
        raise RuntimeError("item_e_operator_source_capture_missing")

    import glp_item_e_operator_intake as intake_mod  # lazy: module imports this parser

    med_ruling_path = Path(med_ruling_path or intake_mod.DEFAULT_MED_RULING)
    rules_root = Path(rules_root or (Path(blocker_path).parent / "operator-rulings"))

    transaction_root = Path(transaction_root or (Path(blocker_path).parent / "item-e-canonical-transactions"))
    base_tx_id = f"item-e-live-{message_id}-{source_sha[:16]}"
    canonical_targets = [
        Path(blocker_path), Path(ledger_path), Path(state_path),
        rules_root / f"operator-rulings-{message_id}.json",
        rules_root / f"operator-rulings-{intake_mod.MED_MESSAGE_ID}.json",
        Path(queue_root) / f"exact-47-{message_id}.json",
    ]
    # The live path owns the same trusted six-target lock from interrupted
    # transaction recovery/replay classification through retry staging, CAS,
    # replacement, and commit. Journal/pointer bytes never select targets or a
    # transaction identity; retry IDs derive only from this source-bound base.
    with intake_mod.canonical_writer_guard(canonical_targets):
        attempt = intake_mod.prepare_item_e_transaction_attempt(
            transaction_root, canonical_targets=canonical_targets,
            base_transaction_id=base_tx_id)
        if attempt["status"] == "COMMITTED_VERIFIED":
            return {
                "status": "ALREADY_COMMITTED",
                "transaction_id": attempt["transaction_id"],
                "journal_path": attempt["journal_path"],
                "target_count": attempt["target_count"],
                "source_path": str(source_path), "source_sha256": source_sha,
                "prior_live_recovery": attempt.get("prior_recovery"),
                "effects": {"provider_calls": 0, "browser_actions": 0,
                            "provider_writes": 0, "outbound_sends": 0},
            }

        result = intake_mod.process_item_e_intake(
            intake_path=source_path, inventory_path=Path(inventory_path),
            blocker_store_path=Path(blocker_path), ledger_path=Path(ledger_path),
            daily_state_path=Path(state_path), med_ruling_path=med_ruling_path,
            output_root=transaction_root / f"build-{message_id}-{source_sha[:16]}")
        mapping = intake_mod.canonical_target_map(
            result, blocker_path=Path(blocker_path), ledger_path=Path(ledger_path),
            state_path=Path(state_path), rules_root=rules_root, queue_root=Path(queue_root))
        if sorted(map(str, mapping.values())) != sorted(map(str, canonical_targets)):
            raise RuntimeError("item_e_live_canonical_target_vector_drift")
        expected = {str(target): _sha_path_or_none(target) for target in mapping.values()}
        promotion = intake_mod.atomic_promote_item_e(
            staged_to_target=mapping, expected_sha256=expected,
            transaction_root=transaction_root,
            transaction_id=attempt["transaction_id"], fault_at=fault_at)
        return {
            **promotion, "source_path": str(source_path), "source_sha256": source_sha,
            "staged_result": result,
            "prior_live_recovery": attempt.get("prior_recovery"),
            "transaction_attempt_number": attempt["attempt_number"],
        }


def ack_sync(*, live: bool, now: dt.datetime | None = None) -> dict[str, Any]:
    """Read operator replies and serialize every canonical read/modify/write."""
    cp = load_control_plane()
    try:
        messages = cp.discord_request(f"/channels/{cp.DECISIONS_CHANNEL_ID}/messages?limit=50")
    except Exception as error:  # network failure: try again next firing, never crash the cron
        return {"status": "ack_fetch_failed", "error": str(error)[:200], "applied": 0}
    if not isinstance(messages, list):
        return {"status": "ack_fetch_invalid", "applied": 0}
    if not live:
        return _ack_sync_under_guard(cp=cp, messages=messages, live=False, now=now)
    import glp_item_e_operator_intake as intake_mod  # lazy circular-safe import
    with intake_mod.canonical_writer_guard([BLOCKER_STORE, LEDGER_PATH, STATE_PATH]):
        return _ack_sync_under_guard(cp=cp, messages=messages, live=True, now=now)


def _ack_sync_under_guard(*, cp, messages: list[dict[str, Any]], live: bool,
                          now: dt.datetime | None = None) -> dict[str, Any]:
    state = _load_json(STATE_PATH, {})
    processed: list[str] = list(state.get("processed_ack_message_ids") or [])
    snapshot = collect_snapshot(now)
    store = sync_blockers(snapshot, seed_manual_blockers(_load_json(BLOCKER_STORE, {"blockers": {}})))
    open_ids = {e["id"]: set(e.get("keywords") or []) | {e["id"].lower()}
                for e in _open_blockers(store)}
    state_ids = {str(e["id"]): set(e.get("keywords") or []) | {str(e["id"]).lower()}
                 for e in (store.get("blockers") or {}).values()
                 if (e.get("status") != "verified_cleared"
                     or str(e.get("id") or "").startswith("BLK-awin-terms-"))}
    applied, state_applied, asks = [], [], []
    pending_messages: list[tuple[dict[str, Any], str, str, dict[str, Any]]] = []
    for message in sorted(messages, key=lambda m: int(m.get("id") or 0)):
        message_id = str(message.get("id") or "")
        author = str((message.get("author") or {}).get("id") or "")
        if author != cp.OPERATOR_ID or message_id in processed:
            continue
        content = str(message.get("content") or "")
        exact_terms_line = any(_TERMS_DECISION_RE.fullmatch(line) for line in content.splitlines())
        if (not exact_terms_line and "blk-" not in content.lower()
                and not _DONE_RE.match(content) and "ruling " not in content.lower()):
            lowered = set(re.findall(r"[a-z0-9]+", content.lower()))
            if not any(words & lowered for words in open_ids.values()):
                continue
        parsed = parse_acks(content, open_ids, state_ids=state_ids, message_id=message_id)
        pending_messages.append((message, message_id, content, parsed))

    # Preflight the complete unprocessed batch before constructing a writable
    # ledger or touching any canonical/output path.  A same-message semantic
    # conflict must be observable as a strict zero-effect retryable block.
    conflict_messages = [
        {"message_id": message_id,
         "conflicting_blocker_ids": list(parsed.get("conflicting_blocker_ids") or [])}
        for _, message_id, _, parsed in pending_messages
        if parsed.get("conflicting_blocker_ids")
    ]
    invalid_identity_messages = [
        {"message_id": message_id,
         "invalid_blocker_token_lines": list(parsed.get("invalid_blocker_token_lines") or [])}
        for _, message_id, _, parsed in pending_messages
        if parsed.get("invalid_blocker_token_lines")
    ]
    if invalid_identity_messages:
        return {
            "status": "blocked_invalid_blocker_identity", "applied": 0, "applied_ids": [],
            "awaiting_external_updates": 0, "awaiting_external_ids": [],
            "clarifications": 0, "invalid_blocker_identities": invalid_identity_messages,
            "effects": {"canonical_writes": 0, "ledger_writes": 0,
                        "notifications": 0, "processed_message_writes": 0},
        }
    if conflict_messages:
        return {
            "status": "blocked_conflicting_instructions", "applied": 0, "applied_ids": [],
            "awaiting_external_updates": 0, "awaiting_external_ids": [],
            "clarifications": 0, "conflicts": conflict_messages,
            "effects": {"canonical_writes": 0, "ledger_writes": 0,
                        "notifications": 0, "processed_message_writes": 0},
        }

    ledger = cp.Ledger(INBOX_DB)
    try:
        for message, message_id, content, parsed in pending_messages:
            captured_now = _now_utc()
            if live and parsed["queue_requests"]:
                promotion = promote_live_item_e_instruction(
                    message_id=message_id, content=content, parsed=parsed,
                    captured_at_utc=captured_now)
                return {"status": "pass", "applied": 0, "applied_ids": [],
                        "awaiting_external_updates": 0, "awaiting_external_ids": [],
                        "clarifications": 0, "atomic_item_e_promotion": promotion}
            processed.append(message_id)
            state_applied.extend(apply_awaiting_external_updates(
                store, snapshot, message_id=message_id,
                state_updates=parsed["state_update_lines"], captured_at_utc=captured_now))
            if live and parsed["operational_rulings"]:
                rules_mod = load_operator_rulings()
                rules_mod.write_immutable_ruling_document(
                    message_id, ruled_at_utc=captured_now, root=rules_mod.CANONICAL_RULES_DIR,
                    operational_rulings=parsed["operational_rulings"])
                store.setdefault("new_operator_rulings", []).extend([
                    {**ruling, "source_message_id": message_id, "captured_at_utc": captured_now}
                    for ruling in parsed["operational_rulings"]
                ])
            if live and parsed["narrative_lines"]:
                ingested = load_operator_rulings().ingest_operator_message_rulings(
                    message_id, parsed["narrative_lines"], ruled_at_utc=_now_utc())
                if ingested:
                    store.setdefault("new_operator_rulings", []).extend([
                        {**ruling, "source_message_id": message_id, "captured_at_utc": _now_utc()}
                        for ruling in ingested
                    ])
            for blocker_id in sorted(parsed["acks"]):
                kind = parsed["acks"][blocker_id]
                entry = store["blockers"][blocker_id]
                if entry.get("status") == "operator_declined":
                    continue
                entry["status"] = "operator_says_done"
                entry["ack"] = {"message_id": message_id, "kind": kind, "at_utc": _now_utc(),
                                "text": content[:200]}
                entry["updated_at_utc"] = _now_utc()
                if live:
                    if kind == "terms_exact":
                        entry["on_ack_result"] = _execute_terms_decision(
                            entry, message_id, parsed["terms_decision_lines"][blocker_id])
                    else:
                        entry["on_ack_result"] = _execute_on_ack(cp, ledger, entry, message_id)
                applied.append(blocker_id)
            if parsed["narrative_lines"] and live:
                # Process feedback in an operator reply is bridge input, never noise:
                # capture it verbatim for the bridge's workflow-improvement pass.
                intake_dir = BLOCKER_STORE.parent / "operator-feedback-intake"
                intake_dir.mkdir(parents=True, exist_ok=True)
                _atomic_write(intake_dir / f"{message_id}.json", {
                    "message_id": message_id, "captured_at_utc": _now_utc(),
                    # Source capture is deliberately broader than the parser's
                    # semantic narrative projection. Recognized BLK commands
                    # must remain in the intake record rather than disappearing.
                    "narrative_lines": operator_feedback_capture_lines(content),
                    "acked_ids": sorted(parsed["acks"]),
                })
            if parsed["acks"]:
                remaining = sorted(set(open_ids) - set(applied))
                cleared_list = ", ".join(f"`{i}`" for i in sorted(parsed["acks"]))
                remaining_line = ("remaining open: " + ", ".join(f"`{i}`" for i in remaining)
                                 ) if remaining else "remaining open: none — every blocker is marked done"
                feedback_line = (f"process feedback captured ({len(parsed['narrative_lines'])} line(s)) — the bridge reviews it"
                                 ) if parsed["narrative_lines"] else None
                confirmation = render(
                    title("ok", "GLP Affiliate Ops",
                          f"{len(parsed['acks'])} blocker(s) marked done — machine verifying"),
                    "done: " + cleared_list,
                    remaining_line,
                    *( [feedback_line] if feedback_line else [] ),
                    "→ Owning lanes re-run on their next pass (portal drain 9:05 PT / reconciler every 10m); the next report shows verified-cleared vs re-opened, automatically.",
                    subtext("state: operator-blockers.json"),
                    output_class="event",
                )
                if live:
                    ledger.queue_notification(None, PURPOSE, cp.DECISIONS_CHANNEL_ID,
                                              f"{REPORT_KEY}:ack-confirm:{message_id}", confirmation)
            if parsed["ambiguous_lines"]:
                asks.append(message_id)
                unmatched = "; ".join(parsed["ambiguous_lines"][:3])
                ask = render(
                    title("warn", "GLP Affiliate Ops", "which blocker did you finish?"),
                    f"These line(s) mention several blockers or an id I don't have open: {unmatched}",
                    "open: " + (", ".join(f"`{i}`" for i in sorted(open_ids)) or "none"),
                    "→ Reply exactly `DONE <id>` so the right lane retries.",
                    subtext("state: operator-blockers.json"),
                    output_class="event",
                )
                if live:
                    ledger.queue_notification(None, PURPOSE, cp.DECISIONS_CHANNEL_ID,
                                              f"{REPORT_KEY}:ack-clarify:{message_id}", ask)
    finally:
        ledger.close()
    if live:
        _atomic_write(BLOCKER_STORE, store)
        state["processed_ack_message_ids"] = processed[-500:]
        _atomic_write(STATE_PATH, state)
    return {"status": "pass", "applied": len(applied), "applied_ids": applied,
            "awaiting_external_updates": len(state_applied),
            "awaiting_external_ids": state_applied, "clarifications": len(asks)}


# ---------------------------------------------------------------------------- cycle
def run_cycle(*, live: bool, force_post: bool = False, now: dt.datetime | None = None) -> dict[str, Any]:
    now = now or dt.datetime.now(dt.timezone.utc)
    ack = ack_sync(live=live, now=now)
    if live and ack.get("atomic_item_e_promotion"):
        # The exact instruction transaction already committed the complete
        # blocker/ledger/state/rulings/manifest vector.  Do not let this cycle's
        # earlier in-memory snapshot overwrite any member of that vector.
        return {"status": "pass", "date_pt": now.astimezone(PT).strftime("%Y-%m-%d"),
                "ack": ack, "posted": [], "due": False,
                "open_blockers": [], "cycle_stopped_after_item_e_atomic_commit": True}
    if live:
        import glp_item_e_operator_intake as intake_mod  # lazy circular-safe import
        with intake_mod.canonical_writer_guard([BLOCKER_STORE, LEDGER_PATH, STATE_PATH]):
            return _finish_cycle_under_guard(live=True, force_post=force_post, now=now, ack=ack)
    return _finish_cycle_under_guard(live=False, force_post=force_post, now=now, ack=ack)


def _finish_cycle_under_guard(*, live: bool, force_post: bool, now: dt.datetime,
                              ack: dict[str, Any]) -> dict[str, Any]:
    snapshot = collect_snapshot(now)
    store = seed_manual_blockers(_load_json(BLOCKER_STORE, {"blockers": {}}))
    store = sync_blockers(snapshot, store)
    if live:
        _atomic_write(BLOCKER_STORE, store)
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        _atomic_write(REPORT_DIR / f"{snapshot['date_pt']}.json", snapshot)
    state = _load_json(STATE_PATH, {})
    local = now.astimezone(PT)
    due = force_post or (
        state.get("last_posted_day") != snapshot["date_pt"]
        and (local.hour, local.minute) >= POST_AFTER_PT
    )
    posted: list[str] = []
    if due:
        chunks = compose_report(snapshot, store)
        posted = post_report(chunks, snapshot["date_pt"], live=live)
        if live:
            state["last_posted_day"] = snapshot["date_pt"]
            state["last_post_outbox_ids"] = posted
            _atomic_write(STATE_PATH, state)
    return {"status": "pass", "date_pt": snapshot["date_pt"], "ack": ack,
            "posted": posted, "due": due,
            "open_blockers": [e["id"] for e in _open_blockers(store) if e["status"] == "open"]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cycle", action="store_true")
    parser.add_argument("--post-now", action="store_true", help="post today's report regardless of schedule")
    parser.add_argument("--compose-only", action="store_true", help="print the card text and exit")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    live = not args.dry_run
    if args.compose_only:
        snapshot = collect_snapshot()
        store = sync_blockers(snapshot, seed_manual_blockers(_load_json(BLOCKER_STORE, {"blockers": {}})))
        for chunk in compose_report(snapshot, store):
            print(chunk + "\n" + "=" * 20)
        return 0
    result = run_cycle(live=live, force_post=args.post_now)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
