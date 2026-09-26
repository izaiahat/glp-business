#!/usr/bin/env python3
"""Deterministic #decisions router with one bounded Sol residue lane.

The router is an n8n bridge action.  It verifies Discord source identity itself,
applies structured commands without a model, and returns at most one Sol brief
for the unmatched residue.  Every durable write is source-bound and replay-safe.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Callable

BUSINESS = Path("/home/ubuntu/business")
sys.path.insert(0, str(BUSINESS))
sys.path.insert(0, str(BUSINESS / "ops"))

from inbox_control_plane import control_plane as cp  # noqa: E402
from inbox_control_plane.approved_gmail_sender import (  # noqa: E402
    ApprovedSendError,
    run_approved_gmail_send_transport,
)
import action_items  # noqa: E402

DECISIONS_CHANNEL_ID = "1524254375453786294"
OPS_ALERTS_CHANNEL_ID = "1487300132054765629"
OPERATOR_ID = "392067741500309517"
ROOT = BUSINESS / "reports/ops/decisions-router"
RUN_DIR = ROOT / "runs"
AGENT_DIR = ROOT / "agent"
STATE_PATH = ROOT / "state.json"
INTAKE_DIR = BUSINESS / "reports/glp-clinic-scout/affiliate-machine/operator-feedback-intake"
BUNDLE_GLOB = "draft-bundle-*.json"
HISTORICAL_MESSAGE_ID = "1540120879093125140"
HISTORICAL_APPROVAL_MESSAGE_ID = "1540145485434851511"
EVENT_RE = re.compile(r"^(evt_[0-9a-f]{28})\s+(.+)$", re.I)
BLK_RE = re.compile(r"^(BLK-[A-Za-z0-9][A-Za-z0-9-]*)\b(.*)$", re.I)
APPROVE_RE = re.compile(r"^APPROVE\s+([A-Za-z0-9_. -]+(?:\|[A-Za-z0-9_. -]+)*)\s*$", re.I)
CARD_HANDLER_ACTIONS = {"SEND", "EDIT", "HOLD", "DRAFT", "RESOLVED_EXTERNALLY"}
ALLOWED_EVENT_ACTIONS = {*CARD_HANDLER_ACTIONS, "AFFILIATE_TOOL"}

MACHINE_REGISTRY = """ops/error-handler — error trigger only
ops/error-fixer /ops-error-fixer-run
ops/fleet-sentinel /fleet-sentinel-run
ops/google-credit-reminder /ops-google-credit-reminder-run
glp/gsc-ga4-collector /glp-gsc-ga4-collector-run
glp/topic-supply /glp-topic-supply-run
glp/reddit-scanner /glp-reddit-scanner-run
glp/marketplace-enumeration /glp-marketplace-enumeration-run
glp/program-liveness /glp-program-liveness-run
glp/sheet-sync /glp-sheet-sync-run
glp/inbox-mirror /glp-inbox-mirror-run
ops/decisions-router /ops-decisions-ack-run
glp/sheet-reconciler /glp-sheet-reconciler-run
glp/pricing-refresh — webhook was still owned by another builder in the briefing
glp/article-carrier /glp-article-carrier-run
boring/finder-daily /boring-finder-daily-run
glp/applications /glp-applications-run
glp/onboarding-manifest /glp-onboarding-manifest-run"""

REPLAY_DETERMINISTIC = [
    (1, "Google Ads event", "inbox/reconciler", "ack-live-readback-1540120879093125140.json"),
    (5, "RENPHO sister vertical", "sheet_update_queue → glp/sheet-sync", "sheet-final-readback-1540120879093125140.json"),
    (6, "MaxWeb chat event", "inbox/reconciler", "ack-live-readback-1540120879093125140.json"),
    (7, "MaxWeb contact event", "inbox/reconciler", "ack-live-readback-1540120879093125140.json"),
    (8, "Gustavo draft/send", "inbox GmailClient", "approved-sends-1540120879093125140/send-receipts.json"),
    (9, "Ascend blocker", "blocker store → sheet_update_queue", "sheet-final-readback-1540120879093125140.json"),
    (10, "George/Awin invitations", "affiliate machine → inbox GmailClient", "routing-evidence-1540120879093125140.json"),
    (11, "MaxBounty event", "inbox/reconciler", "ack-live-readback-1540120879093125140.json"),
    (12, "Mandy/Impact", "affiliate machine → inbox GmailClient", "approved-sends-1540120879093125140/send-receipts.json"),
]
REPLAY_RESIDUE = [
    (2, "pause legacy ad groups", "Google Ads rebuild machine"),
    (3, "shared budget daily cap", "Google Ads rebuild machine"),
    (4, "healthcare certification path", "Google Ads compliance research"),
    (13, "Levanta prior rejection", "sheet readback"),
]
REPLAY_RESOLVED_EVENTS = {
    "evt_c2b0de4ec1a4ba9a06e4f8653e83",
    "evt_e494880f1b36caca8b22d1c79c28",
    "evt_ed23c909322794b00ae49cd451ff",
    "evt_af66409c130ae11996a2dbcf6629",
}


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256(value: str | bytes) -> str:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(raw).hexdigest()


def private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    private_dir(path.parent)
    raw = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        path.chmod(0o600)
    finally:
        if tmp.exists():
            tmp.unlink()


def load_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def source_checks(message: dict[str, Any]) -> dict[str, bool]:
    author = message.get("author") or {}
    return {
        "id": bool(str(message.get("id") or "").isdigit()),
        "channel": str(message.get("channel_id") or DECISIONS_CHANNEL_ID) == DECISIONS_CHANNEL_ID,
        "operator": str(author.get("id") or "") == OPERATOR_ID,
        "non_bot": not bool(author.get("bot")),
        "non_webhook": not bool(message.get("webhook_id")),
        "content": bool(str(message.get("content") or "").strip()),
    }


def verified_messages(message_id: str = "") -> list[dict[str, Any]]:
    path = f"/channels/{DECISIONS_CHANNEL_ID}/messages"
    if message_id:
        rows: Any = [cp.discord_request(f"{path}/{message_id}")]
    else:
        rows = cp.discord_request(f"{path}?limit=100")
    if not isinstance(rows, list):
        raise RuntimeError("discord_messages_invalid")
    selected = []
    for row in rows:
        checks = source_checks(row)
        if message_id:
            checks["requested_id"] = str(row.get("id") or "") == str(message_id)
        if message_id and not all(checks.values()):
            raise RuntimeError("discord_source_binding_failed:" + ",".join(k for k, ok in checks.items() if not ok))
        if all(checks.values()):
            selected.append(row)
    return sorted(selected, key=lambda row: int(str(row["id"])))


def run_id_for(mode: str, messages: list[dict[str, Any]]) -> str:
    identity = "|".join(f"{row['id']}:{sha256(str(row.get('content') or ''))}" for row in messages)
    return f"{mode}-{sha256(identity)[:20]}"


def capture_message(message: dict[str, Any], *, replay: bool) -> str:
    """Reuse operator-feedback-intake without mutating an older capture on replay."""
    message_id = str(message["id"])
    content = str(message.get("content") or "")
    path = INTAKE_DIR / f"{message_id}.json"
    if path.exists():
        existing = load_json(path, {})
        if str(existing.get("content_verbatim") or "") == content:
            return str(path)
        # Older intake rows intentionally remain immutable.  Add the missing
        # exact source bytes under the same intake lane rather than rewriting a
        # historical semantic projection in place.
        sidecar = INTAKE_DIR / "verbatim" / f"{message_id}.json"
        payload = {
            "schema": "operator-feedback-verbatim-v1",
            "message_id": message_id,
            "channel_id": DECISIONS_CHANNEL_ID,
            "actor_id": OPERATOR_ID,
            "captured_at_utc": utc_now(),
            "content_verbatim": content,
            "content_sha256": sha256(content),
            "lines_verbatim": [line for line in content.splitlines() if line.strip()],
            "message_reference_id": str(
                (message.get("message_reference") or {}).get("message_id") or ""
            ),
            "canonical_intake_path": str(path),
        }
        prior = load_json(sidecar, None)
        if prior is None:
            atomic_json(sidecar, payload)
        elif (
            str(prior.get("message_id") or "") != message_id
            or str(prior.get("content_sha256") or "") != sha256(content)
        ):
            raise RuntimeError("operator_feedback_verbatim_conflict")
        return str(path)
    payload = {
        "schema": "operator-feedback-intake-v2",
        "message_id": message_id,
        "channel_id": DECISIONS_CHANNEL_ID,
        "actor_id": OPERATOR_ID,
        "captured_at_utc": utc_now(),
        "content_verbatim": content,
        "content_sha256": sha256(content),
        "narrative_lines": [line for line in content.splitlines() if line.strip()],
        "message_reference_id": str(
            (message.get("message_reference") or {}).get("message_id") or ""
        ),
        "acked_ids": [],
        "replay_capture": bool(replay),
    }
    atomic_json(path, payload)
    return str(path)


def item(number: int | str, text: str, destination: str, status: str, evidence: str = "", **extra: Any) -> dict[str, Any]:
    return {
        "item": number,
        "text": text.strip(),
        "destination": destination,
        "status": status,
        "evidence": evidence,
        **extra,
    }


def replay_dispatch(message: dict[str, Any], run_id: str) -> dict[str, Any]:
    reference = INTAKE_DIR / f"ROUTING-RECEIPT-{HISTORICAL_MESSAGE_ID}.md"
    if not reference.is_file() or "13 total items" not in reference.read_text(encoding="utf-8"):
        raise RuntimeError("historical_routing_receipt_missing_or_invalid")

    ack = load_json(INTAKE_DIR / "ack-live-readback-1540120879093125140.json", {})
    ack_events = {
        str(row.get("event_id") or "")
        for row in (ack.get("events") or [])
        if row.get("state") == "resolved_externally"
        and str((row.get("action") or {}).get("status") or "") == "verified"
    }
    if (
        ack.get("operator_message_id") != HISTORICAL_MESSAGE_ID
        or ack.get("all_resolved_externally") is not True
        or ack_events != REPLAY_RESOLVED_EVENTS
    ):
        raise RuntimeError("historical_event_readback_failed")

    sheet = load_json(INTAKE_DIR / "sheet-final-readback-1540120879093125140.json", {})
    sheet_rows = {str(row.get("program") or ""): row for row in (sheet.get("rows") or [])}
    if (
        sheet.get("all_expected_dispositions_landed") is not True
        or int(sheet.get("new_queue_rows_created") or 0) != 0
        or sheet.get("sheet_sync_endpoint_fired") is not False
        or not all(
            (sheet_rows.get(name) or {}).get("pass") is True
            and str((sheet_rows.get(name) or {}).get("wire_in_ready")) == "FALSE"
            and str((sheet_rows.get(name) or {}).get("wired_live")) == "FALSE"
            for name in ("Renpho", "Ascend by Partnerize", "Levanta")
        )
    ):
        raise RuntimeError("historical_sheet_readback_failed")

    sends = load_json(INTAKE_DIR / "approved-sends-1540120879093125140/send-receipts.json", {})
    send_rows = sends.get("sends") or {}
    if (
        sends.get("operator_reply") != "APPROVE gustavo|george|mandy"
        or any(
            str((send_rows.get(name) or {}).get("verified_by") or "") != "provider_thread_readback"
            or not str((send_rows.get(name) or {}).get("message_id") or "")
            or not str((send_rows.get(name) or {}).get("thread_id") or "")
            for name in ("gustavo", "george", "mandy")
        )
    ):
        raise RuntimeError("historical_send_readback_failed")

    routing = load_json(INTAKE_DIR / "routing-evidence-1540120879093125140.json", {})
    routing_items = routing.get("items") or {}
    if (
        routing.get("operator_message_id") != HISTORICAL_MESSAGE_ID
        or int(routing.get("external_email_sends") or 0) != 0
        or not str((routing_items.get("renpho") or {}).get("verdict") or "")
        or not str((routing_items.get("impact_13292") or {}).get("confirmation") or "")
        or not (routing_items.get("awin_invitations") or {}).get("programs")
    ):
        raise RuntimeError("historical_affiliate_routing_readback_failed")

    deterministic = []
    for number, label, destination, relative in REPLAY_DETERMINISTIC:
        evidence = INTAKE_DIR / relative
        if not evidence.exists():
            raise RuntimeError(f"historical_readback_missing:{number}:{evidence}")
        deterministic.append(item(number, label, destination, "already_satisfied_noop", str(evidence)))
    expected = [
        {"item": number, "line": line, "destination": destination}
        for number, line, destination in REPLAY_RESIDUE
    ]
    agent_path = AGENT_DIR / f"{run_id}.json"
    prompt = residue_prompt(run_id, expected, agent_path, replay=True)
    return {
        "status": "awaiting_agent",
        "mode": "replay",
        "run_id": run_id,
        "source_message_ids": [str(message["id"])],
        "deterministic_items": deterministic,
        "agent_expected": expected,
        "agent_receipt_path": str(agent_path),
        "free_text_prompt": prompt,
        "sink": "ops-alerts",
        "post_required": False,
        "effects": {"structured_writes": 0, "provider_writes": 0, "outbound_sends": 0},
    }


def event_readback(ledger: cp.Ledger, event_id: str, action: str) -> bool:
    event = ledger.conn.execute("SELECT state FROM inbox_events WHERE event_id=?", (event_id,)).fetchone()
    if event is None:
        return False
    if action == "RESOLVED_EXTERNALLY":
        return str(event["state"]) == "resolved_externally"
    if action == "HOLD":
        return str(event["state"]) == "held"
    if action == "DRAFT":
        draft = ledger.conn.execute(
            "SELECT status FROM local_drafts WHERE event_id=? ORDER BY revision DESC LIMIT 1", (event_id,)
        ).fetchone()
        return draft is not None and str(draft["status"]) in {"draft_generated", "sent_verified"}
    if action == "SEND":
        return str(event["state"]) == "sent_verified"
    if action == "AFFILIATE_TOOL":
        row = ledger.conn.execute(
            "SELECT disposition FROM manual_dispositions WHERE event_id=?", (event_id,)
        ).fetchone()
        return row is not None and str(row["disposition"]) == "AFFILIATE_TOOL"
    return False


def event_action_name(action: str) -> str:
    return "EDIT" if action.startswith("EDIT ") else action.upper()


def card_handler_outcome(
    ledger: cp.Ledger, event_id: str, action: str, decision_message_id: str,
) -> dict[str, Any] | None:
    """Read the inbox card handler's terminal result without replaying its effect."""
    action_name = event_action_name(action)
    event = ledger.conn.execute(
        "SELECT state FROM inbox_events WHERE event_id=?", (event_id,),
    ).fetchone()
    if event is None:
        return None
    event_state = str(event["state"])
    decision = ledger.conn.execute(
        """SELECT action FROM operator_decisions
           WHERE event_id=? AND decision_message_id IN (?,?)
           ORDER BY created_at DESC LIMIT 1""",
        (event_id, decision_message_id, f"{decision_message_id}:{event_id}"),
    ).fetchone()
    decision_matches = decision is not None and str(decision["action"]) == action_name
    owner_outcome = ""
    owner_detail = ""
    if action_name == "SEND" and event_state == "sent_verified":
        verified = ledger._verified_send_local_result(event_id)
        if verified is not None and verified.get("external_send_exists"):
            owner_outcome = "sent_verified"
            owner_detail = str(verified.get("outcome") or "already_sent_verified")
    elif action_name in {"DRAFT", "EDIT"}:
        draft = ledger.conn.execute(
            "SELECT status FROM local_drafts WHERE event_id=? ORDER BY revision DESC LIMIT 1",
            (event_id,),
        ).fetchone()
        draft_status = str(draft["status"]) if draft is not None else ""
        if draft_status in {"draft_generated", "sent_verified"} and (
            action_name == "DRAFT" or decision_matches
        ):
            owner_outcome = draft_status
            owner_detail = "fable_draft_terminal"
    elif action_name == "HOLD" and event_state == "held":
        owner_outcome = "held"
        owner_detail = "operator_disposition_terminal"
    elif action_name == "RESOLVED_EXTERNALLY" and event_state == "resolved_externally":
        owner_outcome = "resolved_externally"
        owner_detail = "operator_disposition_terminal"
    if not owner_outcome:
        return None
    return {
        "status": "already_satisfied_noop",
        "owner": "inbox_card_handler",
        "owner_outcome": owner_outcome,
        "owner_detail": owner_detail,
        "event_state": event_state,
    }


def record_affiliate_tool(ledger: cp.Ledger, event_id: str, message_id: str, note: str) -> dict[str, Any]:
    existing = ledger.conn.execute("SELECT * FROM manual_dispositions WHERE event_id=?", (event_id,)).fetchone()
    if existing is not None:
        if str(existing["disposition"]) != "AFFILIATE_TOOL":
            raise RuntimeError("manual_disposition_conflict")
        return {"status": "pass", "idempotent_replay": True}
    event = ledger.conn.execute("SELECT 1 FROM inbox_events WHERE event_id=?", (event_id,)).fetchone()
    if event is None:
        raise RuntimeError("unknown_event_id")
    now = utc_now()
    receipt = {
        "disposition": "AFFILIATE_TOOL", "source_message_id": message_id,
        "source_channel_id": DECISIONS_CHANNEL_ID, "actor_id": OPERATOR_ID,
        "note": note[:400], "provider_mutation": False, "outbound_send": False,
    }
    action_id = "act_" + sha256(f"{event_id}|affiliate_inbox_tool_routed|{message_id}")[:24]
    with ledger.conn:
        ledger.conn.execute(
            "INSERT INTO manual_dispositions(event_id,disposition,source_message_id,source_channel_id,actor_id,note,created_at) VALUES(?,?,?,?,?,?,?)",
            (event_id, "AFFILIATE_TOOL", message_id, DECISIONS_CHANNEL_ID, OPERATOR_ID, note[:400], now),
        )
        ledger.conn.execute(
            "INSERT OR IGNORE INTO mailbox_actions(action_id,event_id,action,status,provider_receipt_json,attempts,created_at,updated_at) VALUES(?,?,?,?,?,0,?,?)",
            (action_id, event_id, "affiliate_inbox_tool_routed", "verified", json.dumps(receipt, sort_keys=True), now, now),
        )
    ledger.audit("manual_affiliate_tool_route_recorded", "inbox_event", event_id, receipt, actor=f"discord-user:{OPERATOR_ID}")
    return {"status": "pass", "idempotent_replay": False}


def apply_event(
    ledger: cp.Ledger, event_id: str, action: str, message_id: str, note: str,
    *, decision_message_id: str | None = None,
) -> dict[str, Any]:
    action_name = event_action_name(action)
    bound_decision_id = decision_message_id or f"{message_id}:{event_id}"
    handled = card_handler_outcome(ledger, event_id, action, bound_decision_id)
    if handled is not None:
        return handled
    if action_name == "AFFILIATE_TOOL":
        result = record_affiliate_tool(ledger, event_id, message_id, note)
    else:
        card = ledger.conn.execute(
            "SELECT discord_message_id FROM notification_outbox WHERE event_id=? AND purpose='operator_decision' AND status='delivered' ORDER BY delivered_at DESC,outbox_id DESC LIMIT 1",
            (event_id,),
        ).fetchone()
        if card is None:
            raise RuntimeError("active_card_binding_missing")
        result = ledger._handle_synced_operator_action(
            event_id, action, actor_id=OPERATOR_ID, channel_id=DECISIONS_CHANNEL_ID,
            decision_message_id=bound_decision_id, card_message_id=str(card["discord_message_id"]),
        )
    handled = card_handler_outcome(ledger, event_id, action, bound_decision_id)
    if action_name == "AFFILIATE_TOOL" and event_readback(ledger, event_id, action_name):
        handled = {
            "status": "applied_verified", "owner": "inbox/reconciler",
            "owner_outcome": "affiliate_inbox_tool_routed",
        }
    if handled is None:
        raise RuntimeError("event_effect_readback_failed")
    return {**handled, "status": "applied_verified", "result": result}


def bundle_candidates(names: list[str]) -> list[tuple[Path, dict[str, Any]]]:
    wanted = set(names)
    rows = []
    for path in INTAKE_DIR.rglob(BUNDLE_GLOB):
        bundle = load_json(path, {})
        if bundle.get("schema") != "operator-review-draft-bundle-v1":
            continue
        available = {str(name).strip().lower() for name in (bundle.get("drafts") or {})}
        if wanted <= available:
            rows.append((path, bundle))
    return rows


def bundle_binding(ledger: cp.Ledger, name: str, spec: dict[str, Any], decision_message_id: str) -> dict[str, Any]:
    event_id = str(spec.get("event_id") or "")
    event, mailbox, _, classification = ledger._event_context(event_id)
    draft = ledger.conn.execute("SELECT * FROM local_drafts WHERE event_id=? ORDER BY revision DESC LIMIT 1", (event_id,)).fetchone()
    if draft is None or str(draft["status"]) not in {"draft_generated", "sent_verified"}:
        raise RuntimeError(f"approve_draft_not_ready:{name}")
    recipients = [str(v).strip().lower() for v in json.loads(str(draft["recipients_json"] or "[]"))]
    expected = {
        "body": str(draft["body"]), "subject": str(draft["subject"]),
        "from_identity": str(draft["from_identity"]).lower(),
        "recipient": recipients[0] if len(recipients) == 1 else "",
        "provider_thread_id": str(event["provider_thread_id"] or ""),
    }
    observed = {
        "body": str(spec.get("body") or ""), "subject": str(spec.get("subject") or ""),
        "from_identity": str(spec.get("from_identity") or "").lower(),
        "recipient": str(spec.get("recipient") or "").lower(),
        "provider_thread_id": str(spec.get("thread_id") or ""),
    }
    if expected != observed or sha256(expected["body"]) != str(draft["body_sha256"]):
        raise RuntimeError(f"approve_bundle_draft_binding_mismatch:{name}")
    reply_class = str(classification.get("reply_class") or "other")
    if reply_class in {"spam", "other", "medical_legal_boundary", "angry_complaint"}:
        raise RuntimeError(f"approve_reply_class_forbidden:{name}")
    raw_path = Path(str(event["raw_path"] or ""))
    if (
        not raw_path.is_file()
        or sha256(raw_path.read_bytes()) != str(event["raw_sha256"] or "")
        or str(draft["source_hash"] or "") != str(event["raw_sha256"] or "")
    ):
        raise RuntimeError(f"approve_source_hash_mismatch:{name}")
    aliases = {str(mailbox.get("canonical_address") or "").lower(), *(str(x).lower() for x in mailbox.get("aliases") or [])}
    if expected["from_identity"] not in aliases:
        raise RuntimeError(f"approve_from_identity_not_allowlisted:{name}")
    provenance_row = ledger.conn.execute(
        "SELECT payload_json FROM audit_events WHERE event_type='fable_draft_provenance' AND object_type='local_draft' AND object_id=? ORDER BY audit_id DESC LIMIT 1",
        (draft["draft_id"],),
    ).fetchone()
    if provenance_row is None:
        raise RuntimeError(f"approve_provenance_missing:{name}")
    provenance = json.loads(str(provenance_row["payload_json"] or "{}"))
    checks = {
        "event": str(provenance.get("event_id") or "") == event_id,
        "revision": int(provenance.get("revision") or 0) == int(draft["revision"]),
        "body": str(provenance.get("draft_body_sha256") or "") == str(draft["body_sha256"]),
        "raw": str(provenance.get("raw_email_sha256") or "") == str(event["raw_sha256"] or ""),
        "safety": str(provenance.get("safety_result") or "") == "pass",
        "never_auto_send": provenance.get("never_auto_send") is True,
    }
    if not all(checks.values()):
        raise RuntimeError(f"approve_provenance_mismatch:{name}")
    return {
        "event_id": event_id, "draft_id": str(draft["draft_id"]), "draft_revision": int(draft["revision"]),
        "body": expected["body"], "body_sha256": str(draft["body_sha256"]),
        "from_identity": expected["from_identity"], "recipient": expected["recipient"],
        "recipient_display": "", "subject": expected["subject"],
        "provider_message_id": str(event["provider_message_id"] or ""),
        "provider_thread_id": expected["provider_thread_id"], "rfc_message_id": str(event["rfc_message_id"] or ""),
        "decision_message_id": decision_message_id, "mailbox": dict(mailbox),
        "mailbox_id": str(mailbox["mailbox_id"]), "reply_class": reply_class,
    }


def approve_bundle(names: list[str], message_id: str) -> list[dict[str, Any]]:
    candidates = bundle_candidates(names)
    pending = [(p, b) for p, b in candidates if b.get("email_send_authorized") is False]
    if len(pending) > 1:
        raise RuntimeError("approve_bundle_ambiguous")
    resuming = False
    if not pending:
        applied = [
            (p, b) for p, b in candidates
            if b.get("email_send_authorized") is True
            and set(names) <= {str(x).lower() for x in ((b.get("approval") or {}).get("approved_names") or (b.get("drafts") or {}).keys())}
        ]
        if len(applied) != 1:
            raise RuntimeError("approve_pending_bundle_not_found")
        path, bundle = applied[0]
        receipts = ((bundle.get("approval") or {}).get("sends") or {})
        all_verified = all(
            str((receipts.get(name) or {}).get("verified_by") or "") == "provider_thread_readback"
            and bool(str((receipts.get(name) or {}).get("message_id") or ""))
            and bool(str((receipts.get(name) or {}).get("thread_id") or ""))
            for name in names
        )
        if all_verified:
            return [item(name, f"APPROVE {name}", "inbox GmailClient", "already_satisfied_noop", str(path), provider_message_id=str((receipts.get(name) or {}).get("message_id") or "")) for name in names]
        approval = bundle.get("approval") or {}
        if str(approval.get("operator_message_id") or "") != message_id:
            raise RuntimeError("approve_authorized_bundle_incomplete_wrong_source")
        resuming = True
    else:
        path, bundle = pending[0]
    bundle = json.loads(json.dumps(bundle))
    approval = bundle.setdefault("approval", {})
    if not resuming:
        approval.update({
            "operator_reply": "APPROVE " + "|".join(names), "operator_message_id": message_id,
            "actor_id": OPERATOR_ID, "channel_id": DECISIONS_CHANNEL_ID,
            "approved_names": names, "authorized_at_utc": utc_now(),
        })
    states = approval.setdefault("send_states", {})
    ledger = cp.Ledger()
    results = []
    try:
        # Validate every named draft, alias, source hash, and Fable safety receipt
        # before the bundle's false→true authorization transition is persisted.
        # A bad sibling therefore cannot strand a half-authorized bundle.
        bindings = {
            name: bundle_binding(ledger, name, (bundle.get("drafts") or {})[name], message_id)
            for name in names
        }
        if not resuming:
            bundle["email_send_authorized"] = True
            atomic_json(path, bundle)
        for name in names:
            binding = bindings[name]
            state = states.setdefault(name, {"status": "preflight_reserved", "reserved_at_utc": utc_now()})
            existing = ledger._verified_send_local_result(binding["event_id"])
            if existing and existing.get("external_send_exists"):
                state.update({"status": "sent_verified", "provider_message_id": existing.get("provider_message_id"), "verified_at_utc": utc_now()})
                atomic_json(path, bundle)
                results.append(item(name, f"APPROVE {name}", "inbox GmailClient", "already_satisfied_noop", str(path)))
                continue
            allow_new = str(state.get("status")) in {"preflight_reserved", "preflight_failed_safe"}

            def submission_start() -> None:
                state.update({"status": "submission_started", "submission_started_at_utc": utc_now()})
                atomic_json(path, bundle)

            try:
                transport = run_approved_gmail_send_transport(binding, allow_new_submission=allow_new, on_submission_start=submission_start)
            except ApprovedSendError as exc:
                state.update({"status": "outcome_uncertain" if exc.submission_started else "preflight_failed_safe", "reason": exc.reason, "updated_at_utc": utc_now()})
                atomic_json(path, bundle)
                raise
            receipt = {
                "status": "SENT_VERIFIED", "event_id": binding["event_id"], "draft_id": binding["draft_id"],
                "draft_revision": binding["draft_revision"], "body_sha256": binding["body_sha256"],
                "decision_message_id": message_id, "provider_message_id": str(transport["provider_message_id"]),
                "provider_thread_id": str(transport.get("provider_thread_id") or ""), "checks": transport.get("checks") or {},
                "send_executed_now": bool(transport.get("send_executed_now")), "outbound_send": True,
            }
            now = utc_now()
            action_id = "act_" + sha256(f"{binding['event_id']}|bundle_email_sent_verified|{binding['draft_id']}|{message_id}")[:24]
            with ledger.conn:
                ledger.conn.execute("UPDATE inbox_events SET state='sent_verified' WHERE event_id=?", (binding["event_id"],))
                ledger.conn.execute("UPDATE local_drafts SET status='sent_verified',approval_actor_id=?,approval_event_id=?,approved_body_sha256=?,updated_at=? WHERE draft_id=?", (OPERATOR_ID, message_id, binding["body_sha256"], now, binding["draft_id"]))
                ledger.conn.execute("INSERT OR REPLACE INTO mailbox_actions(action_id,event_id,action,status,provider_receipt_json,attempts,created_at,updated_at) VALUES(?,?,?,?,?,1,?,?)", (action_id, binding["event_id"], "email_sent_verified", "verified", json.dumps(receipt, sort_keys=True), now, now))
            ledger.audit("bundle_email_send_verified", "inbox_event", binding["event_id"], receipt, actor=f"discord-user:{OPERATOR_ID}")
            state.update({"status": "sent_verified", "provider_message_id": receipt["provider_message_id"], "provider_thread_id": receipt["provider_thread_id"], "verified_at_utc": now})
            approval.setdefault("sends", {})[name] = {"message_id": receipt["provider_message_id"], "thread_id": receipt["provider_thread_id"], "verified_by": "provider_thread_readback"}
            atomic_json(path, bundle)
            results.append(item(name, f"APPROVE {name}", "inbox GmailClient", "applied_verified" if transport.get("send_executed_now") else "already_satisfied_noop", str(path)))
    finally:
        ledger.close()
    return results


def parse_event_line(line: str) -> tuple[str, str, str] | None:
    match = EVENT_RE.match(line)
    if not match:
        return None
    event_id, rest = match.groups()
    words = rest.strip().split(None, 1)
    action = words[0].upper() if words else ""
    trailing = words[1].strip() if len(words) == 2 else ""
    if action == "EDIT":
        return event_id.lower(), f"EDIT {trailing}" if trailing else "EDIT", ""
    if action in CARD_HANDLER_ACTIONS:
        if trailing.startswith("- "):
            trailing = trailing[2:].strip()
        return event_id.lower(), action, trailing
    if "affiliate inbox tool" in rest.lower():
        return event_id.lower(), "AFFILIATE_TOOL", ""
    return event_id.lower(), action, trailing


def route_live_message(message: dict[str, Any], ledger: cp.Ledger) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    message_id = str(message["id"])
    content = str(message.get("content") or "")
    structured: list[dict[str, Any]] = []
    residue: list[dict[str, Any]] = []
    number = 0
    blocker_sync_needed = False
    parsed_event_count = sum(
        parse_event_line(raw.strip()) is not None
        for raw in content.splitlines() if raw.strip()
    )
    replied_card_message_id = str(
        (message.get("message_reference") or {}).get("message_id") or ""
    )
    for raw in content.splitlines():
        line = raw.strip()
        if not line:
            continue
        number += 1
        acks = action_items.match_acks(line)
        if acks:
            for ack in acks:
                result = action_items.resolve(ack, f"discord:{OPERATOR_ID}", f"decision_message:{message_id}")
                status = "already_satisfied_noop" if result.get("already") else ("applied_verified" if result.get("ok") else "unroutable")
                structured.append(item(number, line, "action_items", status, f"datatable:{action_items.TABLE_ID}", reason=result.get("reason", "")))
            continue
        approve = APPROVE_RE.fullmatch(line)
        if approve:
            names = [part.strip().lower() for part in approve.group(1).split("|") if part.strip()]
            if len(names) != len(set(names)):
                structured.append(item(number, line, "inbox GmailClient", "unroutable", reason="duplicate_approval_name"))
            else:
                try:
                    structured.extend(approve_bundle(names, message_id))
                except Exception as exc:
                    structured.append(item(number, line, "inbox GmailClient", "unroutable", reason=str(exc)[:160]))
            continue
        parsed_event = parse_event_line(line)
        if parsed_event:
            event_id, action, suffix = parsed_event
            action_name = event_action_name(action)
            decision_message_id = (
                message_id if parsed_event_count == 1 else f"{message_id}:{event_id}"
            )
            if action_name not in ALLOWED_EVENT_ACTIONS or action == "EDIT":
                structured.append(item(number, line, "inbox/reconciler", "unroutable", reason="unsupported_event_verdict"))
            else:
                try:
                    result = card_handler_outcome(
                        ledger, event_id, action, decision_message_id,
                    )
                    card_owner = False
                    card_handler_accepts_line = bool(
                        re.fullmatch(
                            rf"{re.escape(event_id)} (?:SEND|DRAFT|HOLD|RESOLVED_EXTERNALLY)(?: - [^\r\n]+)?",
                            line,
                            re.IGNORECASE,
                        )
                        or re.fullmatch(
                            rf"{re.escape(event_id)} EDIT [^\r\n]+", line, re.IGNORECASE,
                        )
                    )
                    if (
                        replied_card_message_id
                        and action_name in CARD_HANDLER_ACTIONS
                        and card_handler_accepts_line
                    ):
                        card_owner = ledger.conn.execute(
                            """SELECT 1 FROM notification_outbox
                               WHERE event_id=? AND discord_message_id=? AND purpose='operator_decision'
                                 AND status='delivered' LIMIT 1""",
                            (event_id, replied_card_message_id),
                        ).fetchone() is not None
                    if result is None and card_owner:
                        event = ledger.conn.execute(
                            "SELECT state FROM inbox_events WHERE event_id=?", (event_id,),
                        ).fetchone()
                        state = str(event["state"]) if event is not None else "unknown_event"
                        structured.append(item(
                            number, f"{event_id} {action}", "inbox card handler", "owned_pending",
                            f"sqlite:{cp.DB_PATH}", owner="inbox_card_handler",
                            owner_outcome=state, reason=f"card_handler_not_terminal:{state}",
                        ))
                    else:
                        if result is None:
                            result = apply_event(
                                ledger, event_id, action, message_id, line,
                                decision_message_id=decision_message_id,
                            )
                        destination = (
                            "inbox card handler"
                            if result.get("owner") == "inbox_card_handler"
                            else "inbox/reconciler"
                        )
                        structured.append(item(
                            number, f"{event_id} {action}", destination, result["status"],
                            f"sqlite:{cp.DB_PATH}",
                            **{
                                key: result[key] for key in
                                ("owner", "owner_outcome", "owner_detail", "event_state")
                                if key in result
                            },
                        ))
                except Exception as exc:
                    structured.append(item(number, f"{event_id} {action}", "inbox/reconciler", "unroutable", reason=str(exc)[:160]))
            if suffix:
                residue.append({"item": f"{number}b", "line": suffix, "source_line": line})
            continue
        blocker = BLK_RE.match(line)
        if blocker:
            blocker_sync_needed = True
            structured.append(item(number, line, "affiliate blocker store", "pending_readback"))
            continue
        residue.append({"item": number, "line": line})
    if blocker_sync_needed:
        try:
            from scripts import glp_affiliate_daily_decisions_report as decisions_report
            outcome = decisions_report.ack_sync(live=True)
            blocker_state = load_json(decisions_report.STATE_PATH, {})
            captured = load_json(INTAKE_DIR / f"{message_id}.json", {})
            captured_lines = {
                str(value) for value in (captured.get("narrative_lines") or [])
            }
            ok = (
                outcome.get("status") == "pass"
                and message_id in set(blocker_state.get("processed_ack_message_ids") or [])
            )
            for row in structured:
                if row["destination"] == "affiliate blocker store" and row["status"] == "pending_readback":
                    exact_capture = str(row["text"]) in captured_lines
                    row["status"] = "applied_verified" if ok and exact_capture else "unroutable"
                    row["evidence"] = str(decisions_report.BLOCKER_STORE)
                    if not ok or not exact_capture:
                        row["reason"] = str(outcome.get("status") or "blocker_sync_readback_failed")
        except Exception as exc:
            for row in structured:
                if row["destination"] == "affiliate blocker store" and row["status"] == "pending_readback":
                    row.update(status="unroutable", reason=str(exc)[:160])
    return structured, residue


def residue_prompt(run_id: str, expected: list[dict[str, Any]], output: Path, *, replay: bool) -> str:
    expected_json = json.dumps(expected, indent=2, ensure_ascii=False)
    mode_rules = (
        "REPLAY MODE. Read only. Do not fire any webhook or mutate any system. Prove each item is already satisfied from the named historical receipts."
        if replay else
        "LIVE MODE. Route every line to its owning machine. Fire the registered entrypoint when needed, verify the effect with a live readback, and receipt it."
    )
    return f"""You are the one bounded Sol routing worker for ops/decisions-router run {run_id}.
{mode_rules}

Machine registry (production webhook paths):
{MACHINE_REGISTRY}

Binding rules:
- Process exactly the expected entries below, once each. Never invent or omit an entry.
- Sheet writes only through sheet_update_queue then /glp-sheet-sync-run.
- Never send email or outreach. APPROVE transport is owned by the deterministic parent lane.
- Legality and brand voice bind any composed text.
- Read before write. An already-satisfied effect is status already_satisfied_noop and causes zero writes.
- Ambiguous entries are status unroutable with the exact original line; never silently drop one.
- Write one JSON object to {output}. Schema: decisions-router-agent-v1, run_id exact, mode, items array, effects object, completed_at_utc. Each item must include item, line, destination, status, evidence. Allowed statuses: applied_verified, already_satisfied_noop, unroutable.
- For replay, use {INTAKE_DIR / ('ROUTING-RECEIPT-' + HISTORICAL_MESSAGE_ID + '.md')} and its cited receipts. All four expected statuses must be already_satisfied_noop and effects must all be zero.
- Finish only after re-reading the JSON file. Print its absolute path on the final line.

Expected entries:
{expected_json}
"""


def confirmation_card(mode: str, items: list[dict[str, Any]], *, run_id: str = "") -> str:
    ordered = sorted(items, key=lambda row: (int(str(row["item"]).rstrip("b")) if str(row["item"]).rstrip("b").isdigit() else 9999, str(row["item"])))
    noop = sum(row.get("status") == "already_satisfied_noop" for row in ordered)
    applied = sum(row.get("status") == "applied_verified" for row in ordered)
    pending = sum(row.get("status") == "owned_pending" for row in ordered)
    unroutable = sum(row.get("status") == "unroutable" for row in ordered)
    heading = "DECISIONS ROUTER — REPLAY PASS" if mode == "replay" and unroutable == 0 else "DECISIONS ROUTER"
    lines = [heading, f"{len(ordered)} items: {noop} already satisfied/no-op · {applied} applied · {pending} owner-pending · {unroutable} unroutable"]
    for row in ordered:
        text = re.sub(r"\s+", " ", str(row.get("text") or row.get("line") or "")).strip()
        if row.get("status") == "unroutable":
            lines.append(f"unroutable: {text[:150]}")
        elif row.get("status") == "owned_pending":
            lines.append(
                f"{row['item']}. {text[:72]} → {row['destination']} "
                f"[pending:{str(row.get('owner_outcome') or 'unknown')[:40]}]"
            )
        else:
            if row.get("owner") == "inbox_card_handler":
                symbol = f"satisfied:{str(row.get('owner_outcome') or 'terminal')[:40]}"
            else:
                symbol = "no-op" if row.get("status") == "already_satisfied_noop" else "verified"
            lines.append(f"{row['item']}. {text[:72]} → {row['destination']} [{symbol}]")
    card = "\n".join(lines)
    if len(card) > 1900:
        compact = lines[:2] + [f"{row['item']}. {str(row.get('text') or row.get('line') or '')[:42]} → {row['destination']}" for row in ordered]
        card = "\n".join(compact)
    if len(card) > 1900:
        # Discord has a 2,000-character limit. Keep the exact count and a
        # bounded pointer to the private receipt instead of dropping the
        # outcome or sending an incomplete per-item list.
        receipt = f"reports/ops/decisions-router/runs/{run_id}.json" if re.fullmatch(r"[A-Za-z0-9_-]+", run_id) else "the private router run receipt"
        card = "\n".join(lines[:2] + [f"Full item dispositions: {receipt}"])
    if len(card) > 1900:
        raise RuntimeError("confirmation_card_too_long")
    return card


def validate_agent(dispatch: dict[str, Any]) -> list[dict[str, Any]]:
    path = Path(str(dispatch["agent_receipt_path"]))
    receipt = load_json(path, {})
    if receipt.get("schema") != "decisions-router-agent-v1" or receipt.get("run_id") != dispatch["run_id"]:
        raise RuntimeError("agent_receipt_binding_mismatch")
    expected_rows = list(dispatch["agent_expected"])
    expected = {
        (str(row["item"]), str(row["line"])): row for row in expected_rows
    }
    got_rows = receipt.get("items") or []
    got = {
        (str(row.get("item")), str(row.get("line") or "")): row
        for row in got_rows if isinstance(row, dict)
    }
    if (
        len(expected_rows) != len(expected)
        or len(got_rows) != len(got)
        or set(got) != set(expected)
    ):
        raise RuntimeError("agent_receipt_item_set_mismatch")
    allowed = {"applied_verified", "already_satisfied_noop", "unroutable"}
    output = []
    for expected_row in expected_rows:
        key = (str(expected_row["item"]), str(expected_row["line"]))
        row = got[key]
        if row.get("status") not in allowed:
            raise RuntimeError(
                f"agent_receipt_item_binding_mismatch:{expected_row['item']}"
            )
        output.append(item(
            expected_row["item"], row["line"],
            str(row.get("destination") or expected_row.get("destination") or "unroutable"),
            str(row["status"]), str(row.get("evidence") or ""),
        ))
    if dispatch["mode"] == "replay":
        if any(row["status"] != "already_satisfied_noop" for row in output):
            raise RuntimeError("replay_agent_not_all_noop")
        if any(int(value or 0) != 0 for value in (receipt.get("effects") or {}).values()):
            raise RuntimeError("replay_agent_effects_nonzero")
    return output


def finalize(dispatch: dict[str, Any], agent_items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    items = list(dispatch.get("deterministic_items") or []) + list(agent_items or [])
    card = confirmation_card(str(dispatch["mode"]), items, run_id=str(dispatch["run_id"]))
    final = {
        **{key: dispatch[key] for key in ("mode", "run_id", "source_message_ids", "sink")},
        "status": "complete", "completed_at_utc": utc_now(), "items": items,
        "item_count": len(items), "already_satisfied_noop_count": sum(row["status"] == "already_satisfied_noop" for row in items),
        "applied_verified_count": sum(row["status"] == "applied_verified" for row in items),
        "owned_pending_count": sum(row["status"] == "owned_pending" for row in items),
        "unroutable_count": sum(row["status"] == "unroutable" for row in items),
        "confirmation_card": card, "confirmation_card_sha256": sha256(card),
        "post_required": True, "confirmation": None,
    }
    atomic_json(RUN_DIR / f"{dispatch['run_id']}.json", final)
    return final


def dispatch(mode: str, message_id: str, sink: str) -> dict[str, Any]:
    replay = mode == "replay"
    if replay and message_id != HISTORICAL_MESSAGE_ID:
        raise RuntimeError("replay_message_not_allowlisted")
    messages = verified_messages(message_id)
    if not replay and not message_id:
        state = load_json(STATE_PATH, {})
        cursor = int(str(state.get("last_message_id") or 0))
        if not cursor:
            latest = max((int(str(row["id"])) for row in messages), default=0)
            atomic_json(STATE_PATH, {"last_message_id": str(latest), "bootstrapped_at_utc": utc_now()})
            run_id = f"live-bootstrap-{latest}"
            base = {"status": "complete", "mode": "live", "run_id": run_id, "source_message_ids": [], "sink": "decisions", "items": [], "item_count": 0, "already_satisfied_noop_count": 0, "applied_verified_count": 0, "owned_pending_count": 0, "unroutable_count": 0, "confirmation_card": "", "confirmation_card_sha256": sha256(""), "post_required": False, "confirmation": None, "completed_at_utc": utc_now()}
            atomic_json(RUN_DIR / f"{run_id}.json", base)
            return base
        messages = [row for row in messages if int(str(row["id"])) > cursor]
        if not messages:
            run_id = f"live-clean-{cursor}"
            base = {"status": "complete", "mode": "live", "run_id": run_id, "source_message_ids": [], "sink": "decisions", "items": [], "item_count": 0, "already_satisfied_noop_count": 0, "applied_verified_count": 0, "owned_pending_count": 0, "unroutable_count": 0, "confirmation_card": "", "confirmation_card_sha256": sha256(""), "post_required": False, "confirmation": None, "completed_at_utc": utc_now()}
            atomic_json(RUN_DIR / f"{run_id}.json", base)
            return base
    if not messages:
        raise RuntimeError("no_operator_messages_selected")
    run_id = run_id_for(mode, messages)
    prior = load_json(RUN_DIR / f"{run_id}.json", None)
    if prior and prior.get("status") == "complete":
        prior["post_required"] = not bool(prior.get("confirmation"))
        return prior
    for message in messages:
        capture_message(message, replay=replay)
    if replay:
        staged = replay_dispatch(messages[0], run_id)
    else:
        ledger = cp.Ledger()
        deterministic: list[dict[str, Any]] = []
        residue: list[dict[str, Any]] = []
        try:
            for message in messages:
                rows, remaining = route_live_message(message, ledger)
                deterministic.extend(rows)
                residue.extend({**row, "message_id": str(message["id"])} for row in remaining)
        finally:
            ledger.close()
        agent_path = AGENT_DIR / f"{run_id}.json"
        staged = {
            "status": "awaiting_agent" if residue else "ready",
            "mode": "live", "run_id": run_id,
            "source_message_ids": [str(row["id"]) for row in messages],
            "deterministic_items": deterministic, "agent_expected": residue,
            "agent_receipt_path": str(agent_path), "sink": sink or "decisions",
            "post_required": False,
        }
        if residue:
            staged["free_text_prompt"] = residue_prompt(run_id, residue, agent_path, replay=False)
    atomic_json(RUN_DIR / f"{run_id}.dispatch.json", staged)
    if staged["status"] == "ready":
        result = finalize(staged)
    else:
        result = staged
    return result


def resume(run_id: str, job_id: str) -> dict[str, Any]:
    dispatch_path = RUN_DIR / f"{run_id}.dispatch.json"
    staged = load_json(dispatch_path, {})
    if staged.get("run_id") != run_id or staged.get("status") != "awaiting_agent":
        raise RuntimeError("resume_dispatch_binding_mismatch")
    try:
        agent_items = validate_agent(staged)
    except Exception as exc:
        agent_items = [item(row["item"], str(row["line"]), "unroutable", "unroutable", reason=str(exc)[:160]) for row in staged.get("agent_expected") or []]
    result = finalize(staged, agent_items)
    result["agent_job_id"] = job_id
    atomic_json(RUN_DIR / f"{run_id}.json", result)
    return result


def confirm(run_id: str, discord_message_id: str, sink: str) -> dict[str, Any]:
    path = RUN_DIR / f"{run_id}.json"
    receipt = load_json(path, {})
    if receipt.get("run_id") != run_id or receipt.get("status") != "complete":
        raise RuntimeError("confirmation_run_binding_mismatch")
    channel = OPS_ALERTS_CHANNEL_ID if sink == "ops-alerts" else DECISIONS_CHANNEL_ID
    live = cp.discord_request(f"/channels/{channel}/messages/{discord_message_id}")
    checks = {
        "id": str(live.get("id") or "") == discord_message_id,
        "channel": str(live.get("channel_id") or channel) == channel,
        "content": str(live.get("content") or "") == str(receipt.get("confirmation_card") or ""),
    }
    if not all(checks.values()):
        raise RuntimeError("confirmation_live_readback_mismatch")
    receipt["confirmation"] = {
        "discord_message_id": discord_message_id, "channel_id": channel,
        "sink": sink, "checks": checks, "verified_at_utc": utc_now(),
    }
    receipt["post_required"] = False
    atomic_json(path, receipt)
    if receipt.get("mode") == "live" and receipt.get("source_message_ids"):
        state = load_json(STATE_PATH, {}) or {}
        prior = int(str(state.get("last_message_id") or 0))
        observed = max(int(str(value)) for value in receipt["source_message_ids"])
        state["last_message_id"] = str(max(prior, observed))
        state["updated_at_utc"] = utc_now()
        atomic_json(STATE_PATH, state)
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("live", "replay", "resume", "confirm"), default="live")
    parser.add_argument("--message-id", default="")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--job-id", default="")
    parser.add_argument("--sink", choices=("decisions", "ops-alerts"), default="decisions")
    parser.add_argument("--discord-message-id", default="")
    args = parser.parse_args(argv)
    private_dir(ROOT); private_dir(RUN_DIR); private_dir(AGENT_DIR)
    try:
        if args.mode in {"live", "replay"}:
            result = dispatch(args.mode, args.message_id, args.sink)
        elif args.mode == "resume":
            result = resume(args.run_id, args.job_id)
        else:
            result = confirm(args.run_id, args.discord_message_id, args.sink)
        print(json.dumps(result, sort_keys=True, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "blocked", "mode": args.mode, "reason": str(exc)[:300], "outbound_sends": 0}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
