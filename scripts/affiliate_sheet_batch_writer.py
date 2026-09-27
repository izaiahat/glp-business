#!/usr/bin/env python3
"""Canonical idempotent batch writer for applied/active Katalys program rows."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
import sys
sys.path.append("/home/ubuntu/business/scripts")

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
import affiliate_program_relevance as program_relevance  # noqa: E402
from affiliate_provider_row_scope import provider_row_scope, require_scope_retraction
import affiliate_provider_metadata as provider_metadata
from affiliate_sheet_schema_guard import (
    EXPECTED_HEADERS,
    EXPECTED_RANGE,
    REQUIRED_MARKETPLACE_PARENT_ROWS,
    VERTICAL_ALLOWED_VALUES,
    audit_checkbox_schema,
    column_letter,
    require_api_capabilities_value,
    require_checkbox_schema_pass,
    require_marketplace_parent_rows_not_mutated,
    require_vertical_value,
    schema_requests,
)
try:
    from affiliate_niche_geo_filter import choke_candidate
except ImportError:
    from scripts.affiliate_niche_geo_filter import choke_candidate
try:
    from affiliate_operator_rulings import load_snapshot, policy_receipt
except ImportError:
    from scripts.affiliate_operator_rulings import load_snapshot, policy_receipt

SHEET_ID = "1P2Tr6ePYN9Q35_V90FyHs6ONUwam4iIErNEftdJPL78"
TAB = "Affiliate Applications"
TOKEN = Path("/home/ubuntu/.hermes/secrets/glp-scout-ops-google/google_token.json")
LOCK = Path("/home/ubuntu/business/locks/glp-affiliate-sheet.lock")
ORG = "098e5455-ca72-4099-9ff9-a9dd014148e7"
PUBLISHER_ID = 13031
TODAY = date.today()
HEADERS = list(EXPECTED_HEADERS)
MUTABLE_COLUMNS = (10, 11, 18, 23, 24, 26)

# --- wire-in gate mode -------------------------------------------------------
# A wire-in run advances the gate columns the affiliate build reads and, after a
# deploy, records that the row is live. It is deliberately narrower than the
# Katalys reconciliation path above: it touches only these columns, only rows
# named explicitly in a signed plan, and never creates a row.
#   10 Status, 14 Tracking Link / Deep Link, 19 Wire-In Ready?,
#   20 Wired Live?, 21 Link QA Status, 24 Last Checked UTC
#
# Column 19 (Wire-In Ready?) joined the set on 2026-08-06. It is the gate the
# affiliate builder reads to decide a row may be wired, and it is the honest
# consequence of columns 10/14 rather than an independent approval claim: a row
# whose Status is already terminal-approved and whose provider-issued tracking
# link is on the sheet IS wire-in ready. It cannot talk a row up into approval,
# because WIRE_IN_ALLOWED_STATUS still bounds column 10 and approval truth is
# established by the network adapters, not here.
WIRE_IN_MUTABLE_COLUMNS = (10, 14, 19, 20, 21, 24)
WIRE_IN_CHECKBOX_COLUMNS = (19, 20)
WIRE_IN_PLAN_SCHEMA = "g6-affiliate-wire-in-sheet-plan-v1"
# Only the terminal wire-in statuses may be written by this mode. It must never
# be usable to talk a row *up* into approval - approval truth is established by
# the network adapters, not here.
WIRE_IN_ALLOWED_STATUS = {"approved_active_tracking_ready", "approved_active_wired_live"}
# `not_clicked_preserve_attribution` is the default and stays the default: we do
# not click affiliate links, because a self-click pollutes attribution.
#
# 2026-08-18 adds ONE alternative, and only for rows that carry the receipt that
# earns it. TRT Nation's AFF-TRT-001 hold could only be cleared by *verifying*
# that a deep link landed on the reviewed weight-loss program rather than the
# provider's testosterone funnel - which cannot be established without observing
# the redirect. The bridge authorised that QA. Writing
# `not_clicked_preserve_attribution` on a row we did click would be a false fact
# on the Sheet, which is the thing this whole writer exists to prevent.
#
# So the value is evidence-bound, not vocabulary-bound: see
# `wire_in_link_click_qa()`. Without a receipt that checks out, the only legal
# Link-QA value is still `not_clicked_preserve_attribution`, and a plan is still
# required to declare zero tracking clicks.
WIRE_IN_QA_VERIFIED_LINK_QA = "verified_redirect_to_official_200"
WIRE_IN_ALLOWED_LINK_QA = {"not_clicked_preserve_attribution", WIRE_IN_QA_VERIFIED_LINK_QA}
WIRE_IN_LINK_CLICK_QA_SCHEMA = "glp.affiliate_link_click_qa.v1"

# Explicit terminal dispositions for approved GLP rows where the provider
# cannot issue a link. This is deliberately separate from wire-in: it can only
# keep a row link-dark and not-ready, and it records the exact blocker so an
# approved-but-unlinked row is never a silent permanent limbo state.
UNLINKED_DISPOSITION_MUTABLE_COLUMNS = (10, 19, 23, 24)
UNLINKED_DISPOSITION_PLAN_SCHEMA = "affiliate-approved-unlinked-disposition-plan-v1"
UNLINKED_DISPOSITION_ALLOWED_STATUS = {
    "blocked_provider_tracking_link_unavailable",
    "blocked_provider_relationship_not_active",
    "duplicate_do_not_use",
}

# --- network-paused disposition mode ----------------------------------------
# The exact inverse precondition of the unlinked mode, for the case the Katalys
# pause of 2026-08-17 exposed: a row that IS live and wired, whose NETWORK
# paused the program. Before this existed the Sheet kept reading
# live_wired_verified / approved_active_wired_live for three programs whose
# offers were paused and whose site surfaces had already been removed, because
# no writer mode was allowed to move a live row DOWN.
#
# It is deliberately one-way and evidence-bound:
#   * the only status it may write is the paused status - it can never talk a
#     row UP into any approved/live state (that truth belongs to the network
#     adapters, exactly as in wire-in),
#   * it may only start from a status that is genuinely live/wired,
#   * it never touches column 14: the tracking link is HISTORY, the Wired Live?
#     flag is the CLAIM, and only the claim is retracted,
#   * every row must carry an evidence reference (the network's own pause
#     notice - message id + saved evidence path) and that reference must appear
#     in the blocker note it writes, so the Sheet itself carries the receipt.
NETWORK_PAUSED_DISPOSITION_MUTABLE_COLUMNS = (10, 19, 20, 21, 23, 24)
NETWORK_PAUSED_DISPOSITION_REQUIRED_COLUMNS = (10, 19, 20, 23, 24)
NETWORK_PAUSED_DISPOSITION_PLAN_SCHEMA = "affiliate-network-paused-disposition-plan-v1"
NETWORK_PAUSED_DISPOSITION_ALLOWED_STATUS = {"paused_network_program_paused"}
NETWORK_PAUSED_DISPOSITION_ALLOWED_LINK_QA = {
    "link_retired_network_program_paused",
    "not_clicked_preserve_attribution",
}
#: Statuses a row may be moved DOWN from. Anything else is refused, so the mode
#: can only ever act on a row that really was making a live claim.
NETWORK_PAUSED_DISPOSITION_SOURCE_STATUS = {
    "live_wired_verified",
    "approved_active_wired_live",
}


# --- add-approved-row mode ---------------------------------------------------
# The invisibility bug, found by the 2026-08-18 approval backfill audit: an
# approval that the network portal confirms but the Sheet never recorded is
# invisible to EVERY downstream machine. The wire-backlog worker projects from
# the Sheet, so Katalys 1575 (System Labs) sat approved in the portal for weeks
# with no row, no ledger key, no queue entry, and no site presence.
#
# Every other mode here is forbidden from creating a row. This one exists only
# to close that hole, and it is deliberately the weakest writer in the file:
#   * it may ONLY create a row - it refuses outright if any row already claims
#     the program, so it can never be used to edit or overwrite existing truth,
#   * it may only write a pre-live approved status, never a wired/live one,
#   * it always writes Wire-In Ready? and Wired Live? FALSE. Promotion to
#     wire-ready remains the wire-in mode's job, from that mode's own gates,
#   * every row must cite the portal capture that proves the approval (path +
#     sha256 + capture timestamp + the relationship string that capture holds),
#     that capture must exist on disk and hash-match, and the citation must
#     appear in the row's own Notes/Blocker cell so the Sheet carries the
#     receipt the same way the network-paused mode does.
ADD_APPROVED_ROW_PLAN_SCHEMA = "affiliate-add-approved-row-plan-v1"
#: Columns this mode writes on the row it creates. It writes a whole new row, so
#: this is the full identity//evidence set - but only ever into a blank row.
ADD_APPROVED_ROW_REQUIRED_COLUMNS = (1, 2, 3, 4, 6, 7, 10, 11, 19, 20, 23, 24, 25)
#: Pre-live approved statuses only. This mode can never write a live/wired claim
#: - that truth belongs to the wire-in mode after a real deploy.
ADD_APPROVED_ROW_ALLOWED_STATUS = {
    "approved_active_not_wired",
    "approved_active_tracking_ready",
}
ADD_APPROVED_ROW_ALLOWED_RELATIONSHIP = {"approved", "active", "approved_active"}
#: All four Sheet checkbox columns, not just the wire-in pair: this mode fills a
#: whole new row, so Apply? and Signup Complete? must be written as real bools.
ADD_APPROVED_ROW_CHECKBOX_COLUMNS = (1, 11, 19, 20)
#: Approval Date is a date-formatted column with its own schema contract. This
#: mode does not write it - the portal capture timestamp lives in the note.
ADD_APPROVED_ROW_FORBIDDEN_COLUMNS = (18,)


def add_approved_row_write_value(column: int, value: Any) -> Any:
    """Coerce a planned cell to the type the column stores (see wire_in_write_value)."""
    if column in ADD_APPROVED_ROW_CHECKBOX_COLUMNS:
        return str(value).strip().upper() == "TRUE"
    return value


def add_approved_row_cell_equal(column: int, actual: Any, expected: Any) -> bool:
    """Compare one planned cell, treating all four checkbox columns as booleans."""
    if column in ADD_APPROVED_ROW_CHECKBOX_COLUMNS:
        if isinstance(actual, bool):
            return actual is add_approved_row_write_value(column, expected)
        if actual == "":
            return add_approved_row_write_value(column, expected) is False
        return False
    return cell_equal(actual, expected)


def add_approved_row_changed_cells(
    values: list[list[Any]], rows: list[dict[str, Any]],
) -> list[tuple[int, int, Any]]:
    """Diff the plan against the live sheet.

    Unlike wire_in_changed_cells this MUST tolerate a blank target row - filling
    a blank row is the entire point of the mode. Blankness is proven separately
    by require_add_approved_row_preconditions before this is called.
    """
    changes: list[tuple[int, int, Any]] = []
    for entry in rows:
        row_num = int(entry["row"])
        actual = pad(values[row_num - 1] if len(values) >= row_num else [])
        for column, expected in entry["cells"].items():
            column = int(column)
            if not add_approved_row_cell_equal(column, actual[column], expected):
                changes.append((row_num, column, expected))
    return changes


def add_approved_row_rows(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate a narrow, portal-evidence-bound new-approved-row plan."""
    if plan.get("schema") != ADD_APPROVED_ROW_PLAN_SCHEMA:
        raise RuntimeError(f"add_approved_row_schema_invalid:{plan.get('schema')}")
    if not re.fullmatch(r"[a-f0-9]{64}", str(plan.get("authority_sha256") or "")):
        raise RuntimeError("add_approved_row_authority_sha256_invalid")
    if int(plan.get("tracking_clicks", -1)) != 0:
        raise RuntimeError("add_approved_row_tracking_clicks_must_be_zero")
    if int(plan.get("provider_writes", -1)) != 0:
        raise RuntimeError("add_approved_row_provider_writes_must_be_zero")
    rows = plan.get("rows")
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("add_approved_row_rows_empty")
    required = {str(value) for value in ADD_APPROVED_ROW_REQUIRED_COLUMNS}
    allowed = {str(value) for value in range(len(HEADERS))}
    seen_identity: set[tuple[str, str]] = set()
    for entry in rows:
        program = str(entry.get("program") or "").strip()
        if not program:
            raise RuntimeError("add_approved_row_program_missing")
        network = str(entry.get("network") or "").strip()
        provider_program_id = str(entry.get("provider_program_id") or "").strip()
        if not network or not provider_program_id:
            raise RuntimeError(f"add_approved_row_identity_missing:{program}")
        identity = (norm(network), provider_program_id.casefold())
        if identity in seen_identity:
            raise RuntimeError(f"add_approved_row_duplicate_identity:{program}")
        seen_identity.add(identity)

        evidence = entry.get("evidence")
        if not isinstance(evidence, dict):
            raise RuntimeError(f"add_approved_row_evidence_missing:{program}")
        capture_path = str(evidence.get("portal_capture_path") or "").strip()
        if not capture_path.startswith("reports/"):
            raise RuntimeError(f"add_approved_row_evidence_path_invalid:{program}")
        resolved = Path("/home/ubuntu/business") / capture_path
        if not resolved.is_file():
            raise RuntimeError(f"add_approved_row_evidence_capture_missing:{program}")
        digest = str(evidence.get("portal_capture_sha256") or "").strip()
        if not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise RuntimeError(f"add_approved_row_evidence_sha256_invalid:{program}")
        if hashlib.sha256(resolved.read_bytes()).hexdigest() != digest:
            raise RuntimeError(f"add_approved_row_evidence_capture_hash_drift:{program}")
        captured_at = str(evidence.get("captured_at_utc") or "").strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T[\d:.]+Z?", captured_at):
            raise RuntimeError(f"add_approved_row_evidence_timestamp_invalid:{program}")
        relationship = str(evidence.get("relationship") or "").strip().lower()
        if relationship not in ADD_APPROVED_ROW_ALLOWED_RELATIONSHIP:
            raise RuntimeError(f"add_approved_row_evidence_relationship_invalid:{program}:{relationship}")

        cells = entry.get("cells")
        if not isinstance(cells, dict) or not cells:
            raise RuntimeError(f"add_approved_row_columns_invalid:{program}")
        keys = {str(key) for key in cells}
        if not keys <= allowed or not required <= keys:
            raise RuntimeError(f"add_approved_row_columns_invalid:{program}")
        forbidden = keys & {str(value) for value in ADD_APPROVED_ROW_FORBIDDEN_COLUMNS}
        if forbidden:
            raise RuntimeError(f"add_approved_row_forbidden_column:{program}:{sorted(forbidden)}")
        get = lambda column: cells.get(str(column), cells.get(column))  # noqa: E731
        if str(get(2) or "").strip() != program:
            raise RuntimeError(f"add_approved_row_program_cell_mismatch:{program}")
        if norm(get(4)) != norm(network):
            raise RuntimeError(f"add_approved_row_network_cell_mismatch:{program}")
        status = str(get(10) or "")
        if status not in ADD_APPROVED_ROW_ALLOWED_STATUS:
            raise RuntimeError(f"add_approved_row_status_not_allowed:{program}:{status}")
        for column in (19, 20):
            if str(get(column)).strip().upper() != "FALSE":
                raise RuntimeError(f"add_approved_row_must_not_claim_live:{program}:{column}")
        tracking = str(get(14) or "").strip()
        if status == "approved_active_tracking_ready" and not tracking.startswith("https://"):
            raise RuntimeError(f"add_approved_row_tracking_link_required:{program}")
        if tracking and not tracking.startswith("https://"):
            raise RuntimeError(f"add_approved_row_tracking_link_invalid:{program}")
        official = str(get(6) or "").strip()
        if not official.startswith("https://"):
            raise RuntimeError(f"add_approved_row_official_url_invalid:{program}")
        require_vertical_value(get(25))
        if "26" in keys:
            require_api_capabilities_value(get(26))
        note = str(get(23) or "").strip()
        if len(note) < 20:
            raise RuntimeError(f"add_approved_row_note_too_short:{program}")
        if capture_path not in note or captured_at not in note:
            raise RuntimeError(f"add_approved_row_note_missing_evidence:{program}")
        checked = str(get(24) or "").strip()
        if not checked or "T" not in checked:
            raise RuntimeError(f"add_approved_row_last_checked_invalid:{program}")
    return rows


def add_approved_row_existing_matches(
    values: list[list[Any]], entry: dict[str, Any],
) -> list[dict[str, Any]]:
    """Every existing row that already claims this program, by any identity axis.

    Identity is checked three ways because the Sheet has historically carried
    the same program under a portal offer id, under a bare name, and under a
    tracking link alone. Any single hit refuses the add: this mode creates rows,
    it never reconciles them.
    """
    # Row identity is provider × canonical marketplace. Display-label variants
    # are one rail; a genuinely different marketplace is a distinct relationship.
    network = canonical_network(entry["network"])
    provider_program_id = str(entry["provider_program_id"]).strip().casefold()
    separate_identity = (
        str((entry.get("evidence") or {}).get("operator_rule") or "")
        == "separate_provider_marketplace_identity_row"
    )
    tracking = str(
        entry["cells"].get("14", entry["cells"].get(14)) or ""
    ).strip().casefold()
    parent_matches: list[dict[str, Any]] = []
    id_matches: list[dict[str, Any]] = []
    name_matches: list[dict[str, Any]] = []
    for row_num, row in enumerate(values[1:], 2):
        actual = pad(row)
        if not row_has_business_value(actual):
            continue
        row_network = canonical_network(actual[4])
        axes: list[str] = []
        # Required marketplace parents are identity records in their own right.
        # Their route cell intentionally varies (for example the canonical
        # MaxBounty parent says "MaxBounty Network"), so an exact parent name on
        # its registered row must block row creation even when the route spelling
        # differs. Restoration, never add_approved_row, owns a missing parent.
        if (REQUIRED_MARKETPLACE_PARENT_ROWS.get(str(entry["program"])) == row_num
                and str(actual[2]).strip() == str(entry["program"]).strip()):
            axes.append("canonical_parent_row")
        if row_network == network:
            axes.extend(provider_program_identity_axes(actual, network, provider_program_id))
        if separate_identity and set(axes) == {"notes_program_id"}:
            # Payout-evaluation notes name a proposed winner before its row
            # exists. For the explicitly required separate identity, that prose
            # is not an ownership axis. Apply URL and tracking-link identities
            # remain authoritative and continue to block duplicates.
            axes = []
        # A byte-identical tracking URL is normally globally unique even if a
        # stale Network cell is wrong.  The sole exception is an evidence-bound
        # provider program -> offer-contract identity for which the operator
        # explicitly requires separate rows while the network issues one Link
        # creative.  That mapping is re-read and hash-verified above; a bare
        # caller assertion never weakens the duplicate guard.
        row_tracking = str(actual[14] or "").strip().casefold()
        if tracking and row_tracking == tracking:
            mapping = (entry.get("evidence") or {}).get("shared_tracking_link_identity")
            shared_identity = False
            if isinstance(mapping, dict):
                source_text = str(mapping.get("source_path") or "").strip()
                source = Path(source_text)
                if not source.is_absolute():
                    source = Path("/home/ubuntu/business") / source
                expected_sha = str(mapping.get("source_sha256") or "").strip()
                winner_program_id = str(mapping.get("winner_program_id") or "").strip()
                incumbent_program_id = str(mapping.get("incumbent_program_id") or "").strip()
                source_ok = False
                if (source.is_file() and re.fullmatch(r"[a-f0-9]{64}", expected_sha)
                        and hashlib.sha256(source.read_bytes()).hexdigest() == expected_sha):
                    try:
                        source_doc = json.loads(source.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        source_doc = {}
                    source_ok = any(
                        str((((record.get("immutable_ids") or {}).get("value") or {})
                             .get("program_id") or "")) == winner_program_id
                        and str((((record.get("immutable_ids") or {}).get("value") or {})
                                .get("offer_contract_id") or "")) == incumbent_program_id
                        and str((((record.get("approval_status") or {}).get("value") or {})
                                .get("relationship_status") or "")).casefold() == "approved"
                        for record in (source_doc.get("rows") or [])
                        if isinstance(record, dict)
                    )
                shared_identity = (
                    mapping.get("rule") == "operator_separate_program_contract_row"
                    and canonical_network(mapping.get("network")) == network
                    and winner_program_id == provider_program_id
                    and int(mapping.get("incumbent_sheet_row") or 0) == row_num
                    and program_id_in_text(incumbent_program_id, actual[23])
                    and source_ok
                )
            if not shared_identity:
                axes.append("tracking_link")
        record = {"row": row_num, "program": str(actual[2]), "axes": sorted(set(axes))}
        if "canonical_parent_row" in axes:
            parent_matches.append(record)
        strong = [axis for axis in axes if axis != "canonical_parent_row"]
        if strong:
            id_matches.append(record)
        elif (row_network == network
              and equivalent_program_name(actual[2], entry["program"])):
            name_matches.append({"row": row_num, "program": str(actual[2]),
                                 "axes": ["program_name"]})
    # A provider id or exact tracking link outranks a name. Names are consulted
    # only when no stronger axis exists on this canonical marketplace.
    if id_matches:
        return parent_matches + [item for item in id_matches if item not in parent_matches]
    if parent_matches:
        return parent_matches
    return name_matches


def require_add_approved_row_preconditions(
    values: list[list[Any]], rows: list[dict[str, Any]], target_rows: dict[str, int],
) -> None:
    """Prove each target row is genuinely blank and the program is genuinely new."""
    for entry in rows:
        program = str(entry["program"])
        matches = add_approved_row_existing_matches(values, entry)
        row_num = target_rows.get(program)
        # Re-runnable: after a successful write the row this mode created is
        # itself a match, and it must be the ONLY one, at the exact target row.
        applied = [item for item in matches if item["row"] == row_num]
        others = [item for item in matches if item["row"] != row_num]
        if others:
            raise RuntimeError(
                f"add_approved_row_program_already_present:{program}:{json.dumps(others, sort_keys=True)}")
        if not row_num:
            raise RuntimeError(f"add_approved_row_target_row_missing:{program}")
        if not applied and len(values) >= row_num and row_has_business_value(values[row_num - 1]):
            raise RuntimeError(f"add_approved_row_target_row_not_blank:{program}:{row_num}")


def network_paused_disposition_rows(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate a narrow, evidence-bound network-pause disposition plan."""
    if plan.get("schema") != NETWORK_PAUSED_DISPOSITION_PLAN_SCHEMA:
        raise RuntimeError(f"network_paused_disposition_schema_invalid:{plan.get('schema')}")
    if not re.fullmatch(r"[a-f0-9]{64}", str(plan.get("authority_sha256") or "")):
        raise RuntimeError("network_paused_disposition_authority_sha256_invalid")
    if int(plan.get("tracking_clicks", -1)) != 0:
        raise RuntimeError("network_paused_disposition_tracking_clicks_must_be_zero")
    if int(plan.get("provider_writes", -1)) != 0:
        raise RuntimeError("network_paused_disposition_provider_writes_must_be_zero")
    rows = plan.get("rows")
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("network_paused_disposition_rows_empty")
    allowed_columns = {str(value) for value in NETWORK_PAUSED_DISPOSITION_MUTABLE_COLUMNS}
    required_columns = {str(value) for value in NETWORK_PAUSED_DISPOSITION_REQUIRED_COLUMNS}
    seen: set[int] = set()
    for entry in rows:
        row_num = entry.get("row")
        if not isinstance(row_num, int) or row_num < 2:
            raise RuntimeError(f"network_paused_disposition_row_number_invalid:{row_num}")
        if row_num in seen:
            raise RuntimeError(f"network_paused_disposition_duplicate_row:{row_num}")
        seen.add(row_num)
        if not str(entry.get("program") or "").strip():
            raise RuntimeError(f"network_paused_disposition_program_missing:{row_num}")
        evidence = entry.get("evidence")
        if not isinstance(evidence, dict):
            raise RuntimeError(f"network_paused_disposition_evidence_missing:{row_num}")
        message_id = str(evidence.get("message_id") or "").strip()
        evidence_path = str(evidence.get("path") or "").strip()
        if not message_id:
            raise RuntimeError(f"network_paused_disposition_evidence_message_id_missing:{row_num}")
        if not evidence_path.startswith("reports/"):
            raise RuntimeError(f"network_paused_disposition_evidence_path_invalid:{row_num}")
        cells = entry.get("cells")
        if not isinstance(cells, dict) or not cells:
            raise RuntimeError(f"network_paused_disposition_columns_invalid:{row_num}")
        keys = {str(key) for key in cells}
        if not keys <= allowed_columns or not required_columns <= keys:
            raise RuntimeError(f"network_paused_disposition_columns_invalid:{row_num}")
        status = str(cells.get("10", cells.get(10)) or "")
        if status not in NETWORK_PAUSED_DISPOSITION_ALLOWED_STATUS:
            raise RuntimeError(f"network_paused_disposition_status_not_allowed:{row_num}:{status}")
        for checkbox in (19, 20):
            flag = cells.get(str(checkbox), cells.get(checkbox))
            if str(flag).strip().upper() != "FALSE":
                raise RuntimeError(
                    f"network_paused_disposition_must_retract_claim:{row_num}:{checkbox}")
        if "21" in keys:
            qa = str(cells.get("21", cells.get(21)) or "")
            if qa not in NETWORK_PAUSED_DISPOSITION_ALLOWED_LINK_QA:
                raise RuntimeError(f"network_paused_disposition_link_qa_not_allowed:{row_num}:{qa}")
        note = str(cells.get("23", cells.get(23)) or "").strip()
        if len(note) < 20:
            raise RuntimeError(f"network_paused_disposition_blocker_too_short:{row_num}")
        if message_id not in note or evidence_path not in note:
            raise RuntimeError(f"network_paused_disposition_blocker_missing_evidence:{row_num}")
        checked = str(cells.get("24", cells.get(24)) or "").strip()
        if not checked or "T" not in checked:
            raise RuntimeError(f"network_paused_disposition_last_checked_invalid:{row_num}")
    return rows


def require_network_paused_disposition_preconditions(
    values: list[list[Any]], rows: list[dict[str, Any]],
) -> None:
    """Prove every target is the exact live, linked, GLP row named by the plan.

    Re-runnable on purpose: after the write the row is no longer Wired Live, so
    a row already sitting at the target status satisfies the mode instead of
    tripping it. The tracking link must be present BOTH before and after - this
    mode is never allowed to blank it.
    """
    for entry in rows:
        row_num = int(entry["row"])
        if len(values) < row_num or not row_has_business_value(values[row_num - 1]):
            raise RuntimeError(f"network_paused_disposition_target_row_blank:{row_num}")
        actual = pad(values[row_num - 1])
        if str(actual[2]).strip() != str(entry["program"]).strip():
            raise RuntimeError(f"network_paused_disposition_program_mismatch:{row_num}")
        if not str(actual[14] or "").strip():
            raise RuntimeError(f"network_paused_disposition_tracking_link_absent:{row_num}")
        if "glp1" not in str(actual[25] or "").lower():
            raise RuntimeError(f"network_paused_disposition_vertical_not_glp1:{row_num}")
        target_status = str(entry["cells"].get("10", entry["cells"].get(10)))
        current_status = str(actual[10] or "")
        already_applied = current_status == target_status
        if not already_applied and current_status not in NETWORK_PAUSED_DISPOSITION_SOURCE_STATUS:
            raise RuntimeError(
                f"network_paused_disposition_current_status_not_live:{row_num}:{current_status}")
        live = actual[20] is True or str(actual[20]).strip().upper() == "TRUE"
        if not already_applied and not live:
            raise RuntimeError(f"network_paused_disposition_row_not_wired_live:{row_num}")


def require_operator_ruling_clear(programs: list[dict[str, Any]], snapshot) -> None:
    """Final provider-free policy gate before any Sheet service is created."""
    for program in programs:
        name = str(program.get("name") or program.get("program") or program.get("provider") or "").strip()
        if not name:
            if any(rule.verdict == "HOLD" for rule in snapshot.rulings.values()):
                raise RuntimeError("operator_ruling_identity_missing:parent_must_bind_wire_in_plan_to_program_name")
            continue
        decision = snapshot.evaluate_program(name)
        if decision is not None and decision.verdict == "HOLD":
            raise RuntimeError(f"operator_ruling_hold:{decision.instruction_key}")


def gate_batch_candidate(program: dict[str, Any], operation: str,
                         persist: bool = True) -> dict[str, Any]:
    candidate = dict(program)
    candidate.setdefault("display_name", candidate.get("name") or candidate.get("program") or "")
    candidate.setdefault("network", candidate.get("network") or "Katalys")
    candidate.setdefault("program_id", candidate.get("offer_id") or candidate.get("program_id") or "")
    candidate.setdefault("application_url", candidate.get("application_url") or candidate.get("apply_url") or candidate.get("url") or "")
    return choke_candidate(candidate, require_application_url=True, operation=operation, persist=persist)


def build_plan(programs: list[dict[str, Any]], *, persist: bool = True) -> dict[str, Any]:
    """Pure local plan gate used immediately before the Sheet writer."""
    allowed, parked = [], []
    for program in programs:
        gate = gate_batch_candidate(program, "affiliate_sheet_batch_writer.build_plan", persist=persist)
        if gate["operational"]:
            allowed.append(program)
        else:
            parked.append({"offer_id": program.get("offer_id"), "name": program.get("name"), "gate": gate})
    return {"schema": "affiliate_sheet_batch_plan_v1", "programs": allowed,
            "parked_programs": parked, "no_operational_blank_application_url": True}


def wire_in_plan_rows(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate a wire-in plan and return its row entries.

    Refuses anything the mode is not allowed to do: a foreign schema, a missing
    authority binding, an unknown column, a status outside the terminal wire-in
    set, a clicked tracking link, or a duplicate/absent row number.
    """
    if plan.get("schema") != WIRE_IN_PLAN_SCHEMA:
        raise RuntimeError(f"wire_in_plan_schema_invalid:{plan.get('schema')}")
    if not re.fullmatch(r"[a-f0-9]{64}", str(plan.get("authority_sha256") or "")):
        raise RuntimeError("wire_in_plan_authority_sha256_invalid")
    rows = plan.get("rows")
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("wire_in_plan_rows_empty")
    # A wire-in declares ZERO tracking clicks unless every click it declares is
    # backed by a receipted link-click QA on one of its own rows. The count must
    # match exactly: a plan may not declare one verified click and slip in two.
    declared_clicks = int(plan.get("tracking_clicks", -1))
    receipted_qa_rows = sum(
        1 for entry in rows
        if isinstance(entry, dict) and isinstance(entry.get("link_click_qa"), dict))
    if declared_clicks != 0 and declared_clicks != receipted_qa_rows:
        raise RuntimeError(
            f"wire_in_plan_tracking_clicks_must_be_zero:{declared_clicks}:{receipted_qa_rows}")
    seen: set[int] = set()
    for entry in rows:
        row_num = entry.get("row")
        if not isinstance(row_num, int) or row_num < 2:
            raise RuntimeError(f"wire_in_plan_row_number_invalid:{row_num}")
        if row_num in seen:
            raise RuntimeError(f"wire_in_plan_duplicate_row:{row_num}")
        seen.add(row_num)
        cells = entry.get("cells")
        if not isinstance(cells, dict) or not cells:
            raise RuntimeError(f"wire_in_plan_row_has_no_cells:{row_num}")
        candidate = entry.get("candidate") or entry.get("program")
        if isinstance(candidate, dict):
            gate = gate_batch_candidate(candidate, "affiliate_sheet_batch_writer.wire_in_plan", persist=True)
            if not gate["operational"]:
                raise RuntimeError(f"wire_in_plan_candidate_parked:{row_num}:{gate['decision']}")
        for column in cells:
            if int(column) not in WIRE_IN_MUTABLE_COLUMNS:
                raise RuntimeError(f"wire_in_plan_column_escape:{row_num}:{column}")
        if "10" in cells or 10 in cells:
            status = str(cells.get("10", cells.get(10)))
            if status not in WIRE_IN_ALLOWED_STATUS:
                raise RuntimeError(f"wire_in_plan_status_not_allowed:{row_num}:{status}")
        verified_qa = wire_in_link_click_qa(entry, row_num)
        if "21" in cells or 21 in cells:
            qa = str(cells.get("21", cells.get(21)))
            if qa not in WIRE_IN_ALLOWED_LINK_QA:
                raise RuntimeError(f"wire_in_plan_link_qa_not_allowed:{row_num}:{qa}")
            if qa == WIRE_IN_QA_VERIFIED_LINK_QA and verified_qa is None:
                raise RuntimeError(f"wire_in_plan_link_qa_verified_without_receipt:{row_num}")
            # The inverse matters just as much: a row that DID click may not tell
            # the Sheet the link was left unclicked to preserve attribution.
            if qa != WIRE_IN_QA_VERIFIED_LINK_QA and verified_qa is not None:
                raise RuntimeError(f"wire_in_plan_link_qa_contradicts_receipt:{row_num}:{qa}")
        for checkbox in WIRE_IN_CHECKBOX_COLUMNS:
            if str(checkbox) in cells or checkbox in cells:
                flag = cells.get(str(checkbox), cells.get(checkbox))
                if str(flag).upper() not in {"TRUE", "FALSE"}:
                    raise RuntimeError(
                        f"wire_in_plan_checkbox_not_boolean:{row_num}:{checkbox}:{flag}")
    return rows


def wire_in_link_click_qa(entry: dict[str, Any], row_num: int) -> dict[str, Any] | None:
    """Validate a row's link-click QA receipt, or refuse.

    Returns the parsed receipt when the row legitimately carries one, None when
    the row does not claim one at all. Anything in between - a claim without a
    receipt, a receipt that does not exist, a receipt whose bytes do not match
    the declared hash, a QA that did not actually happen, one that never reached
    the destination, or one that reached it without an attribution stamp - is a
    refusal. A click that resolves but does not attribute is a click that never
    pays, and recording it as verified would be a false fact.
    """
    qa = entry.get("link_click_qa")
    if qa is None:
        return None
    if not isinstance(qa, dict):
        raise RuntimeError(f"wire_in_plan_link_click_qa_invalid:{row_num}")
    receipt_path = str(qa.get("receipt") or "")
    declared_sha = str(qa.get("receipt_sha256") or "")
    if not receipt_path or not re.fullmatch(r"[a-f0-9]{64}", declared_sha):
        raise RuntimeError(f"wire_in_plan_link_click_qa_binding_invalid:{row_num}")
    path = Path(receipt_path)
    if not path.is_absolute():
        path = Path("/home/ubuntu/business") / receipt_path
    if not path.is_file():
        raise RuntimeError(f"wire_in_plan_link_click_qa_receipt_missing:{row_num}:{receipt_path}")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != declared_sha:
        raise RuntimeError(f"wire_in_plan_link_click_qa_receipt_sha_mismatch:{row_num}")
    receipt = json.loads(raw.decode("utf-8"))
    if receipt.get("schema") != WIRE_IN_LINK_CLICK_QA_SCHEMA:
        raise RuntimeError(f"wire_in_plan_link_click_qa_schema_invalid:{row_num}:{receipt.get('schema')}")
    if receipt.get("link_click_qa_performed") is not True:
        raise RuntimeError(f"wire_in_plan_link_click_qa_not_performed:{row_num}")
    if receipt.get("observed_final_status") != 200:
        raise RuntimeError(f"wire_in_plan_link_click_qa_status_not_200:{row_num}")
    attribution = receipt.get("attribution") or {}
    stamp = str(attribution.get("cjevent") or attribution.get("stamp") or "")
    final_url = str(receipt.get("observed_final_url") or "")
    if not stamp or stamp not in final_url:
        raise RuntimeError(f"wire_in_plan_link_click_qa_attribution_missing:{row_num}")
    if not str(receipt.get("performed_at_utc") or "").startswith("20"):
        raise RuntimeError(f"wire_in_plan_link_click_qa_performed_at_missing:{row_num}")
    # The receipt must be about the link this plan is actually writing.
    cells = entry.get("cells") or {}
    planned_link = cells.get("14", cells.get(14))
    if planned_link is not None and str(planned_link) != str(receipt.get("tracking_url") or ""):
        raise RuntimeError(f"wire_in_plan_link_click_qa_link_mismatch:{row_num}")
    return receipt


def approved_unlinked_disposition_rows(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate a narrow, evidence-bound terminal disposition plan."""
    if plan.get("schema") != UNLINKED_DISPOSITION_PLAN_SCHEMA:
        raise RuntimeError(f"unlinked_disposition_schema_invalid:{plan.get('schema')}")
    if not re.fullmatch(r"[a-f0-9]{64}", str(plan.get("authority_sha256") or "")):
        raise RuntimeError("unlinked_disposition_authority_sha256_invalid")
    if int(plan.get("tracking_clicks", -1)) != 0:
        raise RuntimeError("unlinked_disposition_tracking_clicks_must_be_zero")
    if int(plan.get("provider_writes", -1)) != 0:
        raise RuntimeError("unlinked_disposition_provider_writes_must_be_zero")
    rows = plan.get("rows")
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("unlinked_disposition_rows_empty")
    seen: set[int] = set()
    required_columns = {str(value) for value in UNLINKED_DISPOSITION_MUTABLE_COLUMNS}
    for entry in rows:
        row_num = entry.get("row")
        if not isinstance(row_num, int) or row_num < 2:
            raise RuntimeError(f"unlinked_disposition_row_number_invalid:{row_num}")
        if row_num in seen:
            raise RuntimeError(f"unlinked_disposition_duplicate_row:{row_num}")
        seen.add(row_num)
        if not str(entry.get("program") or "").strip():
            raise RuntimeError(f"unlinked_disposition_program_missing:{row_num}")
        cells = entry.get("cells")
        if not isinstance(cells, dict) or {str(key) for key in cells} != required_columns:
            raise RuntimeError(f"unlinked_disposition_columns_invalid:{row_num}")
        status = str(cells.get("10", cells.get(10)) or "")
        if status not in UNLINKED_DISPOSITION_ALLOWED_STATUS:
            raise RuntimeError(f"unlinked_disposition_status_not_allowed:{row_num}:{status}")
        ready = cells.get("19", cells.get(19))
        if str(ready).strip().upper() != "FALSE":
            raise RuntimeError(f"unlinked_disposition_must_remain_not_ready:{row_num}")
        note = str(cells.get("23", cells.get(23)) or "").strip()
        if len(note) < 20:
            raise RuntimeError(f"unlinked_disposition_blocker_too_short:{row_num}")
        checked = str(cells.get("24", cells.get(24)) or "").strip()
        if not checked or "T" not in checked:
            raise RuntimeError(f"unlinked_disposition_last_checked_invalid:{row_num}")
    return rows


def require_unlinked_disposition_preconditions(
    values: list[list[Any]], rows: list[dict[str, Any]],
) -> None:
    """Prove every target is the exact link-dark GLP row named by the plan."""
    for entry in rows:
        row_num = int(entry["row"])
        if len(values) < row_num or not row_has_business_value(values[row_num - 1]):
            raise RuntimeError(f"unlinked_disposition_target_row_blank:{row_num}")
        actual = pad(values[row_num - 1])
        if str(actual[2]).strip() != str(entry["program"]).strip():
            raise RuntimeError(f"unlinked_disposition_program_mismatch:{row_num}")
        target_status = str(entry["cells"].get("10", entry["cells"].get(10)))
        current_status = str(actual[10] or "")
        if not (current_status.startswith("approved") or current_status == target_status):
            raise RuntimeError(
                f"unlinked_disposition_current_status_not_approved:{row_num}:{current_status}")
        if str(actual[11]).strip().upper() != "TRUE":
            raise RuntimeError(f"unlinked_disposition_signup_not_complete:{row_num}")
        if "glp1" not in str(actual[25] or "").lower():
            raise RuntimeError(f"unlinked_disposition_vertical_not_glp1:{row_num}")
        if str(actual[14] or "").strip():
            raise RuntimeError(f"unlinked_disposition_tracking_link_present:{row_num}")
        if actual[20] is True or str(actual[20]).strip().upper() == "TRUE":
            raise RuntimeError(f"unlinked_disposition_row_already_live:{row_num}")


def wire_in_write_value(column: int, value: Any) -> Any:
    """Coerce a planned cell value to the type the column actually stores.

    Checkbox columns must receive a real boolean. Writing the plan's "TRUE"
    string through valueInputOption RAW puts a stringValue into a checkbox cell,
    which the sheet schema guard reports as checkbox_value_not_boolean.
    """
    if column in WIRE_IN_CHECKBOX_COLUMNS:
        return str(value).strip().upper() == "TRUE"
    return value


def wire_in_cell_equal(column: int, actual: Any, expected: Any) -> bool:
    """Compare one gate cell.

    Column 20 (Wired Live?) is a checkbox: Sheets returns a real bool for it
    under UNFORMATTED_VALUE while a plan carries the string "TRUE"/"FALSE".
    The readback side must therefore be a genuine bool - comparing the string
    forms alone would treat a stringValue regression in a checkbox cell as a
    match and let the writer certify a sheet it had just corrupted. An empty
    cell is accepted as an unchecked box, which is how Sheets renders one.
    """
    if column in WIRE_IN_CHECKBOX_COLUMNS:
        if isinstance(actual, bool):
            return actual is wire_in_write_value(column, expected)
        if actual == "":
            return wire_in_write_value(column, expected) is False
        return False
    return cell_equal(actual, expected)


def wire_in_changed_cells(values: list[list[Any]], rows: list[dict[str, Any]]) -> list[tuple[int, int, Any]]:
    """Diff the plan against the live sheet, returning only cells that differ."""
    changes: list[tuple[int, int, Any]] = []
    for entry in rows:
        row_num = int(entry["row"])
        if len(values) < row_num or not row_has_business_value(values[row_num - 1]):
            raise RuntimeError(f"wire_in_plan_target_row_blank:{row_num}")
        actual = pad(values[row_num - 1])
        for column, expected in entry["cells"].items():
            column = int(column)
            if not wire_in_cell_equal(column, actual[column], expected):
                changes.append((row_num, column, expected))
    return changes


def creds() -> Credentials:
    c = Credentials.from_authorized_user_info(json.loads(TOKEN.read_text()))
    if c.expired and c.refresh_token:
        c.refresh(Request())
    return c


def norm(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


#: Corporate suffixes are spelling noise, not programme identity. This is the
#: one shared definition for Sheet-name fallback matching; consumers must import
#: it rather than carrying a private suffix list. Decorations such as "AWIN" and
#: geographic qualifiers such as "US" are deliberately absent: an exact
#: provider programme id, not a looser name rule, binds those variants.
CORPORATE_PROGRAM_NAME_SUFFIXES: frozenset[str] = frozenset({
    "ag", "aps", "as", "bv", "co", "company", "corp", "corporation", "gmbh",
    "inc", "incorporated", "kk", "l", "limited", "llc", "llp", "ltd", "nv",
    "oy", "plc", "pte", "pty", "sa", "sas", "srl",
})


def canonical_program_name(value: object) -> str:
    """A programme name with trailing corporate suffixes removed.

    Only trailing suffixes are removed. A token in the middle of a brand remains
    identity-bearing, and non-corporate marketplace decorations remain distinct.
    """
    tokens = re.findall(r"[a-z0-9]+", str(value or "").lower())
    while tokens and tokens[-1] in CORPORATE_PROGRAM_NAME_SUFFIXES:
        tokens.pop()
    return "".join(tokens)


def equivalent_program_name(left: object, right: object) -> bool:
    """Whether two programme labels are the same conservative name fallback."""
    left_key = canonical_program_name(left)
    right_key = canonical_program_name(right)
    return bool(left_key and left_key == right_key)


# Provider catalogues add broader display-only words (for example "Affiliate
# Program" and a country suffix). This helper is for matching an authenticated
# provider catalogue entry to an email label, not for Sheet row identity. It
# also lives here so the reconciler has no second private NAME_NOISE definition.
ADVERTISER_DISPLAY_NAME_NOISE: frozenset[str] = frozenset({
    "a", "an", "and", "the", "affiliate", "program", "programme", "awin",
    "early", "gender", "dna", "test", "tests", "us", "usa", "united", "states",
    *CORPORATE_PROGRAM_NAME_SUFFIXES,
})


def advertiser_name_tokens(value: object) -> frozenset[str]:
    return frozenset(
        token for token in re.findall(r"[a-z0-9]+", str(value or "").lower())
        if token not in ADVERTISER_DISPLAY_NAME_NOISE
    )


def equivalent_advertiser_name(left: object, right: object) -> bool:
    """Authenticated provider-display equivalence; never a Sheet identity axis."""
    left_tokens = advertiser_name_tokens(left)
    right_tokens = advertiser_name_tokens(right)
    return bool(left_tokens and left_tokens == right_tokens)


#: One rail, many spellings. The Sheet's Network / Route cell is written by
#: humans and by several materializers, so the same rail arrives as "Impact.com",
#: "impact", and "Impact Radius". `norm` alone folds none of those together:
#: norm("Impact.com") is "impactcom", which does not equal norm("impact").
#:
#: Deliberately NOT a fuzzy matcher: two genuinely different networks carrying
#: the same brand must keep two rows, which is why HealthRX is on the Sheet
#: twice (Katalys row 87, Awin row 489) and must stay that way. Only exact
#: spelling variants of one rail belong here.
#:
#: This lived in glp_sheet_sync_drain.py until 2026-08-25, where it correctly
#: powered the create-supersession check while `sheet_sync_existing_matches`
#: below still compared raw `norm`. The split meant the drain could see that a
#: programme was already on the Sheet, yet `resolve_sheet_row` could not find
#: the row it was sitting on. Nootropics Depot (row 290, "Impact.com") refused
#: with `no_sheet_row_matches_program_identity` for exactly that reason.
NETWORK_LABEL_ALIASES: dict[str, str] = {
    "affiliati": "affiliati",
    "affiliatinetwork": "affiliati",
    "awin": "awin",
    "cj": "cj",
    "cjaffiliate": "cj",
    "commissionjunction": "cj",
    "everflow": "everflow",
    "everflowwhitelabel": "everflow",
    "impact": "impact",
    "impactcom": "impact",
    "impactradius": "impact",
    "partnerize": "partnerize",
    "katalys": "katalys",
    "refersion": "refersion",
    "partnerstack": "partnerstack",
    "shareasale": "shareasale",
    "rakuten": "rakuten",
    "rakutenadvertising": "rakuten",
    # The restored canonical parent row spells the route "MaxBounty Network";
    # provider events and researched queue rows use the network's display name.
    # They are one account-level relationship, not two rails.
    "maxbounty": "maxbounty",
    "maxbountynetwork": "maxbounty",
}


def canonical_network(value: object) -> str:
    """The rail a network label names, with pure spelling differences collapsed.

    Returns the normalised label unchanged when it is not a known alias, so an
    unknown network can never be folded into a known one by accident.
    """
    key = norm(value)
    return NETWORK_LABEL_ALIASES.get(key, key)


def offer_from_url(value: object) -> str:
    match = re.search(r"/(?:explore|programs)/(\d+)(?:$|[/?#])", str(value or ""))
    return match.group(1) if match else ""


def legacy_identity_collisions(programs: list[dict[str, Any]], by_offer: dict[str, int], name_rows: dict[str, list[int]]) -> list[dict[str, Any]]:
    collisions = []
    for program in programs:
        offer = str(program["offer_id"])
        matching_names = name_rows.get(norm(program.get("name")), [])
        if offer not in by_offer and matching_names:
            collisions.append({"offer_id": offer, "name": program.get("name"), "rows": matching_names})
    return collisions


def pad(row: list[Any]) -> list[Any]:
    return (row + [""] * len(HEADERS))[:len(HEADERS)]


def row_has_business_value(row: list[Any]) -> bool:
    values = pad(row)
    return any(
        bool(value) if column in (1, 11, 19, 20) else bool(str(value).strip())
        for column, value in enumerate(values)
    )


def cell_equal(actual: Any, expected: Any) -> bool:
    return str(actual).upper() == str(expected).upper() if isinstance(expected, bool) else str(actual) == str(expected)


def changed_cells(values: list[list[Any]], assignments: list[tuple[int, dict[str, Any], list[Any]]], created_rows: list[int] | None = None) -> list[tuple[int, dict[str, Any], int, Any]]:
    created = set(created_rows or [])
    changes = []
    for row_num, program, expected in assignments:
        actual = pad(values[row_num - 1] if len(values) >= row_num else [])
        columns = range(len(HEADERS)) if row_num in created else MUTABLE_COLUMNS
        for column in columns:
            if not cell_equal(actual[column], expected[column]):
                changes.append((row_num, program, column, expected[column]))
    return changes


def apply_value_assignments(svc: Any, cells: list[tuple[int, dict[str, Any], int, Any]]) -> bool:
    if not cells:
        return False
    require_marketplace_parent_rows_not_mutated(
        (row_num, column) for row_num, _, column, _ in cells
    )
    data = [
        {"range": f"'{TAB}'!{column_letter(column)}{row_num}", "values": [[value]]}
        for row_num, _, column, value in cells
    ]
    svc.spreadsheets().values().batchUpdate(
        spreadsheetId=SHEET_ID,
        body={"valueInputOption": "RAW", "data": data},
    ).execute()
    return True


def zero_sheet_write(changes: list[Any], created_rows: list[Any], rows_added: int, schema_write_requests: int) -> bool:
    return not changes and not created_rows and int(rows_added) == 0 and int(schema_write_requests) == 0


def schema_materialization_is_safe(values_before: list[list[Any]], values_after: list[list[Any]], created_rows: list[int]) -> bool:
    """Allow Sheets to materialize FALSE only in checkbox cells of newly prepared rows."""
    created = set(created_rows)
    row_count = max(len(values_before), len(values_after))
    for row_num in range(1, row_count + 1):
        before = pad(values_before[row_num - 1] if row_num <= len(values_before) else [])
        after = pad(values_after[row_num - 1] if row_num <= len(values_after) else [])
        if row_num not in created:
            if before != after:
                return False
            continue
        for column, (old, new) in enumerate(zip(before, after)):
            if column in (1, 11, 19, 20) and old in ("", False) and new in ("", False):
                continue
            if old != new:
                return False
    return True


def write_verification_pass(
    all_cells: bool,
    all_validations: bool,
    schema_status: str,
    duplicates: dict[str, list[int]],
    full_sheet_post_write_match: bool,
    idempotent_rerun: bool,
    row_count_matches: bool,
    column_count_matches: bool,
) -> bool:
    return all((
        all_cells,
        all_validations,
        schema_status == "PASS",
        not duplicates,
        full_sheet_post_write_match,
        idempotent_rerun,
        row_count_matches,
        column_count_matches,
    ))


def values_sha(values: list[list[Any]]) -> str:
    payload = json.dumps(values, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(payload).hexdigest()


#: Rows appended per growth event. Large enough that growth is rare, small
#: enough that a runaway is visible before it is enormous.
GRID_GROWTH_BATCH = 250
#: A hard stop. Needing more than this many rows is a bug upstream (a queue
#: looping, a matcher failing to dedupe), not genuine capacity demand, and
#: growing forever would hide it.
GRID_ROW_CEILING = 20000


def grow_grid_for_creates(svc, sheet_id_num: int, current_row_count: int,
                          needed: int, mode_key: str) -> list[int]:
    """Append blank rows to the tab and return the new row numbers, schema applied.

    2026-08-26: the Sheet filled up. rowCount was 778 with 777 rows carrying
    business value and **zero** blank rows, so every `create` refused with
    `..._no_blank_grid_row_available`: 34 refusals in a single drain, and every
    future new provider would have refused too. The writer allocated blank rows
    only from within the existing grid and had no way to grow, so the affiliate
    pipeline had a hard ceiling nobody had hit yet.

    Appending is purely additive: it adds empty rows below the data and touches
    no existing cell. But appended rows arrive WITHOUT the tab's checkbox, date
    and Vertical validation, and `require_checkbox_schema_pass` would then fail
    the very next run -- so the immutable schema is applied to the new rows in
    the same batch. Growing without that is trading one blocker for another.
    """
    if needed <= 0:
        return []
    grow_by = max(int(needed), GRID_GROWTH_BATCH)
    target = current_row_count + grow_by
    if target > GRID_ROW_CEILING:
        raise RuntimeError(
            f"{mode_key}_grid_growth_refused_above_ceiling:"
            f"want={target}:ceiling={GRID_ROW_CEILING}:needed={needed}. "
            "Needing this many rows means an upstream queue or matcher is "
            "producing duplicates; grow the ceiling only after checking that.")

    svc.spreadsheets().batchUpdate(
        spreadsheetId=SHEET_ID,
        body={"requests": [{"appendDimension": {
            "sheetId": sheet_id_num, "dimension": "ROWS", "length": grow_by}}]},
    ).execute()

    new_rows = list(range(current_row_count + 1, target + 1))
    requests = schema_requests(sheet_id_num, new_rows)
    if requests:
        svc.spreadsheets().batchUpdate(
            spreadsheetId=SHEET_ID, body={"requests": requests}).execute()
    return new_rows


def expected_full_sheet_values(values: list[list[Any]], assignments: list[tuple[int, dict[str, Any], list[Any]]], observed_rows: int) -> list[list[Any]]:
    total_rows = max(len(values), observed_rows, max((row for row, _, _ in assignments), default=0))
    expected = [pad(values[index]) if index < len(values) else [""] * len(HEADERS) for index in range(total_rows)]
    for row_num, _, row in assignments:
        expected[row_num - 1] = pad(row)
    return expected


def normalized_full_sheet_values(values: list[list[Any]], row_count: int) -> list[list[Any]]:
    return [pad(values[index]) if index < len(values) else [""] * len(HEADERS) for index in range(row_count)]


def date_serial(value: Any) -> int | float | str:
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)):
        return value
    parsed = value.date() if isinstance(value, datetime) else value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
    return (parsed - date(1899, 12, 30)).days


def status_from_application(program: dict[str, Any], app: dict[str, Any] | None, relationship: dict[str, Any] | None) -> tuple[str, bool]:
    if program["decision"] == "DISTINCT_ROUTE_KEEP_ACTIVE":
        return "applied_pending_brand_approval", False
    if program["decision"] == "DUPLICATE_DO_NOT_USE":
        return "duplicate_do_not_use", False
    if program["decision"] == "DUPLICATE_ROUTE_REVIEW":
        return "operator_review_duplicate_route_pending", False
    if program["decision"] == "ALREADY_ACTIVE":
        return "approved_active_not_wired", True
    relationship_state = str((relationship or {}).get("relationship") or "")
    if relationship_state == "Approved":
        return "applied_approved_not_wired", True
    if relationship_state == "Pending":
        return "applied_pending_brand_approval", False
    if not app:
        return "application_receipt_missing", False
    text = str(app.get("after_excerpt") or app.get("before_excerpt") or "")
    if "Approved" in text or program.get("status_before") == "preapproved_join_available":
        return "applied_approved_not_wired", True
    if "Pending" in text:
        return "applied_pending_brand_approval", False
    if app.get("status") in {"SUBMITTED_VERIFIED", "SUBMITTED_ACTION_REMOVED", "ALREADY_RELATIONSHIP"}:
        return "applied_relationship_present_status_unresolved", False
    return "application_failed_or_unverified", False


def make_row(program: dict[str, Any], app: dict[str, Any] | None, relationship: dict[str, Any] | None, existing: list[Any], tracking: dict[str, Any] | None) -> list[Any]:
    existing = pad(existing)
    status, approved = status_from_application(program, app, relationship)
    tracking_link = str((tracking or {}).get("tracking_link") or existing[14] or "")
    approval_date = date_serial(existing[18] or TODAY) if approved else ""
    allowed = ", ".join(program.get("allowed_channels") or [])
    app_status = app.get("status") if app else "existing_active"
    evidence_path = program.get("evidence_path") or "reports/affiliate-marketplace-expansion-2026-07-17"
    notes = (
        f"Katalys offer {program['offer_id']}; application receipt={app_status}; "
        f"independent relationship={str((relationship or {}).get('relationship') or 'not available')}; "
        f"pre-application state={program.get('status_before')}; allowed channels={allowed}. "
        f"Verified provider terms: {program.get('terms_summary') or 'see captured Katalys provider record before public use'}. "
        f"Duplicate-route review: {program.get('duplicate_reason') or 'not applicable'}. "
        f"Final route determination: {program.get('route_determination') or 'not applicable'}. "
        f"First-party and Katalys evidence saved at {evidence_path}."
    )
    checked = (app or {}).get("verified_at_utc") or (app or {}).get("completed_at_utc") or existing[24] or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    if row_has_business_value(existing):
        row = list(existing)
        row[10] = status
        row[11] = True
        row[18] = approval_date
        row[23] = notes[:5000]
        row[24] = checked
        if "api_capabilities" in program:
            row[26] = require_api_capabilities_value(program.get("api_capabilities"))
        if program["decision"] in {"DUPLICATE_ROUTE_REVIEW", "DUPLICATE_DO_NOT_USE", "DISTINCT_ROUTE_KEEP_ACTIVE"}:
            if str(row[14] or "").strip() or str(row[19]).upper() == "TRUE" or str(row[20]).upper() == "TRUE":
                raise RuntimeError(f"route_determination_requires_link_dark_existing_row:{program['offer_id']}")
            row[18] = ""
        return row
    wire_ready = bool(approved and tracking_link)
    return [
        program.get("priority") or "B",
        True,
        program.get("name") or f"Katalys offer {program['offer_id']}",
        program.get("category") or "Prescription telehealth / GLP-1 weight loss",
        "Katalys",
        program.get("apply_url") or f"https://app.katalys.com/pub/{ORG}/explore/{program['offer_id']}",
        program.get("official_url") or "",
        program.get("fit_reason") or "Relevant to GLP Clinic Scout provider comparison and education content.",
        program.get("suggested_asset") or "GLP Clinic Scout comparison/editorial provider review",
        program.get("seo_slug") or "",
        status,
        True,
        "https://auth.katalys.com/login",
        PUBLISHER_ID,
        tracking_link,
        "",
        program.get("commission_terms") or "",
        program.get("cookie_window") or "Not stated in captured Katalys terms",
        approval_date,
        wire_ready,
        False,
        "not_tested_no_link" if not tracking_link else "not_clicked_preserve_attribution",
        program.get("compliance_notes") or "FTC affiliate disclosure; no unsupported medical claims; follow Katalys/brand terms.",
        notes[:5000],
        checked,
        require_vertical_value(program.get("vertical")),
        require_api_capabilities_value(program.get("api_capabilities")),
    ]


def load_baseline_rows(path: str | None) -> dict[int, list[Any]]:
    if not path:
        return {}
    payload = json.loads(Path(path).read_text())
    rows: dict[int, list[Any]] = {}
    for item in payload.get("all_rows", []):
        if not isinstance(item, dict) or item.get("row") is None:
            continue
        values = [item.get(header, "") for header in HEADERS]
        for column in (1, 11, 19, 20):
            if str(values[column]).upper() in {"TRUE", "FALSE"}:
                values[column] = str(values[column]).upper() == "TRUE"
        rows[int(item["row"])] = values
    return rows


def checkbox_value_repairs(values: list[list[Any]], issues: list[dict[str, Any]]) -> list[tuple[int, int, bool]]:
    """Plan boolean rewrites for checkbox cells that hold a string.

    The generic schema-repair path deliberately refuses this kind because
    fixing it needs a value write. Scope is deliberately tiny: only cells the
    audit flagged as checkbox_value_not_boolean, only in the known checkbox
    columns, and only where the existing text is an unambiguous TRUE/FALSE so
    the repair preserves what the cell already meant.
    """
    repairs: list[tuple[int, int, bool]] = []
    for issue in issues:
        if issue.get("kind") != "checkbox_value_not_boolean":
            continue
        row_num, column = int(issue["row"]), int(issue["column_index"])
        if column not in WIRE_IN_CHECKBOX_COLUMNS:
            raise RuntimeError(f"checkbox_repair_column_not_recognised:{row_num}:{column}")
        if len(values) < row_num:
            raise RuntimeError(f"checkbox_repair_row_missing:{row_num}")
        actual = pad(values[row_num - 1])[column]
        if isinstance(actual, bool):
            continue
        text = str(actual).strip().upper()
        if text not in {"TRUE", "FALSE"}:
            raise RuntimeError(f"checkbox_repair_value_ambiguous:{row_num}:{column}:{actual!r}")
        repairs.append((row_num, column, text == "TRUE"))
    return repairs


#: Category / route text that marks a row as a MARKETPLACE RAIL - an account we
#: hold with a network, not a merchant program. Rails carry no vertical of their
#: own; every rail row the machine created writes "general-wellness".
MARKETPLACE_RAIL_MARKERS = ("marketplace", "network rail", "network signup")
MARKETPLACE_RAIL_VERTICAL = "general-wellness"


def blank_schema_cell_repairs(
    values: list[list[Any]], issues: list[dict[str, Any]],
) -> list[tuple[int, int, Any]]:
    """Plan writes for typed cells a hand edit left EMPTY.

    A hand-added row (Sheet UI, no writer) lands with blank checkbox and blank
    Vertical cells, which the schema guard reports as a regression - and
    require_checkbox_schema_pass then fails EVERY governed writer mode closed,
    including the pause disposition. Refusing forever is not a fix, and neither
    is guessing, so this repairs exactly the two blanks whose honest value is
    already determined by what the sheet renders:

      * a blank checkbox renders as UNCHECKED, so FALSE preserves its meaning;
      * a blank Vertical on a MARKETPLACE RAIL row is the rail convention
        ("general-wellness"), and a rail is identified positively - no tracking
        link, no program status, and a marketplace/network category or route.

    Any other blank, any non-blank value, and any other issue kind is refused:
    a merchant program row's vertical is a claim about the program and is not
    this function's to invent.
    """
    repairs: list[tuple[int, int, Any]] = []
    for issue in issues:
        kind = issue.get("kind")
        row_num = int(issue["row"])
        if len(values) < row_num:
            raise RuntimeError(f"blank_schema_repair_row_missing:{row_num}")
        row = pad(values[row_num - 1])
        if kind == "checkbox_value_not_boolean":
            column = int(issue["column_index"])
            if column not in WIRE_IN_CHECKBOX_COLUMNS and column not in (1, 11):
                raise RuntimeError(f"blank_schema_repair_column_not_recognised:{row_num}:{column}")
            if issue.get("observed_type") != "BLANK" or str(row[column]).strip() != "":
                raise RuntimeError(
                    f"blank_schema_repair_checkbox_not_blank:{row_num}:{column}:{row[column]!r}")
            repairs.append((row_num, column, False))
            continue
        if kind == "vertical_value_not_allowed":
            if issue.get("observed") != "BLANK" or str(row[25]).strip() != "":
                raise RuntimeError(
                    f"blank_schema_repair_vertical_not_blank:{row_num}:{row[25]!r}")
            haystack = f"{row[3]} {row[4]}".lower()
            is_rail = any(marker in haystack for marker in MARKETPLACE_RAIL_MARKERS)
            if not is_rail or str(row[14]).strip() or str(row[10]).strip():
                raise RuntimeError(f"blank_schema_repair_vertical_not_a_marketplace_rail:{row_num}")
            repairs.append((row_num, 25, MARKETPLACE_RAIL_VERTICAL))
            continue
        raise RuntimeError(f"blank_schema_repair_issue_kind_not_repairable:{row_num}:{kind}")
    return repairs


def run_blank_schema_repair(out: Path, apply: bool) -> int:
    """Fill typed cells a hand edit left empty, then prove the audit passes."""
    svc = build("sheets", "v4", credentials=creds(), cache_discovery=False)
    values = svc.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=f"'{TAB}'!{EXPECTED_RANGE}", valueRenderOption="UNFORMATTED_VALUE"
    ).execute().get("values", [])
    if not values or pad(values[0]) != HEADERS:
        raise RuntimeError("canonical header mismatch; refusing mutation")
    before = audit_checkbox_schema(svc, SHEET_ID, TAB)
    repairs = blank_schema_cell_repairs(values, before["issues"])
    baseline_sha = values_sha(values)
    receipt: dict[str, Any] = {
        "status": "PASS",
        "schema": "affiliate-blank-schema-cell-repair-v1",
        "mode": "blank_schema_cell_repair",
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "sheet_id": SHEET_ID,
        "tab": TAB,
        "cells": [{"row": r, "column": c, "value": v} for r, c, v in repairs],
        "cell_count": len(repairs),
        "issues_before": before["issues"],
        "full_sheet_values_precondition_sha256": baseline_sha,
        "tracking_clicks": 0,
        "provider_writes": 0,
    }
    if not apply:
        receipt.update({"dry_run": True, "no_sheet_write": True})
    elif repairs:
        live = svc.spreadsheets().values().get(
            spreadsheetId=SHEET_ID, range=f"'{TAB}'!{EXPECTED_RANGE}", valueRenderOption="UNFORMATTED_VALUE"
        ).execute().get("values", [])
        if values_sha(live) != baseline_sha:
            raise RuntimeError("sheet_concurrency_drift_before_blank_schema_repair")
        require_marketplace_parent_rows_not_mutated(
            (row_num, column) for row_num, column, _ in repairs
        )
        svc.spreadsheets().values().batchUpdate(
            spreadsheetId=SHEET_ID,
            body={"valueInputOption": "RAW", "data": [
                {"range": f"'{TAB}'!{column_letter(column)}{row_num}", "values": [[value]]}
                for row_num, column, value in repairs
            ]},
        ).execute()
        reread = svc.spreadsheets().values().get(
            spreadsheetId=SHEET_ID, range=f"'{TAB}'!{EXPECTED_RANGE}", valueRenderOption="UNFORMATTED_VALUE"
        ).execute().get("values", [])
        planned = {(r, c) for r, c, _ in repairs}
        collateral = [
            {"row": row_index + 1, "column": column, "before": old, "after": new}
            for row_index in range(max(len(values), len(reread)))
            for column, (old, new) in enumerate(zip(
                pad(values[row_index] if row_index < len(values) else []),
                pad(reread[row_index] if row_index < len(reread) else []),
            ))
            if old != new and (row_index + 1, column) not in planned
        ]
        mismatches = [{"row": r, "column": c, "expected": v, "actual": pad(reread[r - 1])[c]}
                      for r, c, v in repairs if pad(reread[r - 1])[c] != v]
        after = audit_checkbox_schema(svc, SHEET_ID, TAB)
        receipt.update({"dry_run": False, "collateral_changes": collateral,
                        "readback_mismatches": mismatches,
                        "post_write_values_sha256": values_sha(reread),
                        "post_audit_status": after["status"],
                        "post_audit_issues": after["issues"]})
        if collateral or mismatches or after["status"] != "PASS":
            receipt["status"] = "FAIL"
    else:
        receipt.update({"dry_run": False, "no_sheet_write": True,
                        "note": "no blank typed cells found"})
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    out.chmod(0o600)
    print(json.dumps({"mode": receipt["mode"], "status": receipt["status"],
                      "cell_count": receipt["cell_count"], "dry_run": receipt.get("dry_run"),
                      "post_audit_status": receipt.get("post_audit_status")},
                     indent=2, sort_keys=True))
    return 0 if receipt["status"] == "PASS" else 1


def run_checkbox_repair(out: Path, apply: bool) -> int:
    """Rewrite string-typed checkbox cells as real booleans, preserving meaning."""
    svc = build("sheets", "v4", credentials=creds(), cache_discovery=False)
    values = svc.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=f"'{TAB}'!{EXPECTED_RANGE}", valueRenderOption="UNFORMATTED_VALUE"
    ).execute().get("values", [])
    if not values or pad(values[0]) != HEADERS:
        raise RuntimeError("canonical header mismatch; refusing mutation")
    before = audit_checkbox_schema(svc, SHEET_ID, TAB)
    other = [i for i in before["issues"] if i.get("kind") != "checkbox_value_not_boolean"]
    if other:
        raise RuntimeError(f"checkbox_repair_unrelated_schema_issues:{json.dumps(other[:20], sort_keys=True)}")
    repairs = checkbox_value_repairs(values, before["issues"])
    baseline_sha = values_sha(values)
    receipt: dict[str, Any] = {
        "status": "PASS",
        "schema": "g6-affiliate-checkbox-value-repair-v1",
        "mode": "checkbox_value_repair",
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "sheet_id": SHEET_ID,
        "tab": TAB,
        "cells": [{"row": r, "column": c, "value": v} for r, c, v in repairs],
        "cell_count": len(repairs),
        "full_sheet_values_precondition_sha256": baseline_sha,
        "issues_before": before["issues"],
        "tracking_clicks": 0,
    }
    if not apply:
        receipt.update({"dry_run": True, "no_sheet_write": True})
    elif repairs:
        live = svc.spreadsheets().values().get(
            spreadsheetId=SHEET_ID, range=f"'{TAB}'!{EXPECTED_RANGE}", valueRenderOption="UNFORMATTED_VALUE"
        ).execute().get("values", [])
        if values_sha(live) != baseline_sha:
            raise RuntimeError("sheet_concurrency_drift_before_checkbox_repair")
        require_marketplace_parent_rows_not_mutated(
            (row_num, column) for row_num, column, _ in repairs
        )
        svc.spreadsheets().values().batchUpdate(
            spreadsheetId=SHEET_ID,
            body={"valueInputOption": "RAW", "data": [
                {"range": f"'{TAB}'!{column_letter(column)}{row_num}", "values": [[value]]}
                for row_num, column, value in repairs
            ]},
        ).execute()
        reread = svc.spreadsheets().values().get(
            spreadsheetId=SHEET_ID, range=f"'{TAB}'!{EXPECTED_RANGE}", valueRenderOption="UNFORMATTED_VALUE"
        ).execute().get("values", [])
        planned = {(r, c) for r, c, _ in repairs}
        collateral = []
        for row_index in range(max(len(values), len(reread))):
            old_row = pad(values[row_index] if row_index < len(values) else [])
            new_row = pad(reread[row_index] if row_index < len(reread) else [])
            for column, (old, new) in enumerate(zip(old_row, new_row)):
                if old != new and (row_index + 1, column) not in planned:
                    collateral.append({"row": row_index + 1, "column": column,
                                       "before": old, "after": new})
        mismatches = [{"row": r, "column": c, "expected": v, "actual": pad(reread[r - 1])[c]}
                      for r, c, v in repairs if pad(reread[r - 1])[c] is not v]
        after = audit_checkbox_schema(svc, SHEET_ID, TAB)
        receipt.update({"dry_run": False, "collateral_changes": collateral,
                        "readback_mismatches": mismatches,
                        "post_write_values_sha256": values_sha(reread),
                        "post_audit_status": after["status"],
                        "post_audit_issues": after["issues"]})
        if collateral or mismatches or after["status"] != "PASS":
            receipt["status"] = "FAIL"
    else:
        receipt.update({"dry_run": False, "no_sheet_write": True,
                        "note": "no checkbox value regressions found"})
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    out.chmod(0o600)
    print(json.dumps({"mode": receipt["mode"], "status": receipt["status"],
                      "cell_count": receipt["cell_count"], "dry_run": receipt.get("dry_run")},
                     indent=2, sort_keys=True))
    return 0 if receipt["status"] == "PASS" else 1


def run_wire_in(plan_path: Path, out: Path, apply: bool) -> int:
    """Plan-driven wire-in gate write: narrow columns, explicit rows, CAS + readback."""
    plan = json.loads(plan_path.read_text())
    rows = wire_in_plan_rows(plan)
    policy_snapshot = load_snapshot()
    require_operator_ruling_clear(rows, policy_snapshot)
    svc = build("sheets", "v4", credentials=creds(), cache_discovery=False)
    values = svc.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=f"'{TAB}'!{EXPECTED_RANGE}", valueRenderOption="UNFORMATTED_VALUE"
    ).execute().get("values", [])
    if not values or pad(values[0]) != HEADERS:
        raise RuntimeError("canonical header mismatch; refusing mutation")
    schema_preflight = require_checkbox_schema_pass(svc, SHEET_ID, TAB)
    baseline_sha = values_sha(values)
    changes = wire_in_changed_cells(values, rows)
    receipt: dict[str, Any] = {
        "status": "PASS",
        "schema": "g6-affiliate-wire-in-sheet-write-v1",
        "mode": "wire_in_gate",
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "sheet_id": SHEET_ID,
        "tab": TAB,
        "plan_path": str(plan_path),
        "plan_sha256": hashlib.sha256(plan_path.read_bytes()).hexdigest(),
        "authority_sha256": plan["authority_sha256"],
        "rows": sorted(int(entry["row"]) for entry in rows),
        "mutable_columns": list(WIRE_IN_MUTABLE_COLUMNS),
        "operator_rulings": policy_receipt(policy_snapshot),
        "value_cells_planned": len(changes),
        "full_sheet_values_precondition_sha256": baseline_sha,
        "schema_preflight_status": schema_preflight["status"],
        "tracking_clicks": 0,
    }
    if not apply:
        receipt.update({"dry_run": True, "no_value_write": True, "no_sheet_write": True})
    else:
        live = svc.spreadsheets().values().get(
            spreadsheetId=SHEET_ID, range=f"'{TAB}'!{EXPECTED_RANGE}", valueRenderOption="UNFORMATTED_VALUE"
        ).execute().get("values", [])
        if values_sha(live) != baseline_sha:
            raise RuntimeError("sheet_concurrency_drift_before_value_write")
        if changes:
            require_marketplace_parent_rows_not_mutated(
                (row_num, column) for row_num, column, _ in changes
            )
            svc.spreadsheets().values().batchUpdate(
                spreadsheetId=SHEET_ID,
                body={"valueInputOption": "RAW", "data": [
                    {"range": f"'{TAB}'!{column_letter(column)}{row_num}",
                     "values": [[wire_in_write_value(column, value)]]}
                    for row_num, column, value in changes
                ]},
            ).execute()
        reread = svc.spreadsheets().values().get(
            spreadsheetId=SHEET_ID, range=f"'{TAB}'!{EXPECTED_RANGE}", valueRenderOption="UNFORMATTED_VALUE"
        ).execute().get("values", [])
        mismatches = []
        for entry in rows:
            row_num = int(entry["row"])
            actual = pad(reread[row_num - 1] if len(reread) >= row_num else [])
            for column, expected in entry["cells"].items():
                if not wire_in_cell_equal(int(column), actual[int(column)], expected):
                    mismatches.append({"row": row_num, "column": int(column),
                                       "expected": expected, "actual": actual[int(column)]})
        # Nothing outside the planned cells may have moved.
        planned = {(int(entry["row"]), int(column)) for entry in rows for column in entry["cells"]}
        collateral = []
        for row_index in range(max(len(values), len(reread))):
            before = pad(values[row_index] if row_index < len(values) else [])
            after = pad(reread[row_index] if row_index < len(reread) else [])
            for column, (old, new) in enumerate(zip(before, after)):
                if old != new and (row_index + 1, column) not in planned:
                    collateral.append({"row": row_index + 1, "column": column, "before": old, "after": new})
        idempotent = not wire_in_changed_cells(reread, rows)
        receipt.update({
            "dry_run": False,
            "value_cells_written": len(changes),
            "readback_mismatches": mismatches,
            "collateral_changes": collateral,
            "idempotent_rerun": idempotent,
            "post_write_values_sha256": values_sha(reread),
        })
        if mismatches or collateral or not idempotent:
            receipt["status"] = "FAIL"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    os.chmod(out, 0o600)
    print(json.dumps({k: v for k, v in receipt.items()
                      if k in ("status", "mode", "rows", "value_cells_planned",
                               "value_cells_written", "idempotent_rerun", "dry_run")}, indent=2, sort_keys=True))
    return 0 if receipt["status"] == "PASS" else 2


def run_approved_unlinked_disposition(plan_path: Path, out: Path, apply: bool) -> int:
    """Apply a terminal link-dark disposition with exact CAS and readback."""
    plan = json.loads(plan_path.read_text())
    rows = approved_unlinked_disposition_rows(plan)
    svc = build("sheets", "v4", credentials=creds(), cache_discovery=False)
    values = svc.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=f"'{TAB}'!{EXPECTED_RANGE}", valueRenderOption="UNFORMATTED_VALUE"
    ).execute().get("values", [])
    if not values or pad(values[0]) != HEADERS:
        raise RuntimeError("canonical header mismatch; refusing mutation")
    schema_preflight = require_checkbox_schema_pass(svc, SHEET_ID, TAB)
    require_unlinked_disposition_preconditions(values, rows)
    baseline_sha = values_sha(values)
    changes = wire_in_changed_cells(values, rows)
    receipt: dict[str, Any] = {
        "status": "PASS",
        "schema": "affiliate-approved-unlinked-disposition-write-v1",
        "mode": "approved_unlinked_disposition",
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "sheet_id": SHEET_ID,
        "tab": TAB,
        "plan_path": str(plan_path),
        "plan_sha256": hashlib.sha256(plan_path.read_bytes()).hexdigest(),
        "authority_sha256": plan["authority_sha256"],
        "rows": sorted(int(entry["row"]) for entry in rows),
        "mutable_columns": list(UNLINKED_DISPOSITION_MUTABLE_COLUMNS),
        "value_cells_planned": len(changes),
        "full_sheet_values_precondition_sha256": baseline_sha,
        "schema_preflight_status": schema_preflight["status"],
        "tracking_clicks": 0,
        "provider_writes": 0,
    }
    if not apply:
        receipt.update({"dry_run": True, "no_value_write": True, "no_sheet_write": True})
    else:
        live = svc.spreadsheets().values().get(
            spreadsheetId=SHEET_ID, range=f"'{TAB}'!{EXPECTED_RANGE}", valueRenderOption="UNFORMATTED_VALUE"
        ).execute().get("values", [])
        if values_sha(live) != baseline_sha:
            raise RuntimeError("sheet_concurrency_drift_before_value_write")
        if changes:
            require_marketplace_parent_rows_not_mutated(
                (row_num, column) for row_num, column, _ in changes
            )
            svc.spreadsheets().values().batchUpdate(
                spreadsheetId=SHEET_ID,
                body={"valueInputOption": "RAW", "data": [
                    {"range": f"'{TAB}'!{column_letter(column)}{row_num}",
                     "values": [[wire_in_write_value(column, value)]]}
                    for row_num, column, value in changes
                ]},
            ).execute()
        reread = svc.spreadsheets().values().get(
            spreadsheetId=SHEET_ID, range=f"'{TAB}'!{EXPECTED_RANGE}", valueRenderOption="UNFORMATTED_VALUE"
        ).execute().get("values", [])
        mismatches = []
        for entry in rows:
            row_num = int(entry["row"])
            actual = pad(reread[row_num - 1] if len(reread) >= row_num else [])
            for column, expected in entry["cells"].items():
                if not wire_in_cell_equal(int(column), actual[int(column)], expected):
                    mismatches.append({"row": row_num, "column": int(column),
                                       "expected": expected, "actual": actual[int(column)]})
        planned = {(int(entry["row"]), int(column)) for entry in rows for column in entry["cells"]}
        collateral = []
        for row_index in range(max(len(values), len(reread))):
            before = pad(values[row_index] if row_index < len(values) else [])
            after = pad(reread[row_index] if row_index < len(reread) else [])
            for column, (old, new) in enumerate(zip(before, after)):
                if old != new and (row_index + 1, column) not in planned:
                    collateral.append({"row": row_index + 1, "column": column,
                                       "before": old, "after": new})
        require_unlinked_disposition_preconditions(reread, rows)
        idempotent = not wire_in_changed_cells(reread, rows)
        receipt.update({
            "dry_run": False,
            "value_cells_written": len(changes),
            "readback_mismatches": mismatches,
            "collateral_changes": collateral,
            "idempotent_rerun": idempotent,
            "post_write_values_sha256": values_sha(reread),
        })
        if mismatches or collateral or not idempotent:
            receipt["status"] = "FAIL"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    os.chmod(out, 0o600)
    print(json.dumps({key: value for key, value in receipt.items()
                      if key in ("status", "mode", "rows", "value_cells_planned",
                                 "value_cells_written", "idempotent_rerun", "dry_run")},
                     indent=2, sort_keys=True))
    return 0 if receipt["status"] == "PASS" else 2


def run_network_paused_disposition(plan_path: Path, out: Path, apply: bool) -> int:
    """Retract a live/wired claim for a program its NETWORK paused.

    Same CAS + readback discipline as the other narrow modes: exact rows, exact
    columns, a full-sheet precondition hash, a collateral-change scan, and a
    re-derived idempotency check.
    """
    plan = json.loads(plan_path.read_text())
    rows = network_paused_disposition_rows(plan)
    svc = build("sheets", "v4", credentials=creds(), cache_discovery=False)
    values = svc.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=f"'{TAB}'!{EXPECTED_RANGE}", valueRenderOption="UNFORMATTED_VALUE"
    ).execute().get("values", [])
    if not values or pad(values[0]) != HEADERS:
        raise RuntimeError("canonical header mismatch; refusing mutation")
    schema_preflight = require_checkbox_schema_pass(svc, SHEET_ID, TAB)
    require_network_paused_disposition_preconditions(values, rows)
    baseline_sha = values_sha(values)
    changes = wire_in_changed_cells(values, rows)
    receipt: dict[str, Any] = {
        "status": "PASS",
        "schema": "affiliate-network-paused-disposition-write-v1",
        "mode": "network_paused_disposition",
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "sheet_id": SHEET_ID,
        "tab": TAB,
        "plan_path": str(plan_path),
        "plan_sha256": hashlib.sha256(plan_path.read_bytes()).hexdigest(),
        "authority_sha256": plan["authority_sha256"],
        "rows": sorted(int(entry["row"]) for entry in rows),
        "mutable_columns": list(NETWORK_PAUSED_DISPOSITION_MUTABLE_COLUMNS),
        "evidence": [
            {"row": int(entry["row"]), "program": entry["program"], **entry["evidence"]}
            for entry in rows
        ],
        "value_cells_planned": len(changes),
        "full_sheet_values_precondition_sha256": baseline_sha,
        "schema_preflight_status": schema_preflight["status"],
        "tracking_clicks": 0,
        "provider_writes": 0,
    }
    if not apply:
        receipt.update({"dry_run": True, "no_value_write": True, "no_sheet_write": True})
    else:
        live = svc.spreadsheets().values().get(
            spreadsheetId=SHEET_ID, range=f"'{TAB}'!{EXPECTED_RANGE}", valueRenderOption="UNFORMATTED_VALUE"
        ).execute().get("values", [])
        if values_sha(live) != baseline_sha:
            raise RuntimeError("sheet_concurrency_drift_before_value_write")
        if changes:
            require_marketplace_parent_rows_not_mutated(
                (row_num, column) for row_num, column, _ in changes
            )
            svc.spreadsheets().values().batchUpdate(
                spreadsheetId=SHEET_ID,
                body={"valueInputOption": "RAW", "data": [
                    {"range": f"'{TAB}'!{column_letter(column)}{row_num}",
                     "values": [[wire_in_write_value(column, value)]]}
                    for row_num, column, value in changes
                ]},
            ).execute()
        reread = svc.spreadsheets().values().get(
            spreadsheetId=SHEET_ID, range=f"'{TAB}'!{EXPECTED_RANGE}", valueRenderOption="UNFORMATTED_VALUE"
        ).execute().get("values", [])
        mismatches = []
        for entry in rows:
            row_num = int(entry["row"])
            actual = pad(reread[row_num - 1] if len(reread) >= row_num else [])
            for column, expected in entry["cells"].items():
                if not wire_in_cell_equal(int(column), actual[int(column)], expected):
                    mismatches.append({"row": row_num, "column": int(column),
                                       "expected": expected, "actual": actual[int(column)]})
        planned = {(int(entry["row"]), int(column)) for entry in rows for column in entry["cells"]}
        collateral = []
        for row_index in range(max(len(values), len(reread))):
            before = pad(values[row_index] if row_index < len(values) else [])
            after = pad(reread[row_index] if row_index < len(reread) else [])
            for column, (old, new) in enumerate(zip(before, after)):
                if old != new and (row_index + 1, column) not in planned:
                    collateral.append({"row": row_index + 1, "column": column,
                                       "before": old, "after": new})
        # The link must still be on the sheet: this mode retracts the claim, not
        # the history. Re-running the preconditions proves it.
        require_network_paused_disposition_preconditions(reread, rows)
        idempotent = not wire_in_changed_cells(reread, rows)
        receipt.update({
            "dry_run": False,
            "value_cells_written": len(changes),
            "readback_mismatches": mismatches,
            "collateral_changes": collateral,
            "idempotent_rerun": idempotent,
            "post_write_values_sha256": values_sha(reread),
            "tracking_links_preserved": [
                {"row": int(entry["row"]),
                 "tracking_link_present": bool(str(pad(reread[int(entry["row"]) - 1])[14]).strip())}
                for entry in rows
            ],
        })
        if mismatches or collateral or not idempotent:
            receipt["status"] = "FAIL"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    os.chmod(out, 0o600)
    print(json.dumps({key: value for key, value in receipt.items()
                      if key in ("status", "mode", "rows", "value_cells_planned",
                                 "value_cells_written", "idempotent_rerun", "dry_run")},
                     indent=2, sort_keys=True))
    return 0 if receipt["status"] == "PASS" else 2


def run_add_approved_row(plan_path: Path, out: Path, apply: bool) -> int:
    """Create a Sheet row for a portal-verified approval the Sheet never recorded.

    Same CAS + readback discipline as the other narrow modes, plus the two extra
    obligations that come with being the only mode allowed to create a row: it
    proves the program is absent first, and it proves the destination row is
    blank first.
    """
    plan = json.loads(plan_path.read_text())
    rows = add_approved_row_rows(plan)
    svc = build("sheets", "v4", credentials=creds(), cache_discovery=False)
    meta = svc.spreadsheets().get(spreadsheetId=SHEET_ID).execute()
    props = next(
        sheet["properties"] for sheet in meta["sheets"]
        if sheet["properties"]["title"] == TAB
    )
    grid_before = dict(props["gridProperties"])
    values = svc.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=f"'{TAB}'!{EXPECTED_RANGE}", valueRenderOption="UNFORMATTED_VALUE"
    ).execute().get("values", [])
    if not values or pad(values[0]) != HEADERS:
        raise RuntimeError("canonical header mismatch; refusing mutation")
    schema_preflight = require_checkbox_schema_pass(svc, SHEET_ID, TAB)

    # Reuse an already-created row on a re-run; otherwise take the next blank.
    used = {
        row_num for row_num, row in enumerate(values[1:], 2)
        if row_has_business_value(row)
    }
    blank_rows = [row for row in range(2, int(grid_before["rowCount"]) + 1) if row not in used]
    target_rows: dict[str, int] = {}
    for entry in rows:
        program = str(entry["program"])
        applied = [
            item for item in add_approved_row_existing_matches(values, entry)
            if str(pad(values[item["row"] - 1])[10] or "") in ADD_APPROVED_ROW_ALLOWED_STATUS
        ]
        if applied:
            target_rows[program] = applied[0]["row"]
            continue
        if not blank_rows:
            # The grid is full. Grow it rather than refusing a real approval.
            still_needed = sum(1 for item in rows if str(item["program"]) not in target_rows)
            grown = grow_grid_for_creates(
                svc, int(props["sheetId"]), int(grid_before["rowCount"]),
                still_needed, "add_approved_row")
            blank_rows.extend(grown)
            grid_before["rowCount"] = int(grid_before["rowCount"]) + len(grown)
        if not blank_rows:
            raise RuntimeError("add_approved_row_no_blank_grid_row_available")
        target_rows[program] = blank_rows.pop(0)
    require_add_approved_row_preconditions(values, rows, target_rows)

    baseline_sha = values_sha(values)
    planned_rows = [
        {**entry, "row": target_rows[str(entry["program"])]} for entry in rows
    ]
    created = [
        item["row"] for item in planned_rows
        if item["row"] not in used
    ]
    changes = add_approved_row_changed_cells(values, planned_rows)
    receipt: dict[str, Any] = {
        "status": "PASS",
        "schema": "affiliate-add-approved-row-write-v1",
        "mode": "add_approved_row",
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "sheet_id": SHEET_ID,
        "tab": TAB,
        "plan_path": str(plan_path),
        "plan_sha256": hashlib.sha256(plan_path.read_bytes()).hexdigest(),
        "authority_sha256": plan["authority_sha256"],
        "rows": sorted(item["row"] for item in planned_rows),
        "created_rows": sorted(created),
        "evidence": [
            {"row": item["row"], "program": item["program"], "network": item["network"],
             "provider_program_id": item["provider_program_id"], **item["evidence"]}
            for item in planned_rows
        ],
        "value_cells_planned": len(changes),
        "full_sheet_values_precondition_sha256": baseline_sha,
        "schema_preflight_status": schema_preflight["status"],
        "grid_before": grid_before,
        "tracking_clicks": 0,
        "provider_writes": 0,
    }
    if not apply:
        receipt.update({"dry_run": True, "no_value_write": True, "no_sheet_write": True})
    else:
        if created:
            source_row = next(
                (row_num for row_num, row in enumerate(values[1:], 2)
                 if offer_from_url(pad(row)[5]) == "1630"),
                None,
            )
            if not source_row:
                raise RuntimeError("add_approved_row_format_source_row_missing")
            svc.spreadsheets().batchUpdate(spreadsheetId=SHEET_ID, body={"requests": [
                {"copyPaste": {
                    "source": {"sheetId": props["sheetId"], "startRowIndex": source_row - 1,
                               "endRowIndex": source_row, "startColumnIndex": 0,
                               "endColumnIndex": len(HEADERS)},
                    "destination": {"sheetId": props["sheetId"], "startRowIndex": row - 1,
                                    "endRowIndex": row, "startColumnIndex": 0,
                                    "endColumnIndex": len(HEADERS)},
                    "pasteType": "PASTE_FORMAT", "pasteOrientation": "NORMAL",
                }} for row in created
            ] + schema_requests(props["sheetId"], created)}).execute()
        live = svc.spreadsheets().values().get(
            spreadsheetId=SHEET_ID, range=f"'{TAB}'!{EXPECTED_RANGE}", valueRenderOption="UNFORMATTED_VALUE"
        ).execute().get("values", [])
        if values_sha(live) != baseline_sha and not schema_materialization_is_safe(
            values, live, created
        ):
            raise RuntimeError("sheet_concurrency_drift_before_value_write")
        if changes:
            require_marketplace_parent_rows_not_mutated(
                (row_num, column) for row_num, column, _ in changes
            )
            svc.spreadsheets().values().batchUpdate(
                spreadsheetId=SHEET_ID,
                body={"valueInputOption": "RAW", "data": [
                    {"range": f"'{TAB}'!{column_letter(column)}{row_num}",
                     "values": [[add_approved_row_write_value(column, value)]]}
                    for row_num, column, value in changes
                ]},
            ).execute()
        reread = svc.spreadsheets().values().get(
            spreadsheetId=SHEET_ID, range=f"'{TAB}'!{EXPECTED_RANGE}", valueRenderOption="UNFORMATTED_VALUE"
        ).execute().get("values", [])
        mismatches = []
        for item in planned_rows:
            row_num = int(item["row"])
            actual = pad(reread[row_num - 1] if len(reread) >= row_num else [])
            for column, expected in item["cells"].items():
                if not add_approved_row_cell_equal(int(column), actual[int(column)], expected):
                    mismatches.append({"row": row_num, "column": int(column),
                                       "expected": expected, "actual": actual[int(column)]})
        planned_cells = {
            (int(item["row"]), int(column)) for item in planned_rows for column in item["cells"]
        }
        collateral = []
        for row_index in range(max(len(values), len(reread))):
            before = pad(values[row_index] if row_index < len(values) else [])
            after = pad(reread[row_index] if row_index < len(reread) else [])
            for column, (old, new) in enumerate(zip(before, after)):
                if old == new or (row_index + 1, column) in planned_cells:
                    continue
                # Sheets materializes a blank checkbox as FALSE in a row this
                # mode just prepared; that is schema, not a value change.
                if (row_index + 1) in created and column in (1, 11, 19, 20) \
                        and old in ("", False) and new in ("", False):
                    continue
                collateral.append({"row": row_index + 1, "column": column,
                                   "before": old, "after": new})
        require_add_approved_row_preconditions(reread, rows, target_rows)
        idempotent = not add_approved_row_changed_cells(reread, planned_rows)
        post_schema = require_checkbox_schema_pass(svc, SHEET_ID, TAB)
        receipt.update({
            "dry_run": False,
            "value_cells_written": len(changes),
            "readback_mismatches": mismatches,
            "collateral_changes": collateral,
            "idempotent_rerun": idempotent,
            "post_write_values_sha256": values_sha(reread),
            "post_write_schema_status": post_schema["status"],
            "rows_not_wire_ready": [
                {"row": int(item["row"]),
                 "wire_in_ready": pad(reread[int(item["row"]) - 1])[19],
                 "wired_live": pad(reread[int(item["row"]) - 1])[20]}
                for item in planned_rows
            ],
        })
        if mismatches or collateral or not idempotent:
            receipt["status"] = "FAIL"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    os.chmod(out, 0o600)
    print(json.dumps({key: value for key, value in receipt.items()
                      if key in ("status", "mode", "rows", "created_rows", "value_cells_planned",
                                 "value_cells_written", "idempotent_rerun", "dry_run")},
                     indent=2, sort_keys=True))
    return 0 if receipt["status"] == "PASS" else 2


# =============================================================================
# Queue-drained narrow modes (the sheet-sync writer)
# =============================================================================
# The five modes above were each written by hand for one incident. The 2026-08-18
# architecture makes the Sheet the single source of truth with exactly ONE writer
# draining a queue, which means every queue `kind` needs a governed mode - and
# hand-writing six more near-identical 200-line modes would be six more places to
# get the CAS/readback/idempotency discipline subtly wrong.
#
# So the remaining modes are declared as SPECS and executed by one engine that
# reproduces the existing modes' discipline exactly:
#   * a mode-specific plan schema string and an authority_sha256 binding,
#   * a closed set of mutable columns and a closed set of required columns,
#   * a closed set of statuses the mode may write (it can never write a status
#     that belongs to another mode),
#   * a closed set of statuses it may act FROM, so no mode can talk a row up out
#     of a state that is not its business,
#   * mandatory checkbox flags, so a mode that must not claim live cannot,
#   * mandatory evidence whose files must exist on disk (and hash-match where a
#     digest is declared) and whose citation must appear in the row's own
#     Notes/Blocker cell, so the Sheet carries the receipt,
#   * a full-sheet precondition hash, a collateral-change scan, a readback and a
#     re-derived idempotency check on every apply.
#
# The five hand-written modes are deliberately left untouched: they are proven,
# they have ~100 passing tests, and re-homing them into the engine would risk
# exactly the governance they exist to provide.

#: Every Sheet checkbox column. The engine coerces all four, not just the
#: wire-in pair, because application_pending may fill a whole new row.
SHEET_SYNC_CHECKBOX_COLUMNS = (1, 11, 19, 20)

#: Statuses that assert an approval. A row in one of these is making a claim
#: that only the network adapters may establish - several modes refuse to act on
#: one, and status_correction exists precisely to retract one.
SHEET_SYNC_APPROVAL_CLAIM_PREFIXES = ("approved", "live_wired", "applied_approved")

#: Statuses that assert the program is LIVE on the site. Retracting one is the
#: network-paused / removed modes' job and nothing else's.
SHEET_SYNC_LIVE_STATUS = frozenset({"live_wired_verified", "approved_active_wired_live"})

#: Rail-row statuses that mean exactly one thing: **the machine cannot proceed
#: until a human does one specific thing.** They are not relationships, not
#: approvals and not rejections - they are parked notes addressed to the
#: operator.
#:
#: `marketplace_rail_status` originally required a BLANK source status, which was
#: right when the only case was "a rail row nobody has written yet". But on
#: 2026-08-19 the operator worked their list and did those things - created the
#: Admitad account, minted the Rakuten developer app - and then there was no
#: sentence the Sheet could say. The row still read `blocked_operator_action_
#: required` while a first-party authenticated API readback answered 200 for our
#: own account. Leaving it there would have made the Sheet lie in the one
#: direction that costs money: hiding access we hold.
#:
#: So the mode accepts these FOUR statuses as a source in addition to blank, and
#: nothing else is relaxed - it still writes only Status/Notes/Last-Checked, can
#: never write a flag, a tracking link, an approval-of-a-programme status or a
#: live status, still refuses on a live row, still demands the marketplace rail
#: marker, and still requires a hash-bound first-party evidence file. The claim
#: it makes ("we hold marketplace ACCOUNT access") is exactly the claim the
#: evidence supports.
MARKETPLACE_RAIL_OPERATOR_BLOCKED_SOURCE_STATUS = frozenset({
    "blocked_operator_action_required",
    "operator_rakuten_developer_app_credentials_required",
    "blocked_google_sso_hard_attestation",
    "approved_marketplace_access_ui_auth_gated",
})

#: Pre-live approved statuses - the only states a deploy or an evidence block
#: may legitimately start from.
SHEET_SYNC_APPROVED_NOT_LIVE_STATUS = frozenset({
    "approved_active_not_wired",
    "approved_active_tracking_ready",
    "applied_approved_not_wired",
})

#: Removal reason class -> the ONE Link-QA value that describes it.
#:
#: 2026-08-18: three programs came off the site because the OPERATOR ruled they
#: are not GLP Clinic Scout fits. No network paused anything, all three
#: relationships stay approved and all three tracking links stay valid. The
#: `removed` mode's only Link-QA value at the time said
#: `link_retired_network_program_paused`, so the Sheet was made to state a false
#: fact for want of a word. The vocabulary is now bound to the removal receipt's
#: own reason class - the writer never chooses which sentence to tell.
REMOVAL_REASON_CLASS_LINK_QA = {
    "network_paused": "link_retired_network_program_paused",
    "niche_fit_not_glp_clinic_scout": "link_retired_niche_fit_removal",
}

#: Fit verdict -> the Vertical value that must accompany it. A verdict and a
#: vertical that disagree are two different claims in one row, so the mode
#: refuses rather than letting a human pick the pair.
FIT_ASSESSMENT_CORE_VERTICAL = "glp1-weightloss"
FIT_ASSESSMENT_OUT_OF_SCOPE_VERTICAL = "out-of-scope"
FIT_ASSESSMENT_VERDICTS = ("in_scope", "adjacent_sister_vertical", "out_of_scope")
#: A Fit Reason short enough to be a label is not an assessment.
FIT_REASON_MIN_LENGTH = 40


def removal_receipt_reason_class(path: Path) -> str:
    """Read a removal receipt's OWN reason class. Fails closed on anything else.

    This is the authority for the `removed` mode's Link-QA vocabulary. A receipt
    that does not declare a reason class (the pre-2026-08-18 Katalys receipt does
    not) authorises no Link-QA value at all - it is not evidence for a sentence
    it never wrote.
    """
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"removal_receipt_unreadable:{path}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"removal_receipt_malformed:{path}")
    if payload.get("schema") != "glp.site_paused_program_removal.v1":
        raise RuntimeError(f"removal_receipt_schema_invalid:{payload.get('schema')}")
    evidence = payload.get("evidence")
    reason = str(
        payload.get("removal_reason_class")
        or (evidence.get("reason_class") if isinstance(evidence, dict) else "")
        or ""
    ).strip()
    if not reason:
        raise RuntimeError(f"removal_receipt_reason_class_missing:{path}")
    if reason not in REMOVAL_REASON_CLASS_LINK_QA:
        raise RuntimeError(f"removal_receipt_reason_class_unknown:{reason}")
    return reason


#: Relationship words a network uses for "we approved you". A cross-network
#: duplicate is only sayable when the SECOND network genuinely approved us -
#: `pending` is an application, not a duplicate relationship.
CROSS_NETWORK_APPROVED_RELATIONSHIP = ("joined", "approved", "active", "approved_active")

#: The states a row may legitimately be sitting in when a second network's
#: approval supersedes it. A LIVE row is excluded by
#: `forbidden_source_status` - a live row that stops earning is a pause or a
#: removal, and a live row on the SAME network is not a duplicate at all.
CROSS_NETWORK_SOURCE_STATUS = frozenset({
    "applied_pending_brand_approval",
    "invited_pending_acceptance",
    "approved_active_not_wired",
    "approved_active_tracking_ready",
    "applied_approved_not_wired",
})


#: --- marketplace candidate vocabulary ------------------------------------
#: What KIND of thing a researched network is. The operator works the list by
#: hand, and "affiliate marketplace" and "CPA network" want completely
#: different applications, so the type is part of the record rather than prose.
MARKETPLACE_CANDIDATE_TYPES = (
    "affiliate_marketplace",
    "cpa_network",
    "saas_partner_platform",
    "content_monetization",
    "lead_gen_network",
    "retail_creator_program",
)
#: The research priority. It is written into the Sheet's own Priority column
#: (0) AND it determines the Apply? checkbox (1), so the two can never disagree.
MARKETPLACE_CANDIDATE_PRIORITIES = ("HIGH", "MED", "LOW")
#: The one status this mode may write. It is deliberately a NON-relationship: a
#: candidate row asserts research, never an account, an application or an
#: approval. `SHEET_SYNC_APPROVAL_CLAIM_PREFIXES` does not match it, so no
#: downstream machine can mistake it for one.
MARKETPLACE_CANDIDATE_STATUS = "candidate_not_applied"
#: A GLP-relevance sentence short enough to be a label is not evidence.
MARKETPLACE_CANDIDATE_RELEVANCE_MIN_LENGTH = 40

# A merchant programme already visible through an authenticated marketplace
# account, but not joined on that account. This is distinct from
# `marketplace_candidate`: we DO hold the Impact account, while the programme
# relationship itself is exactly `Never Joined`.
IMPACT_PROGRAM_CANDIDATE_STATUS = "not_applied"
IMPACT_PROGRAM_CANDIDATE_RELATIONSHIP = "never_joined"
IMPACT_PROGRAM_CANDIDATE_SOURCE_STATUS = frozenset({
    "", IMPACT_PROGRAM_CANDIDATE_STATUS, "provider_declined_impact",
})

# An authenticated network invitation that the deterministic fit classifier
# assigns outside the GLP portfolio. This is deliberately not an application or
# an approval claim: the relationship remains notjoined and every live flag is
# false. Core-fit invitations take the acceptance lane instead of this mode.
INVITATION_DISPOSITION_STATUS = "invited_not_joined"
INVITATION_DISPOSITION_FIT = ("sister", "out_of_scope")
INVITATION_DISPOSITION_VALUE = "not_joined_fit_unsupported"


def marketplace_candidate_existing_network_rows(
    values: list[list[Any]], program: str,
) -> list[dict[str, Any]]:
    """Every row that already represents this NETWORK, under any route label.

    `sheet_sync_existing_matches` scopes every identity axis by the Network /
    Route cell, which is right for merchant programs and wrong for rails: the
    engaged rails spell that cell "Network signup", "MaxBounty Network",
    "Publisher marketplace account" and "Takeads / Mitgo ID", so a candidate
    row for a network we are already on would sail past it and mint a duplicate
    saying we have never applied. That is the exact false fact this mode exists
    to avoid, so the name is checked against every rail row and against the
    canonical parent registry regardless of how the route cell is spelled.
    """
    wanted = norm(program)
    matches: list[dict[str, Any]] = []
    canonical = REQUIRED_MARKETPLACE_PARENT_ROWS.get(program)
    if canonical:
        matches.append({"row": canonical, "program": program, "axis": "canonical_parent_row"})
    for row_num, row in enumerate(values[1:], 2):
        actual = pad(row)
        if not row_has_business_value(actual):
            continue
        haystack = f"{actual[3]} {actual[4]}".lower()
        if not any(marker in haystack for marker in MARKETPLACE_RAIL_MARKERS):
            continue
        if norm(actual[2]) == wanted:
            matches.append({"row": row_num, "program": str(actual[2]), "axis": "rail_row_name"})
    return matches


def require_marketplace_candidate_row(
    spec: SheetSyncModeSpec, entry: dict[str, Any], cells: dict[str, Any],
) -> None:
    """Prove a created candidate row is a RAIL, un-engaged, and self-consistent.

    Five facts, all read off the plan the caller shipped and the evidence the
    writer has already proved is real:

    * the row is minted AS a marketplace rail (Category/Network carry a rail
      marker), because a network is not a merchant program and must never be
      wired, deployed or counted as inventory;
    * the Apply URL cell IS the researched application URL - the operator works
      this list by clicking that cell, so a cell that disagrees with the
      evidence is worse than no row;
    * the Priority cell IS the researched priority, and the Apply? checkbox
      follows it mechanically (HIGH/MED -> TRUE, LOW -> FALSE) so nobody can
      quietly promote a network the research called weak;
    * the GLP-relevance sentence is long enough to BE a finding; and
    * Signup Complete? is FALSE. That single flag is the whole honesty
      contract: this mode may never say we hold an account.
    """
    evidence = entry["evidence"]
    get = lambda column: cells.get(str(column), cells.get(column))  # noqa: E731
    program = str(entry["program"]).strip()
    haystack = f"{get(3)} {get(4)}".lower()
    if not any(marker in haystack for marker in MARKETPLACE_RAIL_MARKERS):
        raise RuntimeError(
            f"{spec.key}_created_row_is_not_a_marketplace_rail:{program}:"
            f"{str(get(3)).strip()!r}/{str(get(4)).strip()!r}")
    application_url = _sheet_sync_evidence_value(evidence, "application_url")
    if str(get(5) or "").strip() != application_url:
        raise RuntimeError(
            f"{spec.key}_apply_url_does_not_follow_evidence:{program}:"
            f"{str(get(5)).strip()!r}!={application_url!r}")
    priority = _sheet_sync_evidence_value(evidence, "priority")
    if str(get(0) or "").strip() != priority:
        raise RuntimeError(
            f"{spec.key}_priority_does_not_follow_evidence:{program}:"
            f"{str(get(0)).strip()!r}!={priority!r}")
    expected_apply = "FALSE" if priority == "LOW" else "TRUE"
    if str(get(1)).strip().upper() != expected_apply:
        raise RuntimeError(
            f"{spec.key}_apply_flag_does_not_follow_priority:{program}:"
            f"{priority}:{str(get(1)).strip()!r}")
    relevance = _sheet_sync_evidence_value(evidence, "glp_relevance_evidence")
    if len(relevance) < MARKETPLACE_CANDIDATE_RELEVANCE_MIN_LENGTH:
        raise RuntimeError(f"{spec.key}_glp_relevance_too_short:{program}")


def _impact_campaign_public_economics(campaign: dict[str, Any]) -> tuple[str, str]:
    """Derive the public payout sentence and cookie window from an API capture."""
    summary = campaign.get("public_terms_summary") or {}
    payout_fields = summary.get("payout_fields") or []
    terms = []
    if payout_fields and isinstance(payout_fields[0], dict):
        terms = payout_fields[0].get("PayoutTermsList") or []
    rendered: list[str] = []
    windows: set[tuple[str, str]] = set()
    for term in terms:
        if not isinstance(term, dict):
            continue
        amount = str(term.get("PayoutAmount") or "").strip()
        percentage = str(term.get("PayoutPercentage") or "").strip()
        value = f"${amount}" if amount else (f"{percentage}%" if percentage else "rate unstated")
        tracker = str(term.get("TrackerName") or "unnamed event").strip()
        tracker_type = str(term.get("TrackerType") or "event").strip()
        rendered.append(f"{value} {tracker} ({tracker_type})")
        period = str(term.get("ReferralPeriod") or "").strip()
        unit = str(term.get("ReferralPeriodUnit") or "").strip().lower()
        if period and unit:
            windows.add((period, unit))
    if not rendered:
        raise RuntimeError("impact_program_candidate_capture_has_no_public_payout_terms")
    cookie = ", ".join(
        f"{period} {unit}{'' if period == '1' or unit.endswith('s') else 's'}"
        for period, unit in sorted(windows)
    ) or "Not stated"
    return "; ".join(rendered), cookie


def require_impact_program_candidate_capture(
    spec: SheetSyncModeSpec, entry: dict[str, Any], cells: dict[str, Any],
) -> None:
    """Bind a not-applied Impact row to the exact API campaign and public terms."""
    evidence = entry["evidence"]
    program = str(entry["program"]).strip()
    program_id = _sheet_sync_evidence_value(evidence, "network_program_id")
    if norm(entry.get("network")) != norm("Impact.com"):
        raise RuntimeError(f"{spec.key}_network_must_be_impact:{program}")
    if str(entry.get("provider_program_id") or "").strip() != program_id:
        raise RuntimeError(f"{spec.key}_program_id_contradicts_entry:{program}:{program_id}")
    capture_path = _REPO_ROOT / _sheet_sync_evidence_value(evidence, "campaign_capture_path")
    try:
        payload = json.loads(capture_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{spec.key}_capture_unreadable:{program}") from exc
    matches = [
        item for item in (payload.get("campaigns") or [])
        if str(item.get("campaign_id") or item.get("campaign_id_requested") or "") == program_id
    ]
    if len(matches) != 1:
        raise RuntimeError(f"{spec.key}_campaign_missing_or_ambiguous:{program}:{program_id}")
    campaign = matches[0]
    if str(campaign.get("contract_status") or "") != "Never Joined":
        raise RuntimeError(
            f"{spec.key}_campaign_not_never_joined:{program}:{campaign.get('contract_status')}")
    if campaign.get("glp_keyword_fit") is not True:
        raise RuntimeError(f"{spec.key}_campaign_not_glp_fit:{program}:{program_id}")
    if norm(campaign.get("campaign_name")) != norm(program):
        raise RuntimeError(
            f"{spec.key}_campaign_name_mismatch:{program}:{campaign.get('campaign_name')}")
    terms, cookie = _impact_campaign_public_economics(campaign)
    if _sheet_sync_evidence_value(evidence, "commission_terms") != terms:
        raise RuntimeError(f"{spec.key}_commission_terms_contradict_capture:{program}")
    if _sheet_sync_evidence_value(evidence, "cookie_window") != cookie:
        raise RuntimeError(f"{spec.key}_cookie_window_contradicts_capture:{program}")
    get = lambda column: cells.get(str(column), cells.get(column))  # noqa: E731
    if str(get(10) or "") != IMPACT_PROGRAM_CANDIDATE_STATUS:
        raise RuntimeError(f"{spec.key}_status_not_not_applied:{program}")
    if str(get(4) or entry.get("network") or "").strip() != "Impact.com":
        raise RuntimeError(f"{spec.key}_network_cell_must_be_impact:{program}")


def require_affiliati_inventory_capture(
    spec: SheetSyncModeSpec, entry: dict[str, Any], cells: dict[str, Any],
) -> None:
    """Bind an Affiliati inventory row to one raw offer in the signed fit census."""
    evidence = entry["evidence"]
    program = str(entry["program"]).strip()
    program_id = _sheet_sync_evidence_value(evidence, "network_program_id")
    if canonical_network(entry.get("network")) != "affiliati":
        raise RuntimeError(f"{spec.key}_network_must_be_affiliati:{program}")
    if str(entry.get("provider_program_id") or "").strip() != program_id:
        raise RuntimeError(f"{spec.key}_program_id_contradicts_entry:{program}:{program_id}")
    capture_path = _REPO_ROOT / _sheet_sync_evidence_value(evidence, "api_capture_path")
    try:
        payload = json.loads(capture_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{spec.key}_capture_unreadable:{program}") from exc
    if payload.get("schema") != "affiliati.glp-fit-census.v1":
        raise RuntimeError(f"{spec.key}_capture_schema_invalid:{program}")
    effects = payload.get("effects") or {}
    if any(int(effects.get(field, -1)) != 0 for field in (
            "applications_submitted", "provider_mutations", "tracking_links_generated")):
        raise RuntimeError(f"{spec.key}_capture_has_provider_effects:{program}")
    matches = [
        item for item in (payload.get("fits") or [])
        if str((item.get("source_offer") or {}).get("offer_id") or "") == program_id
    ]
    if len(matches) != 1:
        raise RuntimeError(f"{spec.key}_offer_missing_or_ambiguous:{program}:{program_id}")
    fit = matches[0]
    source = fit.get("source_offer") or {}
    try:
        from scripts.marketplace_adapters.affiliati import normalize_offer
    except ImportError:
        from marketplace_adapters.affiliati import normalize_offer
    normalized = normalize_offer(source)
    expected_terms = (
        f"{normalized['payout_display']}; paid conversion event: {normalized['paid_event']}"
    )
    expected = {
        "source_offer_name": str(source.get("offer_name") or "").strip(),
        "offer_contract_id": str(source.get("offer_contract_id") or "").strip(),
        "provider_relationship": str(normalized.get("relationship") or "").strip(),
        "offer_status_raw": str((source.get("offer_status") or {}).get("offer_status_name") or "").strip(),
        "exact_paid_event": str(normalized.get("paid_event") or "").strip(),
        "payout_terms": expected_terms,
        "official_url": str(normalized.get("official_url") or "").strip(),
        "provider_name": str(fit.get("provider_name") or "").strip(),
        "fit_reason": str(fit.get("fit_reason") or "").strip(),
        "suggested_glp_page": str(fit.get("suggested_glp_page") or "").strip(),
        "promotion_notes": str(fit.get("promotion_notes") or "").strip(),
        "vertical": str(fit.get("vertical") or "").strip(),
    }
    for field, value in expected.items():
        if _sheet_sync_evidence_value(evidence, field) != value:
            raise RuntimeError(f"{spec.key}_{field}_contradicts_capture:{program}")
    if normalized.get("seo_allowed") is not True or _sheet_sync_evidence_value(
            evidence, "seo_allowed") != "TRUE":
        raise RuntimeError(f"{spec.key}_seo_not_allowed:{program}")
    if _sheet_sync_evidence_value(evidence, "fit_classification") != "core_glp1":
        raise RuntimeError(f"{spec.key}_fit_not_core_glp1:{program}")
    if str(fit.get("program_name") or "").strip() != program:
        raise RuntimeError(f"{spec.key}_program_name_contradicts_capture:{program}")
    get = lambda column: cells.get(str(column), cells.get(column))  # noqa: E731
    relationship = expected["provider_relationship"]
    status = AFFILIATI_INVENTORY_STATUS.get(relationship)
    if not status or str(get(10) or "") != status:
        raise RuntimeError(f"{spec.key}_status_does_not_follow_relationship:{program}")
    expected_apply = "TRUE" if relationship == "notjoined" else "FALSE"
    expected_signup = "FALSE" if relationship == "notjoined" else "TRUE"
    if str(get(1)).strip().upper() != expected_apply:
        raise RuntimeError(f"{spec.key}_apply_flag_does_not_follow_relationship:{program}")
    if str(get(11)).strip().upper() != expected_signup:
        raise RuntimeError(f"{spec.key}_signup_flag_does_not_follow_relationship:{program}")


def require_affiliati_switch_capture(
    spec: SheetSyncModeSpec, entry: dict[str, Any], cells: dict[str, Any],
) -> None:
    """Bind a staged rail switch to the canonical postback and raw offer terms."""
    evidence = entry["evidence"]
    program = str(entry["program"]).strip()
    offer_id = _sheet_sync_evidence_value(evidence, "network_program_id")
    capture_path = _REPO_ROOT / _sheet_sync_evidence_value(evidence, "switch_capture_path")
    try:
        capture = json.loads(capture_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{spec.key}_capture_unreadable:{program}") from exc
    if capture.get("schema") != "glp.affiliati-switch-capture.v1":
        raise RuntimeError(f"{spec.key}_capture_schema_invalid:{program}")
    effects = capture.get("effects") or {}
    if any(int(effects.get(field, -1)) != 0 for field in (
            "applications_submitted", "provider_mutations", "tracking_links_generated",
            "deployments")):
        raise RuntimeError(f"{spec.key}_capture_has_effects:{program}")
    matches = [row for row in capture.get("offers") or []
               if str(row.get("offer_id") or "") == offer_id]
    if len(matches) != 1:
        raise RuntimeError(f"{spec.key}_offer_missing_or_ambiguous:{program}:{offer_id}")
    offer = matches[0]
    provider = str(offer.get("provider") or "")
    target_status = AFFILIATI_SWITCH_STATUS.get(provider)
    expected = {
        "provider_name": provider,
        "offer_contract_id": str(offer.get("offer_contract_id") or ""),
        "campaign_id": str(offer.get("campaign_id") or ""),
        "tracking_host": str(offer.get("host") or ""),
        "payout_terms": str(offer.get("payout_terms") or ""),
        "exact_paid_event": str(offer.get("exact_paid_event") or ""),
        "official_url": str(offer.get("official_url") or ""),
        "target_status": str(target_status or ""),
        "current_payout_usd": f"{float(offer.get('current_payout_usd')):g}",
        "replacement_payout_usd": f"{float(offer.get('payout_usd')):g}",
        "dollar_delta": f"{float(offer.get('dollar_delta')):g}",
    }
    for field, value in expected.items():
        if _sheet_sync_evidence_value(evidence, field) != value:
            raise RuntimeError(f"{spec.key}_{field}_contradicts_capture:{program}")
    if program != f"{provider} | Affiliati" or not target_status:
        raise RuntimeError(f"{spec.key}_program_name_or_provider_invalid:{program}")

    contract_path = _REPO_ROOT / _sheet_sync_evidence_value(evidence, "postback_contract_path")
    registry_path = _REPO_ROOT / _sheet_sync_evidence_value(evidence, "prerequisite_registry_path")
    terms_path = _REPO_ROOT / _sheet_sync_evidence_value(evidence, "terms_capture_path")
    try:
        contract = json.loads(contract_path.read_text())
        registry = json.loads(registry_path.read_text())
        terms = json.loads(terms_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{spec.key}_bound_artifact_unreadable:{program}") from exc
    contract_offer = [row for row in contract.get("starting_offers") or []
                      if str(row.get("offer_id") or "") == offer_id]
    if (contract.get("schema") != "affiliati-postback-contract-v1"
            or len(contract_offer) != 1
            or str(contract_offer[0].get("provider") or "") != provider
            or str(contract_offer[0].get("campaign_id") or "") != expected["campaign_id"]
            or str(contract_offer[0].get("host") or "") != expected["tracking_host"]
            or float(contract_offer[0].get("payout_usd") or -1)
            != float(offer.get("payout_usd") or -2)):
        raise RuntimeError(f"{spec.key}_postback_contract_offer_mismatch:{program}")
    prereq = (registry.get("offers") or {}).get(offer_id) or {}
    required = set(registry.get("required_prerequisites") or [])
    expected_required = {
        "per_offer_tracking_link_obtained", "outbound_s1_population",
        "outbound_s2_population", "postback_receiver_exists",
        "network_echo_tokens_confirmed", "network_approval_confirmed",
        "offer_terms_compliance_cleared",
    }
    prerequisites = prereq.get("prerequisites") or {}
    always_blocking_now = {
        "per_offer_tracking_link_obtained", "outbound_s1_population",
        "outbound_s2_population", "postback_receiver_exists",
        "network_echo_tokens_confirmed",
    }
    if (registry.get("schema") != "glp.affiliati-switch-prerequisites.v1"
            or required != expected_required
            or str(prereq.get("provider") or "") != provider
            or any(prerequisites.get(name) is not False for name in always_blocking_now)
            or (provider == "Direct Meds"
                and prerequisites.get("network_approval_confirmed") is not False)
            or (provider == "MEDVI"
                and prerequisites.get("offer_terms_compliance_cleared") is not False)):
        raise RuntimeError(f"{spec.key}_prerequisites_not_staged_blocking:{program}")
    if (terms.get("schema") != "glp.affiliati-offer-terms-verbatim.v1"
            or str(terms.get("offer_id") or "") != offer_id
            or terms.get("verbatim") != offer.get("verbatim_terms")):
        raise RuntimeError(f"{spec.key}_terms_capture_mismatch:{program}")
    if provider == "MEDVI" and "SENSITIVE" not in " ".join(offer.get("flags") or []):
        raise RuntimeError(f"{spec.key}_medvi_sensitive_flag_missing:{program}")

    get = lambda column: cells.get(str(column), cells.get(column))  # noqa: E731
    if (str(get(10) or "") != target_status or str(get(14) or "")
            or str(get(19)).upper() != "FALSE" or str(get(20)).upper() != "FALSE"):
        raise RuntimeError(f"{spec.key}_link_dark_status_invalid:{program}")


def require_affiliati_tracking_capture(
    spec: SheetSyncModeSpec, entry: dict[str, Any], cells: dict[str, Any],
) -> None:
    """Bind an Affiliati href to campaign config, best terms, and nine-field research.

    Every check is local string/file inspection. No tracking URL is requested,
    resolved, opened, or clicked by this validator.
    """
    from urllib.parse import urlsplit
    try:
        import glp_affiliate_link_rails as rails
    except ImportError:
        from scripts import glp_affiliate_link_rails as rails

    evidence = entry["evidence"]
    program = str(entry["program"]).strip()
    capture_path = _REPO_ROOT / _sheet_sync_evidence_value(
        evidence, "campaign_capture_path")
    research_path = _REPO_ROOT / _sheet_sync_evidence_value(
        evidence, "research_packet_path")
    reconciliation_path = _REPO_ROOT / _sheet_sync_evidence_value(
        evidence, "reconciliation_path")
    try:
        capture = json.loads(capture_path.read_text())
        research = json.loads(research_path.read_text())
        reconciliation = json.loads(reconciliation_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{spec.key}_bound_artifact_unreadable:{program}") from exc

    offer = capture.get("offer") or {}
    creative = offer.get("tracking_creative") or {}
    href = str(creative.get("unique_link") or "").strip()
    effects = capture.get("effects") or {}
    zero_effects = (
        "applications_submitted", "provider_mutations", "tracking_links_requested",
        "tracking_links_clicked", "tracking_redirects_resolved",
    )
    if (capture.get("schema") != "glp.affiliati-campaign-configuration-capture.v1"
            or capture.get("network") != "Affiliati"
            or any(int(effects.get(field, -1)) != 0 for field in zero_effects)):
        raise RuntimeError(f"{spec.key}_capture_invalid_or_effectful:{program}")

    program_id = str(offer.get("program_id") or "")
    creative_id = str(creative.get("creative_id") or "")
    commission = offer.get("commission") or {}
    cookie = offer.get("cookie_window") or {}
    geo = offer.get("geo") or []
    restrictions = str(offer.get("restrictions") or "").strip()
    canonical_geo = ", ".join(str(value).strip() for value in geo if str(value).strip())
    if not canonical_geo and re.search(r"(?:Geos?|Allowable Geos)\s*[-:]\s*US\b",
                                       restrictions, re.I):
        canonical_geo = "US"
    cookie_window = (str(cookie.get("quote") or "").strip()
                     if cookie.get("disclosed") else
                     "not disclosed in authenticated offer or campaign API")
    expected = {
        "captured_at_utc": str(capture.get("captured_at_utc") or ""),
        "network_program_id": program_id,
        "campaign_id": str(offer.get("campaign_id") or ""),
        "offer_contract_id": str(offer.get("offer_contract_id") or ""),
        "tracking_creative_id": creative_id,
        "provider_relationship": str(offer.get("relationship") or ""),
        "tracking_link": href,
        "tracking_link_sha256": hashlib.sha256(href.encode()).hexdigest(),
        "tracking_href_host": str(offer.get("tracking_href_host") or ""),
        "payout_terms": (f"{commission.get('display')}; paid conversion event: "
                         f"{offer.get('paid_event_from_offer_terms')}"),
        "commission_type": str(commission.get("type") or ""),
        "exact_paid_event": str(offer.get("paid_event_from_offer_terms") or ""),
        "cookie_window": cookie_window,
        "geo": canonical_geo,
        "restrictions": restrictions,
        "tracking_inspection_method": (
            "authenticated campaign configuration field inspection only"),
    }
    for field, value in expected.items():
        if _sheet_sync_evidence_value(evidence, field) != value:
            raise RuntimeError(f"{spec.key}_{field}_contradicts_capture:{program}")
    provider = _sheet_sync_evidence_value(evidence, "provider_name")
    href_source = str(offer.get("tracking_href_source") or "")
    if (program != f"{provider} | Affiliati" or expected["provider_relationship"] != "approved"
            or str(offer.get("tracking_href") or "") != href
            or "configuration field inspection" not in href_source
            or "never requested" not in href_source
            or not offer.get("tracking_href_structurally_present")):
        raise RuntimeError(f"{spec.key}_program_or_tracking_identity_invalid:{program}")
    parsed = urlsplit(href)
    if parsed.hostname != expected["tracking_href_host"]:
        raise RuntimeError(f"{spec.key}_tracking_host_contradicts_href:{program}")
    rails.validate_link("Affiliati", creative_id, href,
                        allowed_host=expected["tracking_href_host"])

    required_fields = {
        "first_month_price", "monthly_price", "consultation_fee", "eligibility",
        "states_served", "pharmacy_type", "shipping_wait", "cancellation", "legitscript",
    }
    slug = _sheet_sync_evidence_value(evidence, "governed_seo_slug")
    research_effects = research.get("effects") or {}
    if (research.get("schema") != "program-deep-research-v1"
            or research.get("program_id") != slug or research.get("slug") != slug
            or int(research.get("field_count") or 0) != 9
            or set(research.get("fields") or {}) != required_fields
            or any(int(research_effects.get(field, -1)) != 0 for field in (
                "tracking_clicks", "tracking_links_requested", "applications_submitted"))):
        raise RuntimeError(f"{spec.key}_research_packet_invalid:{program}:{slug}")
    for name, field in (research.get("fields") or {}).items():
        path = Path(str(field.get("source_capture_path") or ""))
        if not path.is_absolute():
            path = _REPO_ROOT / path
        quote = str(field.get("quote") or "")
        digest = str(field.get("source_capture_sha256") or "")
        try:
            source_document = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            source_document = None

        def contains_quote(value: Any) -> bool:
            if isinstance(value, str):
                return quote in value
            if isinstance(value, dict):
                return any(contains_quote(item) for item in value.values())
            if isinstance(value, list):
                return any(contains_quote(item) for item in value)
            return False

        if (not field.get("value") or not quote or not path.is_file()
                or hashlib.sha256(path.read_bytes()).hexdigest() != digest
                or not contains_quote(source_document)):
            raise RuntimeError(f"{spec.key}_research_field_invalid:{program}:{name}")

    reconciled = [item for item in reconciliation.get("approved_affiliati_offers") or []
                  if str(item.get("program_id") or "") == program_id]
    decisions = [item for item in reconciliation.get("reconciliation") or []
                 if str((item.get("winner") or {}).get("program_id") or "") == program_id]
    candidates = [item for item in reconciliation.get("queue_candidates") or []
                  if str(item.get("program_id") or "") == program_id]
    rec_effects = reconciliation.get("effects") or {}
    if (reconciliation.get("schema") != "job-a.affiliati-best-terms-reconciliation.v1"
            or reconciliation.get("status") != "PASS"
            or len(reconciled) != 1 or len(decisions) != 1 or len(candidates) != 1
            or decisions[0].get("decision") != "QUEUE_AFFILIATI_WINNER"
            or str(candidates[0].get("seo_slug") or "") != slug
            or int(candidates[0].get("sheet_row") or 0) != int(entry.get("row") or 0)
            or any(int(rec_effects.get(field, -1)) != 0 for field in (
                "applications_submitted", "tracking_links_requested",
                "tracking_links_clicked", "tracking_redirects_resolved", "sheet_writes",
                "deployments"))):
        raise RuntimeError(f"{spec.key}_reconciliation_invalid:{program}")

    get = lambda column: cells.get(str(column), cells.get(column))  # noqa: E731
    if (str(get(10) or "") != "approved_active_tracking_ready"
            or str(get(11)).upper() != "TRUE"
            or str(get(19)).upper() != "FALSE" or str(get(20)).upper() != "FALSE"
            or str(get(21) or "") != "not_clicked_preserve_attribution"):
        raise RuntimeError(f"{spec.key}_target_cells_invalid:{program}")


def require_higher_rail_pending_capture(
    spec: SheetSyncModeSpec, entry: dict[str, Any], row_num: int, actual: list[Any],
) -> None:
    evidence = entry["evidence"]
    contract_path = _REPO_ROOT / _sheet_sync_evidence_value(evidence, "postback_contract_path")
    capture_path = _REPO_ROOT / _sheet_sync_evidence_value(evidence, "switch_capture_path")
    try:
        contract = json.loads(contract_path.read_text())
        capture = json.loads(capture_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{spec.key}_evidence_unreadable:{row_num}") from exc
    provider = _sheet_sync_evidence_value(evidence, "provider_name")
    replacement_id = _sheet_sync_evidence_value(evidence, "replacement_offer_id")
    source_id = _sheet_sync_evidence_value(evidence, "source_program_id")
    contract_rows = [row for row in contract.get("starting_offers") or []
                     if str(row.get("offer_id") or "") == replacement_id]
    capture_rows = [row for row in capture.get("offers") or []
                    if str(row.get("offer_id") or "") == replacement_id]
    source_rows = [
        row for capture_row in capture_rows
        for row in capture_row.get("current_rail_offers") or []
        if int(row.get("sheet_row") or 0) == row_num
        and str(row.get("offer_id") or "") == source_id
    ]
    if (len(contract_rows) != 1 or len(capture_rows) != 1
            or len(source_rows) != 1
            or str(contract_rows[0].get("provider") or "") != provider
            or row_num not in [int(value) for value in contract_rows[0].get("current_sheet_rows") or []]
            or f"{float(source_rows[0].get('payout_usd')):g}"
            != _sheet_sync_evidence_value(evidence, "current_payout_usd")
            or f"{float(capture_rows[0].get('payout_usd')):g}"
            != _sheet_sync_evidence_value(evidence, "replacement_payout_usd")
            or f"{float(source_rows[0].get('dollar_delta')):g}"
            != _sheet_sync_evidence_value(evidence, "dollar_delta")):
        raise RuntimeError(f"{spec.key}_switch_identity_mismatch:{row_num}")
    if (canonical_network(actual[4]) != "xentraffic"
            or not program_id_in_text(source_id, actual[5])):
        raise RuntimeError(f"{spec.key}_source_rail_identity_mismatch:{row_num}")
    target = str(entry["cells"].get("10", entry["cells"].get(10)) or "")
    if target != HIGHER_RAIL_PENDING_STATUS:
        raise RuntimeError(f"{spec.key}_target_status_invalid:{row_num}")


def registrable_domain(url: Any) -> str:
    """Host of a URL, lowercased, without scheme, credentials, port, path or `www.`.

    The brand axis for cross-network identity. The governed Sheet spells the
    same brand differently per network (`HealthRX` on Awin, `HealthRX Affiliate
    Program` on Katalys), so a name comparison cannot prove two rows are one
    brand - but both rows carry the brand's own Official URL, and
    `www.healthrx.com` and `healthrx.com/` are the same company.
    """
    text = str(url or "").strip().casefold()
    text = re.sub(r"^[a-z][a-z0-9+.\-]*://", "", text)
    text = text.split("/")[0].split("?")[0].split("#")[0].split("@")[-1].split(":")[0]
    return text[4:] if text.startswith("www.") else text


#: The same approval vocabulary, used by the mode that talks a PENDING row up.
#: One list, so "what a network calls an approval" can never mean two things in
#: this file.
APPROVED_RELATIONSHIP_WORDS = CROSS_NETWORK_APPROVED_RELATIONSHIP

#: The only states `approval_confirmed` may act FROM: a row that says we asked
#: and are waiting. A rejected, not-related, paused, removed, suppressed or
#: already-approved row is somebody else's business, and a LIVE row is excluded
#: twice over (it is not in this set AND it is in `forbidden_source_status`).
APPROVAL_CONFIRMED_SOURCE_STATUS = frozenset({
    "applied_pending_brand_approval",
    "invited_pending_acceptance",
    # 2026-08-18. `provider_not_joined_reapply_decision_required` is the third
    # state that means "we asked and are waiting", and adding it closes a real
    # class rather than one row.
    #
    # It is written when an authenticated readback answered `notjoined` for a
    # program we had applied to, and it says in as many words: a decision is
    # required about whether to reapply. 25 rows sit in it. The state is NOT an
    # approval claim (so `forbid_source_approval_claim` still bites), NOT a
    # rejection (the network never declined us - a readback lagged), and NOT
    # terminal.
    #
    # The live case that forced this: CJ approved `EuNatural` on 2026-08-18 and
    # the inbox router could not record it, because row 297 held this status
    # from a 2026-07-31 readback. The contradiction sat in the operator's queue
    # as AI-9 - a row-state technicality no human should have to adjudicate.
    #
    # What makes it safe to act from here is that NOTHING else is relaxed. A
    # fresh first-party readback that answers `joined` for that exact program id
    # is strictly better evidence than the stale one that produced this status,
    # and it must still clear every other guard: hash-bound capture, the program
    # id present as a standalone number in BOTH the capture's own bytes and the
    # target row, an approval word from `APPROVED_RELATIONSHIP_WORDS`, flags
    # forced FALSE, no tracking link, and no materialization path from the drain.
    # The mode still cannot create a row, cannot write a link, and cannot claim
    # wire-ready or live.
    "provider_not_joined_reapply_decision_required",
})


#: The one status `commercial_economics_hold` may write. It is an APPROVAL claim
#: on purpose - the relationship really is approved, the tracking link really was
#: issued, and pretending otherwise would be the same lie in the other
#: direction. What it withholds is any suggestion that the program is placeable.
#: It matches no wire-in, deploy or live vocabulary, so nothing downstream can
#: act on it. The vocabulary is not invented here: it is the exact string three
#: existing files already use for this state -
#: `affiliate_approval_auto_reconciler.py:861`,
#: `finalize_partnerize_legacy_approved_wiring_20260802.py:18` and
#: `build_glp_affiliate_live_accounting.py:130`.
COMMERCIAL_ECONOMICS_HOLD_STATUS = "approved_no_positive_commission_configured"

#: The states an economics hold may act FROM: the three approved-not-live
#: statuses, plus the evidence-blocked state.
#:
#: The evidence-blocked source is the live case and the reason this mode exists.
#: Row 148 (eMed) has read `approved_active_first_party_evidence_blocked` since
#: 2026-08-18, and on 2026-08-18 that stopped being true: the Vercel Security
#: Checkpoint was defeated with a headful browser and five first-party pages were
#: captured. The row was left asserting a blocker that no longer exists, and no
#: governed mode could say the real one. `status_correction` could not: its
#: vocabulary is `pending|not_related|rejected|invited` and every one of those
#: would deny an approval we genuinely hold. `deployed_live` and `removed` are
#: both false. So the row could only keep lying.
#:
#: A LIVE row is excluded twice over - it is not in this set AND it is in
#: `forbidden_source_status`. A program that is live and earning whose commission
#: goes to zero is a PAUSE or a REMOVAL (the site must stop pointing at it), not
#: a note on an unplaced row, and those modes retract the live claim and set
#: Link-QA. This mode does neither, so it must never be the one that handles it.
COMMERCIAL_ECONOMICS_HOLD_SOURCE_STATUS = frozenset(
    SHEET_SYNC_APPROVED_NOT_LIVE_STATUS | {"approved_active_first_party_evidence_blocked"})


def require_program_identity_binding(
    spec: SheetSyncModeSpec,
    entry: dict[str, Any],
    row_num: int,
    actual: list[Any],
) -> None:
    """Prove the cited capture and the target row are the SAME programme.

    A mode that talks a row UP is the one place where citing the wrong file is
    catastrophic: any hash-valid capture would otherwise let a caller flip an
    unrelated pending row to approved. So the declared network programme id must
    appear as a standalone number BOTH in the capture file's own bytes (the
    capture is about that programme) AND somewhere in the target row's own
    identity/evidence cells - Apply URL, tracking link or Notes (the row is about
    that programme). Neither side is the caller's say-so.
    """
    evidence = entry["evidence"]
    program_id = _sheet_sync_evidence_value(evidence, "network_program_id")
    if not program_id:
        raise RuntimeError(f"{spec.key}_program_id_missing:{row_num}")
    pattern = re.compile(rf"(?<![0-9]){re.escape(program_id)}(?![0-9])")
    capture = _REPO_ROOT / _sheet_sync_evidence_value(evidence, spec.identity_capture_field)
    if not capture.is_file():
        raise RuntimeError(f"{spec.key}_capture_missing:{row_num}:{capture}")
    capture_text = capture.read_text(errors="replace")
    # A partner manager's direct email can carry a negotiated rate without the
    # marketplace's internal campaign id. Only an explicitly operator-confirmed
    # terms correction may use this path. It remains bound independently by the
    # programme name in the original email and the immutable id on the target row.
    fetch_method = _sheet_sync_evidence_value(evidence, "fetch_method")
    if (spec.key == "commission_terms_correction"
            and fetch_method == "operator_confirmed_partner_email"):
        program_name = " ".join(str(actual[2] or "").split())
        if not program_name or not re.search(re.escape(program_name), capture_text, re.I):
            raise RuntimeError(
                f"{spec.key}_partner_email_does_not_name_the_programme:{row_num}:{program_name}")
        haystack = f"{actual[5]} {actual[14]} {actual[23]}"
        if not pattern.search(haystack):
            raise RuntimeError(
                f"{spec.key}_row_does_not_name_the_programme:{row_num}:{program_id}")
        return
    # An account-level approval has no merchant/campaign id: `network-account`
    # is our canonical synthetic id, and restored parent rows predate it. Bind
    # that one case by the immutable parent identity instead. This remains as
    # strict as the child-program path: exact canonical row + exact Program cell
    # + the provider capture naming that network. No ordinary programme or
    # noncanonical row can enter this branch.
    if (spec.key == "approval_confirmed" and program_id == "network-account"
            and REQUIRED_MARKETPLACE_PARENT_ROWS.get(str(entry.get("program") or "")) == row_num
            and str(actual[2]).strip() == str(entry.get("program") or "").strip()):
        program_name = str(entry["program"]).strip()
        if not re.search(re.escape(program_name), capture_text, re.I):
            raise RuntimeError(
                f"{spec.key}_capture_does_not_name_the_parent_network:{row_num}:{program_name}")
        return
    if not pattern.search(capture_text):
        raise RuntimeError(
            f"{spec.key}_capture_does_not_name_the_programme:{row_num}:{program_id}")
    haystack = f"{actual[5]} {actual[14]} {actual[23]}"
    if not pattern.search(haystack):
        raise RuntimeError(
            f"{spec.key}_row_does_not_name_the_programme:{row_num}:{program_id}")


def deployed_live_tuple(entry: dict[str, Any]) -> dict[str, Any]:
    """Validate the full provider × rail tuple retained by deployed_live."""
    evidence = evidence_of(entry)
    network_raw = str(entry.get("network") or "").strip()
    network = canonical_network(network_raw)
    provider_program_id = str(entry.get("provider_program_id") or "").strip()
    program = str(entry.get("program") or "").strip()
    provider_slug = str(entry.get("provider_slug") or "").strip()
    stable_key = str(entry.get("stable_key") or "").strip()
    row_num = entry.get("row")
    if (
        not isinstance(row_num, int)
        or isinstance(row_num, bool)
        or row_num < 2
        or not network
        or not provider_program_id
        or not program
        or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,127}", provider_slug)
        or stable_key != f"{network}:{provider_program_id}"
    ):
        raise RuntimeError("deployed_live_tuple_shape_invalid")
    registration = {
        "provider": provider_slug,
        "network": network_raw,
        "program_name": program,
        "program_id": provider_program_id,
        "sheet_row": row_num,
    }
    tuple_value = {
        **registration,
        "network": network,
        "stable_key": stable_key,
    }
    digest = lambda value: hashlib.sha256(  # noqa: E731
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    expected = {
        "network": network,
        "network_program_id": provider_program_id,
        "program_name": program,
        "provider_slug": provider_slug,
        "sheet_row": row_num,
        "stable_key": stable_key,
        "tuple_sha256": digest(tuple_value),
        "program_registration_sha256": digest(registration),
    }
    if str(entry.get("tuple_sha256") or "") != expected["tuple_sha256"]:
        raise RuntimeError("deployed_live_tuple_sha256_mismatch")
    for key, wanted in expected.items():
        actual: Any = evidence.get(key)
        if key == "sheet_row":
            try:
                actual = int(actual)
            except (TypeError, ValueError):
                pass
        if actual != wanted:
            raise RuntimeError(f"deployed_live_evidence_tuple_mismatch:{key}")
    return {**tuple_value, "tuple_sha256": expected["tuple_sha256"]}


def require_deployed_live_tuple_binding(
    entry: dict[str, Any], row_num: int, actual: list[Any]
) -> None:
    binding = deployed_live_tuple(entry)
    if canonical_network(actual[4]) != binding["network"]:
        raise RuntimeError(
            f"deployed_live_network_contradicts_sheet:{row_num}:"
            f"{binding['network']}!={canonical_network(actual[4])}"
        )
    if not provider_program_identity_axes(
        actual, binding["network"], binding["program_id"]
    ):
        raise RuntimeError(
            f"deployed_live_program_id_contradicts_sheet:{row_num}:"
            f"{binding['program_id']}"
        )
    if norm(actual[9]) != norm(binding["provider"]):
        raise RuntimeError(
            f"deployed_live_provider_slug_contradicts_sheet:{row_num}:"
            f"{binding['provider']}!={str(actual[9]).strip()}"
        )


# --- pause -> reactivation lifecycle -----------------------------------------
# 2026-08-19 (PHASE0-BACKLOG-LEDGER §E). Katalys paused three LIVE, earning
# programmes on 2026-08-17 - Breeze Meds (1518), Care Bare Rx GLP1 (1519),
# Synergy Rx (1520) - and RE-ACTIVATED all three on 2026-08-19. The pause was
# recorded correctly by `network_paused_disposition`. The reactivation could not
# be recorded at all: no mode in this file may act FROM
# `paused_network_program_paused`, so three approved programmes stayed parked in
# a state that says "the network stopped this" while the network had already
# restarted it. That is money-losing in the one direction that matters - the
# deploy machine skips paused rows, so the site never restores the placements.
#
# Two facts shape this mode, both proven on the live mails:
#
#   1. **The wording is "activated", never "reactivated."** A read-only probe of
#      ops@ for `reactivat|resumed|unpaused|reinstated` scored ZERO hits while
#      all three notices sat in the mailbox ("Advertiser BreezeMeds has
#      Activated Their Program Breeze Meds!"). Any detector keyed on
#      "reactivat*" is structurally blind, so the accepted vocabulary leads with
#      the word the networks actually use.
#   2. **The notice does not carry the programme id.** Katalys names the
#      ADVERTISER and the PROGRAMME by name and nothing else, so the
#      id-in-the-capture binding `approval_confirmed` uses cannot be satisfied
#      here. The binding is by programme NAME instead: the capture's own bytes
#      must contain the target row's Program cell verbatim.
#
# What it may say is deliberately the weakest true sentence: the programme is
# approved again and NOT live. It writes 19=FALSE/20=FALSE, so restoring the
# site surfaces stays the deploy machine's job behind its own receipt, and the
# Link-QA cell states that the retained tracking link needs re-validation before
# publish - it does not claim the link was re-tested, because nothing tested it.
NETWORK_REACTIVATION_SOURCE_STATUS = frozenset({"paused_network_program_paused"})
#: Which approved status is written is DETERMINED by the row's own tracking-link
#: cell, never chosen by the caller: a paused row that kept its provider-issued
#: link comes back as link-ready, one without a link comes back as approved-only.
NETWORK_REACTIVATION_STATUS_WITH_LINK = "approved_active_tracking_ready"
NETWORK_REACTIVATION_STATUS_WITHOUT_LINK = "approved_active_not_wired"
#: The pause set `link_retired_network_program_paused`. That is false the moment
#: the programme is active again, and `not_clicked_preserve_attribution` would
#: be a different lie (it asserts a link we know is good). This says exactly what
#: is true: the link is retained and unproven until someone re-validates it.
NETWORK_REACTIVATION_LINK_QA = "link_revalidation_required_after_reactivation"
#: The words a network actually uses when a paused programme comes back. Ordered
#: with the PROVEN one first; `reactivated` is kept because other networks do use
#: it, but a detector that accepts only `reactivated` is the documented blindness.
NETWORK_REACTIVATION_WORDS = (
    "activated", "active", "reactivated", "resumed", "unpaused", "reinstated",
)

# --- portal readback completion ----------------------------------------------
# 2026-08-19 (PHASE0-BACKLOG-LEDGER §I). Four rows sat in states that say "a
# readback is required" or "we never applied" while the readback had ALREADY been
# done and had answered. Nothing could record a COMPLETED readback, and two of
# the answers had no vocabulary at all:
#
#   iHealth Labs (row 80)   `approval_email_received_provider_readback_required`
#                           -> GoAffPro API says approved + ACTIVE, affiliate id
#                              20591328, live ref link. A completion path simply
#                              did not exist.
#   Renpho (row 76)         the account holds ten Renpho store registrations, and
#                           per-store APPROVAL is not exposed by any endpoint.
#                           "We are a member, and whether they approved us is
#                           unproven" was unsayable - so the row said
#                           `ready_to_apply`, which is flatly false.
#   Digistore24 (row 358)   `approved_marketplace_access_ui_auth_gated` is stale:
#                           the API authenticates fine FROM THIS HOST; the real
#                           constraint is that the key is IP-locked, which is why
#                           n8n-cloud runs fail. "UI auth gated" sends the fleet
#                           looking for a login wall that does not exist.
#   SpeedyHealth (row 349)  HealthTrader has no per-offer relationship model at
#                           all; every offer is available via the approved network
#                           account. The row claimed a pending relationship that
#                           the network cannot even represent.
#
# The verdict the readback returned DETERMINES the status - the caller may not
# choose one - and each verdict may only be applied from the states that verdict
# can honestly resolve.
#: The readback proved the provider approved us. Deliberately the weakest
#: approval sentence, and reachable ONLY from the readback-required state.
PORTAL_READBACK_STATUS_APPROVED = "approved_active_not_wired"
#: Membership is proven, approval is NOT. The string deliberately does NOT start
#: with an approval prefix (`SHEET_SYNC_APPROVAL_CLAIM_PREFIXES`), so no wire-in,
#: deploy or live lane can mistake it for an approval.
PORTAL_READBACK_STATUS_MEMBERSHIP = "membership_confirmed_approval_unproven"
#: We hold the marketplace ACCOUNT and the API works - from the licensed host
#: only. Keeps the rail's existing `approved_marketplace_access` prefix because
#: the account claim is true; adds the constraint that explains cloud failures.
PORTAL_READBACK_STATUS_IP_LOCKED = "approved_marketplace_access_api_ip_locked_to_host"
#: The network exposes no per-offer relationship: the offer is usable through the
#: approved network account. Not an approval of the OFFER, so no approval prefix.
PORTAL_READBACK_STATUS_NETWORK_ACCOUNT = "available_via_approved_network_account"
PORTAL_READBACK_VERDICT_STATUS = {
    "approved_active": PORTAL_READBACK_STATUS_APPROVED,
    "membership_confirmed_approval_unproven": PORTAL_READBACK_STATUS_MEMBERSHIP,
    "api_ip_locked_to_host": PORTAL_READBACK_STATUS_IP_LOCKED,
    "available_via_approved_network_account": PORTAL_READBACK_STATUS_NETWORK_ACCOUNT,
}
#: Per-verdict source states. `approved_active` is the tightest of the four
#: because it is the only one that writes an approval: it may ONLY resolve a row
#: that already says a readback is what is missing. An ordinary pending row is
#: `approval_confirmed`'s business, with its stricter id-level binding.
PORTAL_READBACK_VERDICT_SOURCE_STATUS = {
    "approved_active": frozenset({
        "approval_email_received_provider_readback_required",
    }),
    "membership_confirmed_approval_unproven": frozenset({
        "approval_email_received_provider_readback_required",
        "ready_to_apply",
        "applied_pending_brand_approval",
    }),
    "api_ip_locked_to_host": frozenset({
        "approved_marketplace_access_ui_auth_gated",
        "approved_marketplace_access",
        "blocked_operator_action_required",
    }),
    "available_via_approved_network_account": frozenset({
        "provider_support_referral_url_unavailable_relationship_pending",
        "applied_pending_brand_approval",
        "ready_to_apply",
    }),
}
#: Evidence a particular verdict cannot be honest without. An IP-lock claim that
#: does not name the host it is locked to explains nothing.
PORTAL_READBACK_VERDICT_REQUIRED_EVIDENCE = {
    "api_ip_locked_to_host": ("api_ip_lock_host",),
    "membership_confirmed_approval_unproven": ("membership_basis",),
}

# --- operator notes intake ----------------------------------------------------
# 2026-08-19 (PHASE0-BACKLOG-LEDGER §J). The operator works the TO-JOIN view and
# writes ground truth into the Notes/Blocker cell - "Not a fit - didn't apply",
# "Application submitted - pending approval", "account made, approved, awaiting
# their tax verification". Thirty-three such notes existed and the machine read
# NONE of them: the column is write-preserved and consumed by nothing, so the
# canonical Status column kept contradicting the operator indefinitely.
#
# The ruling the operator wrote DETERMINES the status. What makes that safe
# without a portal capture is `require_operator_note_quote`: the mode may only
# assert a ruling whose verbatim words are IN THE LIVE CELL at write time,
# re-read off the Sheet inside the write path. A caller cannot invent an operator
# ruling, because the proof is the Sheet's own bytes.
OPERATOR_RULING_STATUS = {
    "not_a_fit_did_not_apply": "not_fit_operator_ruled",
    "application_submitted": "applied_pending_brand_approval",
    "account_approved_pending_network_tax_verification":
        "approved_pending_network_tax_verification",
}
OPERATOR_RULING_SOURCE_STATUS = {
    "not_a_fit_did_not_apply": frozenset({
        "candidate_not_applied", "ready_to_apply", "blocked_operator_action_required",
    }),
    "application_submitted": frozenset({
        "blocked_operator_action_required", "ready_to_apply", "candidate_not_applied",
        # The state that says "this application needs the operator's legal
        # identity / address / tax context". It is precisely the state an
        # operator resolves BY submitting, and MaxBounty (row 470) sat in it
        # while the operator's note read "Application submitted".
        "operator_identity_address_tax_required",
    }),
    "account_approved_pending_network_tax_verification": frozenset({
        "blocked_operator_action_required", "applied_pending_brand_approval",
        "operator_identity_address_tax_required",
    }),
}

#: --- secret scrub -------------------------------------------------------------
#: TWO Sheet cells held LIVE API CREDENTIALS in plain text (Rakuten row 7,
#: Admitad row 433) because the operator pasted them where the machine asked for
#: them. Moving them to `~/.hermes/secrets/` is only half the fix; the cell has
#: to stop holding them, and the ONLY writer allowed to change a cell is this
#: file. No mode could rewrite Notes/Blocker without also making a status claim.
#:
#: This is the narrowest possible closure: Notes + Last Checked, nothing else,
#: no status, no flags, no link. Its guards make it useless for anything but a
#: scrub - the replacement text must match the scrub template, must be short, and
#: must not itself contain a secret-shaped token; the transfer receipt must name
#: this row and this program; and the receipt's `note_sha256_before` must match
#: the bytes actually in the cell, so a scrub written against one cell can never
#: erase a different one.
SECRET_SCRUB_NOTE_MAX_LENGTH = 400
SECRET_SCRUB_REQUIRED_PHRASES = ("credentials stored", "secrets store")
#: A run of 24+ URL-safe characters is what an API key, a client secret or a
#: base64 header looks like. Cited evidence values are removed before the scan,
#: so a receipt path is not mistaken for a key.
SECRET_SHAPED_TOKEN = re.compile(r"[A-Za-z0-9_\-]{24,}")


def require_secret_scrub_note(spec: "SheetSyncModeSpec", entry: dict[str, Any],
                              note: str) -> None:
    """The replacement note must be a scrub, and must not carry a secret itself."""
    lowered = note.casefold()
    for phrase in SECRET_SCRUB_REQUIRED_PHRASES:
        if phrase not in lowered:
            raise RuntimeError(
                f"{spec.key}_note_is_not_a_scrub:{entry.get('program')}:{phrase}")
    if len(note) > SECRET_SCRUB_NOTE_MAX_LENGTH:
        raise RuntimeError(f"{spec.key}_note_too_long:{entry.get('program')}:{len(note)}")
    residue = note
    for field, _kind in spec.evidence_fields:
        value = _sheet_sync_evidence_value(entry.get("evidence") or {}, field)
        if value:
            residue = residue.replace(value, " ")
    leaked = SECRET_SHAPED_TOKEN.findall(residue)
    if leaked:
        # The token itself is NEVER printed - only its length and position.
        raise RuntimeError(
            f"{spec.key}_note_still_carries_a_secret_shaped_token:"
            f"{entry.get('program')}:count={len(leaked)}:len={len(leaked[0])}")


def require_secret_transfer_receipt(
    spec: "SheetSyncModeSpec", entry: dict[str, Any], row_num: int, actual: list[Any],
) -> None:
    """Bind the scrub to the exact cell whose secret was moved to the store.

    The receipt is written by the intake lane BEFORE the queue row exists and
    records what it moved, where to, and the sha256 of the note it read. Here we
    re-hash the cell that is actually on the Sheet: if it does not match, either
    the cell changed since (so the scrub would destroy something nobody has read)
    or the receipt is about a different cell entirely. Both are refusals.

    A replay is not a refusal: once the scrub has been applied the cell holds the
    planned note, which is proof the work is done.
    """
    evidence = entry["evidence"]
    planned_note = str(entry["cells"].get("23", entry["cells"].get(23)) or "").strip()
    current_note = str(actual[23] or "").strip()
    receipt_rel = _sheet_sync_evidence_value(evidence, "secret_transfer_receipt_path")
    receipt_path = _REPO_ROOT / receipt_rel
    try:
        receipt = json.loads(receipt_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{spec.key}_receipt_unreadable:{row_num}:{receipt_rel}") from exc
    if int(receipt.get("sheet_row") or 0) != row_num:
        raise RuntimeError(
            f"{spec.key}_receipt_is_about_another_row:{row_num}:{receipt.get('sheet_row')}")
    if norm(receipt.get("program")) != norm(actual[2]):
        raise RuntimeError(f"{spec.key}_receipt_is_about_another_program:{row_num}")
    declared = str(receipt.get("note_sha256_before") or "").strip()
    if not re.fullmatch(r"[a-f0-9]{64}", declared):
        raise RuntimeError(f"{spec.key}_receipt_note_hash_invalid:{row_num}")
    if _sheet_sync_evidence_value(evidence, "source_note_sha256") != declared:
        raise RuntimeError(f"{spec.key}_receipt_note_hash_contradicts_evidence:{row_num}")
    if current_note == planned_note:
        return  # already scrubbed - a replay, not a refusal
    if hashlib.sha256(current_note.encode("utf-8")).hexdigest() != declared:
        raise RuntimeError(
            f"{spec.key}_sheet_note_changed_since_the_transfer:{row_num}")
    # A scrub that does not actually remove anything is a no-op the operator
    # would read as "the secret is gone". Refuse it rather than lie.
    if not SECRET_SHAPED_TOKEN.search(current_note):
        raise RuntimeError(f"{spec.key}_cell_holds_no_secret_shaped_token:{row_num}")


def require_operator_ruling_capture(
    spec: "SheetSyncModeSpec", entry: dict[str, Any], row_num: int, actual: list[Any],
) -> None:
    """Prove the quoted ruling is really the operator's, about really this row.

    The operator writes on the `TO JOIN` worklist tab, which is a derived VIEW
    rebuilt on every run - so the words are not on the canonical row at all and
    cannot be re-read from it. The attestation capture is therefore the binding:
    it is written by the intake lane at harvest time, hash-matched by the generic
    evidence gate before this runs, and it must itself declare WHICH canonical row
    and WHICH programme the note was written against. All three have to agree -
    the capture's row, the capture's programme, and the row we are about to
    write - or the ruling is refused. Without this a valid attestation for one
    network could stamp its ruling onto any other row.
    """
    evidence = entry["evidence"]
    quote = " ".join(_sheet_sync_evidence_value(evidence, "operator_note_quote").split())
    if len(quote) < 8:
        raise RuntimeError(f"{spec.key}_operator_quote_too_short:{row_num}")
    capture_rel = _sheet_sync_evidence_value(evidence, "ruling_capture_path")
    capture = _REPO_ROOT / capture_rel
    try:
        payload = json.loads(capture.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{spec.key}_ruling_capture_unreadable:{row_num}") from exc
    if int(payload.get("canonical_row") or 0) != row_num:
        raise RuntimeError(
            f"{spec.key}_ruling_capture_is_about_another_row:"
            f"{row_num}:{payload.get('canonical_row')}")
    if norm(payload.get("program")) != norm(actual[2]):
        raise RuntimeError(f"{spec.key}_ruling_capture_is_about_another_program:{row_num}")
    captured_quote = " ".join(str(payload.get("operator_note") or "").split())
    if quote.casefold() not in captured_quote.casefold():
        raise RuntimeError(
            f"{spec.key}_quote_is_not_in_the_attested_note:{row_num}")
    declared = str(payload.get("operator_ruling") or "").strip()
    if declared and declared != _sheet_sync_evidence_value(evidence, "operator_ruling"):
        raise RuntimeError(
            f"{spec.key}_ruling_contradicts_the_capture:{row_num}:{declared}")


def require_program_name_binding(
    spec: "SheetSyncModeSpec", entry: dict[str, Any], row_num: int, actual: list[Any],
) -> None:
    """Prove the cited capture is about the programme in THIS row, by name.

    The id-level binding (`require_program_identity_binding`) is stronger and is
    used wherever the provider hands us an id. Two whole classes of evidence do
    not carry one - a Katalys activation notice names the advertiser and the
    programme, a GoAffPro/portal readback is scoped to an account - and refusing
    them forever would mean the Sheet can never record a fact the provider only
    ever states by name. So the capture's own bytes must contain the target row's
    Program cell, case-insensitively and whitespace-normalised, and the row's
    Program cell is itself already pinned to the queue row's program name by
    `require_sheet_sync_preconditions`.
    """
    program = " ".join(str(actual[2] or "").split())
    if not program:
        raise RuntimeError(f"{spec.key}_row_has_no_program_name:{row_num}")
    capture = _REPO_ROOT / _sheet_sync_evidence_value(evidence_of(entry),
                                                      spec.identity_capture_field)
    if not capture.is_file():
        raise RuntimeError(f"{spec.key}_capture_missing:{row_num}:{capture}")
    haystack = " ".join(capture.read_text(errors="replace").split()).casefold()
    if program.casefold() not in haystack:
        raise RuntimeError(
            f"{spec.key}_capture_does_not_name_the_programme:{row_num}:{program!r}")


def evidence_of(entry: dict[str, Any]) -> dict[str, Any]:
    value = entry.get("evidence")
    return value if isinstance(value, dict) else {}


#: Portal relationship -> the ONE status a correction may write for it. A
#: correction is a transcription of what the portal says; it may not editorialise.
STATUS_CORRECTION_RELATIONSHIP_STATUS = {
    "pending": "applied_pending_brand_approval",
    "not_related": "not_related_portal_no_relationship",
    "rejected": "rejected_network_declined_application",
    "invited": "invited_pending_acceptance",
}

# MaxBounty relationship truth is exposed by either an exact campaign-detail
# action button or the authenticated campaign API's relationship status. The
# inventory writer combines that provider evidence with researched fit; campaign
# names and historical allowlists never choose a status.
MAXBOUNTY_INVENTORY_RELATIONSHIP = {
    "Build tracking link": "approved",
    "Request approval to run": "application needed",
    "Request under review": "pending",
    "Request denied": "denied",
    "Request capacity blocked": "provider capacity blocked",
}
MAXBOUNTY_INVENTORY_STATUS = {
    ("Build tracking link", "core_glp1"): "approved_active_not_wired",
    ("Request approval to run", "core_glp1"): "application_gated_site_redesign_and_ad_image",
    ("Request under review", "core_glp1"): "applied_pending_brand_approval",
    ("Request denied", "core_glp1"): "rejected_network_declined_application",
    ("Request capacity blocked", "core_glp1"): "provider_capacity_blocked_no_attempt",
    **{(evidence, "sister_vertical"): "hold_sister_site_not_live"
       for evidence in MAXBOUNTY_INVENTORY_RELATIONSHIP},
    **{(evidence, "out_of_scope"): "hold_out_of_scope"
       for evidence in MAXBOUNTY_INVENTORY_RELATIONSHIP},
}
MAXBOUNTY_INVENTORY_PRESERVED_STATUS = frozenset({
    "approved_active_tracking_ready",
})
MAXBOUNTY_INVENTORY_SOURCE_STATUS = frozenset({
    "", "approved_active_not_wired", "approved_active_tracking_ready",
    "applied_pending_brand_approval", "not_related_portal_no_relationship",
    "invited_pending_acceptance", "application_gated_site_redesign_and_ad_image",
    "hold_sister_site_not_live", "hold_out_of_scope",
    "rejected_network_declined_application", "provider_capacity_blocked_no_attempt",
})

# Affiliati's authenticated CAKE feed exposes exact offer identity, relationship,
# economics, conversion event and media eligibility. Inventory is still link-dark:
# enumeration is not an application, and no campaign or tracking code exists until
# an explicitly authorised later batch creates it.
AFFILIATI_INVENTORY_STATUS = {
    "notjoined": "not_applied",
    "pending": "applied_pending_brand_approval",
    "approved": "approved_active_not_wired",
}
AFFILIATI_INVENTORY_SOURCE_STATUS = frozenset({
    "", "not_applied", "applied_pending_brand_approval",
    "approved_active_not_wired", "not_related_portal_no_relationship",
})
AFFILIATI_SWITCH_STATUS = {
    "MEDVI": "approved_affiliati_tracking_link_required",
    "Direct Meds": "awaiting_network_approval",
    "JRNYS": "approved_affiliati_tracking_link_required",
}
AFFILIATI_SWITCH_SOURCE_STATUS = frozenset({
    "", "not_applied", "approved_affiliati_tracking_link_required",
    "awaiting_network_approval",
})
HIGHER_RAIL_PENDING_STATUS = "approved_pending_retirement_higher_rail"


class SheetSyncModeSpec:
    """Declarative contract for one queue-drained narrow writer mode."""

    def __init__(
        self,
        key: str,
        plan_schema: str,
        write_schema: str,
        mutable_columns: tuple[int, ...],
        required_columns: tuple[int, ...],
        allowed_status: frozenset[str],
        required_flags: dict[int, str],
        evidence_fields: tuple[tuple[str, str], ...],
        note_must_cite: tuple[str, ...],
        source_status: frozenset[str] | None = None,
        forbidden_source_status: frozenset[str] = frozenset(),
        forbid_source_approval_claim: bool = False,
        require_source_approval_claim: bool = False,
        allowed_link_qa: frozenset[str] | None = None,
        require_tracking_link: bool | None = None,
        forbid_wired_live_source: bool = True,
        may_create: bool = False,
        create_required_columns: tuple[int, ...] = (),
        link_qa_from_removal_receipt: bool = False,
        require_marketplace_rail_row: bool = False,
        require_marketplace_parent_row: bool = False,
        require_blank_source_status: bool = False,
        blank_source_exempt_status: frozenset = frozenset(),
        require_fit_assessment_cells: bool = False,
        require_existing_live_placement: bool = False,
        require_program_identity_binding: bool = False,
        identity_capture_field: str = "",
        parent_authorized: bool = False,
        parent_authorized_on_own_canonical_row: bool = False,
        cells_from_evidence: dict[int, str] | None = None,
        require_marketplace_candidate_row: bool = False,
        verdict_field: str = "",
        verdict_status: dict[str, str] | None = None,
        verdict_source_status: dict[str, frozenset[str]] | None = None,
        verdict_required_evidence: dict[str, tuple[str, ...]] | None = None,
        status_follows_tracking_link: tuple[str, str] | None = None,
        require_program_name_binding: bool = False,
        require_operator_note_quote: bool = False,
        note_scrub: bool = False,
        require_link_rail_validation: bool = False,
        description: str = "",
    ) -> None:
        self.key = key
        self.plan_schema = plan_schema
        self.write_schema = write_schema
        self.mutable_columns = mutable_columns
        self.required_columns = required_columns
        self.allowed_status = allowed_status
        self.required_flags = required_flags
        self.evidence_fields = evidence_fields
        self.note_must_cite = note_must_cite
        self.source_status = source_status
        self.forbidden_source_status = forbidden_source_status
        self.forbid_source_approval_claim = forbid_source_approval_claim
        self.require_source_approval_claim = require_source_approval_claim
        self.allowed_link_qa = allowed_link_qa
        self.require_tracking_link = require_tracking_link
        self.forbid_wired_live_source = forbid_wired_live_source
        self.may_create = may_create
        self.create_required_columns = create_required_columns
        self.link_qa_from_removal_receipt = link_qa_from_removal_receipt
        self.require_marketplace_rail_row = require_marketplace_rail_row
        self.require_marketplace_parent_row = require_marketplace_parent_row
        self.require_blank_source_status = require_blank_source_status
        #: Statuses that satisfy `require_blank_source_status` without being
        #: blank. Empty by default, so the guard is unchanged for every mode
        #: that does not opt in.
        self.blank_source_exempt_status = frozenset(blank_source_exempt_status)
        self.require_fit_assessment_cells = require_fit_assessment_cells
        self.require_existing_live_placement = require_existing_live_placement
        self.require_program_identity_binding = require_program_identity_binding
        self.identity_capture_field = identity_capture_field
        self.parent_authorized = parent_authorized
        #: Bridge-authorized 2026-08-20 for `portal_readback_confirmed` and
        #: `operator_status_ruling`, then operator-authorized 2026-08-21 for
        #: `approval_confirmed`. Unlike `parent_authorized` (which
        #: exempts every row in a plan, safe because that mode's own
        #: precondition proves every one of them is a canonical parent), this is
        #: evaluated PER ROW: a planned row is exempt only when it is that
        #: marketplace's canonical parent row in
        #: `REQUIRED_MARKETPLACE_PARENT_ROWS`. Every other row in the same batch
        #: goes through the unchanged guard.
        self.parent_authorized_on_own_canonical_row = parent_authorized_on_own_canonical_row
        #: {column: evidence_field}. The cell must equal the evidence value
        #: verbatim. Used where the cell's ENTIRE content is a value read off a
        #: provider surface, so a valid capture cannot be paired with a number
        #: nobody fetched.
        self.cells_from_evidence = dict(cells_from_evidence or {})
        self.require_marketplace_candidate_row = require_marketplace_candidate_row
        #: An evidence field whose value DETERMINES both the status written and
        #: the states the mode may act from. A mode that declares one can never
        #: let the caller pick the sentence: the verdict picks it.
        self.verdict_field = verdict_field
        self.verdict_status = dict(verdict_status or {})
        self.verdict_source_status = dict(verdict_source_status or {})
        self.verdict_required_evidence = dict(verdict_required_evidence or {})
        #: (status_when_the_row_holds_a_tracking_link, status_when_it_does_not).
        #: Read off the live Sheet, so the caller cannot choose either.
        self.status_follows_tracking_link = status_follows_tracking_link
        self.require_program_name_binding = require_program_name_binding
        self.require_operator_note_quote = require_operator_note_quote
        self.note_scrub = note_scrub
        #: Prove, structurally and without fetching anything, that the row's own
        #: tracking link is a genuine link on the rail the row names. A mode that
        #: flips `Wire-In Ready?` TRUE is asserting the builder can build from
        #: this link; asserting that without checking the link was exactly the
        #: hole the Katalys-only resolver papered over.
        self.require_link_rail_validation = require_link_rail_validation
        #: A mode that does not carry column 10 makes no status claim at all -
        #: it may neither write nor be steered by one.
        self.writes_status = 10 in mutable_columns
        self.description = description


SHEET_SYNC_MODES: dict[str, SheetSyncModeSpec] = {
    "network_cancelled": SheetSyncModeSpec(
        key="network_cancelled",
        plan_schema="affiliate-network-cancelled-plan-v1",
        write_schema="affiliate-network-cancelled-write-v1",
        mutable_columns=(10, 19, 20, 21, 23, 24),
        required_columns=(10, 19, 20, 21, 23, 24),
        allowed_status=frozenset({"cancelled_by_network"}),
        required_flags={19: "FALSE", 20: "FALSE"},
        evidence_fields=(("path", "repo_path"), ("event_id", "text"),
                         ("event_at_utc", "timestamp")),
        note_must_cite=("path", "event_id", "event_at_utc"),
        allowed_link_qa=frozenset({"link_retired_network_program_cancelled"}),
        forbid_wired_live_source=False,
        description="Record network cancellation; retract ready/live claims, retain tracking history.",
    ),
    "maxbounty_identity_category_repair": SheetSyncModeSpec(
        key="maxbounty_identity_category_repair",
        plan_schema="affiliate-maxbounty-identity-category-repair-plan-v1",
        write_schema="affiliate-maxbounty-identity-category-repair-write-v1",
        mutable_columns=(3, 5), required_columns=(3, 5),
        allowed_status=frozenset(), required_flags={},
        evidence_fields=(("metadata_manifest_path", "repo_path"),
                         ("metadata_manifest_sha256", "sha256_of:metadata_manifest_path")),
        note_must_cite=(), source_status=provider_metadata.SLUG_APPROVED_STATUSES | {"approved", "applied_pending_brand_approval", "hold_sister_site_not_live"},
        description="Hash-bound approved MaxBounty identity/category CAS; held BistroMD exception is row-800-only in metadata validator.",
    ),
    "provider_slug_correction": SheetSyncModeSpec(
        key="provider_slug_correction",
        plan_schema="affiliate-provider-slug-correction-plan-v1",
        write_schema="affiliate-provider-slug-correction-write-v1",
        mutable_columns=provider_metadata.SLUG_COLUMNS,
        required_columns=provider_metadata.SLUG_COLUMNS,
        allowed_status=frozenset(), required_flags={},
        evidence_fields=(("metadata_manifest_path", "repo_path"),
                         ("metadata_manifest_sha256", "sha256_of:metadata_manifest_path")),
        note_must_cite=(), source_status=provider_metadata.SLUG_APPROVED_STATUSES,
        forbid_wired_live_source=False,
        description="Q57 exact Coivas identity and literal nonblank slug CAS; column 9 only.",
    ),
    "provider_fit_reclassification": SheetSyncModeSpec(
        key="provider_fit_reclassification",
        plan_schema="affiliate-provider-fit-reclassification-plan-v1",
        write_schema="affiliate-provider-fit-reclassification-write-v1",
        mutable_columns=provider_metadata.FIT_COLUMNS,
        required_columns=provider_metadata.FIT_COLUMNS,
        allowed_status=frozenset({"hold_sister_site_not_live", "hold_out_of_scope"}), required_flags={},
        evidence_fields=(("metadata_manifest_path", "repo_path"),
                         ("metadata_manifest_sha256", "sha256_of:metadata_manifest_path")),
        note_must_cite=(), forbidden_source_status=SHEET_SYNC_LIVE_STATUS,
        description="Catalogue-bound fit hold and vertical only, with checked/append-only notes; never tracking or flags.",
    ),
    "provider_slug_provisioning": SheetSyncModeSpec(
        key="provider_slug_provisioning",
        plan_schema="affiliate-provider-slug-provisioning-plan-v1",
        write_schema="affiliate-provider-slug-provisioning-write-v1",
        mutable_columns=provider_metadata.SLUG_COLUMNS,
        required_columns=provider_metadata.SLUG_COLUMNS,
        allowed_status=frozenset(), required_flags={},
        evidence_fields=(("metadata_manifest_path", "repo_path"),
                         ("metadata_manifest_sha256", "sha256_of:metadata_manifest_path")),
        note_must_cite=(), source_status=provider_metadata.SLUG_APPROVED_STATUSES,
        forbid_wired_live_source=False,
        description="Blank approved in-scope provider slug only; independent of link and ready gate.",
    ),
    # --- application_pending --------------------------------------------------
    # Records that an application was SUBMITTED. It is the weakest claim in the
    # file: it may never write an approval, never claim wire-ready or live, and
    # never act on a row that already claims an approval (that would be a silent
    # downgrade of real money). It is the only engine mode allowed to create a
    # row, because an application to a program we have never tracked is exactly
    # the case the applications machine produces.
    "application_pending": SheetSyncModeSpec(
        key="application_pending",
        plan_schema="affiliate-application-pending-plan-v1",
        write_schema="affiliate-application-pending-write-v1",
        mutable_columns=(10, 11, 19, 20, 21, 23, 24),
        required_columns=(10, 11, 19, 20, 23, 24),
        allowed_status=frozenset({"applied_pending_brand_approval"}),
        required_flags={11: "TRUE", 19: "FALSE", 20: "FALSE"},
        evidence_fields=(
            ("submission_receipt_path", "repo_path"),
            ("submitted_at_utc", "timestamp"),
        ),
        note_must_cite=("submission_receipt_path", "submitted_at_utc"),
        forbid_source_approval_claim=True,
        may_create=True,
        create_required_columns=(1, 2, 3, 4, 6, 7, 10, 11, 19, 20, 23, 24, 25),
        description="Mark an application as submitted and awaiting the brand's decision.",
    ),
    # --- rejection ------------------------------------------------------------
    "rejection": SheetSyncModeSpec(
        key="rejection",
        plan_schema="affiliate-rejection-plan-v1",
        write_schema="affiliate-rejection-write-v1",
        mutable_columns=(10, 19, 20, 21, 23, 24),
        required_columns=(10, 19, 20, 23, 24),
        allowed_status=frozenset({
            "rejected_network_declined_application",
            "rejected_brand_declined_application",
        }),
        required_flags={19: "FALSE", 20: "FALSE"},
        evidence_fields=(
            ("rejection_evidence_path", "repo_path"),
            ("observed_at_utc", "timestamp"),
            ("source", "text"),
        ),
        note_must_cite=("rejection_evidence_path", "observed_at_utc"),
        forbidden_source_status=SHEET_SYNC_LIVE_STATUS,
        # 2026-08-18: Awin REJECTED MicroVitamin (122466) and the machinery could
        # not say so - the program had never reached the Sheet, and this mode
        # could not create a row. A rejection nobody can record is a program we
        # will cheerfully re-apply to next month, which is precisely the loop the
        # cross-network dedupe exists to break. The create path is the same
        # bargain `application_pending` strikes: it may only fill a blank row for
        # a program no existing row claims, the researched columns must be
        # SHIPPED as evidence rather than invented, and the row it writes is a
        # dead one - `rejected_*`, both live flags FALSE, no tracking link.
        may_create=True,
        create_required_columns=(1, 2, 3, 4, 6, 7, 10, 11, 19, 20, 23, 24, 25),
        description="Record a network/brand rejection. Never usable on a live row - "
                    "a live program that stops is a pause or a removal, not a rejection. "
                    "May create the row when the rejected program never reached the Sheet.",
    ),
    # Email custody is NOT a portal relationship capture. This mode only records
    # the exact retained advertiser/postback hold; it cannot create or release one.
    "partner_email_hold": SheetSyncModeSpec(
        key="partner_email_hold",
        plan_schema="affiliate-partner-email-hold-plan-v1",
        write_schema="affiliate-partner-email-hold-write-v1",
        mutable_columns=(10, 19, 23),
        required_columns=(10, 19, 23),
        allowed_status=frozenset({"hold_advertiser_postback_pending"}),
        required_flags={19: "FALSE"},
        evidence_fields=(("event_id", "text"), ("raw_sha256", "sha256_hex"),
                         ("received_at", "timestamp"), ("corrected_at_utc", "timestamp")),
        note_must_cite=("event_id", "raw_sha256", "received_at", "corrected_at_utc"),
        source_status=frozenset({"approved_active_tracking_ready"}),
        require_tracking_link=True,
        forbid_wired_live_source=True,
        description="Retract readiness for the exact retained XenTraffic 3277 email hold; "
                    "preserve all other cells and prior notes, including Last Checked UTC.",
    ),
    # --- status_correction ----------------------------------------------------
    # The class the 2026-08-18 backfill audit found five of: the Sheet claims an
    # approval the portal does not confirm. It is one-way (it can only retract a
    # claim), and the status it writes is DETERMINED by the cited portal
    # relationship - it may not choose.
    "status_correction": SheetSyncModeSpec(
        key="status_correction",
        plan_schema="affiliate-status-correction-plan-v1",
        write_schema="affiliate-status-correction-write-v1",
        mutable_columns=(10, 19, 20, 21, 23, 24),
        required_columns=(10, 19, 20, 23, 24),
        allowed_status=frozenset(STATUS_CORRECTION_RELATIONSHIP_STATUS.values()),
        required_flags={19: "FALSE", 20: "FALSE"},
        evidence_fields=(
            ("portal_capture_path", "repo_path"),
            ("portal_capture_sha256", "sha256_of:portal_capture_path"),
            ("captured_at_utc", "timestamp"),
            ("portal_relationship", "enum:pending|not_related|rejected|invited"),
        ),
        note_must_cite=("portal_capture_path", "captured_at_utc", "portal_relationship"),
        forbidden_source_status=SHEET_SYNC_LIVE_STATUS,
        require_source_approval_claim=True,
        description="Retract a Sheet approval claim the cited portal capture contradicts.",
    ),
    # --- deployed_live --------------------------------------------------------
    # The only engine mode that may talk a row UP, and it is bounded three ways:
    # the row must ALREADY be approved (approval truth still belongs to the
    # network adapters), it must already carry a provider-issued tracking link,
    # and the deploy receipt it cites must exist on disk and report sensor PASS.
    "deployed_live": SheetSyncModeSpec(
        key="deployed_live",
        plan_schema="affiliate-deployed-live-plan-v1",
        write_schema="affiliate-deployed-live-write-v1",
        mutable_columns=(10, 19, 20, 21, 23, 24),
        required_columns=(10, 19, 20, 23, 24),
        allowed_status=frozenset({"approved_active_wired_live", "live_wired_verified"}),
        required_flags={19: "TRUE", 20: "TRUE"},
        evidence_fields=(
            ("deploy_receipt_path", "repo_path"),
            ("live_url", "url"),
            ("sensor_status", "enum:PASS"),
            ("release", "text"),
            ("verified_at_utc", "timestamp"),
            ("network", "text"),
            ("network_program_id", "text"),
            ("program_name", "text"),
            ("provider_slug", "text"),
            ("sheet_row", "int_min1"),
            ("stable_key", "text"),
            ("tuple_sha256", "sha256_hex"),
            ("program_registration_sha256", "sha256_hex"),
            ("postback_configuration_receipt_path", "repo_path"),
            ("postback_configuration_receipt_sha256",
             "sha256_of:postback_configuration_receipt_path"),
            ("postback_readback_receipt_path", "repo_path"),
            ("postback_readback_receipt_sha256",
             "sha256_of:postback_readback_receipt_path"),
        ),
        note_must_cite=("deploy_receipt_path", "live_url", "release"),
        source_status=SHEET_SYNC_APPROVED_NOT_LIVE_STATUS,
        allowed_link_qa=frozenset({"not_clicked_preserve_attribution"}),
        require_tracking_link=True,
        forbid_wired_live_source=False,
        require_program_identity_binding=True,
        identity_capture_field="postback_configuration_receipt_path",
        description="Record that an approved, linked program is wired and live, "
                    "against a deploy receipt whose completeness sensor passed.",
    ),
    # --- removed --------------------------------------------------------------
    "removed": SheetSyncModeSpec(
        key="removed",
        plan_schema="affiliate-removed-plan-v1",
        write_schema="affiliate-removed-write-v1",
        mutable_columns=(10, 19, 20, 21, 23, 24),
        required_columns=(10, 19, 20, 23, 24),
        allowed_status=frozenset({"removed_from_site_surfaces_deleted"}),
        required_flags={19: "FALSE", 20: "FALSE"},
        evidence_fields=(
            ("removal_receipt_path", "repo_path"),
            ("release", "text"),
            ("removed_at_utc", "timestamp"),
        ),
        note_must_cite=("removal_receipt_path", "release", "removed_at_utc"),
        source_status=frozenset({
            "paused_network_program_paused",
            "live_wired_verified",
            "approved_active_wired_live",
        }),
        allowed_link_qa=frozenset({
            "link_retired_network_program_paused",
            "link_retired_niche_fit_removal",
            "not_clicked_preserve_attribution",
        }),
        forbid_wired_live_source=False,
        link_qa_from_removal_receipt=True,
        description="Record a receipted removal of the program's site surfaces. "
                    "Which retirement sentence the Link-QA cell tells is chosen by "
                    "the removal receipt's reason class, never by the caller.",
    ),
    # --- evidence_blocked -----------------------------------------------------
    # The gap the 2026-08-18 audit named: eMed is genuinely approved with a live
    # tracking link, but every first-party capture route returns HTTP 429, so no
    # review page can be built without fabricating evidence. Before this mode the
    # only sayable things were false (the approved-unlinked statuses all claim
    # either no link or no relationship), so the row silently kept claiming
    # approved-not-wired forever and nobody could see why nothing happened.
    "evidence_blocked": SheetSyncModeSpec(
        key="evidence_blocked",
        plan_schema="affiliate-evidence-blocked-plan-v1",
        write_schema="affiliate-evidence-blocked-write-v1",
        mutable_columns=(10, 19, 20, 21, 23, 24),
        required_columns=(10, 19, 20, 23, 24),
        allowed_status=frozenset({"approved_active_first_party_evidence_blocked"}),
        required_flags={19: "FALSE", 20: "FALSE"},
        evidence_fields=(
            ("capture_attempts_path", "repo_path"),
            ("blocker", "text"),
            ("attempts", "int_min1"),
            ("attempted_at_utc", "timestamp"),
        ),
        note_must_cite=("capture_attempts_path", "blocker", "attempted_at_utc"),
        source_status=SHEET_SYNC_APPROVED_NOT_LIVE_STATUS,
        description="Honest terminal-for-now state for an approval whose first-party "
                    "evidence capture is blocked, so nothing claims it is wired.",
    ),
    # --- marketplace_rail_status ----------------------------------------------
    # A marketplace RAIL row is an account we hold with a network, not a merchant
    # program: rows 4 (Awin), 5 (Partnerize), 778 (CJ) all carry
    # `approved_marketplace_access`. Row 82 (GoAffPro) carried a BLANK status
    # because no governed mode could write one - the blank-schema repair only
    # fills checkboxes and a blank Vertical, and every sheet-sync mode's mutable
    # set stopped at (10, 19, 20, 21, 23, 24) applied to PROGRAM rows.
    #
    # This mode is deliberately the narrowest thing that closes that gap: it may
    # write the Status column and nothing else, only on a row the sheet itself
    # identifies as a rail, and only when that Status is BLANK. It can therefore
    # never retract, downgrade or overwrite an existing claim of any kind.
    "marketplace_rail_status": SheetSyncModeSpec(
        key="marketplace_rail_status",
        plan_schema="affiliate-marketplace-rail-status-plan-v1",
        write_schema="affiliate-marketplace-rail-status-write-v1",
        mutable_columns=(10, 23, 24),
        required_columns=(10, 23, 24),
        allowed_status=frozenset({"approved_marketplace_access"}),
        required_flags={},
        evidence_fields=(
            ("rail_access_evidence_path", "repo_path"),
            ("rail_access_evidence_sha256", "sha256_of:rail_access_evidence_path"),
            ("observed_at_utc", "timestamp"),
            ("access_basis", "text"),
        ),
        note_must_cite=("rail_access_evidence_path", "observed_at_utc"),
        forbidden_source_status=SHEET_SYNC_LIVE_STATUS,
        require_marketplace_rail_row=True,
        require_blank_source_status=True,
        blank_source_exempt_status=MARKETPLACE_RAIL_OPERATOR_BLOCKED_SOURCE_STATUS,
        require_tracking_link=False,
        description="Record that we hold marketplace ACCOUNT access on a network rail "
                    "row whose Status is blank. Writes the Status column only.",
    ),
    # --- fit_assessment -------------------------------------------------------
    # The 2026-08-18 reconciler's UNKNOWN-FIT class: a row claims an approval and
    # nobody ever wrote down whether the program is a GLP Clinic Scout fit, so it
    # can be routed neither to deploy nor to suppression. No mode could write
    # Fit Reason (7) or Vertical (25) on an EXISTING row, so the finding was
    # literally unqueueable and a suppression-ledger entry does not clear it.
    #
    # This mode writes the fit assessment and NOTHING else - no status, no flags,
    # no link. It refuses on a live row, because a program that is live on the
    # site has already been decided in scope; re-deciding one is a removal, not a
    # note. The verdict and the Vertical must agree, so a row cannot say
    # "out of scope" in one cell and "glp1-weightloss" in another.
    "fit_assessment": SheetSyncModeSpec(
        key="fit_assessment",
        plan_schema="affiliate-fit-assessment-plan-v1",
        write_schema="affiliate-fit-assessment-write-v1",
        mutable_columns=(7, 23, 24, 25),
        required_columns=(7, 23, 24, 25),
        allowed_status=frozenset(),
        required_flags={},
        evidence_fields=(
            ("fit_assessment_path", "repo_path"),
            ("fit_assessment_sha256", "sha256_of:fit_assessment_path"),
            ("assessed_at_utc", "timestamp"),
            ("verdict", "enum:" + "|".join(FIT_ASSESSMENT_VERDICTS)),
            ("basis", "text"),
        ),
        note_must_cite=("fit_assessment_path", "assessed_at_utc", "verdict"),
        forbidden_source_status=SHEET_SYNC_LIVE_STATUS,
        require_fit_assessment_cells=True,
        description="Record an evidenced GLP-fit verdict (Fit Reason + Vertical) on a "
                    "row nobody ever assessed. Writes no status and no flags.",
    ),
    # --- marketplace_parent_relabel -------------------------------------------
    # The marketplace PARENT rows are Sheet infrastructure and
    # `require_marketplace_parent_rows_not_mutated` blocks every ordinary writer
    # from touching them - correctly: an ordinary program disposition must never
    # be able to reach the Awin or CJ control record.
    #
    # But Levanta REJECTED our Creator Marketplace application on 2026-08-18
    # (gmail 1a01312f1dc3b58e), and row 348 went on reading `application_pending`
    # because the guard refused the queued rejection. A dead relationship reading
    # as a live application is exactly the false fact this whole architecture
    # exists to prevent, so the guard is NARROWED rather than lifted: this mode,
    # and only this mode, is `parent_authorized`, it may only act on the row that
    # IS that parent's canonical row under its canonical name, it may write the
    # Status column and nothing else, it demands a hash-bound authority document
    # plus the message id, and the status it writes must equal the decision that
    # document records. Every other mode still hits the guard on every parent row.
    "marketplace_parent_relabel": SheetSyncModeSpec(
        key="marketplace_parent_relabel",
        plan_schema="affiliate-marketplace-parent-relabel-plan-v1",
        write_schema="affiliate-marketplace-parent-relabel-write-v1",
        mutable_columns=(10, 23, 24),
        required_columns=(10, 23, 24),
        allowed_status=frozenset({"rejected_creator_eligibility"}),
        required_flags={},
        evidence_fields=(
            ("parent_authority_path", "repo_path"),
            ("parent_authority_sha256", "sha256_of:parent_authority_path"),
            ("gmail_message_id", "text"),
            ("observed_at_utc", "timestamp"),
            ("operator_directive", "text"),
            ("decision", "enum:rejected_creator_eligibility"),
        ),
        note_must_cite=("parent_authority_path", "gmail_message_id", "observed_at_utc"),
        source_status=frozenset({"application_pending"}),
        forbidden_source_status=SHEET_SYNC_LIVE_STATUS,
        require_marketplace_parent_row=True,
        parent_authorized=True,
        description="Relabel a marketplace PARENT row whose network-level application "
                    "was decided. The parent guard stays in force for every other mode "
                    "and every other row.",
    ),
    # --- cross_network_duplicate_relationship ---------------------------------
    # 2026-08-18: Awin approved us for HealthRX (programme 129105) while the SAME
    # brand had been live and earning through Katalys (offer 1630) since
    # 2026-07-25 - and Awin's own terms forbid earning on HealthRX through two
    # networks at once. Nothing was deployed, correctly. But there was no queue
    # kind and no mode that could SAY so, and every candidate lied:
    #
    #   new_approval     would create/claim a deployable new provider row
    #   evidence_blocked asserts first-party capture FAILED; it did not
    #   status_correction vocabulary is pending|not_related|rejected|invited -
    #                    it cannot say `approved`, and it only ever retracts
    #   fit_assessment   not a fit question; the brand is a proven fit
    #
    # So Sheet row 489 went on reading `applied_pending_brand_approval` while the
    # network had actually approved us - a false fact held open for want of a
    # word, exactly what this architecture exists to prevent.
    #
    # This mode is the narrowest thing that closes it. It says one sentence:
    # "a second network approved us, an existing placement already earns on this
    # brand, and this row must NOT be deployed." It therefore:
    #   * writes 19=FALSE and 20=FALSE, so it can never claim wire-ready or live;
    #   * refuses on any live row (that is a pause/removal question, not this);
    #   * refuses if the target row carries a tracking link at all, because a
    #     do-not-deploy row holding a deployable link is a contradiction;
    #   * demands the SECOND network's approval evidence, hash-bound;
    #   * and - the load-bearing guard - refuses unless the cited existing
    #     placement is a real Sheet row that is LIVE, on a DIFFERENT network, and
    #     carries the SAME brand domain. Without a proven live placement there is
    #     nothing to be superseded BY, and this would just be a way to write
    #     "approved" without the evidence `add_approved_row` demands.
    #
    # Which network the brand should ultimately run on is an operator decision
    # (it is a contract hard-trigger). This mode records the standoff honestly;
    # it never resolves it and never re-points a link.
    "cross_network_duplicate_relationship": SheetSyncModeSpec(
        key="cross_network_duplicate_relationship",
        plan_schema="affiliate-cross-network-duplicate-relationship-plan-v1",
        write_schema="affiliate-cross-network-duplicate-relationship-write-v1",
        mutable_columns=(10, 19, 20, 23, 24),
        required_columns=(10, 19, 20, 23, 24),
        allowed_status=frozenset({"approved_superseded_by_existing_placement"}),
        required_flags={19: "FALSE", 20: "FALSE"},
        evidence_fields=(
            ("approval_evidence_path", "repo_path"),
            ("approval_evidence_sha256", "sha256_of:approval_evidence_path"),
            ("approved_at_utc", "timestamp"),
            ("network_relationship", "enum:" + "|".join(CROSS_NETWORK_APPROVED_RELATIONSHIP)),
            ("existing_placement_network", "text"),
            ("existing_placement_program_id", "text"),
            ("existing_placement_sheet_row", "int_min1"),
        ),
        note_must_cite=("approval_evidence_path", "approved_at_utc",
                        "existing_placement_network", "existing_placement_program_id"),
        source_status=CROSS_NETWORK_SOURCE_STATUS,
        forbidden_source_status=SHEET_SYNC_LIVE_STATUS,
        require_tracking_link=False,
        require_existing_live_placement=True,
        description="Record that a SECOND network approved a brand an existing placement "
                    "already earns on, so the new relationship is superseded and must "
                    "not be deployed. Never writes a live or wire-ready claim, and "
                    "refuses unless the existing live placement is proved on the Sheet.",
    ),
    # --- approval_confirmed ---------------------------------------------------
    # `HEALTHRX-NETWORK-CHOICE-20260818.md` §5.1: there was NO mode that could
    # talk an EXISTING pending row up to approved.
    #
    #   add_approved_row  only CREATES; on a program already on the Sheet it
    #                     refuses with `program_already_present`
    #   status_correction vocabulary is pending|not_related|rejected|invited and
    #                     it is one-way DOWN - it exists to retract a claim
    #   deployed_live     needs a row that is ALREADY approved and already holds
    #                     a provider-issued tracking link
    #   cross_network_*   needs another live placement on another network
    #
    # So every approval that FOLLOWS an application - i.e. the normal case - was
    # unsayable, and four rows (479, 509, 335, 336) sat claiming we were still
    # waiting on brands that had already said yes. The same silence is what hid
    # ShakePlease, Vitaturbo and Dr. Tobias until the enumeration fix.
    #
    # This mode says exactly one sentence: "the network we applied to has
    # approved us." It is bounded so it can never mean anything more:
    #   * it writes `approved_active_not_wired` and NOTHING else - never a live
    #     status, never `approved_active_tracking_ready` (a link is the wire-in
    #     mode's business), never a tracking link, never an identity column;
    #   * 19=FALSE / 20=FALSE, so it cannot claim wire-ready or live;
    #   * it may only act FROM `applied_pending_brand_approval` or
    #     `invited_pending_acceptance` - a rejected, not-related, paused,
    #     removed, suppressed or already-approved row is somebody else's
    #     business, and a live row is forbidden outright;
    #   * `require_tracking_link=False` - it refuses on a row that already
    #     carries a deployable link, because "approved and NOT wired" is then
    #     the wrong sentence (that row wants `approved_active_tracking_ready`
    #     from a mode that can prove the link);
    #   * evidence is a hash-bound first-party capture (authenticated portal
    #     page or publisher-API relationship read) whose relationship word is
    #     one of joined|approved|active|approved_active - `pending` is refused;
    #   * and the capture must NAME the programme, and so must the row, so a
    #     valid capture for one programme can never flip a different pending row.
    #
    # No create path: an approval for a program the Sheet has never heard of is
    # `add_approved_row`'s job, with the whole researched row it demands.
    # Deliberately NOT in the drain's MATERIALIZABLE set either: the drain may
    # persist an API observation that has nowhere else to live, but it may never
    # manufacture the capture that talks a row UP.
    "approval_confirmed": SheetSyncModeSpec(
        key="approval_confirmed",
        plan_schema="affiliate-approval-confirmed-plan-v1",
        write_schema="affiliate-approval-confirmed-write-v1",
        mutable_columns=(10, 19, 20, 23, 24),
        required_columns=(10, 19, 20, 23, 24),
        allowed_status=frozenset({"approved_active_not_wired"}),
        required_flags={19: "FALSE", 20: "FALSE"},
        evidence_fields=(
            ("approval_capture_path", "repo_path"),
            ("approval_capture_sha256", "sha256_of:approval_capture_path"),
            ("confirmed_at_utc", "timestamp"),
            ("network_relationship", "enum:" + "|".join(APPROVED_RELATIONSHIP_WORDS)),
            ("network_program_id", "text"),
        ),
        note_must_cite=("approval_capture_path", "confirmed_at_utc",
                        "network_relationship", "network_program_id"),
        source_status=APPROVAL_CONFIRMED_SOURCE_STATUS,
        forbidden_source_status=SHEET_SYNC_LIVE_STATUS,
        forbid_source_approval_claim=True,
        require_tracking_link=False,
        require_program_identity_binding=True,
        identity_capture_field="approval_capture_path",
        # Operator-authorized 2026-08-21 by the MaxBounty approval-processing
        # task. Network-account approvals belong on the network's own canonical
        # parent row. The exemption is still computed per row from the immutable
        # parent registry; ordinary programme rows and other parents remain
        # behind the unchanged guard.
        parent_authorized_on_own_canonical_row=True,
        description="Record that the network we applied to has APPROVED an existing "
                    "pending/invited row, against a hash-bound first-party capture that "
                    "names the programme. Writes approved-not-wired only - never a live "
                    "or wire-ready claim, never a tracking link, and never a new row.",
    ),
    # --- invitation_disposition ----------------------------------------------
    # A known-network invite is machine work, but an out-of-portfolio invite is
    # not permission to join it. This mode records the exact provider invitation
    # and the deterministic fit disposition while keeping the row link-dark. It
    # excludes `glp_core` by evidence vocabulary, so it can never replace the
    # acceptance lane for a supported core offer.
    "invitation_disposition": SheetSyncModeSpec(
        key="invitation_disposition",
        plan_schema="affiliate-invitation-disposition-plan-v1",
        write_schema="affiliate-invitation-disposition-write-v1",
        mutable_columns=(0, 1, 10, 11, 19, 20, 21, 23, 24),
        required_columns=(0, 1, 10, 11, 19, 20, 21, 23, 24),
        allowed_status=frozenset({INVITATION_DISPOSITION_STATUS}),
        required_flags={1: "FALSE", 11: "FALSE", 19: "FALSE", 20: "FALSE"},
        evidence_fields=(
            ("invitation_capture_path", "repo_path"),
            ("invitation_capture_sha256", "sha256_of:invitation_capture_path"),
            ("observed_at_utc", "timestamp"),
            ("source_event_id", "text"),
            ("network_program_id", "text"),
            ("provider_relationship", "enum:notjoined"),
            ("fit_classification", "enum:" + "|".join(INVITATION_DISPOSITION_FIT)),
            ("disposition", "enum:" + INVITATION_DISPOSITION_VALUE),
            ("application_url", "url"),
            ("official_url", "url"),
            ("category", "text"),
            ("fit_reason", "text"),
            ("vertical", "enum:" + "|".join(VERTICAL_ALLOWED_VALUES)),
        ),
        note_must_cite=(
            "invitation_capture_path", "observed_at_utc", "network_program_id",
            "fit_classification", "disposition",
        ),
        source_status=frozenset({""}),
        forbidden_source_status=SHEET_SYNC_LIVE_STATUS,
        forbid_source_approval_claim=True,
        allowed_link_qa=frozenset({"not_tested_no_link"}),
        require_tracking_link=False,
        may_create=True,
        create_required_columns=(
            0, 1, 2, 3, 4, 5, 6, 7, 10, 11, 12, 13, 19, 20, 21, 22, 23, 24, 25, 26,
        ),
        cells_from_evidence={
            3: "category", 5: "application_url", 6: "official_url",
            7: "fit_reason", 25: "vertical",
        },
        description="Record a provider-verified invitation that deterministic fit policy "
                    "left unjoined. May create only a link-dark, non-live merchant row and "
                    "cannot express a core-fit invitation or any joined relationship.",
    ),
    # --- marketplace_candidate ------------------------------------------------
    # 2026-08-18: the operator asked for BREADTH - a researched list of affiliate
    # networks we have NOT joined and have NOT been rejected from, on the Sheet,
    # so they can work down it by hand. Every existing mode refused, and each for
    # a good reason:
    #
    #   marketplace_rail_status  needs a rail row that ALREADY exists with a
    #                            blank status, and writes `approved_marketplace_
    #                            access` - i.e. it asserts we hold the account
    #   marketplace_parent_relabel  only relabels a decided parent row
    #   application_pending      would state we APPLIED. We have not.
    #   rejection                would state the network said no. It has not.
    #   new_approval             would state we are approved. We are not.
    #   fit_assessment           writes no status and needs an existing row
    #
    # So a researched-but-unengaged network was literally unsayable, and the only
    # way to put one on the Sheet was to hand-edit it - which is precisely what
    # this whole architecture exists to make unnecessary.
    #
    # This mode says exactly one sentence: "we researched this network, here is
    # where you apply, and we have no relationship with it whatsoever." It is
    # bounded so it can never mean anything more:
    #   * the ONLY status it may write is `candidate_not_applied`, which does not
    #     match `SHEET_SYNC_APPROVAL_CLAIM_PREFIXES` and is in no wire-in,
    #     deploy or live vocabulary, so no downstream machine can act on it;
    #   * 11=FALSE - Signup Complete? is the honesty contract. This mode is the
    #     only one in the file that forces it FALSE, and it may therefore never
    #     claim we hold an account;
    #   * 19=FALSE / 20=FALSE and `require_tracking_link=False`, so a candidate
    #     row can never be wire-ready, live, or carry a deployable link;
    #   * `source_status=frozenset()` - on an EXISTING row the only way through
    #     is `already_applied` (the row is already a candidate). It can therefore
    #     never talk a real relationship of any kind down into "we never applied";
    #   * the created row must be a marketplace RAIL by its own Category/Network
    #     cells - a network is not a merchant program and must never be counted
    #     as inventory;
    #   * the Apply URL, the Priority and the Apply? checkbox are all DETERMINED
    #     by the research evidence rather than chosen, so the cell the operator
    #     clicks and the cell the research justified cannot drift apart;
    #   * and `require_marketplace_candidate_new_network` refuses outright if any
    #     rail row - under any route spelling - or the canonical parent registry
    #     already names this network. Proposing a network we are already joined
    #     to, rejected from or blocked on is the one thing a breadth list must
    #     never do.
    #
    # Deliberately NOT in the drain's MATERIALIZABLE set: the research capture is
    # the whole content of the claim, and a machine that could manufacture it
    # could put any network on the Sheet with an invented application URL.
    "marketplace_candidate": SheetSyncModeSpec(
        key="marketplace_candidate",
        plan_schema="affiliate-marketplace-candidate-plan-v1",
        write_schema="affiliate-marketplace-candidate-write-v1",
        mutable_columns=(0, 1, 10, 11, 19, 20, 23, 24),
        required_columns=(0, 1, 10, 11, 19, 20, 23, 24),
        allowed_status=frozenset({MARKETPLACE_CANDIDATE_STATUS}),
        required_flags={11: "FALSE", 19: "FALSE", 20: "FALSE"},
        evidence_fields=(
            ("research_capture_path", "repo_path"),
            ("research_capture_sha256", "sha256_of:research_capture_path"),
            ("researched_at_utc", "timestamp"),
            ("application_url", "url"),
            ("network_type", "enum:" + "|".join(MARKETPLACE_CANDIDATE_TYPES)),
            ("priority", "enum:" + "|".join(MARKETPLACE_CANDIDATE_PRIORITIES)),
            ("api_availability", "text"),
            ("approval_requirements", "text"),
            ("glp_relevance_evidence", "text"),
        ),
        note_must_cite=("research_capture_path", "researched_at_utc", "application_url"),
        source_status=frozenset(),
        forbidden_source_status=SHEET_SYNC_LIVE_STATUS,
        forbid_source_approval_claim=True,
        require_tracking_link=False,
        may_create=True,
        create_required_columns=(0, 1, 2, 3, 4, 5, 6, 7, 10, 11, 19, 20, 23, 24, 25, 26),
        require_marketplace_candidate_row=True,
        description="Record a RESEARCHED affiliate network we have no relationship with, "
                    "as a rail row in an explicitly un-applied state, so the operator can "
                    "work down the list. Never claims an account, an application, an "
                    "approval, a link or a live wire, and refuses any network the Sheet "
                    "already names.",
    ),
    # --- Impact programme candidate ------------------------------------------
    # The Partner API can resolve a KNOWN campaign id and return public terms even
    # though marketplace browse is denied. `Never Joined` is therefore a real
    # merchant-programme state on an account we hold, not a network candidate and
    # not an application. Payout and cookie cells are re-derived from the
    # hash-bound API census by `require_impact_program_candidate_capture`.
    "impact_program_candidate": SheetSyncModeSpec(
        key="impact_program_candidate",
        plan_schema="affiliate-impact-program-candidate-plan-v1",
        write_schema="affiliate-impact-program-candidate-write-v1",
        mutable_columns=(0, 1, 7, 10, 11, 16, 17, 19, 20, 21, 23, 24, 25, 26),
        required_columns=(0, 1, 7, 10, 11, 16, 17, 19, 20, 21, 23, 24, 25, 26),
        allowed_status=frozenset({IMPACT_PROGRAM_CANDIDATE_STATUS}),
        required_flags={1: "TRUE", 11: "FALSE", 19: "FALSE", 20: "FALSE"},
        evidence_fields=(
            ("campaign_capture_path", "repo_path"),
            ("campaign_capture_sha256", "sha256_of:campaign_capture_path"),
            ("observed_at_utc", "timestamp"),
            ("network_program_id", "text"),
            ("provider_relationship", "enum:" + IMPACT_PROGRAM_CANDIDATE_RELATIONSHIP),
            ("application_url", "url"),
            ("official_url", "url"),
            ("fit_reason", "text"),
            ("commission_terms", "text"),
            ("cookie_window", "text"),
            ("vertical", "enum:" + "|".join(VERTICAL_ALLOWED_VALUES)),
            ("api_capabilities", "text"),
        ),
        note_must_cite=(
            "campaign_capture_path", "observed_at_utc", "network_program_id",
            "provider_relationship",
        ),
        source_status=IMPACT_PROGRAM_CANDIDATE_SOURCE_STATUS,
        forbidden_source_status=SHEET_SYNC_LIVE_STATUS,
        forbid_source_approval_claim=True,
        allowed_link_qa=frozenset({"not_tested_no_link"}),
        require_tracking_link=False,
        may_create=True,
        create_required_columns=(
            0, 1, 2, 3, 4, 5, 6, 7, 10, 11, 12, 13, 16, 17, 19, 20, 21, 22,
            23, 24, 25, 26,
        ),
        cells_from_evidence={
            5: "application_url", 6: "official_url", 7: "fit_reason",
            16: "commission_terms", 17: "cookie_window", 25: "vertical",
            26: "api_capabilities",
        },
        description="Record a known Impact merchant programme whose authenticated "
                    "campaign detail says Never Joined. May create or refresh only a "
                    "not_applied, link-dark row; never submits an application.",
    ),
    # --- commercial_economics_hold --------------------------------------------
    # The seventh missing mode, named in `EMED-WIREUP-20260818.md` §6.
    #
    # The state it exists to say: **approved, in-scope, research complete, and
    # unplaceable because the ADVERTISER configured no payable commission.** eMed
    # (Partnerize campaign 1011l6645, row 148) is the live case. We are approved,
    # the tracking link is issued, the GLP-1 fit is proven with verbatim
    # first-party quotes - and the only active commission on the campaign is a
    # campaign-level bounty with `commission_value: 0` and `commission_rate:
    # null`. That is a POPULATED zero, not an unpopulated field: 18 of our 20
    # other approved Partnerize campaigns expose a positive rate on the identical
    # authenticated endpoint. Wiring it would route readers away from providers
    # that pay us, to one that pays nothing.
    #
    # Why every other mode refused, which is what makes this one necessary rather
    # than convenient:
    #   evidence_blocked   writes `approved_active_first_party_evidence_blocked`,
    #                      which is exactly the sentence that went stale
    #   status_correction  vocabulary is pending|not_related|rejected|invited -
    #                      all four DENY an approval we genuinely hold
    #   deployed_live      false; the program is not on the site
    #   removed            false; it was never placed, so there is nothing to
    #                      retract, and `removed` would rewrite Link-QA
    #   fit_assessment     writes no status at all - and the fit is not in doubt
    #
    # Three deliberate design choices, each of which had an alternative:
    #
    #   * `require_tracking_link=None`, NOT `False`. `False` means "refuse a row
    #     that already holds a link", which is right for `approval_confirmed`
    #     (approved-and-not-wired is the wrong sentence for a row with a
    #     deployable link). Here it is exactly backwards: this row HAS a live
    #     link and the sentence is still true, because what is missing is a
    #     payable rate, not a link. Column 14 is not in `mutable_columns`, so the
    #     link is untouched either way - the relationship stays approved and the
    #     history stays intact.
    #   * Column 21 (Link-QA) is NOT mutable. A zero commission says nothing
    #     about whether the link resolves. Only a pause or a removal retires a
    #     link, and this is neither.
    #   * `require_program_identity_binding` - the same guard `approval_confirmed`
    #     carries, for the same reason. Any hash-valid commission read would
    #     otherwise let a caller stamp an economics hold onto an unrelated
    #     approved row, and an economics hold takes a row OUT of the wire-in
    #     path. So the campaign id must appear as a standalone token in the
    #     cited capture's own bytes AND in the target row's own Apply URL,
    #     tracking link or Notes. Row 148 carries `1011l6645` in both.
    #
    # `commission_value` must be exactly 0 and `commission_rate` must be zero or
    # null: the mode may only describe the state it is named for. If a positive
    # rate ever appears in the capture, the evidence gate REFUSES, because a
    # positive commission REOPENS the wire-in path - `g6_r1a_terms_gate.py` is
    # already written to reopen this row rather than hold it dead, and holding it
    # anyway would be the expensive error.
    #
    # Deliberately NOT in the drain's MATERIALIZABLE set. The authenticated
    # commission read IS the whole content of the claim. A drain that could
    # manufacture it from a queue row's say-so could park any approved,
    # payable program in a hold and quietly take it off the earning path.
    "commercial_economics_hold": SheetSyncModeSpec(
        key="commercial_economics_hold",
        plan_schema="affiliate-commercial-economics-hold-plan-v1",
        write_schema="affiliate-commercial-economics-hold-write-v1",
        mutable_columns=(10, 19, 20, 23, 24),
        required_columns=(10, 19, 20, 23, 24),
        allowed_status=frozenset({COMMERCIAL_ECONOMICS_HOLD_STATUS}),
        required_flags={19: "FALSE", 20: "FALSE"},
        evidence_fields=(
            ("commission_verification_path", "repo_path"),
            ("commission_verification_sha256", "sha256_of:commission_verification_path"),
            ("verified_at_utc", "timestamp"),
            ("commission_value", "int_exact0"),
            ("commission_rate", "zero_or_null"),
            ("network_program_id", "text"),
            ("network_advertiser_id", "text"),
        ),
        note_must_cite=("commission_verification_path", "verified_at_utc",
                        "network_program_id", "network_advertiser_id"),
        source_status=COMMERCIAL_ECONOMICS_HOLD_SOURCE_STATUS,
        forbidden_source_status=SHEET_SYNC_LIVE_STATUS,
        require_source_approval_claim=True,
        require_tracking_link=None,
        require_program_identity_binding=True,
        identity_capture_field="commission_verification_path",
        description="Record that an APPROVED, in-scope program cannot be placed because "
                    "the advertiser has configured no payable commission, against an "
                    "authenticated commission read showing a populated zero. Never claims "
                    "wire-ready or live, never writes or retires a tracking link, never "
                    "creates a row, and refuses outright if the commission is positive.",
    ),
    # --- network_reactivated --------------------------------------------------
    # See NETWORK_REACTIVATION_* above for the whole argument. The one-line
    # version: `network_paused_disposition` could park a live programme, and
    # NOTHING could bring it back, so three paying Katalys programmes stayed
    # parked after the advertiser restarted them.
    "network_reactivated": SheetSyncModeSpec(
        key="network_reactivated",
        plan_schema="affiliate-network-reactivated-plan-v1",
        write_schema="affiliate-network-reactivated-write-v1",
        mutable_columns=(10, 19, 20, 21, 23, 24),
        required_columns=(10, 19, 20, 23, 24),
        allowed_status=frozenset({
            NETWORK_REACTIVATION_STATUS_WITH_LINK,
            NETWORK_REACTIVATION_STATUS_WITHOUT_LINK,
        }),
        required_flags={19: "FALSE", 20: "FALSE"},
        evidence_fields=(
            ("reactivation_capture_path", "repo_path"),
            ("reactivation_capture_sha256", "sha256_of:reactivation_capture_path"),
            ("observed_at_utc", "timestamp"),
            ("activation_wording", "enum:" + "|".join(NETWORK_REACTIVATION_WORDS)),
            ("advertiser", "text"),
        ),
        note_must_cite=("reactivation_capture_path", "observed_at_utc", "activation_wording"),
        source_status=NETWORK_REACTIVATION_SOURCE_STATUS,
        forbidden_source_status=SHEET_SYNC_LIVE_STATUS,
        allowed_link_qa=frozenset({NETWORK_REACTIVATION_LINK_QA}),
        require_tracking_link=None,
        status_follows_tracking_link=(
            NETWORK_REACTIVATION_STATUS_WITH_LINK,
            NETWORK_REACTIVATION_STATUS_WITHOUT_LINK,
        ),
        require_program_name_binding=True,
        identity_capture_field="reactivation_capture_path",
        description="Bring a row back from `paused_network_program_paused` when the "
                    "network re-activated the programme, against a hash-bound capture "
                    "that names the programme and carries the activation wording. Never "
                    "claims live or wire-ready - restoring the site surfaces stays the "
                    "deploy machine's job - and never re-tests or rewrites the link.",
    ),
    # --- wire_in_ready --------------------------------------------------------
    "provider_scope_retraction": SheetSyncModeSpec(
        key="provider_scope_retraction",
        plan_schema="affiliate-provider-scope-retraction-plan-v1",
        write_schema="affiliate-provider-scope-retraction-write-v1",
        mutable_columns=(9, 19, 23, 24),
        required_columns=(19, 23, 24),
        allowed_status=frozenset(),
        required_flags={19: "FALSE"},
        evidence_fields=(
            ("scope_retraction_path", "repo_path"),
            ("scope_retraction_sha256", "sha256_of:scope_retraction_path"),
            ("assessed_at_utc", "timestamp"),
        ),
        note_must_cite=("scope_retraction_path", "assessed_at_utc"),
        forbidden_source_status=SHEET_SYNC_LIVE_STATUS,
        forbid_wired_live_source=True,
        parent_authorized_on_own_canonical_row=True,
        description="Retract an exact evidenced non-provider wire gate; optionally clear "
                    "only the manifest's erroneous fit-pending slug. No status or live flag writes.",
    ),
    # THE COL-19 GAP (NUIMAGE-ONBOARD-20260819.md §6 gap #3). `Wire-In Ready?`
    # is the gate the whole build queue reads, and the ONLY thing that could
    # ever set it TRUE was `katalys_approved_link_resolver.py`, which hard-filters
    # `Network / Route == "katalys"` at line 110. Every non-Katalys approval —
    # Nu Image on Trackdesk, VitalCore on Refersion, AbundRX on XenTraffic,
    # iHealth on GoAffPro, the whole CJ estate — was structurally unbuildable,
    # and the hand-written `wire_in_gate` plan path was a direct writer, not a
    # queue-drained one.
    #
    # This mode is the network-agnostic replacement, and it is deliberately
    # NARROWER than the thing it replaces:
    #   * it writes NO status (column 10 absent) — it makes no claim about the
    #     relationship, only about buildability;
    #   * it may not act on a row that is already live;
    #   * the row must already carry a provider-issued tracking link, and that
    #     link is STRUCTURALLY VALIDATED against the rail the row names, using
    #     `glp_affiliate_link_rails`. A rail whose link shape has never been
    #     observed first-party refuses here by name rather than passing;
    #   * `Wired Live?` stays out of its mutable set entirely, so it can never
    #     be the thing that says a program is earning.
    "wire_in_ready": SheetSyncModeSpec(
        key="wire_in_ready",
        plan_schema="affiliate-wire-in-ready-plan-v1",
        write_schema="affiliate-wire-in-ready-write-v1",
        # Column 9 joins the mutable set for ONE reason: a blank SEO slug stops
        # the research packet, and therefore the page, for a row that is
        # otherwise wire-in ready — a blocker that recurred on six rows across
        # two rails. The producer only ever supplies a slug when column 9 is
        # BLANK, and the projection writes the cell only when the evidence
        # carries one, so an existing slug is never overwritten. The slug is a
        # naming convention over the row's own official domain and programme
        # name, not a claim about the provider, which is why it needs no capture.
        mutable_columns=(9, 19, 21, 23, 24),
        required_columns=(19, 23, 24),
        allowed_status=frozenset(),
        required_flags={19: "TRUE"},
        evidence_fields=(
            ("network", "text"),
            ("immutable_id", "text"),
            ("rail_evidence", "text"),
            ("link_validated_at_utc", "timestamp"),
            ("wire_in_plan_path", "repo_path"),
            ("wire_in_plan_sha256", "sha256_of:wire_in_plan_path"),
        ),
        note_must_cite=("network", "wire_in_plan_path", "link_validated_at_utc"),
        source_status=SHEET_SYNC_APPROVED_NOT_LIVE_STATUS,
        forbidden_source_status=SHEET_SYNC_LIVE_STATUS,
        allowed_link_qa=frozenset({"not_clicked_preserve_attribution"}),
        require_tracking_link=True,
        forbid_wired_live_source=True,
        require_link_rail_validation=True,
        description="Flip `Wire-In Ready?` TRUE for an approved, linked programme on ANY "
                    "rail, after structurally validating the row's own tracking link "
                    "against that rail. Writes no status and can never claim live.",
    ),
    # --- portal_readback_confirmed -------------------------------------------
    # See PORTAL_READBACK_* above. Records that a REQUIRED readback was actually
    # performed, and says what it found - including the two answers that had no
    # vocabulary at all (membership proven / approval unproven, and an API that
    # works but is IP-locked to this host).
    "portal_readback_confirmed": SheetSyncModeSpec(
        key="portal_readback_confirmed",
        plan_schema="affiliate-portal-readback-confirmed-plan-v1",
        write_schema="affiliate-portal-readback-confirmed-write-v1",
        mutable_columns=(10, 19, 20, 23, 24),
        required_columns=(10, 19, 20, 23, 24),
        allowed_status=frozenset(PORTAL_READBACK_VERDICT_STATUS.values()),
        required_flags={19: "FALSE", 20: "FALSE"},
        evidence_fields=(
            ("readback_capture_path", "repo_path"),
            ("readback_capture_sha256", "sha256_of:readback_capture_path"),
            ("captured_at_utc", "timestamp"),
            ("readback_verdict", "enum:" + "|".join(PORTAL_READBACK_VERDICT_STATUS)),
            ("provider_account_reference", "text"),
        ),
        note_must_cite=("readback_capture_path", "captured_at_utc", "readback_verdict"),
        forbidden_source_status=SHEET_SYNC_LIVE_STATUS,
        require_tracking_link=None,
        verdict_field="readback_verdict",
        verdict_status=PORTAL_READBACK_VERDICT_STATUS,
        verdict_source_status=PORTAL_READBACK_VERDICT_SOURCE_STATUS,
        verdict_required_evidence=PORTAL_READBACK_VERDICT_REQUIRED_EVIDENCE,
        require_program_name_binding=True,
        identity_capture_field="readback_capture_path",
        # Bridge-authorized 2026-08-20 (PHASE1B-BUILD-20260819.md). A marketplace
        # PARENT row is exactly where a portal readback has something to say -
        # rows 358 Digistore24 and 470 MaxBounty are account-level records whose
        # only truth source IS the portal. Narrowed per row: see
        # `require_parent_row_protection`.
        parent_authorized_on_own_canonical_row=True,
        description="Record that a required provider readback was DONE and what it "
                    "answered. The verdict determines the status and the states it may "
                    "resolve; the mode never writes a live or wire-ready claim and never "
                    "touches the tracking link.",
    ),
    # --- operator_status_ruling -----------------------------------------------
    # See OPERATOR_RULING_* above. The operator's own words, quoted from the live
    # cell at write time, become the canonical status.
    "operator_status_ruling": SheetSyncModeSpec(
        key="operator_status_ruling",
        plan_schema="affiliate-operator-status-ruling-plan-v1",
        write_schema="affiliate-operator-status-ruling-write-v1",
        mutable_columns=(10, 23, 24),
        required_columns=(10, 23, 24),
        allowed_status=frozenset(OPERATOR_RULING_STATUS.values()),
        required_flags={},
        evidence_fields=(
            ("ruling_capture_path", "repo_path"),
            ("ruling_capture_sha256", "sha256_of:ruling_capture_path"),
            ("observed_at_utc", "timestamp"),
            ("operator_ruling", "enum:" + "|".join(OPERATOR_RULING_STATUS)),
            ("operator_note_quote", "text"),
        ),
        note_must_cite=("ruling_capture_path", "observed_at_utc", "operator_note_quote"),
        forbidden_source_status=SHEET_SYNC_LIVE_STATUS,
        require_tracking_link=None,
        verdict_field="operator_ruling",
        verdict_status=OPERATOR_RULING_STATUS,
        verdict_source_status=OPERATOR_RULING_SOURCE_STATUS,
        require_operator_note_quote=True,
        # Bridge-authorized 2026-08-20 (PHASE1B-BUILD-20260819.md). The operator's
        # own ruling is the only thing that can settle a network-level account
        # row like 427 Skimlinks or 433 Admitad; the quote gate still requires
        # those words to be in the row's LIVE note at write time. Narrowed per
        # row: see `require_parent_row_protection`.
        parent_authorized_on_own_canonical_row=True,
        description="Make the operator's own written ruling the canonical Status. The "
                    "quoted words must be present in the row's LIVE Notes cell at write "
                    "time, so the ruling cannot be invented; writes no flags and no link.",
    ),
    # --- commission_terms_correction ------------------------------------------
    # Phase 1 found the hole and could not fill it (PHASE1-PROOF §7.7): "no
    # governed mode writes columns 16/17, and Coivas row 346 is LIVE and may
    # publish a stale rate". Awin's own notice is the proof — row 346 read
    # `Default: 10%; GLP-1: 155 USD` while the publisher API had been answering
    # `Default: percentage 0` since the 2026-08-12 rates change. A live review
    # page quoting a commission the advertiser has retired is a false fact on a
    # money surface, and there was no word for correcting it.
    #
    # The mode is the narrowest thing that closes it. It says ONE sentence: "the
    # network's own authenticated surface reports these terms as of this moment".
    # Therefore it:
    #   * writes ONLY Commission/Terms (16), Cookie Window (17) and the note
    #     cells (23/24) — no status, no flags, no tracking link, no row creation.
    #     `writes_status` is False, so it can never talk a row up or down;
    #   * demands a hash-bound capture of the FETCH (the API response or the
    #     authenticated UI capture) plus the network programme id, and re-proves
    #     via `require_program_identity_binding` that the capture and the row are
    #     the same programme — the same guard `commercial_economics_hold` uses,
    #     for the same reason: a hash-valid capture of SOME programme must not be
    #     able to rewrite the terms of ANOTHER;
    #   * records HOW the rate was read (`network_api` or `authenticated_ui`), so
    #     a terms cell can always be traced to a fetch path rather than to a
    #     human's memory of a portal screen;
    #   * runs on a LIVE row on purpose (`forbid_wired_live_source=False`). The
    #     live row is the expensive case — it is the one whose stale rate is
    #     already published. The mode makes no live/approval claim of any kind,
    #     so there is nothing for it to overstate on a live row.
    "commission_terms_correction": SheetSyncModeSpec(
        key="commission_terms_correction",
        plan_schema="affiliate-commission-terms-correction-plan-v1",
        write_schema="affiliate-commission-terms-correction-write-v1",
        mutable_columns=(16, 17, 23, 24),
        required_columns=(16, 23, 24),
        allowed_status=frozenset(),
        required_flags={},
        evidence_fields=(
            ("commission_capture_path", "repo_path"),
            ("commission_capture_sha256", "sha256_of:commission_capture_path"),
            ("fetched_at_utc", "timestamp"),
            ("network_program_id", "text"),
            ("fetch_method", "enum:network_api|authenticated_ui|operator_confirmed_partner_email"),
            ("notice_message_id", "text"),
            ("commission_terms", "text"),
        ),
        note_must_cite=("commission_capture_path", "fetched_at_utc", "fetch_method"),
        cells_from_evidence={16: "commission_terms", 17: "cookie_window"},
        # No status claim at all, so no source-status gate applies and a live row
        # is fair game — see the header comment.
        forbid_wired_live_source=False,
        require_tracking_link=None,
        require_program_identity_binding=True,
        identity_capture_field="commission_capture_path",
        description="Correct a programme's Commission/Terms and Cookie Window from a "
                    "hash-bound authenticated network fetch or an explicitly operator-confirmed "
                    "partner-manager email. Writes no status, flags or link; proves the capture "
                    "and row are the same programme and records the exact source.",
    ),
    # --- Everflow white-label approved inventory -----------------------------
    # A brand-hosted Everflow tenant can expose an already-approved/public offer,
    # its exact payout and the provider-issued tracking URL in one authenticated
    # BFF read. Existing modes could record the approval, the terms, OR the link,
    # but none could transcribe the complete evidenced inventory onto an EXISTING
    # row. This mode does exactly that and never creates a row. It deliberately
    # leaves column 19 FALSE: the independent `wire_in_ready` producer must prove
    # the opaque Everflow offer token and rotating tracking host before the build
    # gate opens.
    "everflow_white_label_inventory": SheetSyncModeSpec(
        key="everflow_white_label_inventory",
        plan_schema="affiliate-everflow-white-label-inventory-plan-v1",
        write_schema="affiliate-everflow-white-label-inventory-write-v1",
        mutable_columns=(
            3, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 20, 21, 22, 23, 24, 25, 26,
        ),
        required_columns=(
            3, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 20, 21, 22, 23, 24, 25, 26,
        ),
        allowed_status=frozenset({"approved_active_tracking_ready"}),
        required_flags={11: "TRUE", 19: "FALSE", 20: "FALSE"},
        evidence_fields=(
            ("portal_capture_path", "repo_path"),
            ("portal_capture_sha256", "sha256_of:portal_capture_path"),
            ("captured_at_utc", "timestamp"),
            ("portal_origin", "url"),
            ("tenant_display_name", "text"),
            ("network_program_id", "text"),
            ("offer_name", "text"),
            ("provider_relationship", "enum:active_public_available"),
            ("publisher_id", "text"),
            ("tracking_link", "url"),
            ("tracking_host", "text"),
            ("payout_terms", "text"),
            ("paid_event", "text"),
            ("cookie_window_availability", "enum:unavailable_in_captured_offer_detail"),
            ("offer_detail_url", "url"),
            ("official_url", "url"),
            ("category", "text"),
            ("fit_reason", "text"),
            ("suggested_glp_page", "text"),
            ("seo_slug", "text"),
            ("vertical", "enum:" + "|".join(VERTICAL_ALLOWED_VALUES)),
            ("api_capabilities", "text"),
            ("compliance_notes", "text"),
            ("fit_ledger_path", "repo_path"),
            ("fit_ledger_sha256", "sha256_of:fit_ledger_path"),
        ),
        note_must_cite=(
            "portal_capture_path", "captured_at_utc", "network_program_id",
            "paid_event", "fit_ledger_path",
        ),
        source_status=frozenset({
            "approved_active_not_wired", "approved_active_tracking_ready",
        }),
        forbidden_source_status=SHEET_SYNC_LIVE_STATUS,
        require_source_approval_claim=True,
        allowed_link_qa=frozenset({"not_clicked_preserve_attribution"}),
        require_tracking_link=None,
        cells_from_evidence={
            3: "category", 5: "offer_detail_url", 6: "official_url",
            7: "fit_reason", 8: "suggested_glp_page", 9: "seo_slug",
            12: "portal_origin", 13: "publisher_id", 14: "tracking_link",
            16: "payout_terms", 22: "compliance_notes", 25: "vertical",
            26: "api_capabilities",
        },
        description="Populate an EXISTING approved Everflow white-label programme from a "
                    "hash-bound authenticated offer capture and fit ledger. Stores the exact "
                    "offer link and payout, keeps Wire-In Ready FALSE pending independent rail "
                    "validation, never claims live, and never creates a row.",
    ),
    # --- Affiliati authenticated inventory -----------------------------------
    "affiliati_inventory": SheetSyncModeSpec(
        key="affiliati_inventory",
        plan_schema="affiliate-affiliati-inventory-plan-v1",
        write_schema="affiliate-affiliati-inventory-write-v1",
        mutable_columns=(0, 1, 7, 8, 9, 10, 11, 16, 19, 20, 21, 22, 23, 24, 25, 26),
        required_columns=(0, 1, 7, 8, 9, 10, 11, 16, 19, 20, 21, 22, 23, 24, 25, 26),
        allowed_status=frozenset(AFFILIATI_INVENTORY_STATUS.values()),
        required_flags={19: "FALSE", 20: "FALSE"},
        evidence_fields=(
            ("api_capture_path", "repo_path"),
            ("api_capture_sha256", "sha256_of:api_capture_path"),
            ("captured_at_utc", "timestamp"),
            ("network_program_id", "text"),
            ("offer_contract_id", "text"),
            ("provider_name", "text"),
            ("source_offer_name", "text"),
            ("provider_relationship", "enum:notjoined|pending|approved"),
            ("offer_status_raw", "text"),
            ("fit_classification", "enum:core_glp1"),
            ("seo_allowed", "enum:TRUE"),
            ("exact_paid_event", "text"),
            ("payout_terms", "text"),
            ("application_url", "url"),
            ("official_url", "url"),
            ("category", "text"),
            ("fit_reason", "text"),
            ("suggested_glp_page", "text"),
            ("seo_slug", "text"),
            ("promotion_notes", "text"),
            ("vertical", "enum:" + "|".join(VERTICAL_ALLOWED_VALUES)),
            ("api_capabilities", "text"),
            ("compliance_notes", "text"),
        ),
        note_must_cite=("api_capture_path", "captured_at_utc", "network_program_id",
                        "provider_relationship", "exact_paid_event"),
        source_status=AFFILIATI_INVENTORY_SOURCE_STATUS,
        forbidden_source_status=SHEET_SYNC_LIVE_STATUS,
        allowed_link_qa=frozenset({"not_tested_no_link"}),
        require_tracking_link=None,
        may_create=True,
        create_required_columns=(0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 16,
                                 19, 20, 21, 22, 23, 24, 25, 26),
        cells_from_evidence={
            5: "application_url", 6: "official_url", 7: "fit_reason",
            8: "suggested_glp_page", 9: "seo_slug", 16: "payout_terms",
            22: "compliance_notes", 25: "vertical", 26: "api_capabilities",
        },
        require_program_identity_binding=True,
        identity_capture_field="api_capture_path",
        description="Create or refresh a complete Affiliati GLP offer row from a hash-bound "
                    "authenticated API census. Relationship and exact paid event come from the "
                    "raw offer; enumeration remains application-free, link-dark and not live.",
    ),
    "affiliati_switchover_stage": SheetSyncModeSpec(
        key="affiliati_switchover_stage",
        plan_schema="affiliate-affiliati-switchover-stage-plan-v1",
        write_schema="affiliate-affiliati-switchover-stage-write-v1",
        mutable_columns=(0, 1, 7, 8, 9, 10, 11, 16, 19, 20, 21, 22, 23, 24, 25, 26),
        required_columns=(0, 1, 7, 8, 9, 10, 11, 16, 19, 20, 21, 22, 23, 24, 25, 26),
        allowed_status=frozenset(AFFILIATI_SWITCH_STATUS.values()),
        required_flags={19: "FALSE", 20: "FALSE"},
        evidence_fields=(
            ("switch_capture_path", "repo_path"),
            ("switch_capture_sha256", "sha256_of:switch_capture_path"),
            ("captured_at_utc", "timestamp"),
            ("postback_contract_path", "repo_path"),
            ("postback_contract_sha256", "sha256_of:postback_contract_path"),
            ("prerequisite_registry_path", "repo_path"),
            ("prerequisite_registry_sha256", "sha256_of:prerequisite_registry_path"),
            ("terms_capture_path", "repo_path"),
            ("terms_capture_sha256", "sha256_of:terms_capture_path"),
            ("network_program_id", "text"), ("offer_contract_id", "text"),
            ("provider_name", "text"), ("campaign_id", "text"),
            ("tracking_host", "text"), ("target_status", "enum:" + "|".join(AFFILIATI_SWITCH_STATUS.values())),
            ("current_payout_usd", "text"), ("replacement_payout_usd", "text"),
            ("dollar_delta", "text"), ("exact_paid_event", "text"),
            ("payout_terms", "text"), ("application_url", "url"),
            ("official_url", "url"), ("category", "text"), ("fit_reason", "text"),
            ("suggested_glp_page", "text"), ("seo_slug", "text"),
            ("vertical", "enum:" + "|".join(VERTICAL_ALLOWED_VALUES)),
            ("api_capabilities", "text"), ("compliance_notes", "text"),
        ),
        note_must_cite=("switch_capture_path", "postback_contract_path",
                        "prerequisite_registry_path", "network_program_id"),
        source_status=AFFILIATI_SWITCH_SOURCE_STATUS,
        forbidden_source_status=SHEET_SYNC_LIVE_STATUS,
        allowed_link_qa=frozenset({"not_tested_no_link"}),
        require_tracking_link=False,
        may_create=True,
        create_required_columns=(0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 16,
                                 19, 20, 21, 22, 23, 24, 25, 26),
        cells_from_evidence={
            5: "application_url", 6: "official_url", 7: "fit_reason",
            8: "suggested_glp_page", 9: "seo_slug", 16: "payout_terms",
            22: "compliance_notes", 25: "vertical", 26: "api_capabilities",
        },
        require_program_identity_binding=True,
        identity_capture_field="switch_capture_path",
        description="Stage the three canonical Affiliati replacements link-dark, with exact "
                    "campaign/host/payout terms and all postback prerequisites still blocking.",
    ),
    "higher_rail_pending_replacement": SheetSyncModeSpec(
        key="higher_rail_pending_replacement",
        plan_schema="affiliate-higher-rail-pending-replacement-plan-v1",
        write_schema="affiliate-higher-rail-pending-replacement-write-v1",
        mutable_columns=(10, 23, 24), required_columns=(10, 23, 24),
        allowed_status=frozenset({HIGHER_RAIL_PENDING_STATUS}), required_flags={},
        evidence_fields=(
            ("postback_contract_path", "repo_path"),
            ("postback_contract_sha256", "sha256_of:postback_contract_path"),
            ("switch_capture_path", "repo_path"),
            ("switch_capture_sha256", "sha256_of:switch_capture_path"),
            ("checked_at_utc", "timestamp"), ("provider_name", "text"),
            ("source_program_id", "text"), ("replacement_offer_id", "text"),
            ("replacement_campaign_id", "text"), ("replacement_host", "text"),
            ("current_payout_usd", "text"), ("replacement_payout_usd", "text"),
            ("dollar_delta", "text"),
        ),
        note_must_cite=("postback_contract_path", "switch_capture_path",
                        "source_program_id", "replacement_offer_id"),
        source_status=SHEET_SYNC_APPROVED_NOT_LIVE_STATUS,
        forbidden_source_status=SHEET_SYNC_LIVE_STATUS,
        require_source_approval_claim=True,
        require_tracking_link=True,
        description="Mark an approved lower rail pending replacement without deleting its "
                    "fallback link or claiming the replacement is live.",
    ),
    # --- maxbounty_inventory --------------------------------------------------
    # MaxBounty has no publisher API. The authenticated campaign detail is the
    # first-party evidence, and the exact action button decides relationship.
    # New inventory rows are link-dark: enumeration records approval/application-needed,
    # economics, coupons, and sister-vertical HOLDs while the submit/mint/deploy
    # gate remains closed. Existing tracking-ready rows keep earned rail state byte-for-byte.
    "maxbounty_inventory": SheetSyncModeSpec(
        key="maxbounty_inventory",
        plan_schema="affiliate-maxbounty-inventory-plan-v1",
        write_schema="affiliate-maxbounty-inventory-write-v1",
        # Existing rows keep their identity columns immutable. The create path
        # supplies 2/3/4/5/6 once, but later enumeration passes may refresh only
        # relationship, fit, economics, coupons, evidence and timestamps.
        mutable_columns=(0, 1, 7, 8, 9, 10, 11, 15, 16, 17, 19, 20, 21, 23, 24, 25, 26),
        required_columns=(0, 1, 7, 10, 11, 15, 16, 17, 19, 20, 21, 23, 24, 25, 26),
        allowed_status=(frozenset(MAXBOUNTY_INVENTORY_STATUS.values())
                        | MAXBOUNTY_INVENTORY_PRESERVED_STATUS),
        # Existing tracking-ready rows preserve their earned wire/live flags;
        # row-contract validation below requires FALSE only for link-dark inventory.
        required_flags={1: "FALSE"},
        evidence_fields=(
            ("portal_capture_path", "repo_path"),
            ("portal_capture_sha256", "sha256_of:portal_capture_path"),
            ("captured_at_utc", "timestamp"),
            ("network_program_id", "text"),
            ("relationship_button",
             "enum:Build tracking link|Request approval to run|Request under review|"
             "Request denied|Request capacity blocked"),
            ("fit_classification", "enum:core_glp1|sister_vertical|out_of_scope"),
            ("vertical", "text"),
            ("fit_reason", "text"),
            ("coupon_summary", "text"),
            ("payout_terms", "text"),
            ("cookie_window", "text"),
            ("detail_url", "url"),
            ("official_url", "url"),
        ),
        note_must_cite=("portal_capture_path", "captured_at_utc",
                        "relationship_button", "vertical"),
        source_status=MAXBOUNTY_INVENTORY_SOURCE_STATUS,
        forbidden_source_status=SHEET_SYNC_LIVE_STATUS,
        require_tracking_link=None,
        may_create=True,
        create_required_columns=(0, 1, 2, 3, 4, 5, 6, 7, 10, 11, 15, 16, 17,
                                 19, 20, 21, 23, 24, 25, 26),
        cells_from_evidence={
            5: "detail_url", 6: "official_url", 7: "fit_reason", 15: "coupon_summary",
            16: "payout_terms", 17: "cookie_window", 25: "vertical",
        },
        description="Create or refresh a MaxBounty campaign row from authenticated "
                    "button/API relationship evidence. Records coupons and economics, keeps "
                    "new rows link-dark, preserves earned rail state on retries, and puts other "
                    "verticals on an explicit HOLD.",
    ),
    # A link can enter the Sheet only after the same hard gate used by the portal
    # builder is open and a hash-bound Copy-link receipt exists. This mode stores
    # the provider-issued link and reproducible four-option configuration but
    # deliberately leaves Wire-In Ready? FALSE; independent rail validation owns
    # that next transition, so a bad challenger can never displace an incumbent.
    # --- rail_tracking_link ----------------------------------------------------
    # ONE mode for EVERY registered rail, not one mode per rail.
    #
    # WHY THIS EXISTS (2026-09-02). Awin carried 32 tracking links on the Sheet
    # and CJ carried 3, five of them on live rows — so the estate plainly used to
    # be able to record a link on those rails. It could not any more: the only
    # `*_tracking_link` kind that has EVER drained is `maxbounty_tracking_link`
    # (71 times; no Awin or CJ link mode appears in any drain receipt). Reading
    # the notes on those rows says why — they were written in the
    # `affiliate-marketplace-expansion-2026-07-17` era, by DIRECT Sheet writes,
    # before the single-writer rule existed. So there was no older governed path
    # to revive: reviving it would mean re-introducing direct writes, which is
    # the one thing the architecture forbids outright.
    #
    # WHY ONE MODE AND NOT SIX. The per-rail differences — host class, path
    # shape, which id is immutable, whether our publisher token appears in the
    # path — already live in `glp_affiliate_link_rails`, which is the single
    # authority on them and is exercised by `require_link_rail_validation`. Six
    # hand-written modes would restate that authority six times and let the
    # copies drift. This mode declares the CONTRACT (first-party capture,
    # re-hashed; identity bound in the same capture as the href; link never
    # clicked; never claims wire-ready or live) and asks the rail registry for
    # the shape. Adding a seventh rail then needs a rail entry and evidence, not
    # a new writer.
    #
    # It is deliberately STRICTER than `maxbounty_tracking_link`: that mode
    # defers rail validation to the wire-in gate, while this one validates the
    # link it is about to write, against its rail, at write time. An unverified
    # rail refuses by name rather than passing.
    # --- provider_relationship_withdrawn ---------------------------------------
    # THE GAP THIS CLOSES (2026-09-02). MaxBounty 31644 Viviomd and 31973 TrimRX
    # were approved on 2026-09-01 and both reached Wire-In Ready with a
    # provider-issued link. A day later both had vanished from the bookmarked
    # enumeration, and their own authenticated campaign pages answered HTTP 404.
    # The network had withdrawn campaigns we were approved on.
    #
    # No mode could say that. `rejection` would have written "declined
    # application", which is a different and false fact — we were approved, then
    # the campaign went away. `evidence_blocked` says our evidence is missing,
    # not that the relationship is gone. So two rows would have stayed Wire-In
    # Ready with links the network no longer honours, and the deploy machine
    # would have shipped dead placements.
    #
    # It retracts the GATE, not the history: the tracking link stays in column 14
    # as the record of what was issued, and `Wired Live?` is untouched. A row
    # that is already LIVE is deliberately out of scope — retracting a live
    # placement is a deploy action with an operator gate, not a Sheet edit.
    "provider_relationship_withdrawn": SheetSyncModeSpec(
        key="provider_relationship_withdrawn",
        plan_schema="affiliate-provider-relationship-withdrawn-plan-v1",
        write_schema="affiliate-provider-relationship-withdrawn-write-v1",
        mutable_columns=(10, 19, 21, 23, 24),
        required_columns=(10, 19, 21, 23, 24),
        allowed_status=frozenset({"provider_withdrew_campaign_relationship_gone"}),
        required_flags={19: "FALSE"},
        evidence_fields=(
            ("network", "text"),
            ("network_program_id", "text"),
            ("readback_path", "repo_path"),
            ("readback_sha256", "sha256_of:readback_path"),
            ("captured_at_utc", "timestamp"),
            ("provider_api_status", "text"),
            ("classification", "text"),
        ),
        note_must_cite=("network", "network_program_id", "readback_path",
                        "captured_at_utc", "provider_api_status"),
        source_status=frozenset({
            "approved_active_not_wired", "approved_active_tracking_ready"}),
        forbidden_source_status=frozenset({
            "approved_active_wired_live", "live_wired_verified"}),
        forbid_wired_live_source=True,
        require_program_identity_binding=True,
        identity_capture_field="readback_path",
        description=(
            "Record that the network has withdrawn a campaign we were approved on, "
            "proven by the authenticated campaign page answering 404, and close the "
            "wire-in gate so nothing ships a link the network no longer honours. "
            "Never acts on a live row and never erases the issued link."),
    ),
    "rail_tracking_link": SheetSyncModeSpec(
        key="rail_tracking_link",
        plan_schema="affiliate-rail-tracking-link-plan-v1",
        write_schema="affiliate-rail-tracking-link-write-v1",
        mutable_columns=(14, 19, 20, 21, 23, 24),
        required_columns=(14, 19, 20, 21, 23, 24),
        # NO `allowed_status`. This mode does not carry column 10, so per this
        # file's own rule it makes no status claim at all — it may neither write
        # a status nor be steered by one. Recording a link is not an approval.
        # The pre-write gate that DOES apply is `source_status` below: the row
        # must already be approved-and-not-live before a link can be recorded on
        # it. Declaring `allowed_status` here also wrongly listed this mode among
        # the writers of `approved_active_not_wired`, whose disjoint-source-state
        # invariant exists to stop a row being talked up by two routes.
        allowed_status=frozenset(),
        required_flags={19: "FALSE", 20: "FALSE"},
        evidence_fields=(
            ("network", "text"),
            # TWO IDS, ON PURPOSE. `network_program_id` is the network's own
            # numeric programme id — it is what `require_program_identity_binding`
            # matches in BOTH the capture bytes and the target row's identity
            # cells, so neither side is the caller's say-so. `immutable_id` is
            # whatever the RAIL validates a link against, which is frequently a
            # different namespace: an opaque offer token on the Everflow-client
            # rail, a camref on Partnerize, a link id on CJ. Collapsing the two
            # is what produced 13 confident wrong refusals on XenTraffic in
            # August, so this mode carries both and never infers one from the
            # other.
            ("network_program_id", "text"),
            ("immutable_id", "text"),
            ("tracking_link", "url"),
            ("capture_path", "repo_path"),
            ("capture_sha256", "sha256_of:capture_path"),
            ("captured_at_utc", "timestamp"),
            ("capture_quote", "text"),
        ),
        note_must_cite=("network", "network_program_id", "immutable_id",
                        "capture_path", "captured_at_utc"),

        source_status=frozenset({
            "approved_active_not_wired", "approved_active_tracking_ready"}),
        forbidden_source_status=frozenset({
            "approved_active_wired_live", "live_wired_verified",
            "rejected_network_declined_application", "paused_network_program_paused"}),
        allowed_link_qa=frozenset({"not_clicked_preserve_attribution"}),
        forbid_wired_live_source=True,
        require_program_identity_binding=True,
        identity_capture_field="capture_path",
        require_link_rail_validation=True,
        cells_from_evidence={14: "tracking_link"},
        description=(
            "Record a first-party tracking link on ANY registered, verified rail. "
            "The href and the programme's immutable id come from the same "
            "authenticated capture, which is re-hashed before the write; the link "
            "is validated against its rail's own observed shape at write time; the "
            "link is never requested, opened or clicked; and wire-ready and live "
            "both stay FALSE, because opening the gate is a separate decision."),
    ),
    "maxbounty_publisher_id": SheetSyncModeSpec(
        key="maxbounty_publisher_id",
        plan_schema="affiliate-maxbounty-publisher-id-plan-v1",
        write_schema="affiliate-maxbounty-publisher-id-write-v1",
        mutable_columns=(13,),
        required_columns=(13,),
        allowed_status=frozenset(),
        required_flags={},
        evidence_fields=(("publisher_id", "text"), ("tracking_link", "url"),
                         ("network_program_id", "text")),
        note_must_cite=(),
        require_tracking_link=True,
        cells_from_evidence={13: "publisher_id"},
        description="Fill only the publisher-id cell from the existing MaxBounty "
                    "link's literal affiliate parameter; preserve all status and flags.",
    ),
    "maxbounty_tracking_link": SheetSyncModeSpec(
        key="maxbounty_tracking_link",
        plan_schema="affiliate-maxbounty-tracking-link-plan-v1",
        write_schema="affiliate-maxbounty-tracking-link-write-v1",
        mutable_columns=(10, 14, 19, 20, 21, 23, 24),
        required_columns=(10, 14, 19, 20, 21, 23, 24),
        allowed_status=frozenset({"approved_active_tracking_ready"}),
        required_flags={19: "FALSE", 20: "FALSE"},
        evidence_fields=(
            ("mint_receipt_path", "repo_path"),
            ("mint_receipt_sha256", "sha256_of:mint_receipt_path"),
            ("minted_at_utc", "timestamp"),
            ("network_program_id", "text"),
            ("tracking_link", "url"),
            ("tracking_configuration", "text"),
            ("gate_policy_snapshot_path", "repo_path"),
            ("gate_policy_snapshot_sha256", "sha256_of:gate_policy_snapshot_path"),
        ),
        note_must_cite=("mint_receipt_path", "minted_at_utc", "network_program_id",
                        "tracking_configuration"),
        source_status=frozenset({"approved_active_not_wired", "approved_active_tracking_ready"}),
        forbidden_source_status=SHEET_SYNC_LIVE_STATUS,
        allowed_link_qa=frozenset({"not_clicked_preserve_attribution"}),
        require_tracking_link=None,
        cells_from_evidence={14: "tracking_link"},
        description="Store a gated MaxBounty Copy-link result and its full reproducible "
                    "configuration. Keeps wire-ready/live false until the independent rail "
                    "validator accepts the challenger.",
    ),
    # An Everflow WHITE-LABEL BRAND tenant (one advertiser running its own
    # Everflow) issues a per-offer tracking href inside its own authenticated
    # publisher API the moment the offer relationship turns `approved`.
    #
    # `everflow_white_label_inventory` cannot carry that case and must not be
    # bent to: it is bound to `visibility == public`,
    # `offer_affiliate_status == public`, `provider_relationship ==
    # active_public_available` and a `sister_vertical:` fit verdict. Eden's
    # offer 10 is `require_approval` / `approved`, in the CORE GLP-1 vertical,
    # and only became linkable because the brand approved OUR application. Those
    # are different facts, so they get their own mode rather than a loosened one.
    #
    # The binding is the rail contract itself: the cited capture must be a
    # `glp.affiliate_tracking_capture.v1` that `glp_affiliate_link_rails
    # .validate_tracking_capture` accepts, which pins the tenant's declared
    # host, the publisher token, the opaque offer token, the href hash and
    # tracking_clicks == 0. A second, independent capture must show the
    # provider's own `offer_affiliate_status == approved` for that same offer.
    "everflow_brand_portal_tracking_link": SheetSyncModeSpec(
        key="everflow_brand_portal_tracking_link",
        plan_schema="affiliate-everflow-brand-portal-tracking-link-plan-v1",
        write_schema="affiliate-everflow-brand-portal-tracking-link-write-v1",
        mutable_columns=(10, 11, 13, 14, 16, 19, 20, 21, 23, 24),
        required_columns=(10, 11, 13, 14, 16, 19, 20, 21, 23, 24),
        allowed_status=frozenset({"approved_active_tracking_ready"}),
        required_flags={11: "TRUE", 19: "FALSE", 20: "FALSE"},
        evidence_fields=(
            ("rail_capture_path", "repo_path"),
            ("rail_capture_sha256", "sha256_of:rail_capture_path"),
            ("readback_capture_path", "repo_path"),
            ("readback_capture_sha256", "sha256_of:readback_capture_path"),
            ("captured_at_utc", "timestamp"),
            ("network", "text"),
            ("tenant_display_name", "text"),
            ("portal_origin", "url"),
            ("offer_detail_url", "url"),
            ("network_program_id", "text"),
            ("offer_name", "text"),
            ("provider_relationship", "enum:approved"),
            ("publisher_id", "text"),
            ("tracking_link", "url"),
            ("tracking_host", "text"),
            ("tracking_link_sha256", "sha256_hex"),
            ("immutable_id", "text"),
            ("payout_terms", "text"),
            ("exact_paid_event", "text"),
            ("tracking_inspection_method",
             "enum:authenticated publisher API field inspection only"),
            ("applications_submitted", "int_exact0"),
            ("provider_mutations", "int_exact0"),
            ("tracking_links_requested", "int_exact0"),
            ("tracking_links_clicked", "int_exact0"),
            ("tracking_redirects_resolved", "int_exact0"),
        ),
        note_must_cite=("rail_capture_path", "readback_capture_path", "captured_at_utc",
                        "network_program_id", "immutable_id", "exact_paid_event"),
        source_status=frozenset({"approved_active_not_wired",
                                 "approved_active_tracking_ready"}),
        forbidden_source_status=SHEET_SYNC_LIVE_STATUS,
        allowed_link_qa=frozenset({"not_clicked_preserve_attribution"}),
        require_tracking_link=None,
        require_program_identity_binding=True,
        identity_capture_field="rail_capture_path",
        cells_from_evidence={13: "publisher_id", 14: "tracking_link", 16: "payout_terms"},
        description="Copy an Everflow white-label BRAND tenant's own approved-offer tracking "
                    "href out of its authenticated publisher API by field inspection only, "
                    "bound to a rail-validated capture and an independent approved-relationship "
                    "readback. Requests/clicks stay zero; wire-ready and live stay FALSE.",
    ),
    # Affiliati exposes the already-issued provider href in authenticated
    # campaign configuration. This mode copies that field without requesting it,
    # binds it to the exact winning rail and nine-field research packet, and
    # leaves wire-ready/live FALSE for the independent structural gate.
    "affiliati_tracking_link": SheetSyncModeSpec(
        key="affiliati_tracking_link",
        plan_schema="affiliate-affiliati-tracking-link-plan-v1",
        write_schema="affiliate-affiliati-tracking-link-write-v1",
        mutable_columns=(10, 11, 14, 16, 17, 19, 20, 21, 23, 24),
        required_columns=(10, 11, 14, 16, 17, 19, 20, 21, 23, 24),
        allowed_status=frozenset({"approved_active_tracking_ready"}),
        required_flags={11: "TRUE", 19: "FALSE", 20: "FALSE"},
        evidence_fields=(
            ("campaign_capture_path", "repo_path"),
            ("campaign_capture_sha256", "sha256_of:campaign_capture_path"),
            ("captured_at_utc", "timestamp"),
            ("network_program_id", "text"),
            ("campaign_id", "text"),
            ("offer_contract_id", "text"),
            ("tracking_creative_id", "text"),
            ("provider_name", "text"),
            ("provider_relationship", "enum:approved"),
            ("tracking_link", "url"),
            ("tracking_link_sha256", "sha256_hex"),
            ("tracking_href_host", "text"),
            ("payout_terms", "text"),
            ("commission_type", "text"),
            ("exact_paid_event", "text"),
            ("cookie_window", "text"),
            ("geo", "text"),
            ("restrictions", "text"),
            ("tracking_inspection_method", "enum:authenticated campaign configuration field inspection only"),
            ("applications_submitted", "int_exact0"),
            ("provider_mutations", "int_exact0"),
            ("tracking_links_requested", "int_exact0"),
            ("tracking_links_clicked", "int_exact0"),
            ("tracking_redirects_resolved", "int_exact0"),
            ("research_packet_path", "repo_path"),
            ("research_packet_sha256", "sha256_of:research_packet_path"),
            ("reconciliation_path", "repo_path"),
            ("reconciliation_sha256", "sha256_of:reconciliation_path"),
            ("governed_seo_slug", "text"),
        ),
        note_must_cite=(
            "campaign_capture_path", "captured_at_utc", "network_program_id",
            "campaign_id", "research_packet_path", "reconciliation_path",
            "governed_seo_slug",
        ),
        source_status=frozenset({
            "awaiting_network_approval", "approved_affiliati_tracking_link_required",
            "approved_active_tracking_ready",
        }),
        forbidden_source_status=SHEET_SYNC_LIVE_STATUS,
        allowed_link_qa=frozenset({"not_clicked_preserve_attribution"}),
        require_tracking_link=None,
        cells_from_evidence={14: "tracking_link", 16: "payout_terms", 17: "cookie_window"},
        require_program_identity_binding=True,
        identity_capture_field="campaign_capture_path",
        description="Copy a provider-issued Affiliati Link creative href from authenticated "
                    "configuration after best-terms and nine-field research validation. "
                    "Requests/clicks remain zero and wire-ready/live remain false.",
    ),
    # --- payout_evaluation ----------------------------------------------------
    # Every enumeration pass evaluates exact provider identities across all
    # supported rails. This mode writes only its audit basis into Notes; it can
    # run on a live incumbent because it changes no status, flag, link, or terms.
    "payout_evaluation": SheetSyncModeSpec(
        key="payout_evaluation",
        plan_schema="affiliate-payout-evaluation-plan-v1",
        write_schema="affiliate-payout-evaluation-write-v1",
        mutable_columns=(23, 24),
        required_columns=(23, 24),
        allowed_status=frozenset(),
        required_flags={},
        evidence_fields=(
            ("payout_evaluation_path", "repo_path"),
            ("payout_evaluation_sha256", "sha256_of:payout_evaluation_path"),
            ("evaluated_at_utc", "timestamp"),
            ("provider_identity", "text"),
            ("basis", "enum:headline|measured|mixed"),
            ("formula", "text"),
            ("incumbent_effective_value", "text"),
            ("challenger_effective_value", "text"),
            ("decision", "text"),
        ),
        note_must_cite=("payout_evaluation_path", "evaluated_at_utc", "basis",
                        "incumbent_effective_value", "challenger_effective_value", "decision"),
        forbid_wired_live_source=False,
        require_tracking_link=None,
        description="Record the automatic cross-network payout comparison and its headline "
                    "or measured basis. Writes only Notes and Last Checked, including on a "
                    "live incumbent, so it cannot create a monetization gap.",
    ),
    # --- best_terms_winner_confirmed ------------------------------------------
    "best_terms_winner_confirmed": SheetSyncModeSpec(
        key="best_terms_winner_confirmed",
        plan_schema="affiliate-best-terms-winner-confirmed-plan-v1",
        write_schema="affiliate-best-terms-winner-confirmed-write-v1",
        mutable_columns=(10, 19, 20, 21, 23, 24),
        required_columns=(10, 19, 20, 21, 23, 24),
        allowed_status=frozenset({"live_wired_verified"}),
        required_flags={19: "TRUE", 20: "TRUE"},
        evidence_fields=(
            ("route_reconcile_path", "repo_path"),
            ("route_reconcile_sha256", "sha256_of:route_reconcile_path"),
            ("payout_decision_path", "repo_path"),
            ("payout_decision_sha256", "sha256_of:payout_decision_path"),
            ("checked_at_utc", "timestamp"),
            ("release", "text"),
            ("incumbent_sheet_row", "int_min1"),
            ("winner_sheet_row", "int_min1"),
            ("decision", "enum:highest_headline_payout"),
            ("tracking_clicks", "int_exact0"),
        ),
        note_must_cite=("route_reconcile_path", "payout_decision_path", "checked_at_utc",
                        "release", "winner_sheet_row", "decision"),
        source_status=SHEET_SYNC_LIVE_STATUS,
        allowed_link_qa=frozenset({"not_clicked_preserve_attribution"}),
        require_tracking_link=True,
        forbid_wired_live_source=False,
        require_source_approval_claim=True,
        description="Confirm the selected best-terms winner remains the sole href-verified "
                    "live route and repair its wire-ready flag without changing its link.",
    ),
    # --- best_terms_route_retired ---------------------------------------------
    # One merchant can have real approved relationships on two marketplaces, but
    # only the earning winner may stay live. This mode retires the losing ROUTE,
    # not the relationship: its provider-issued link and wire-ready status remain
    # true, while the current-release href receipt and the still-live winner row
    # jointly authorize retracting only the live claim.
    "best_terms_route_retired": SheetSyncModeSpec(
        key="best_terms_route_retired",
        plan_schema="affiliate-best-terms-route-retired-plan-v1",
        write_schema="affiliate-best-terms-route-retired-write-v1",
        mutable_columns=(10, 19, 20, 21, 23, 24),
        required_columns=(10, 19, 20, 21, 23, 24),
        allowed_status=frozenset({"approved_active_tracking_ready"}),
        required_flags={19: "TRUE", 20: "FALSE"},
        evidence_fields=(
            ("route_reconcile_path", "repo_path"),
            ("route_reconcile_sha256", "sha256_of:route_reconcile_path"),
            ("payout_decision_path", "repo_path"),
            ("payout_decision_sha256", "sha256_of:payout_decision_path"),
            ("checked_at_utc", "timestamp"),
            ("release", "text"),
            ("incumbent_sheet_row", "int_min1"),
            ("winner_sheet_row", "int_min1"),
            ("decision", "enum:highest_headline_payout"),
            ("tracking_clicks", "int_exact0"),
        ),
        note_must_cite=("route_reconcile_path", "payout_decision_path", "checked_at_utc",
                        "release", "winner_sheet_row", "decision"),
        source_status=SHEET_SYNC_LIVE_STATUS,
        allowed_link_qa=frozenset({"not_clicked_preserve_attribution"}),
        require_tracking_link=True,
        forbid_wired_live_source=False,
        require_source_approval_claim=True,
        description="Retire only a lower-paying live route after an href-only production "
                    "receipt proves the winner is live and the incumbent is absent. Keeps "
                    "the losing marketplace relationship approved and tracking-ready.",
    ),
    # --- live_claim_correction -----------------------------------------------
    # A site-vs-Sheet reconciliation can prove a narrower fact than `removed`:
    # the provider relationship and wire-ready link remain valid, but the current
    # release has no monetized CTA or /go route. In that case only the stale
    # `Wired Live?` assertion is false. Requiring a removal receipt would force a
    # lie (the provider was not removed), while leaving the checkbox TRUE hides a
    # real revenue gap. This mode retracts column 20 only and preserves status,
    # tracking link, Wire-In Ready?, and Link QA verbatim.
    "live_claim_correction": SheetSyncModeSpec(
        key="live_claim_correction",
        plan_schema="affiliate-live-claim-correction-plan-v1",
        write_schema="affiliate-live-claim-correction-write-v1",
        mutable_columns=(20, 23, 24),
        required_columns=(20, 23, 24),
        allowed_status=frozenset(),
        required_flags={20: "FALSE"},
        evidence_fields=(
            ("site_reconcile_path", "repo_path"),
            ("site_reconcile_sha256", "sha256_of:site_reconcile_path"),
            ("checked_at_utc", "timestamp"),
            ("release", "text"),
            ("sheet_row", "int_min1"),
            ("finding", "enum:stale_review_cta_held|stale_review_live_go_route_unwired"),
        ),
        note_must_cite=("site_reconcile_path", "checked_at_utc", "release", "finding"),
        source_status=SHEET_SYNC_APPROVED_NOT_LIVE_STATUS,
        require_tracking_link=True,
        forbid_wired_live_source=False,
        description="Retract only a stale Wired Live checkbox after a hash-bound current-release "
                    "reconcile proves the approved wire-ready row has no monetized CTA or /go "
                    "route. Preserves the relationship, tracking link and wire-ready gate.",
    ),
    # --- operator_note_secret_scrub -------------------------------------------
    # See SECRET_SCRUB_* above. Removes a live credential from a Sheet cell after
    # the intake lane has moved it into the 0600 secrets store, and can do
    # nothing else: no status, no flags, no link, no row creation.
    "operator_note_secret_scrub": SheetSyncModeSpec(
        key="operator_note_secret_scrub",
        plan_schema="affiliate-operator-note-secret-scrub-plan-v1",
        write_schema="affiliate-operator-note-secret-scrub-write-v1",
        mutable_columns=(23, 24),
        required_columns=(23, 24),
        allowed_status=frozenset(),
        required_flags={},
        evidence_fields=(
            ("secret_transfer_receipt_path", "repo_path"),
            ("secret_transfer_receipt_sha256", "sha256_of:secret_transfer_receipt_path"),
            ("transferred_at_utc", "timestamp"),
            ("secret_store_path", "text"),
            ("credential_fields", "text"),
            ("source_note_sha256", "sha256_hex"),
        ),
        note_must_cite=("secret_store_path", "transferred_at_utc"),
        # A live row's note may hold a secret too; refusing there would leave the
        # worst case unfixable. The mode makes no claim of any kind, so letting it
        # run on a live row costs nothing and closes the hole.
        forbid_wired_live_source=False,
        require_tracking_link=None,
        note_scrub=True,
        description="Replace a Notes cell that holds live credentials with a pointer to "
                    "the secrets store, bound to a transfer receipt that names this row "
                    "and hash-matches the cell's current bytes. Writes no status, no "
                    "flags and no link, and refuses a replacement that still looks like "
                    "it carries a secret.",
    ),
}

# Katalys link acquisition is deliberately NOT rail_tracking_link/wire_in_ready:
# it preserves Notes and Wired Live and binds original captures, not queue claims.
KATALYS_TRACKING_COLUMNS = (10, 14, 19, 21, 24)
KATALYS_TRACKING_SOURCE_STATUSES = frozenset({
    "approved_active_not_wired", "applied_approved_not_wired",
})
SHEET_SYNC_MODES["katalys_tracking_ready"] = SheetSyncModeSpec(
    key="katalys_tracking_ready",
    plan_schema="glp.katalys_tracking_ready_plan.v1",
    write_schema="glp.katalys_tracking_ready_write.v1",
    mutable_columns=KATALYS_TRACKING_COLUMNS,
    required_columns=KATALYS_TRACKING_COLUMNS,
    allowed_status=frozenset({"approved_active_tracking_ready"}),
    required_flags={19: "TRUE"}, evidence_fields=(), note_must_cite=(),
    source_status=KATALYS_TRACKING_SOURCE_STATUSES,
    allowed_link_qa=frozenset({"not_clicked_preserve_attribution"}),
    cells_from_evidence={14: "tracking_link", 24: "checked_at_utc"},
    description="Frozen first-party Katalys link capture; five cells, no live claim.",
)


#: Queue `kind` -> writer mode. Every kind maps to exactly one governed mode;
#: an unmapped kind is escalated by the drain, never guessed at.
SHEET_SYNC_KIND_MODE = {
    "maxbounty_publisher_id": "maxbounty_publisher_id",
    "maxbounty_identity_category_repair": "maxbounty_identity_category_repair",
    "katalys_tracking_ready": "katalys_tracking_ready",
    "new_approval": "add_approved_row",
    "application_pending": "application_pending",
    "rejection": "rejection",
    "network_paused": "network_paused_disposition",
    "network_cancelled": "network_cancelled",
    "status_correction": "status_correction",
    "partner_email_hold": "partner_email_hold",
    "deployed_live": "deployed_live",
    "removed": "removed",
    "evidence_blocked": "evidence_blocked",
    "marketplace_rail_status": "marketplace_rail_status",
    "fit_assessment": "fit_assessment",
    "provider_fit_reclassification": "provider_fit_reclassification",
    "provider_slug_provisioning": "provider_slug_provisioning",
    "provider_slug_correction": "provider_slug_correction",
    "marketplace_parent_relabel": "marketplace_parent_relabel",
    "cross_network_duplicate_relationship": "cross_network_duplicate_relationship",
    "approval_confirmed": "approval_confirmed",
    "invitation_disposition": "invitation_disposition",
    "marketplace_candidate": "marketplace_candidate",
    "impact_program_candidate": "impact_program_candidate",
    "commercial_economics_hold": "commercial_economics_hold",
    "network_reactivated": "network_reactivated",
    "portal_readback_confirmed": "portal_readback_confirmed",
    "operator_status_ruling": "operator_status_ruling",
    "operator_note_secret_scrub": "operator_note_secret_scrub",
    "wire_in_ready": "wire_in_ready",
    "provider_scope_retraction": "provider_scope_retraction",
    "everflow_white_label_inventory": "everflow_white_label_inventory",
    "affiliati_inventory": "affiliati_inventory",
    "affiliati_switchover_stage": "affiliati_switchover_stage",
    "higher_rail_pending_replacement": "higher_rail_pending_replacement",
    "maxbounty_inventory": "maxbounty_inventory",
    "provider_relationship_withdrawn": "provider_relationship_withdrawn",
    "rail_tracking_link": "rail_tracking_link",
    "maxbounty_tracking_link": "maxbounty_tracking_link",
    "everflow_brand_portal_tracking_link": "everflow_brand_portal_tracking_link",
    "affiliati_tracking_link": "affiliati_tracking_link",
    "payout_evaluation": "payout_evaluation",
    "best_terms_winner_confirmed": "best_terms_winner_confirmed",
    "best_terms_route_retired": "best_terms_route_retired",
    "live_claim_correction": "live_claim_correction",
    # Phase 1b: the inbox mirror classifies Awin's rate-change notice as
    # `commission_terms_changed` and had NO mode to route to, so three real
    # terms changes sat unroutable (H3/H4/H5) with the live Coivas row quoting a
    # retired rate.
    "commission_terms_changed": "commission_terms_correction",
    # One-time/bounded damage repair still enters through sheet_update_queue and
    # the same single-writer lock. This special mode clears exact hash-bound rows;
    # it is not a general deletion surface.
    "junk_row_purge": "junk_row_purge",
}

_REPO_ROOT = Path("/home/ubuntu/business")
_EVIDENCE_PATH_PREFIXES = ("reports/", "site-system/", "data/")
_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T[\d:.+\-]+Z?")


def sheet_sync_write_value(column: int, value: Any) -> Any:
    """Coerce a planned cell to the type the column stores (see wire_in_write_value)."""
    if column in SHEET_SYNC_CHECKBOX_COLUMNS:
        return str(value).strip().upper() == "TRUE"
    return value


def sheet_sync_cell_equal(column: int, actual: Any, expected: Any) -> bool:
    """Compare one planned cell, treating all four checkbox columns as booleans."""
    if column in SHEET_SYNC_CHECKBOX_COLUMNS:
        if isinstance(actual, bool):
            return actual is sheet_sync_write_value(column, expected)
        if actual == "":
            return sheet_sync_write_value(column, expected) is False
        return False
    return cell_equal(actual, expected)


def sheet_sync_changed_cells(
    values: list[list[Any]], rows: list[dict[str, Any]],
) -> list[tuple[int, int, Any]]:
    """Diff the plan against the live sheet, returning only cells that differ."""
    changes: list[tuple[int, int, Any]] = []
    for entry in rows:
        row_num = int(entry["row"])
        actual = pad(values[row_num - 1] if len(values) >= row_num else [])
        for column, expected in entry["cells"].items():
            column = int(column)
            if not sheet_sync_cell_equal(column, actual[column], expected):
                changes.append((row_num, column, expected))
    return changes


def _sheet_sync_evidence_value(evidence: dict[str, Any], field: str) -> str:
    return str(evidence.get(field, "")).strip()


def _file_mtime_utc(path: Path) -> str:
    """When the cited evidence file was last written, for a drift refusal to name."""
    try:
        return datetime.fromtimestamp(
            path.stat().st_mtime, tz=timezone.utc).replace(
                microsecond=0).isoformat().replace("+00:00", "Z")
    except OSError:
        return "unknown"


def require_sheet_sync_evidence(spec: SheetSyncModeSpec, entry: dict[str, Any], label: str) -> None:
    """Prove every declared evidence field is present, well-formed and real.

    "Real" is load-bearing: a path field must resolve to a file that exists on
    disk under the business root, and a declared digest must hash-match it. An
    evidence citation nobody can open is not evidence.
    """
    evidence = entry.get("evidence")
    if not isinstance(evidence, dict):
        raise RuntimeError(f"{spec.key}_evidence_missing:{label}")
    for field, kind in spec.evidence_fields:
        raw = _sheet_sync_evidence_value(evidence, field)
        if not raw:
            raise RuntimeError(f"{spec.key}_evidence_field_missing:{label}:{field}")
        if kind == "repo_path":
            if not raw.startswith(_EVIDENCE_PATH_PREFIXES):
                raise RuntimeError(f"{spec.key}_evidence_path_invalid:{label}:{field}")
            if ".." in raw:
                raise RuntimeError(f"{spec.key}_evidence_path_escape:{label}:{field}")
            if not (_REPO_ROOT / raw).is_file():
                raise RuntimeError(f"{spec.key}_evidence_file_missing:{label}:{raw}")
        elif kind.startswith("sha256_of:"):
            target = kind.split(":", 1)[1]
            if not re.fullmatch(r"[a-f0-9]{64}", raw):
                raise RuntimeError(f"{spec.key}_evidence_sha256_invalid:{label}:{field}")
            relative = _sheet_sync_evidence_value(evidence, target)
            path = _REPO_ROOT / relative
            if not path.is_file():
                raise RuntimeError(f"{spec.key}_evidence_file_missing:{label}:{target}")
            observed = hashlib.sha256(path.read_bytes()).hexdigest()
            if observed != raw:
                # Never weakened: changed evidence still refuses. But the refusal
                # has to be diagnosable in ONE read, so it names the file whose
                # bytes moved, both digests and when the file was last rewritten.
                # The 2026-09-01 repeat offender was a producer citing a path a
                # later run of the same producer overwrote in place; the old
                # message named only the FIELD, so 728 dead rows looked like one
                # unexplained refusal repeated 15 times.
                raise RuntimeError(
                    f"{spec.key}_evidence_hash_drift:{label}:{target}"
                    f":path={relative}"
                    f":queued_sha={raw[:16]}"
                    f":file_sha={observed[:16]}"
                    f":file_mtime_utc={_file_mtime_utc(path)}"
                )
        elif kind == "timestamp":
            if not _TIMESTAMP_RE.fullmatch(raw):
                raise RuntimeError(f"{spec.key}_evidence_timestamp_invalid:{label}:{field}")
        elif kind == "url":
            if not raw.startswith("https://"):
                raise RuntimeError(f"{spec.key}_evidence_url_invalid:{label}:{field}")
        elif kind == "int_min1":
            try:
                if int(raw) < 1:
                    raise ValueError(raw)
            except (TypeError, ValueError) as exc:
                raise RuntimeError(f"{spec.key}_evidence_count_invalid:{label}:{field}") from exc
        elif kind == "int_exact0":
            # A mode named for a zero may only be handed a zero. This is not a
            # formatting check: a caller who ships a positive value here is
            # describing a program that can EARN, and the correct answer is to
            # refuse and let it back onto the wire-in path.
            try:
                if int(raw) != 0:
                    raise ValueError(raw)
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"{spec.key}_evidence_value_must_be_zero:{label}:{field}:{raw}") from exc
        elif kind == "zero_or_null":
            # The networks spell "no rate" three ways and all three are the same
            # fact: JSON `null` (which arrives here as the string `None` or
            # `null` depending on how the queuer serialized it) and a literal 0.
            # Anything else - including a rate of "0.5" - is a positive rate.
            if raw.strip().lower() not in ("0", "0.0", "null", "none"):
                raise RuntimeError(
                    f"{spec.key}_evidence_value_must_be_zero_or_null:{label}:{field}:{raw}")
        elif kind == "sha256_hex":
            # A digest of something that is NOT a repo file - e.g. the bytes a
            # Sheet cell holds. Format-checked here; bound to the real thing by
            # the mode's own precondition.
            if not re.fullmatch(r"[a-f0-9]{64}", raw):
                raise RuntimeError(f"{spec.key}_evidence_sha256_invalid:{label}:{field}")
        elif kind.startswith("enum:"):
            allowed = set(kind.split(":", 1)[1].split("|"))
            if raw not in allowed:
                raise RuntimeError(f"{spec.key}_evidence_value_not_allowed:{label}:{field}:{raw}")
        elif kind != "text":
            raise RuntimeError(f"{spec.key}_evidence_kind_unknown:{label}:{kind}")


def require_maxbounty_inventory_capture(
    spec: SheetSyncModeSpec, entry: dict[str, Any], cells: dict[str, Any],
) -> None:
    """Bind a MaxBounty inventory projection to its exact authenticated campaign."""
    evidence = entry["evidence"]
    program = str(entry.get("program") or "").strip()
    program_id = _sheet_sync_evidence_value(evidence, "network_program_id")
    capture_path = _REPO_ROOT / _sheet_sync_evidence_value(evidence, "portal_capture_path")
    try:
        capture = json.loads(capture_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{spec.key}_capture_unreadable:{program}") from exc
    candidates = [
        item for item in (capture.get("programs") or [])
        if isinstance(item, dict) and str(item.get("program_id") or "").strip() == program_id
    ]
    if len(candidates) != 1:
        raise RuntimeError(
            f"{spec.key}_capture_program_identity_count:{program}:{program_id}:{len(candidates)}")
    captured = candidates[0]
    bindings = {
        "name": program,
        "detail_url": _sheet_sync_evidence_value(evidence, "detail_url"),
        "official_url": _sheet_sync_evidence_value(evidence, "official_url"),
        "relationship_button": _sheet_sync_evidence_value(evidence, "relationship_button"),
        "fit_classification": _sheet_sync_evidence_value(evidence, "fit_classification"),
        "vertical": _sheet_sync_evidence_value(evidence, "vertical"),
        "fit_reason": _sheet_sync_evidence_value(evidence, "fit_reason"),
    }
    drift = sorted(
        field for field, expected in bindings.items()
        if str(captured.get(field) or "").strip() != expected
    )
    if drift:
        raise RuntimeError(
            f"{spec.key}_capture_projection_drift:{program}:{','.join(drift)}")

    approved_core = (
        bindings["relationship_button"] == "Build tracking link"
        and bindings["fit_classification"] == "core_glp1"
    )
    if not approved_core:
        return
    get = lambda column: str(cells.get(str(column), cells.get(column)) or "").strip()  # noqa: E731
    slug = _sheet_sync_evidence_value(evidence, "seo_slug")
    suggested = _sheet_sync_evidence_value(evidence, "suggested_glp_page")
    basis = _sheet_sync_evidence_value(evidence, "site_identity_basis")
    checks = {
        "slug_format": re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug) is not None,
        "suggested_equals_slug": suggested == slug,
        "cell_slug": get(9) == slug,
        "cell_suggested": get(8) == suggested,
        "capture_slug": str(captured.get("seo_slug") or "").strip() == slug,
        "capture_suggested": (
            str(captured.get("suggested_glp_page") or "").strip() == suggested),
        "capture_basis": str(captured.get("basis") or "").strip() == basis,
        "basis_allowed": basis in {
            "operator_mission_explicit_site_slug",
            "same_official_domain_existing_site_slug",
            "deterministic_provider_review_slug",
        },
    }
    failed = sorted(name for name, ok in checks.items() if not ok)
    if failed:
        raise RuntimeError(
            f"{spec.key}_approved_core_site_identity_unbound:"
            f"{program}:{program_id}:{','.join(failed)}")


def require_everflow_white_label_inventory_capture(
    spec: SheetSyncModeSpec, entry: dict[str, Any], cells: dict[str, Any],
) -> None:
    """Bind every Everflow inventory cell to one authenticated offer response.

    The capture proves tenant/account identity, public availability, economics,
    exact provider link and the absence of a cookie-window field. The separate
    fit ledger proves the official brand domain and sister-vertical verdict.
    Neither a valid capture for another tenant nor a caller-typed payout can pass.
    """
    from urllib.parse import urlparse

    evidence = entry["evidence"]
    program = str(entry.get("program") or "").strip()
    capture_path = _REPO_ROOT / _sheet_sync_evidence_value(evidence, "portal_capture_path")
    try:
        capture = json.loads(capture_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{spec.key}_capture_unreadable:{program}") from exc

    checks = {
        "schema": capture.get("schema") == "glp.everflow_white_label.offer_api_capture.v1",
        "status": capture.get("status") == "PASS",
        "authenticated": capture.get("authenticated") is True,
        "tenant_identity": capture.get("tenant_identity_verified") is True,
        "account_identity": capture.get("account_identity_verified") is True,
        "no_blocker": capture.get("blocker") is None,
        "captured_at": str(capture.get("captured_at_utc") or "")
                       == _sheet_sync_evidence_value(evidence, "captured_at_utc"),
        "origin": str(capture.get("origin") or "").rstrip("/")
                  == _sheet_sync_evidence_value(evidence, "portal_origin").rstrip("/"),
        "tenant": str(capture.get("tenant_display_name") or "")
                  == _sheet_sync_evidence_value(evidence, "tenant_display_name"),
    }
    statuses = capture.get("source_http_statuses") or {}
    required_sources = {
        "bff_user", "affiliate_user", "network", "affiliate", "offerstable", "offer_detail",
    }
    checks["all_authenticated_reads_200"] = (
        required_sources <= set(statuses)
        and all(statuses.get(key) == 200 for key in required_sources)
    )
    effects = capture.get("effects") or {}
    checks["no_provider_mutations"] = all(
        effects.get(key) in (0, 0.0) for key in (
            "applications_submitted", "tracking_links_generated", "affiliate_redirect_clicks",
            "provider_business_writes", "sheet_writes", "deploys", "spend_usd",
        )
    )

    table = capture.get("target_table_entry") or {}
    detail = capture.get("offer_detail") or {}
    account = capture.get("publisher_account") or {}
    table_relationship = table.get("relationship") or {}
    detail_relationship = detail.get("relationship") or {}
    offer_id = _sheet_sync_evidence_value(evidence, "network_program_id")
    offer_name = _sheet_sync_evidence_value(evidence, "offer_name")
    try:
        numeric_offer_id = int(offer_id)
    except ValueError as exc:
        raise RuntimeError(f"{spec.key}_offer_id_invalid:{program}:{offer_id!r}") from exc
    checks.update({
        "offer_id": (capture.get("offer_id") == numeric_offer_id
                     and table.get("network_offer_id") == numeric_offer_id
                     and detail.get("network_offer_id") == numeric_offer_id),
        "network_id": (account.get("network_id") == table.get("network_id")
                       == detail.get("network_id")),
        "offer_name": table.get("name") == detail.get("name") == offer_name,
        "offer_active": table.get("offer_status") == detail.get("offer_status") == "active",
        "offer_public": table.get("visibility") == detail.get("visibility") == "public",
        "relationship_public": (
            table_relationship.get("offer_affiliate_status") == "public"
            and detail_relationship.get("offer_affiliate_status") == "public"
            and _sheet_sync_evidence_value(evidence, "provider_relationship")
            == "active_public_available"
        ),
        "account_active": (account.get("account_status") == "active"
                           and account.get("affiliate_account_status") == "active"),
        "publisher_id": str(account.get("affiliate_id") or "")
                        == _sheet_sync_evidence_value(evidence, "publisher_id"),
        "program_names_tenant": norm(capture.get("tenant_display_name")) in norm(program),
    })

    tracking = _sheet_sync_evidence_value(evidence, "tracking_link")
    tracking_host = _sheet_sync_evidence_value(evidence, "tracking_host").casefold()
    checks.update({
        "tracking_link": detail.get("tracking_url") == detail.get("redirect_tracking_url") == tracking,
        "tracking_host": (urlparse(tracking).scheme == "https"
                          and (urlparse(tracking).hostname or "").casefold() == tracking_host),
        "offer_detail_url": _sheet_sync_evidence_value(evidence, "offer_detail_url")
                            == (f"{str(capture.get('origin') or '').rstrip('/')}/offers/"
                                f"{numeric_offer_id}"),
    })

    payouts = (detail_relationship.get("payouts") or {}).get("entries") or []
    default_payouts = [item for item in payouts if item.get("is_default") is True]
    # Everflow's detail response nests payouts under the detail relationship but
    # exposes conversion-event inventory on the table relationship. Both come
    # from the same authenticated offer ID and are cross-checked above.
    events = table_relationship.get("events") or {}
    if len(default_payouts) != 1:
        raise RuntimeError(f"{spec.key}_default_payout_missing_or_ambiguous:{program}")
    payout = default_payouts[0]
    try:
        amount = float(payout.get("payout_amount"))
        table_amount = float(table.get("payout_amount"))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{spec.key}_payout_amount_invalid:{program}") from exc
    paid_event = str(payout.get("entry_name") or "").strip()
    expected_terms = f"{paid_event} conversion event: USD ${amount:.2f} CPA"
    checks.update({
        "paid_event": paid_event == _sheet_sync_evidence_value(evidence, "paid_event"),
        "payout_type": str(payout.get("payout_type") or "").casefold() == "cpa",
        "payout_amount": amount == table_amount,
        "payout_currency": detail.get("currency_id") == "USD",
        "payout_terms": expected_terms == _sheet_sync_evidence_value(evidence, "payout_terms"),
        "payout_table_display": table.get("payout") == f"CPA: ${amount:.3f}",
        "no_additional_paid_events": events.get("total") == 0 and not (events.get("entries") or []),
    })

    def keys_of(value: Any) -> set[str]:
        if isinstance(value, dict):
            return {str(key).casefold() for key in value} | {
                nested for item in value.values() for nested in keys_of(item)
            }
        if isinstance(value, list):
            return {nested for item in value for nested in keys_of(item)}
        return set()

    cookie_keys = {key for key in keys_of(detail)
                   if "cookie" in key or "attribution" in key}
    checks["cookie_window_unavailable"] = (
        _sheet_sync_evidence_value(evidence, "cookie_window_availability")
        == "unavailable_in_captured_offer_detail" and not cookie_keys
    )
    get = lambda column: cells.get(str(column), cells.get(column))  # noqa: E731
    checks["cookie_cell_blank"] = str(get(17) or "").strip() == ""

    ledger_path = _REPO_ROOT / _sheet_sync_evidence_value(evidence, "fit_ledger_path")
    try:
        ledger = json.loads(ledger_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{spec.key}_fit_ledger_unreadable:{program}") from exc
    fit_matches = [item for item in (ledger.get("entries") or [])
                   if item.get("program") == program and int(item.get("sheet_row") or 0)
                   == int(entry.get("row") or 0)]
    if len(fit_matches) != 1:
        raise RuntimeError(f"{spec.key}_fit_ledger_row_missing_or_ambiguous:{program}")
    fit = fit_matches[0]
    cite = fit.get("cite") or {}
    cite_path = _REPO_ROOT / str(cite.get("capture_path") or "")
    cite_hash_ok = cite_path.is_file() and hashlib.sha256(cite_path.read_bytes()).hexdigest() \
        == str(cite.get("capture_sha256") or "")
    cite_body = cite_path.read_text(errors="replace") if cite_hash_ok else ""
    if cite_body:
        import html
        cite_body = html.unescape(cite_body)
    official_url = _sheet_sync_evidence_value(evidence, "official_url")
    checks.update({
        "fit_network": canonical_network(fit.get("network")) == "everflow",
        "fit_official_url": fit.get("official_url") == cite.get("source_url") == official_url,
        "fit_verdict": str(fit.get("verdict") or "").startswith("sister_vertical:"),
        "fit_vertical": _sheet_sync_evidence_value(evidence, "vertical") == "general-wellness",
        "fit_citation": cite_hash_ok and str(cite.get("quote") or "") in cite_body,
        "fit_reason": ("sister vertical" in _sheet_sync_evidence_value(evidence, "fit_reason").casefold()
                       and "no glp-1" in _sheet_sync_evidence_value(evidence, "fit_reason").casefold()),
    })

    failed = sorted(key for key, passed in checks.items() if not passed)
    if failed:
        raise RuntimeError(
            f"{spec.key}_capture_does_not_prove_inventory:{program}:{','.join(failed)}")


def require_everflow_brand_portal_tracking_capture(
    spec: SheetSyncModeSpec, entry: dict[str, Any], cells: dict[str, Any],
) -> None:
    """Bind every cell of an Everflow brand-portal link write to two captures.

    Capture 1 is the RAIL capture. It is re-validated here through the rail
    table itself (`glp_affiliate_link_rails.validate_tracking_capture`), so the
    tenant's tracking host, our publisher token, the opaque offer token, the
    href hash and `tracking_clicks == 0` are all re-proved at write time rather
    than trusted from a receipt.

    Capture 2 is the RELATIONSHIP readback from the same tenant's own
    authenticated publisher API. It must report `offer_affiliate_status ==
    approved` for that exact offer. An approved relationship and an issued href
    are two different facts and the writer requires both, because an href alone
    would let a merely-visible offer be written up as an approval.

    The payout sentence is READ OFF the readback, never typed by the caller.
    """
    from urllib.parse import urlparse

    evidence = entry["evidence"]
    program = str(entry.get("program") or "").strip()

    import sys as _sys
    _scripts = str(_REPO_ROOT / "scripts")
    if _scripts not in _sys.path:
        _sys.path.insert(0, _scripts)
    import glp_affiliate_link_rails as _rails

    rail_path = _REPO_ROOT / _sheet_sync_evidence_value(evidence, "rail_capture_path")
    readback_path = _REPO_ROOT / _sheet_sync_evidence_value(evidence, "readback_capture_path")
    try:
        rail_capture = json.loads(rail_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{spec.key}_rail_capture_unreadable:{program}") from exc
    try:
        readback = json.loads(readback_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{spec.key}_readback_capture_unreadable:{program}") from exc

    try:
        _rails.validate_tracking_capture(rail_capture)
    except _rails.RailError as exc:
        raise RuntimeError(f"{spec.key}_rail_capture_rejected:{program}:{exc}") from exc

    network = _sheet_sync_evidence_value(evidence, "network")
    tracking = _sheet_sync_evidence_value(evidence, "tracking_link")
    tracking_host = _sheet_sync_evidence_value(evidence, "tracking_host").casefold()
    immutable_id = _sheet_sync_evidence_value(evidence, "immutable_id")
    offer_id = _sheet_sync_evidence_value(evidence, "network_program_id")
    offer_name = _sheet_sync_evidence_value(evidence, "offer_name")
    origin = _sheet_sync_evidence_value(evidence, "portal_origin").rstrip("/")

    checks = {
        "rail_capture_schema": rail_capture.get("schema") == _rails.CAPTURE_SCHEMA,
        "rail_network": str(rail_capture.get("network") or "") == network,
        "rail_network_registered": _rails.rail_for(network) is not None,
        "rail_verified": bool(getattr(_rails.rail_for(network), "verified", False)),
        "rail_link": str(rail_capture.get("tracking_link") or "") == tracking,
        "rail_immutable_id": str(rail_capture.get("immutable_id") or "") == immutable_id,
        "rail_host": str(rail_capture.get("tracking_host") or "").casefold() == tracking_host,
        "rail_link_sha256": (
            str(rail_capture.get("tracking_link_sha256") or "")
            == _sheet_sync_evidence_value(evidence, "tracking_link_sha256")
            == hashlib.sha256(tracking.encode("utf-8")).hexdigest()),
        "rail_clicks_zero": rail_capture.get("tracking_clicks") in (0, 0.0),
        "rail_offer_binding": str(rail_capture.get("offer_id") or "") == offer_id,
        "link_https_on_declared_host": (
            urlparse(tracking).scheme == "https"
            and (urlparse(tracking).hostname or "").casefold() == tracking_host),
        "offer_detail_url": (
            _sheet_sync_evidence_value(evidence, "offer_detail_url")
            == f"{origin}/offers/{offer_id}"),
        "declared_relationship_approved": (
            _sheet_sync_evidence_value(evidence, "provider_relationship") == "approved"),
    }

    readback_offers = [
        row for row in (readback.get("offers") or [])
        if str(row.get("offer_id")) == offer_id
    ]
    checks["readback_authenticated"] = readback.get("authenticated") is True
    checks["readback_portal_bound"] = str(readback.get("final_url") or "").rstrip("/") == (
        f"{origin}/offers")
    checks["readback_tenant"] = _sheet_sync_evidence_value(
        evidence, "tenant_display_name") in str(readback.get("page_title") or "")
    checks["readback_names_offer"] = len(readback_offers) == 1
    if len(readback_offers) == 1:
        observed = readback_offers[0]
        checks["readback_offer_name"] = str(observed.get("name") or "").strip() == offer_name
        checks["readback_relationship_approved"] = (
            observed.get("affiliate_status") == "approved")
        checks["readback_offer_active"] = observed.get("offer_status") == "active"
        payout = str(observed.get("payout") or "").strip()
        checks["readback_payout_terms"] = (
            payout and payout == _sheet_sync_evidence_value(evidence, "payout_terms"))
    effects = readback.get("effects") or {}
    checks["readback_no_effects"] = all(
        effects.get(key) in (0, 0.0) for key in
        ("provider_writes", "tracking_clicks", "tracking_opens", "sheet_writes", "deploys")
        if key in effects)

    get = lambda column: cells.get(str(column), cells.get(column))  # noqa: E731
    checks["cell_tracking_link"] = str(get(14) or "") == tracking
    checks["cell_payout_terms"] = str(get(16) or "") == _sheet_sync_evidence_value(
        evidence, "payout_terms")
    checks["cell_publisher_id"] = str(get(13) or "") == _sheet_sync_evidence_value(
        evidence, "publisher_id")

    failed = sorted(key for key, passed in checks.items() if not passed)
    if failed:
        raise RuntimeError(
            f"{spec.key}_capture_does_not_prove_link:{program}:{','.join(failed)}")


def require_everflow_white_label_inventory_preconditions(
    spec: SheetSyncModeSpec, values: list[list[Any]], entry: dict[str, Any],
    row_num: int, actual: list[Any],
) -> None:
    """Bind the capture to this existing tenant row and prove there is only one."""
    from urllib.parse import urlparse

    evidence = entry["evidence"]
    origin_host = (urlparse(_sheet_sync_evidence_value(evidence, "portal_origin")).hostname or "").casefold()
    row_hosts = {(urlparse(str(actual[column] or "")).hostname or "").casefold()
                 for column in (5, 12)}
    if canonical_network(actual[4]) != "everflow":
        raise RuntimeError(f"{spec.key}_row_is_not_everflow:{row_num}:{actual[4]!r}")
    if not origin_host or origin_host not in row_hosts:
        raise RuntimeError(f"{spec.key}_portal_tenant_contradicts_sheet:{row_num}:{origin_host}")

    same_tenant_rows = []
    for candidate_row, raw in enumerate(values[1:], 2):
        candidate = pad(raw)
        if not row_has_business_value(candidate) or canonical_network(candidate[4]) != "everflow":
            continue
        hosts = {(urlparse(str(candidate[column] or "")).hostname or "").casefold()
                 for column in (5, 12)}
        if origin_host in hosts:
            same_tenant_rows.append(candidate_row)
    if same_tenant_rows != [row_num]:
        raise RuntimeError(
            f"{spec.key}_same_tenant_row_missing_or_duplicated:{origin_host}:{same_tenant_rows}")


def katalys_apply_id(value: Any) -> str:
    """Independent immutable identity; never infer it from the new href/Notes."""
    from urllib.parse import urlsplit
    raw = str(value or "")
    try:
        url = urlsplit(raw)
        matches = re.findall(r"/(?:explore|offers)/([0-9]+)(?=/|$)", url.path)
        if (raw != raw.strip() or url.scheme != "https" or url.hostname != "app.katalys.com"
                or url.username or url.password or url.port not in (None, 443)
                or len(matches) != 1):
            return ""
        return matches[0]
    except ValueError:
        return ""


def require_katalys_tracking_ready(entry: dict[str, Any], values: list[list[Any]] | None = None) -> None:
    """Rebind original evidence and current baseline before apply AND replay.

    authority_sha256 is capture provenance, never independent release approval.
    No network IO, tracking requests or evidence materialization occurs here.
    """
    from urllib.parse import urlsplit, parse_qs
    def refuse(reason: str) -> None:
        raise RuntimeError(f"katalys_tracking_ready_{reason}")
    evidence = entry.get("evidence")
    if not isinstance(evidence, dict):
        refuse("evidence_missing")
    pid = entry.get("provider_program_id")
    name = entry.get("program")
    row_num = entry.get("row")
    if (not isinstance(pid, str) or not re.fullmatch(r"[0-9]+", pid)
            or entry.get("network") != "Katalys" or not isinstance(name, str)
            or not name or name != name.strip() or type(row_num) is not int or row_num < 2
            or evidence.get("network_program_id") != pid):
        refuse("identity_invalid")
    if "fella" in name.casefold():
        refuse("fella_permanently_excluded")
    require_operator_ruling_clear([entry], load_snapshot())
    captures, raw_bytes = {}, {}
    for key in ("coverage", "inventory", "tracking_capture", "sheet_capture"):
        raw = evidence.get(key + "_path")
        digest = evidence.get(key + "_sha256")
        if (not isinstance(raw, str) or not raw or raw != raw.strip()
                or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
            refuse(f"evidence_binding_invalid:{key}")
        relative = Path(raw)
        if ".." in relative.parts:
            refuse(f"evidence_path_escape:{key}")
        path = (_REPO_ROOT / relative).resolve()
        if not path.is_relative_to(_REPO_ROOT.resolve()):
            refuse(f"evidence_path_escape:{key}")
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise RuntimeError(f"katalys_tracking_ready_evidence_unreadable:{key}") from exc
        if hashlib.sha256(data).hexdigest() != digest:
            refuse(f"evidence_hash_drift:{key}")
        try:
            captures[key] = json.loads(data)
        except (ValueError, UnicodeError) as exc:
            raise RuntimeError(f"katalys_tracking_ready_capture_invalid:{key}") from exc
        if not isinstance(captures[key], dict):
            refuse(f"capture_invalid:{key}")
        raw_bytes[key] = data
    authority = hashlib.sha256(raw_bytes["coverage"] + raw_bytes["inventory"]
                               + raw_bytes["tracking_capture"]).hexdigest()
    if evidence.get("authority_sha256") != authority:
        refuse("original_authority_mismatch")
    checked = evidence.get("checked_at_utc")
    try:
        when = datetime.fromisoformat(checked.replace("Z", "+00:00"))
        if when.utcoffset() is None or when.utcoffset().total_seconds() != 0:
            refuse("timestamp_not_utc")
    except (ValueError, TypeError, AttributeError):
        refuse("timestamp_invalid")
    href = evidence.get("tracking_link")
    try:
        url = urlsplit(href)
        query = parse_qs(url.query, keep_blank_values=True)
        if (not isinstance(href, str) or any(ch.isspace() or ord(ch) < 32 for ch in href)
                or url.scheme != "https" or url.hostname != "track.revoffers.com"
                or url.path != "/aff_c" or url.username or url.password
                or url.port not in (None, 443) or url.fragment
                or query.get("offer_id") != [pid] or query.get("aff_id") != ["13031"]):
            refuse("tracking_link_invalid")
    except (TypeError, ValueError, AttributeError):
        refuse("tracking_link_invalid")
    tracking = captures["tracking_capture"]
    if tracking.get("captured_at_utc") != checked:
        refuse("capture_timestamp_mismatch")
    generated = [item for item in tracking.get("programs", [])
                 if str(item.get("offer_id", "")) == pid]
    if (len(generated) != 1 or generated[0].get("status") != "GENERATED_VERIFIED"
            or generated[0].get("name") != name or generated[0].get("tracking_link") != href
            or str(generated[0].get("traffic_source_id")) != "13031"
            or tracking.get("traffic_source") != "GLP Clinic Scout Site"
            or str(tracking.get("traffic_source_id")) != "13031"
            or tracking.get("tracking_clicks", 0) != 0):
        refuse("generated_capture_mismatch")
    coverage = captures["coverage"]
    joins = [item for item in (coverage.get("join") or {}).get("program_dispositions", [])
             if str(item.get("network", "")).casefold() == "katalys"
             and str(item.get("program_id", "")) == pid]
    if (coverage.get("status") != "PASS" or len(joins) != 1
            or joins[0].get("disposition") != "MATCHED_TO_SHEET"
            or joins[0].get("sheet_row") != row_num or joins[0].get("name") != name):
        refuse("coverage_identity_missing_or_ambiguous")
    inventory = [item for item in captures["inventory"].get("programs", [])
                 if str(item.get("program_id", "")) == pid]
    if len(inventory) != 1 or inventory[0].get("relationship") != "approved":
        refuse("inventory_not_uniquely_approved")
    source = captures["sheet_capture"]
    if source.get("headers") != HEADERS or not isinstance(source.get("rows"), dict):
        refuse("original_sheet_shape_invalid")
    original = pad(source["rows"].get(str(row_num), []))
    desired = {"10": "approved_active_tracking_ready", "14": href, "19": "TRUE",
               "21": "not_clicked_preserve_attribution", "24": checked}
    cells = {str(k): v for k, v in entry.get("cells", {}).items()}
    if cells != desired:
        refuse("exact_five_cell_contract_invalid")
    def eligible(row: list[Any], *, replay: bool, captured: bool = False) -> None:
        # Backrun exports use formatted TRUE/FALSE strings; the live API must
        # still prove typed booleans through the canonical schema guard.
        signup = (str(row[11]).upper() == "TRUE" if captured
                  else sheet_sync_cell_equal(11, row[11], "TRUE"))
        not_live = (row[20] in ("", "FALSE", False) if captured
                    else sheet_sync_cell_equal(20, row[20], "FALSE"))
        if (row[2] != name or row[4] != "Katalys" or katalys_apply_id(row[5]) != pid
                or not signup or str(row[25]).casefold() != "glp1-weightloss" or not not_live):
            refuse("source_identity_signup_vertical_or_live_invalid")
        if not replay and (row[10] not in KATALYS_TRACKING_SOURCE_STATUSES
                           or str(row[14] or "").strip()):
            refuse("source_status_or_link_invalid")
    eligible(original, replay=False, captured=True)
    original_hits = [key for key, row in source["rows"].items()
                     if pad(row)[4] == "Katalys" and katalys_apply_id(pad(row)[5]) == pid]
    if original_hits != [str(row_num)]:
        refuse("original_identity_ambiguous")
    if values is not None:
        hits = [n for n, row in enumerate(values[1:], 2)
                if pad(row)[4] == "Katalys" and katalys_apply_id(pad(row)[5]) == pid]
        if hits != [row_num]:
            refuse("current_identity_missing_or_ambiguous")
        actual = pad(values[row_num - 1])
        replay = all(sheet_sync_cell_equal(int(k), actual[int(k)], v) for k, v in desired.items())
        eligible(actual, replay=replay)
        if not replay:
            # Partial targets are not safe replays; require the frozen source
            # transaction cells exactly, including old check time and QA.
            if any(not sheet_sync_cell_equal(c, actual[c], original[c])
                   for c in KATALYS_TRACKING_COLUMNS):
                refuse("partial_target_or_source_drift")


# Nonsecret custody pins only. The retained mailbox bytes stay outside Git.
# This is one historical hold, not a classifier or authority to impose new holds.
PARTNER_EMAIL_HOLD_EVENT = {
    "event_id": "evt_b9198b5da083f97f78cd4a79685f",
    "raw_sha256": "5bd183e1fd002fd99ffb04312c2fa57cbba75218f37cd4b5e7e7f4f61fa863e9",
    "received_at": "2026-09-21T14:06:14Z",
}
PARTNER_EMAIL_HOLD_INBOX_DB = Path("/home/ubuntu/.hermes/state/email-monitor.sqlite3")
PARTNER_EMAIL_HOLD_APPLY_URL = "https://xentraffic.everflowclient.io/offers/3277"


def partner_email_hold_cells(evidence: dict[str, Any]) -> dict[str, Any]:
    """Deterministic three-cell projection; no free-form instruction/note input."""
    source = evidence.get("source_row")
    if (not isinstance(source, list) or len(source) != len(HEADERS)
            or source[4] != "XenTraffic" or source[5] != PARTNER_EMAIL_HOLD_APPLY_URL
            or source[10] != "approved_active_tracking_ready"
            or source[19] is not True or source[20] is not False
            or not isinstance(source[14], str) or not source[14]
            or not isinstance(source[23], str)):
        raise RuntimeError("partner_email_hold_source_invalid")
    if any(evidence.get(key) != value for key, value in PARTNER_EMAIL_HOLD_EVENT.items()):
        raise RuntimeError("partner_email_hold_exact_event_mismatch")
    try:
        corrected = datetime.strptime(evidence["corrected_at_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        received = datetime.strptime(evidence["received_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        if not received < corrected <= datetime.now(timezone.utc):
            raise ValueError("correction must follow historical mail and not be in the future")
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("partner_email_hold_correction_time_invalid") from exc
    note = (f"Partner-email advertiser/postback HOLD; event_id={evidence['event_id']}; "
            f"raw_sha256={evidence['raw_sha256']}; email_received_at={evidence['received_at']}; "
            f"metadata_corrected_at={evidence['corrected_at_utc']}. "
            "Advertiser clearance and postback configuration readback remain pending. "
            "Wire-In Ready retracted; prior readiness and no-per-offer-gate claims do not release this hold. "
            "Current network eligibility UNKNOWN: not a portal pause, withdrawal or zero-economics finding. "
            "Existing tracker, payout and Last Checked UTC preserved, not reverified; no activation or hold release.")
    return {"10": "hold_advertiser_postback_pending", "19": "FALSE",
            "23": source[23] + ("\n" if source[23] else "") + note}


def require_partner_email_hold(entry: dict[str, Any], values: list[list[Any]] | None = None) -> None:
    """Rebind exact mail custody and the full preimage OR full postimage on replay."""
    import sqlite3
    from contextlib import closing
    from email import policy
    from email.parser import BytesParser
    from email.utils import parseaddr

    evidence = entry["evidence"]
    desired = partner_email_hold_cells(evidence)
    source = evidence["source_row"]
    expected_id = f"partner-email-hold-{evidence['event_id']}-{evidence['raw_sha256']}"
    if (entry.get("network") != "XenTraffic" or entry.get("provider_program_id") != "3277"
            or entry.get("program") != source[2] or entry.get("queue_update_id") != expected_id
            or entry.get("create") or entry.get("cells") != desired):
        raise RuntimeError("partner_email_hold_identity_or_projection_mismatch")
    # URI read-only + query_only: preview and apply never mutate inbox state.
    with closing(sqlite3.connect(
            PARTNER_EMAIL_HOLD_INBOX_DB.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        records = db.execute(
            "SELECT event_id, received_at, raw_path, raw_sha256 FROM inbox_events WHERE event_id=?",
            (evidence["event_id"],),
        ).fetchall()
    if (len(records) != 1 or records[0][1] != evidence["received_at"]
            or records[0][3] != evidence["raw_sha256"]):
        raise RuntimeError("partner_email_hold_retained_event_mismatch")
    raw = Path(records[0][2]).read_bytes()
    if hashlib.sha256(raw).hexdigest() != evidence["raw_sha256"]:
        raise RuntimeError("partner_email_hold_raw_hash_drift")
    message = BytesParser(policy=policy.default).parsebytes(raw)
    sender = parseaddr(str(message.get("From") or ""))[1].lower()
    trusted = [str(value).lower() for value in message.get_all("Authentication-Results", [])
               if str(value).strip().lower().startswith("mx.google.com;")]
    if (sender.rsplit("@", 1)[-1] != "xentraffic.com" or not any(
            re.search(r"(?:^|;)\s*dmarc=pass\b[^;]*\bheader\.from=xentraffic\.com(?:\s|;|$)", auth)
            for auth in trusted)):
        raise RuntimeError("partner_email_hold_authentication_failed")
    if values is not None:
        hits = [n for n, row in enumerate(values[1:], 2)
                if pad(row)[4] == "XenTraffic" and pad(row)[5] == PARTNER_EMAIL_HOLD_APPLY_URL]
        if hits != [entry["row"]]:
            raise RuntimeError("partner_email_hold_current_identity_missing_or_ambiguous")
        post = list(source)
        for column, value in desired.items():
            post[int(column)] = sheet_sync_write_value(int(column), value)
        # JSON equality preserves typed FALSE vs 0; no partial-postimage shortcut.
        actual = json.dumps(pad(values[entry["row"] - 1]), ensure_ascii=False)
        if actual not in (json.dumps(source, ensure_ascii=False), json.dumps(post, ensure_ascii=False)):
            raise RuntimeError("partner_email_hold_full_preimage_or_postimage_required")


def require_maxbounty_publisher_id(entry: dict[str, Any], actual: list[Any] | None = None) -> None:
    from urllib.parse import parse_qs, urlsplit

    evidence = entry["evidence"]
    href = str(evidence["tracking_link"])
    parsed = urlsplit(href)
    query = parse_qs(parsed.query, keep_blank_values=True)
    publisher = str(evidence["publisher_id"])
    if (entry.get("network") != "MaxBounty" or parsed.scheme != "https"
            or not parsed.hostname or not publisher.isdecimal()
            or query.get("a") != [publisher]
            or query.get("o") != [str(evidence["network_program_id"])]):
        raise RuntimeError(f"maxbounty_publisher_id_evidence_mismatch:{entry['row']}")
    if actual is not None:
        if str(actual[4]).strip() != "MaxBounty" or str(actual[14]).strip() != href:
            raise RuntimeError(f"maxbounty_publisher_id_source_drift:{entry['row']}")
        if str(actual[13] or "").strip() not in {"", publisher}:
            raise RuntimeError(f"publisher_id_drift:{entry['row']}:MaxBounty")


def sheet_sync_plan_rows(mode: str, plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate a queue-drained plan for one mode and return its row entries."""
    spec = SHEET_SYNC_MODES.get(mode)
    if spec is None:
        raise RuntimeError(f"sheet_sync_mode_unknown:{mode}")
    if plan.get("schema") != spec.plan_schema:
        raise RuntimeError(f"{spec.key}_schema_invalid:{plan.get('schema')}")
    if not re.fullmatch(r"[a-f0-9]{64}", str(plan.get("authority_sha256") or "")):
        raise RuntimeError(f"{spec.key}_authority_sha256_invalid")
    if int(plan.get("tracking_clicks", -1)) != 0:
        raise RuntimeError(f"{spec.key}_tracking_clicks_must_be_zero")
    if int(plan.get("provider_writes", -1)) != 0:
        raise RuntimeError(f"{spec.key}_provider_writes_must_be_zero")
    rows = plan.get("rows")
    if not isinstance(rows, list) or not rows:
        raise RuntimeError(f"{spec.key}_rows_empty")

    seen_rows: set[int] = set()
    for entry in rows:
        program = str(entry.get("program") or "").strip()
        if not program:
            raise RuntimeError(f"{spec.key}_program_missing")
        creating = bool(entry.get("create"))
        if creating and not spec.may_create:
            raise RuntimeError(f"{spec.key}_may_not_create_rows:{program}")
        row_num = entry.get("row")
        if creating:
            if row_num is not None:
                raise RuntimeError(f"{spec.key}_create_must_not_pin_row:{program}")
            if not str(entry.get("network") or "").strip() or not str(
                entry.get("provider_program_id") or ""
            ).strip():
                raise RuntimeError(f"{spec.key}_identity_missing:{program}")
        else:
            if not isinstance(row_num, int) or row_num < 2:
                raise RuntimeError(f"{spec.key}_row_number_invalid:{program}:{row_num}")
            if row_num in seen_rows:
                raise RuntimeError(f"{spec.key}_duplicate_row:{row_num}")
            seen_rows.add(row_num)

        require_sheet_sync_evidence(spec, entry, program)
        evidence = entry["evidence"]
        if spec.key in provider_metadata.KINDS:
            provider_metadata.validate_entry(spec.key, entry, _REPO_ROOT)
        if spec.key == "partner_email_hold":
            require_partner_email_hold(entry)
        if spec.key == "katalys_tracking_ready":
            require_katalys_tracking_ready(entry)
        if spec.key == "maxbounty_publisher_id":
            require_maxbounty_publisher_id(entry)
        if spec.key == "deployed_live":
            deployed_live_tuple(entry)

        cells = entry.get("cells")
        if not isinstance(cells, dict) or not cells:
            raise RuntimeError(f"{spec.key}_columns_invalid:{program}")
        keys = {str(key) for key in cells}
        allowed = ({str(value) for value in range(len(HEADERS))} if creating
                   else {str(value) for value in spec.mutable_columns})
        required = {str(value) for value in
                    (spec.create_required_columns if creating else spec.required_columns)}
        if not keys <= allowed or not required <= keys:
            raise RuntimeError(f"{spec.key}_columns_invalid:{program}")
        get = lambda column: cells.get(str(column), cells.get(column))  # noqa: E731

        status = str(get(10) or "")
        if spec.writes_status:
            if status not in spec.allowed_status:
                raise RuntimeError(f"{spec.key}_status_not_allowed:{program}:{status}")
        elif status:
            # A mode without column 10 in its mutable set may not smuggle one in.
            raise RuntimeError(f"{spec.key}_may_not_write_status:{program}:{status}")
        if spec.key == "status_correction":
            relationship = _sheet_sync_evidence_value(evidence, "portal_relationship")
            expected_status = STATUS_CORRECTION_RELATIONSHIP_STATUS[relationship]
            if status != expected_status:
                raise RuntimeError(
                    f"status_correction_status_does_not_follow_evidence:"
                    f"{program}:{relationship}:{status}")
        if spec.key == "maxbounty_inventory":
            relationship_button = _sheet_sync_evidence_value(evidence, "relationship_button")
            fit_classification = _sheet_sync_evidence_value(evidence, "fit_classification")
            expected_status = MAXBOUNTY_INVENTORY_STATUS.get(
                (relationship_button, fit_classification))
            preserved_tracking_ready = (
                relationship_button == "Build tracking link"
                and status in MAXBOUNTY_INVENTORY_PRESERVED_STATUS
            )
            if not expected_status or (status != expected_status and not preserved_tracking_ready):
                raise RuntimeError(
                    f"maxbounty_inventory_status_does_not_follow_button_and_fit:"
                    f"{program}:{relationship_button}:{fit_classification}:{status}")
            wire_ready = str(get(19)).strip().upper()
            wired_live = str(get(20)).strip().upper()
            if preserved_tracking_ready:
                if wire_ready != "TRUE":
                    raise RuntimeError(
                        f"maxbounty_inventory_preserved_wire_ready_required:{program}:{wire_ready}")
                if wired_live not in {"TRUE", "FALSE"}:
                    raise RuntimeError(
                        f"maxbounty_inventory_preserved_wired_live_invalid:{program}:{wired_live}")
                if str(get(21) or "").strip() != "not_clicked_preserve_attribution":
                    raise RuntimeError(
                        f"maxbounty_inventory_preserved_link_qa_invalid:{program}:{get(21)}")
            elif wire_ready != "FALSE" or wired_live != "FALSE":
                raise RuntimeError(
                    f"maxbounty_inventory_link_dark_flags_required:"
                    f"{program}:{wire_ready}:{wired_live}")
            expected_signup = "TRUE" if relationship_button == "Build tracking link" else "FALSE"
            if str(get(11)).strip().upper() != expected_signup:
                raise RuntimeError(
                    f"maxbounty_inventory_signup_flag_does_not_follow_button:"
                    f"{program}:{relationship_button}:{get(11)}")
            priority = "HIGH" if fit_classification == "core_glp1" else "LOW"
            if str(get(0)).strip().upper() != priority:
                raise RuntimeError(
                    f"maxbounty_inventory_priority_does_not_follow_fit:"
                    f"{program}:{fit_classification}:{get(0)}")
            require_maxbounty_inventory_capture(spec, entry, cells)
        if spec.key == "impact_program_candidate":
            require_impact_program_candidate_capture(spec, entry, cells)
        if spec.key == "affiliati_inventory":
            require_affiliati_inventory_capture(spec, entry, cells)
        if spec.key == "affiliati_switchover_stage":
            require_affiliati_switch_capture(spec, entry, cells)
        if spec.key == "affiliati_tracking_link":
            require_affiliati_tracking_capture(spec, entry, cells)
        if spec.key == "everflow_white_label_inventory":
            require_everflow_white_label_inventory_capture(spec, entry, cells)
        if spec.key == "everflow_brand_portal_tracking_link":
            require_everflow_brand_portal_tracking_capture(spec, entry, cells)
        if spec.key == "maxbounty_tracking_link":
            # The reports/ snapshot must always hash-match the CURRENT policy.
            # Ordinarily that policy must still be open.  A hash-bound,
            # operator-scoped retrieval receipt may authorize only one named
            # already-approved campaign's link while the broader application /
            # deploy gate remains closed; it cannot authorize any other action.
            import sys as _sys
            _scripts = str(_REPO_ROOT / "scripts")
            if _scripts not in _sys.path:
                _sys.path.insert(0, _scripts)
            from marketplace_adapters import maxbounty as _maxbounty
            policy_path = _REPO_ROOT / _sheet_sync_evidence_value(
                evidence, "gate_policy_snapshot_path")
            snapshot_sha = _sheet_sync_evidence_value(
                evidence, "gate_policy_snapshot_sha256")
            current_sha = hashlib.sha256(
                _maxbounty.DEFAULT_GATE_POLICY.read_bytes()).hexdigest()
            if current_sha != snapshot_sha:
                raise RuntimeError(
                    f"maxbounty_tracking_link_current_gate_policy_drift:{program}")
            try:
                _maxbounty.require_mutation_gate(policy_path)
            except _maxbounty.GateClosed:
                authorization_rel = str(
                    evidence.get("retrieval_authorization_path") or ""
                ).strip()
                declared_sha = str(
                    evidence.get("retrieval_authorization_sha256") or ""
                ).strip()
                if not authorization_rel or not declared_sha:
                    raise
                authorization_path = _REPO_ROOT / authorization_rel
                authorization = _maxbounty.require_tracking_retrieval_authorization(
                    authorization_path,
                    _sheet_sync_evidence_value(evidence, "network_program_id"),
                )
                if authorization["authorization_sha256"] != declared_sha:
                    raise RuntimeError(
                        f"maxbounty_tracking_link_retrieval_authorization_drift:{program}")
        for column, flag in spec.required_flags.items():
            if str(get(column)).strip().upper() != flag:
                raise RuntimeError(f"{spec.key}_flag_required:{program}:{column}:{flag}")
        if spec.allowed_link_qa is not None and "21" in keys:
            qa = str(get(21) or "")
            if qa not in spec.allowed_link_qa:
                raise RuntimeError(f"{spec.key}_link_qa_not_allowed:{program}:{qa}")
            if spec.link_qa_from_removal_receipt:
                # The retirement sentence is READ OFF the receipt, not chosen.
                receipt_path = _REPO_ROOT / _sheet_sync_evidence_value(
                    evidence, "removal_receipt_path")
                reason = removal_receipt_reason_class(receipt_path)
                declared = _sheet_sync_evidence_value(evidence, "removal_reason_class")
                if declared and declared != reason:
                    raise RuntimeError(
                        f"{spec.key}_reason_class_contradicts_receipt:"
                        f"{program}:{declared}:{reason}")
                expected_qa = REMOVAL_REASON_CLASS_LINK_QA[reason]
                if qa != expected_qa:
                    raise RuntimeError(
                        f"{spec.key}_link_qa_does_not_follow_receipt:"
                        f"{program}:{reason}:{qa}")
        for column, field in spec.cells_from_evidence.items():
            # The cell is the EVIDENCE's value, verbatim, or the plan is refused.
            # A mode whose whole content is a fetched number must never let the
            # caller type a different number next to a valid capture.
            if str(column) not in keys:
                continue
            written = " ".join(str(get(column) or "").split())
            recorded = " ".join(_sheet_sync_evidence_value(evidence, field).split())
            if written != recorded:
                raise RuntimeError(
                    f"{spec.key}_cell_does_not_follow_evidence:{program}:{column}:"
                    f"{written!r}!={recorded!r}")
        if spec.require_fit_assessment_cells:
            verdict = _sheet_sync_evidence_value(evidence, "verdict")
            fit_reason = str(get(7) or "").strip()
            if len(fit_reason) < FIT_REASON_MIN_LENGTH:
                raise RuntimeError(f"{spec.key}_fit_reason_too_short:{program}")
            vertical = require_vertical_value(get(25))
            if verdict == "in_scope" and vertical != FIT_ASSESSMENT_CORE_VERTICAL:
                raise RuntimeError(
                    f"{spec.key}_verdict_vertical_disagree:{program}:{verdict}:{vertical}")
            if verdict == "out_of_scope" and vertical != FIT_ASSESSMENT_OUT_OF_SCOPE_VERTICAL:
                raise RuntimeError(
                    f"{spec.key}_verdict_vertical_disagree:{program}:{verdict}:{vertical}")
            if verdict == "adjacent_sister_vertical" and vertical in (
                FIT_ASSESSMENT_CORE_VERTICAL, FIT_ASSESSMENT_OUT_OF_SCOPE_VERTICAL,
            ):
                raise RuntimeError(
                    f"{spec.key}_verdict_vertical_disagree:{program}:{verdict}:{vertical}")
        if spec.verdict_field:
            # The verdict the evidence RECORDS chooses the sentence. A caller who
            # ships a valid capture and a status that does not follow from it is
            # editorialising, which is the one thing a transcription may not do.
            verdict = _sheet_sync_evidence_value(evidence, spec.verdict_field)
            expected_status = spec.verdict_status.get(verdict)
            if not expected_status:
                raise RuntimeError(f"{spec.key}_verdict_unknown:{program}:{verdict!r}")
            if status != expected_status:
                raise RuntimeError(
                    f"{spec.key}_status_does_not_follow_verdict:"
                    f"{program}:{verdict}:{status}")
            for field in spec.verdict_required_evidence.get(verdict, ()):
                if not _sheet_sync_evidence_value(evidence, field):
                    raise RuntimeError(
                        f"{spec.key}_verdict_evidence_missing:{program}:{verdict}:{field}")
        if spec.parent_authorized:
            decision = _sheet_sync_evidence_value(evidence, "decision")
            if decision != status:
                raise RuntimeError(
                    f"{spec.key}_status_does_not_follow_decision:{program}:{decision}:{status}")
        note = str(get(23) or "").strip()
        if len(note) < 20 and spec.key not in {"maxbounty_publisher_id", "katalys_tracking_ready", provider_metadata.MAXBOUNTY_IDENTITY_KIND, *provider_metadata.SLUG_ONLY_KINDS}:
            raise RuntimeError(f"{spec.key}_note_too_short:{program}")
        for field in spec.note_must_cite:
            if _sheet_sync_evidence_value(evidence, field) not in note:
                raise RuntimeError(f"{spec.key}_note_missing_evidence:{program}:{field}")
        if spec.note_scrub:
            require_secret_scrub_note(spec, entry, note)
        checked = str(get(24) or "").strip()
        if spec.key not in {"partner_email_hold", "maxbounty_publisher_id", provider_metadata.MAXBOUNTY_IDENTITY_KIND, *provider_metadata.SLUG_ONLY_KINDS} and (not checked or "T" not in checked):
            raise RuntimeError(f"{spec.key}_last_checked_invalid:{program}")

        if creating:
            if str(get(2) or "").strip() != program:
                raise RuntimeError(f"{spec.key}_program_cell_mismatch:{program}")
            if norm(get(4)) != norm(entry.get("network")):
                raise RuntimeError(f"{spec.key}_network_cell_mismatch:{program}")
            if not str(get(6) or "").strip().startswith("https://"):
                raise RuntimeError(f"{spec.key}_official_url_invalid:{program}")
            require_vertical_value(get(25))
            if "26" in keys:
                require_api_capabilities_value(get(26))
            tracking = str(get(14) or "").strip()
            if tracking:
                raise RuntimeError(f"{spec.key}_must_not_claim_tracking_link:{program}")
            if spec.require_marketplace_candidate_row:
                require_marketplace_candidate_row(spec, entry, cells)
    return rows


def require_existing_live_placement(
    spec: SheetSyncModeSpec,
    values: list[list[Any]],
    entry: dict[str, Any],
    row_num: int,
    actual: list[Any],
) -> None:
    """Prove the brand really is already live, on a DIFFERENT network, on the Sheet.

    This is the whole justification for `cross_network_duplicate_relationship`:
    without a proven live placement there is nothing for the new relationship to
    be superseded BY, and the mode would degenerate into a way of writing
    "approved" while dodging the evidence `add_approved_row` demands.

    Four facts are proved against the live sheet, never against the caller's
    say-so: the cited row exists and is not this row; it is on the network the
    evidence names and NOT on this row's network; it is LIVE (status and the
    Wired Live? checkbox agree); it carries the cited provider program id in its
    own apply URL or tracking link; and its Official URL is the SAME registrable
    domain as this row's, so the two rows are one brand rather than two.
    """
    evidence = entry["evidence"]
    network = _sheet_sync_evidence_value(evidence, "existing_placement_network")
    program_id = _sheet_sync_evidence_value(evidence, "existing_placement_program_id")
    try:
        other_row = int(_sheet_sync_evidence_value(evidence, "existing_placement_sheet_row"))
    except ValueError as exc:
        raise RuntimeError(f"{spec.key}_existing_placement_row_invalid:{row_num}") from exc
    if other_row == row_num:
        raise RuntimeError(f"{spec.key}_existing_placement_is_the_same_row:{row_num}")
    if len(values) < other_row or not row_has_business_value(values[other_row - 1]):
        raise RuntimeError(f"{spec.key}_existing_placement_row_blank:{other_row}")
    other = pad(values[other_row - 1])
    if norm(other[4]) != norm(network):
        raise RuntimeError(
            f"{spec.key}_existing_placement_network_mismatch:{other_row}:"
            f"sheet={str(other[4]).strip()!r} evidence={network!r}")
    if norm(other[4]) == norm(actual[4]):
        raise RuntimeError(
            f"{spec.key}_existing_placement_same_network:{other_row}:{str(other[4]).strip()!r}")
    other_status = str(other[10] or "")
    other_live = other[20] is True or str(other[20]).strip().upper() == "TRUE"
    if other_status not in SHEET_SYNC_LIVE_STATUS or not other_live:
        raise RuntimeError(
            f"{spec.key}_existing_placement_not_live:{other_row}:"
            f"{other_status or '(blank)'}:wired_live={other_live}")
    haystack = f"{other[5]} {other[14]}".casefold()
    if not program_id or program_id.casefold() not in haystack:
        raise RuntimeError(
            f"{spec.key}_existing_placement_program_id_absent:{other_row}:{program_id!r}")
    this_domain = registrable_domain(actual[6])
    other_domain = registrable_domain(other[6])
    if not this_domain or this_domain != other_domain:
        raise RuntimeError(
            f"{spec.key}_existing_placement_is_a_different_brand:{other_row}:"
            f"{this_domain or '(blank)'}!={other_domain or '(blank)'}")


def require_provider_withdrawal_evidence(
    spec: "SheetSyncModeSpec", entry: dict[str, Any], row_num: int, actual: list[Any],
) -> None:
    """Only a provider 404 may retract an approval we already recorded.

    Retracting `Wire-In Ready?` on a programme we are approved on is a
    money-direction write: get it wrong and a real placement stops earning. So
    this mode does not accept "the campaign was missing from a list" — a
    bookmarked list is OUR bookmark, not the relationship. It accepts exactly
    one thing: the AUTHENTICATED campaign page answered 404 for this account,
    which is what `marketplace_adapters.maxbounty` classifies as
    `terminal_provider_unavailable`.
    """
    evidence = evidence_of(entry)
    status = str(_sheet_sync_evidence_value(evidence, "provider_api_status") or "").strip()
    classification = str(
        _sheet_sync_evidence_value(evidence, "classification") or "").strip()
    if status != "404":
        raise RuntimeError(
            f"{spec.key}_provider_api_status_not_404:{row_num}:{status or '(blank)'} — "
            "an approval is retracted only on the provider's own 404, never on "
            "absence from a bookmarked list")
    if classification != "terminal_provider_unavailable":
        raise RuntimeError(
            f"{spec.key}_classification_not_terminal:{row_num}:"
            f"{classification or '(blank)'}")


def require_link_rail_validation(
    spec: "SheetSyncModeSpec", entry: dict[str, Any], row_num: int, actual: list[Any],
) -> None:
    """Structurally validate the row's own tracking link against its rail.

    NEVER fetches, traverses or clicks the link — a self-click pollutes
    attribution, which is why every writer path in this file declares
    `tracking_clicks: 0`. This is pure string structure: host, path shape,
    immutable id, our publisher identity.
    """
    import sys as _sys
    _scripts = str(_REPO_ROOT / "scripts")
    if _scripts not in _sys.path:
        _sys.path.insert(0, _scripts)
    from glp_affiliate_link_rails import RailError, rail_for, validate_link

    evidence = evidence_of(entry)
    declared_network = _sheet_sync_evidence_value(evidence, "network")
    sheet_network = str(actual[4] or "").strip()
    if declared_network.casefold() != sheet_network.casefold():
        raise RuntimeError(
            f"{spec.key}_network_contradicts_sheet:{row_num}:"
            f"{declared_network!r}!={sheet_network!r}")
    rail = rail_for(sheet_network)
    if rail is None:
        raise RuntimeError(f"{spec.key}_network_not_supported:{row_num}:{sheet_network}")
    if not rail.verified:
        raise RuntimeError(
            f"{spec.key}_rail_link_shape_unverified:{row_num}:{rail.network} — "
            "no tracking link on this rail has ever been observed first-party, "
            "so no link on it can be structurally validated")
    immutable_id = _sheet_sync_evidence_value(evidence, "immutable_id")
    # VALIDATE THE LINK THIS MODE IS ABOUT TO WRITE, not the one already there.
    #
    # `actual` is the row as it stands BEFORE the write. For `wire_in_ready` —
    # the only mode that used this guard until 2026-09-02 — that is exactly
    # right: the link is already on the row and the gate is judging it. A mode
    # that WRITES column 14 is the other case, and validating the pre-write cell
    # there would check the wrong string, or an empty one on a row receiving its
    # first link. So when the mode's own contract says it fills column 14 from
    # evidence, that planned value is what gets validated. Modes that do not
    # write column 14 keep reading the row exactly as before.
    planned_field = (spec.cells_from_evidence or {}).get(14)
    link = ""
    if planned_field and 14 in spec.mutable_columns:
        link = str(_sheet_sync_evidence_value(evidence, planned_field) or "").strip()
    if not link:
        link = str(actual[14] or "").strip()
    allowed_host = str(evidence.get("allowed_host") or "").strip() or None
    if allowed_host is None and rail.host_class in ("merchant_host", "tracking_domain"):
        # A merchant-hosted rail carries our params on the ADVERTISER's own
        # domain, so the expected host cannot be a rail constant. Default it to
        # the row's Official URL host rather than letting the caller name any
        # host it likes.
        allowed_host = registrable_host(str(actual[6] or "")) or None
        link_host = registrable_host(link)
        if allowed_host and link_host and link_host != allowed_host:
            # The Everflow-client rail's tracking DOMAIN is deliberately not the
            # merchant's; accept it only when the evidence says so explicitly.
            raise RuntimeError(
                f"{spec.key}_tracking_host_not_the_official_domain:{row_num}:"
                f"{link_host}!={allowed_host} (declare `allowed_host` in the "
                "evidence if this rail uses its own tracking domain)")
    tracking_form = str(evidence.get("tracking_form") or "query_offer_publisher")
    try:
        validate_link(sheet_network, immutable_id, link, allowed_host, tracking_form)
    except RailError as exc:
        raise RuntimeError(f"{spec.key}_tracking_link_invalid:{row_num}:{exc}") from None


def require_live_claim_correction_reconcile(
    spec: "SheetSyncModeSpec", entry: dict[str, Any], row_num: int, actual: list[Any],
) -> None:
    """Bind a live-claim retraction to the exact current-release finding.

    Generic path/hash checks prove the cited bytes are real. This guard proves
    those bytes actually say THIS row is approved and wire-ready but not earning,
    so a caller cannot pair an unrelated valid reconcile receipt with a desired
    FALSE checkbox.
    """
    evidence = evidence_of(entry)
    path = _REPO_ROOT / _sheet_sync_evidence_value(evidence, "site_reconcile_path")
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{spec.key}_reconcile_unreadable:{row_num}") from exc
    if payload.get("schema") != "glp.sheet_vs_site_reconcile.v1":
        raise RuntimeError(
            f"{spec.key}_reconcile_schema_invalid:{row_num}:{payload.get('schema')}")
    if _sheet_sync_evidence_value(evidence, "checked_at_utc") != str(
        payload.get("checked_at_utc") or ""
    ):
        raise RuntimeError(f"{spec.key}_reconcile_timestamp_mismatch:{row_num}")
    if _sheet_sync_evidence_value(evidence, "release") != str(payload.get("release") or ""):
        raise RuntimeError(f"{spec.key}_reconcile_release_mismatch:{row_num}")
    if int(_sheet_sync_evidence_value(evidence, "sheet_row")) != row_num:
        raise RuntimeError(f"{spec.key}_reconcile_row_mismatch:{row_num}")

    matches = [
        item for item in (payload.get("results") or [])
        if int(item.get("row") or 0) == row_num
    ]
    if len(matches) != 1:
        raise RuntimeError(f"{spec.key}_reconcile_finding_missing_or_ambiguous:{row_num}")
    finding = matches[0]
    declared_finding = _sheet_sync_evidence_value(evidence, "finding")
    checks = {
        "program": str(finding.get("program") or "") == str(entry.get("program") or ""),
        "network": norm(finding.get("network")) == norm(actual[4]),
        "classification": finding.get("classification") == "STALE-STATUS",
        "finding": finding.get("need_code") == declared_finding,
        "source_status": str(finding.get("status") or "") == str(actual[10] or ""),
        "wire_ready": finding.get("wire_in_ready") is True,
        "source_claimed_live": finding.get("wired_live_flag") is True,
        "not_monetized": finding.get("monetized_on_site") is False,
        "no_go_route": (finding.get("site_surfaces") or {}).get("go") is False,
    }
    if declared_finding == "stale_review_cta_held":
        checks["cta_held"] = (finding.get("review_cta") or {}).get("state") == "held"
        checks["cta_not_monetized"] = finding.get("monetized_review_cta") is False
    failed = sorted(name for name, passed in checks.items() if not passed)
    if failed:
        raise RuntimeError(
            f"{spec.key}_reconcile_does_not_prove_retraction:{row_num}:{','.join(failed)}")


def require_best_terms_route_retired_reconcile(
    spec: "SheetSyncModeSpec", values: list[list[Any]], entry: dict[str, Any],
    row_num: int, actual: list[Any],
) -> None:
    """Bind a losing-route retraction to production hrefs and its live winner."""
    from urllib.parse import parse_qs, urlparse

    evidence = evidence_of(entry)
    receipt_path = _REPO_ROOT / _sheet_sync_evidence_value(evidence, "route_reconcile_path")
    decision_path = _REPO_ROOT / _sheet_sync_evidence_value(evidence, "payout_decision_path")
    try:
        receipt = json.loads(receipt_path.read_text())
        decision = json.loads(decision_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{spec.key}_evidence_unreadable:{row_num}") from exc
    incumbent = receipt.get("incumbent") or {}
    winner = receipt.get("winner") or {}
    live = receipt.get("live_verification") or {}
    winner_row_num = int(_sheet_sync_evidence_value(evidence, "winner_sheet_row"))
    if len(values) < winner_row_num:
        raise RuntimeError(f"{spec.key}_winner_row_missing:{winner_row_num}")
    winner_actual = pad(values[winner_row_num - 1])

    def truth(value: Any) -> bool:
        return value is True or str(value).strip().upper() == "TRUE"

    def integer(value: Any, default: int = -1) -> int:
        if value is None or value == "":
            return default
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def tracking_id(link: Any, network: Any) -> str:
        parsed = urlparse(str(link or ""))
        query = parse_qs(parsed.query)
        if norm(network) == "awin":
            return str((query.get("mid") or query.get("awinmid") or [""])[0])
        if norm(network) == "katalys":
            return str((query.get("offer_id") or [""])[0])
        return ""

    decisions = [item for item in (decision.get("new_decisions") or [])
                 if str(item.get("provider") or "").casefold() == "shedrx"]
    payout = decisions[0] if len(decisions) == 1 else {}
    payout_incumbent = payout.get("incumbent") or {}
    payout_winner = payout.get("winner") or {}
    try:
        winner_payout_is_higher = float(payout_winner.get("headline_payout")) \
            > float(payout_incumbent.get("headline_payout"))
    except (TypeError, ValueError):
        winner_payout_is_higher = False

    checks = {
        "receipt_schema": receipt.get("schema") == "glp.affiliate_best_terms_route_reconcile.v1",
        "receipt_status": receipt.get("status") == "PASS",
        "receipt_time": str(receipt.get("verified_at_utc") or "")
                        == _sheet_sync_evidence_value(evidence, "checked_at_utc"),
        "receipt_release": str(receipt.get("release") or "")
                           == _sheet_sync_evidence_value(evidence, "release"),
        "incumbent_row": int(incumbent.get("sheet_row") or 0) == row_num
                         == int(_sheet_sync_evidence_value(evidence, "incumbent_sheet_row")),
        "incumbent_program": str(incumbent.get("sheet_program") or "") == str(actual[2] or ""),
        "incumbent_network": norm(incumbent.get("network")) == norm(actual[4]) == "awin",
        "incumbent_id": str(incumbent.get("program_id") or "")
                        == tracking_id(actual[14], actual[4]) == "80963",
        "incumbent_link_hash": str(incumbent.get("tracking_link_sha256") or "")
                               == hashlib.sha256(str(actual[14]).encode()).hexdigest(),
        "winner_row": int(winner.get("sheet_row") or 0) == winner_row_num,
        "winner_program": str(winner.get("sheet_program") or "") == str(winner_actual[2] or ""),
        "winner_network": norm(winner.get("network")) == norm(winner_actual[4]) == "katalys",
        "winner_id": str(winner.get("program_id") or "")
                     == tracking_id(winner_actual[14], winner_actual[4]) == "1516",
        "winner_link_hash": str(winner.get("tracking_link_sha256") or "")
                            == hashlib.sha256(str(winner_actual[14]).encode()).hexdigest(),
        "winner_status": str(winner_actual[10] or "") in SHEET_SYNC_LIVE_STATUS,
        "winner_wire_ready": truth(winner_actual[19]),
        "winner_live": truth(winner_actual[20]),
        "href_only": live.get("verification_method") == "html_href_inspection_only",
        "awin_absent": integer(live.get("incumbent_href_occurrences")) == 0,
        "winner_present": integer(live.get("winner_href_occurrences"), 0) > 0,
        "no_tracking_requests": integer(live.get("tracking_link_requests")) == 0
                                and integer(receipt.get("tracking_clicks")) == 0,
        "decision_schema": decision.get("schema") == "glp.consolidated-payout-switch-decisions.v1",
        "decision_unique": len(decisions) == 1,
        "decision_name": payout.get("decision") == "STAGE_DEDUP_SWITCH_AWIN_TO_KATALYS_AT_DEPLOY",
        "decision_incumbent": (str(payout_incumbent.get("network")) == "Awin"
                               and str(payout_incumbent.get("program_id")) == "80963"
                               and int(payout_incumbent.get("sheet_row") or 0) == row_num),
        "decision_winner": (str(payout_winner.get("network")) == "Katalys"
                            and str(payout_winner.get("program_id")) == "1516"
                            and int(payout_winner.get("sheet_row") or 0) == winner_row_num),
        "winner_payout_is_higher": winner_payout_is_higher,
    }
    failed = sorted(name for name, passed in checks.items() if not passed)
    if failed:
        raise RuntimeError(
            f"{spec.key}_reconcile_does_not_prove_retirement:{row_num}:{','.join(failed)}")


def require_best_terms_winner_confirmed_reconcile(
    spec: "SheetSyncModeSpec", entry: dict[str, Any], row_num: int, actual: list[Any],
) -> None:
    """Bind the winner's live/ready flags to its deployed production href receipt."""
    from urllib.parse import parse_qs, urlparse

    evidence = evidence_of(entry)
    receipt_path = _REPO_ROOT / _sheet_sync_evidence_value(evidence, "route_reconcile_path")
    decision_path = _REPO_ROOT / _sheet_sync_evidence_value(evidence, "payout_decision_path")
    try:
        receipt = json.loads(receipt_path.read_text())
        decision = json.loads(decision_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{spec.key}_evidence_unreadable:{row_num}") from exc
    winner = receipt.get("winner") or {}
    live = receipt.get("live_verification") or {}
    parsed = urlparse(str(actual[14] or ""))
    offer_id = str((parse_qs(parsed.query).get("offer_id") or [""])[0])
    decisions = [item for item in (decision.get("new_decisions") or [])
                 if str(item.get("provider") or "").casefold() == "shedrx"]
    payout = decisions[0] if len(decisions) == 1 else {}
    payout_incumbent = payout.get("incumbent") or {}
    payout_winner = payout.get("winner") or {}
    try:
        winner_is_higher = float(payout_winner.get("headline_payout")) \
            > float(payout_incumbent.get("headline_payout"))
    except (TypeError, ValueError):
        winner_is_higher = False

    checks = {
        "receipt_schema": receipt.get("schema") == "glp.affiliate_best_terms_route_reconcile.v1",
        "receipt_status": receipt.get("status") == "PASS",
        "receipt_time": str(receipt.get("verified_at_utc") or "")
                        == _sheet_sync_evidence_value(evidence, "checked_at_utc"),
        "receipt_release": str(receipt.get("release") or "")
                           == _sheet_sync_evidence_value(evidence, "release"),
        "winner_row": int(winner.get("sheet_row") or 0) == row_num
                      == int(_sheet_sync_evidence_value(evidence, "winner_sheet_row")),
        "winner_program": str(winner.get("sheet_program") or "") == str(actual[2] or ""),
        "winner_network": norm(winner.get("network")) == norm(actual[4]) == "katalys",
        "winner_id": str(winner.get("program_id") or "") == offer_id == "1516",
        "winner_link_hash": str(winner.get("tracking_link_sha256") or "")
                            == hashlib.sha256(str(actual[14]).encode()).hexdigest(),
        "href_only": live.get("verification_method") == "html_href_inspection_only",
        "incumbent_absent": live.get("incumbent_href_occurrences") == 0,
        "winner_present": isinstance(live.get("winner_href_occurrences"), int)
                          and live["winner_href_occurrences"] > 0,
        "no_tracking_requests": live.get("tracking_link_requests") == 0
                                and receipt.get("tracking_clicks") == 0,
        "decision_schema": decision.get("schema") == "glp.consolidated-payout-switch-decisions.v1",
        "decision_unique": len(decisions) == 1,
        "decision_name": payout.get("decision") == "STAGE_DEDUP_SWITCH_AWIN_TO_KATALYS_AT_DEPLOY",
        "decision_winner": (str(payout_winner.get("network")) == "Katalys"
                            and str(payout_winner.get("program_id")) == "1516"
                            and int(payout_winner.get("sheet_row") or 0) == row_num),
        "winner_payout_is_higher": winner_is_higher,
    }
    failed = sorted(name for name, passed in checks.items() if not passed)
    if failed:
        raise RuntimeError(
            f"{spec.key}_reconcile_does_not_prove_winner:{row_num}:{','.join(failed)}")


def registrable_host(value: str) -> str:
    from urllib.parse import urlparse
    try:
        return (urlparse(str(value)).hostname or "").strip().lower()
    except ValueError:
        return ""


def require_sheet_sync_preconditions(
    mode: str, values: list[list[Any]], rows: list[dict[str, Any]],
) -> None:
    """Prove every target row is the exact row this mode is allowed to act on.

    Re-runnable by construction: a row already sitting at the mode's target
    status satisfies the mode instead of tripping it, exactly as the hand-written
    dispositions do, so a drain replay is a no-op rather than a refusal.
    """
    spec = SHEET_SYNC_MODES[mode]
    for entry in rows:
        if spec.key == "partner_email_hold":
            require_partner_email_hold(entry, values)
        if spec.key == "katalys_tracking_ready":
            require_katalys_tracking_ready(entry, values)
        if entry.get("create"):
            continue
        row_num = int(entry["row"])
        program = str(entry["program"]).strip()
        if len(values) < row_num or not row_has_business_value(values[row_num - 1]):
            raise RuntimeError(f"{spec.key}_target_row_blank:{row_num}")
        actual = pad(values[row_num - 1])
        if str(actual[2]).strip() != program:
            raise RuntimeError(f"{spec.key}_program_mismatch:{row_num}:{str(actual[2]).strip()!r}")
        if spec.key in provider_metadata.KINDS:
            provider_metadata.validate_entry(spec.key, entry, _REPO_ROOT, values,
                                             provider_metadata.SLUG_APPROVED_STATUSES)
        if spec.key == "maxbounty_publisher_id":
            require_maxbounty_publisher_id(entry, actual)
        if spec.key == "provider_scope_retraction":
            require_scope_retraction(entry, row_num, actual, _REPO_ROOT)
        if spec.key == "fit_assessment":
            existing_note = str(actual[23] or "")
            planned_note = str(entry["cells"].get("23", entry["cells"].get(23)) or "")
            if existing_note and planned_note != existing_note and not planned_note.startswith(existing_note + "\n"):
                raise RuntimeError(f"fit_assessment_existing_notes_not_preserved:{row_num}")
        target_ready = entry["cells"].get("19", entry["cells"].get(19))
        if target_ready is True or str(target_ready).strip().upper() == "TRUE":
            scope = provider_row_scope(row_num, actual)
            if not scope["eligible"]:
                raise RuntimeError(f"{spec.key}_provider_row_scope:{row_num}:{scope['reason']}")
        if spec.key == "maxbounty_inventory":
            desired = entry["cells"]
            for column in (8, 9):
                current_identity = str(actual[column] or "").strip()
                target_identity = str(
                    desired.get(str(column), desired.get(column)) or "").strip()
                if current_identity and target_identity != current_identity:
                    raise RuntimeError(
                        f"maxbounty_inventory_existing_site_identity_immutable:"
                        f"{row_num}:{column}:{current_identity}:{target_identity}")
        current_status = str(actual[10] or "")
        if spec.writes_status:
            target_status = str(entry["cells"].get("10", entry["cells"].get(10)))
            already_applied = current_status == target_status
        else:
            # A mode that writes no status is never "already applied" by status,
            # so its source guards are enforced on every single run.
            already_applied = False
        if spec.require_marketplace_rail_row:
            haystack = f"{actual[3]} {actual[4]}".lower()
            if not any(marker in haystack for marker in MARKETPLACE_RAIL_MARKERS):
                raise RuntimeError(
                    f"{spec.key}_row_is_not_a_marketplace_rail:{row_num}:"
                    f"{str(actual[3]).strip()!r}/{str(actual[4]).strip()!r}")
        if spec.require_marketplace_parent_row:
            canonical_row = REQUIRED_MARKETPLACE_PARENT_ROWS.get(program)
            if canonical_row != row_num:
                raise RuntimeError(
                    f"{spec.key}_row_is_not_its_marketplace_parent:{row_num}:{program!r}")
        if not already_applied:
            if (spec.require_blank_source_status and current_status.strip()
                    and current_status not in spec.blank_source_exempt_status):
                raise RuntimeError(
                    f"{spec.key}_source_status_not_blank:{row_num}:{current_status}")
            if current_status in spec.forbidden_source_status:
                raise RuntimeError(
                    f"{spec.key}_source_status_forbidden:{row_num}:{current_status}")
            if spec.source_status is not None and current_status not in spec.source_status:
                raise RuntimeError(
                    f"{spec.key}_source_status_not_allowed:{row_num}:{current_status}")
            if spec.verdict_field:
                # Each verdict resolves only the states it can honestly resolve.
                # The narrowest is the one that writes an approval.
                verdict = _sheet_sync_evidence_value(entry["evidence"], spec.verdict_field)
                allowed_sources = spec.verdict_source_status.get(verdict, frozenset())
                if current_status not in allowed_sources:
                    raise RuntimeError(
                        f"{spec.key}_source_status_not_allowed_for_verdict:"
                        f"{row_num}:{verdict}:{current_status or '(blank)'}")
            claims_approval = current_status.startswith(SHEET_SYNC_APPROVAL_CLAIM_PREFIXES)
            if spec.forbid_source_approval_claim and claims_approval:
                raise RuntimeError(
                    f"{spec.key}_row_already_claims_approval:{row_num}:{current_status}")
            if spec.require_source_approval_claim and not claims_approval:
                raise RuntimeError(
                    f"{spec.key}_row_makes_no_approval_claim:{row_num}:{current_status}")
            live = actual[20] is True or str(actual[20]).strip().upper() == "TRUE"
            if spec.forbid_wired_live_source and live:
                raise RuntimeError(f"{spec.key}_row_is_wired_live:{row_num}")
        if spec.require_tracking_link is True and not str(actual[14] or "").strip():
            raise RuntimeError(f"{spec.key}_tracking_link_absent:{row_num}")
        if spec.require_link_rail_validation:
            require_link_rail_validation(spec, entry, row_num, actual)
        if spec.key == "provider_relationship_withdrawn":
            require_provider_withdrawal_evidence(spec, entry, row_num, actual)
        if spec.key == "live_claim_correction":
            require_live_claim_correction_reconcile(spec, entry, row_num, actual)
        if spec.key == "best_terms_route_retired":
            require_best_terms_route_retired_reconcile(spec, values, entry, row_num, actual)
        if spec.key == "best_terms_winner_confirmed":
            require_best_terms_winner_confirmed_reconcile(spec, entry, row_num, actual)
        if spec.key == "deployed_live":
            require_deployed_live_tuple_binding(entry, row_num, actual)
        if spec.key == "everflow_white_label_inventory":
            require_everflow_white_label_inventory_preconditions(
                spec, values, entry, row_num, actual)
        if spec.key == "affiliati_switchover_stage":
            require_affiliati_switch_capture(spec, entry, entry["cells"])
        if spec.key == "higher_rail_pending_replacement":
            require_higher_rail_pending_capture(spec, entry, row_num, actual)
        if spec.require_tracking_link is False and str(actual[14] or "").strip():
            raise RuntimeError(f"{spec.key}_tracking_link_present:{row_num}")
        if spec.require_existing_live_placement:
            # Checked on EVERY run, replay included: the sentence this mode
            # writes is only true for as long as the other placement is live.
            require_existing_live_placement(spec, values, entry, row_num, actual)
        if spec.require_program_identity_binding:
            # Also checked on every run: a replay must still be able to show
            # that this capture and this row are the same programme.
            require_program_identity_binding(spec, entry, row_num, actual)
        if spec.status_follows_tracking_link:
            # Which approved sentence is told is decided by the SHEET, not the
            # caller: a row that kept its provider-issued link comes back
            # link-ready, one without a link comes back approved-only.
            with_link, without_link = spec.status_follows_tracking_link
            target_status = str(entry["cells"].get("10", entry["cells"].get(10)) or "")
            expected = with_link if str(actual[14] or "").strip() else without_link
            if target_status != expected:
                raise RuntimeError(
                    f"{spec.key}_status_does_not_follow_tracking_link:"
                    f"{row_num}:{target_status}:expected={expected}")
        if spec.require_program_name_binding:
            require_program_name_binding(spec, entry, row_num, actual)
        if spec.require_operator_note_quote:
            require_operator_ruling_capture(spec, entry, row_num, actual)
        if spec.note_scrub:
            require_secret_transfer_receipt(spec, entry, row_num, actual)


def canonical_parent_rows(planned_rows: list[dict[str, Any]]) -> frozenset:
    """The planned rows that ARE their own marketplace's canonical parent row.

    Row-by-row, from `REQUIRED_MARKETPLACE_PARENT_ROWS` — the same immutable
    identity table the guard itself protects. A row whose programme name is not
    a marketplace parent name, or whose number is not that parent's canonical
    number, is not in the set and stays behind the guard.
    """
    authorized = set()
    for item in planned_rows:
        row_num = int(item["row"])
        if REQUIRED_MARKETPLACE_PARENT_ROWS.get(str(item.get("program") or "")) == row_num:
            authorized.add(row_num)
    return frozenset(authorized)


def require_parent_row_protection(
    spec: SheetSyncModeSpec,
    changes: list[tuple[int, int, Any]],
    planned_rows: list[dict[str, Any]],
) -> None:
    """Apply the marketplace-parent guard, exempting only a proved parent row.

    The guard itself is untouched and still refuses every ordinary mode on every
    parent row. A `parent_authorized` mode is exempted ONLY for the exact rows in
    its own plan - which `require_sheet_sync_preconditions` has already proved are
    that marketplace's canonical parent row under its canonical name. Any other
    row the batch happens to touch, parent or not, goes through the guard.

    2026-08-20, bridge authorization recorded in
    `reports/glp-clinic-scout/affiliate-machine/PHASE1B-BUILD-20260819.md`:
    `portal_readback_confirmed` and `operator_status_ruling` are additionally
    authorized for their OWN canonical marketplace parent row, evidence-cited,
    under the SAME narrowing `marketplace_parent_relabel` already uses. This is
    what unblocks rows 358 Digistore24, 427 Skimlinks, 433 Admitad and 470
    MaxBounty, which PHASE1-PROOF §7.1 recorded as
    `required_marketplace_parent_row_mutation_blocked` — four rows holding
    statuses the ledger calls wrong purely because the only exempt mode was one
    that says "the network-level application was DECIDED", which is not what a
    portal readback or an operator ruling says.
    Unlike `marketplace_parent_relabel`, these two modes run mostly on ordinary
    programme rows, so the exemption cannot be a mode-wide flag: it is computed
    PER ROW from `REQUIRED_MARKETPLACE_PARENT_ROWS`. A batch of ten rows in which
    one is Skimlinks' parent exempts exactly that one row and nothing else, and
    any other parent row the batch touches is still refused. Operator direction
    on 2026-08-21 extended the same per-row rule to `approval_confirmed`, because
    a network-account approval (the MaxBounty case) is also a fact whose only
    truthful target is that network's canonical parent row.
    """
    if spec.parent_authorized:
        authorized = frozenset(int(item["row"]) for item in planned_rows)
    elif spec.parent_authorized_on_own_canonical_row:
        authorized = canonical_parent_rows(planned_rows)
    else:
        authorized = frozenset()
    require_marketplace_parent_rows_not_mutated(
        (row_num, column) for row_num, column, _value in changes
        if int(row_num) not in authorized
    )


def program_id_in_text(program_id: str, text: Any) -> bool:
    """Is this provider program id a STANDALONE token in that text?

    2026-08-19: a plain `in` test made `1596` (Katalys, Embody Sermorelin) match
    row 155's Awin tracking link `awinmid=121596`, on a different network, for a
    different brand. That is not a near-miss: `sheet_sync_existing_matches` feeds
    BOTH `resolve_sheet_row` (so a queue row with no explicit `sheet_row` could
    have been written onto that unrelated row) and the create-path collision
    check (so a real new approval was reported as already present, which is how
    Embody Sermorelin nearly vanished a second time).

    Every id must therefore be a standalone alphanumeric token. This applies to
    mixed ids too: Partnerize `1101l104` must not match `1101l1043`, and
    `1011l76` must not match `1011l767`. URL separators and query punctuation
    remain valid boundaries.
    """
    program_id = str(program_id or "").strip()
    if not program_id:
        return False
    haystack = str(text or "").casefold()
    needle = program_id.casefold()
    return re.search(
        rf"(?<![0-9a-z]){re.escape(needle)}(?![0-9a-z])", haystack,
    ) is not None


def provider_program_identity_axes(
    row: list[Any], network: object, provider_program_id: object,
) -> list[str]:
    """Provider-id axes on one row, always scoped to one canonical marketplace."""
    actual = pad(row)
    program_id = str(provider_program_id or "").strip().casefold()
    if not program_id or canonical_network(actual[4]) != canonical_network(network):
        return []
    axes: list[str] = []
    if (offer_from_url(actual[5]).casefold() == program_id
            or program_id_in_text(program_id, actual[5])):
        axes.append("apply_url_program_id")
    if program_id_in_text(program_id, actual[14]):
        axes.append("tracking_link_program_id")
    # Historical application rows can predate an Apply URL. Their governed Notes
    # cite the immutable submission receipt (for example applications/awin-33957.json),
    # which is still a standalone provider id on the same rail.
    if program_id_in_text(program_id, actual[23]):
        axes.append("notes_program_id")
    return axes


def sheet_sync_existing_matches(
    values: list[list[Any]], entry: dict[str, Any],
) -> list[dict[str, Any]]:
    """Existing rows by provider id first, conservative programme name second."""
    # Compare rails, not spellings. A queue row saying "impact" and a Sheet cell
    # saying "Impact.com" are the same relationship; see NETWORK_LABEL_ALIASES.
    network = canonical_network(entry.get("network"))
    provider_program_id = str(entry.get("provider_program_id") or "").strip().casefold()
    strong_id_matches: list[dict[str, Any]] = []
    note_id_matches: list[dict[str, Any]] = []
    name_matches: list[dict[str, Any]] = []
    for row_num, row in enumerate(values[1:], 2):
        actual = pad(row)
        if not row_has_business_value(actual):
            continue
        if canonical_network(actual[4]) != network:
            continue
        axes = provider_program_identity_axes(actual, network, provider_program_id)
        if axes:
            match = {"row": row_num, "program": str(actual[2]),
                     "axes": sorted(set(axes))}
            # A batch/inventory receipt cited in Notes can mention every programme
            # on the rail. It is valid fallback identity only when no row carries a
            # direct application/tracking-link axis for this provider id. Otherwise
            # one batch receipt makes every programme on the network look identical.
            if any(axis != "notes_program_id" for axis in axes):
                strong_id_matches.append(match)
            else:
                note_id_matches.append(match)
        elif equivalent_program_name(actual[2], entry.get("program")):
            name_matches.append({"row": row_num, "program": str(actual[2]),
                                 "axes": ["program_name"]})
    if strong_id_matches:
        return strong_id_matches
    if note_id_matches:
        return note_id_matches
    return name_matches


def require_sheet_sync_create_preconditions(
    mode: str, values: list[list[Any]], rows: list[dict[str, Any]], target_rows: dict[str, int],
) -> None:
    """Prove each created target row is genuinely blank and the program genuinely new."""
    spec = SHEET_SYNC_MODES[mode]
    for entry in rows:
        if not entry.get("create"):
            continue
        program = str(entry["program"])
        matches = sheet_sync_existing_matches(values, entry)
        row_num = target_rows.get(program)
        if spec.require_marketplace_candidate_row:
            # A breadth list must never re-propose a network we already hold, were
            # rejected from, or are blocked on. Checked against every rail row and
            # the canonical parent registry, independent of how that row spells its
            # Network / Route cell. A row this mode itself already wrote is not a
            # collision - that is a replay, and the engine has already reused it.
            engaged = [
                item for item in marketplace_candidate_existing_network_rows(values, program)
                if item["row"] != row_num and str(
                    pad(values[item["row"] - 1] if len(values) >= item["row"] else [])[10] or ""
                ) != MARKETPLACE_CANDIDATE_STATUS
            ]
            if engaged:
                raise RuntimeError(
                    f"{spec.key}_network_already_on_the_sheet:{program}:"
                    f"{json.dumps(engaged, sort_keys=True)}")
        applied = [item for item in matches if item["row"] == row_num]
        others = [item for item in matches if item["row"] != row_num]
        if others:
            raise RuntimeError(
                f"{spec.key}_program_already_present:{program}:"
                f"{json.dumps(others, sort_keys=True)}")
        if not row_num:
            raise RuntimeError(f"{spec.key}_target_row_missing:{program}")
        if not applied and len(values) >= row_num and row_has_business_value(values[row_num - 1]):
            raise RuntimeError(f"{spec.key}_target_row_not_blank:{program}:{row_num}")


JUNK_ROW_PURGE_PLAN_SCHEMA = "affiliate-junk-row-purge-plan-v1"
JUNK_ROW_PURGE_WRITE_SCHEMA = "affiliate-junk-row-purge-write-v1"
JUNK_ROW_PURGE_ALLOWED_STATUS = frozenset({
    "applied_pending_brand_approval", "approved_active_not_wired",
})


def junk_row_purge_plan_rows(plan: dict[str, Any], values: list[list[Any]]) -> list[dict[str, Any]]:
    """Validate an exact, evidence-bound batch of junk rows to clear.

    This is intentionally not represented as ordinary mutable cells: the valid
    result is a reusable blank grid row. Every target must still be byte-for-byte
    the row audited before queueing and must prove it was a link-dark,
    never-deployed enumeration create with no application submission receipt.
    """
    if plan.get("schema") != JUNK_ROW_PURGE_PLAN_SCHEMA:
        raise RuntimeError("junk_row_purge_plan_schema_invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", str(plan.get("authority_sha256") or "")):
        raise RuntimeError("junk_row_purge_authority_invalid")
    if int(plan.get("tracking_clicks") or 0) or int(plan.get("provider_writes") or 0):
        raise RuntimeError("junk_row_purge_forbidden_external_effect")
    rows = plan.get("rows")
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("junk_row_purge_rows_missing")
    seen: set[int] = set()
    checked: list[dict[str, Any]] = []
    for entry in rows:
        if not isinstance(entry, dict):
            raise RuntimeError("junk_row_purge_entry_not_object")
        row_num = int(entry.get("row") or 0)
        if row_num < 2 or row_num in seen:
            raise RuntimeError(f"junk_row_purge_row_invalid_or_duplicate:{row_num}")
        seen.add(row_num)
        if len(values) < row_num or not row_has_business_value(values[row_num - 1]):
            raise RuntimeError(f"junk_row_purge_target_blank:{row_num}")
        actual = pad(values[row_num - 1])
        expected_hash = str(entry.get("expected_row_sha256") or "")
        if expected_hash != values_sha([actual]):
            raise RuntimeError(f"junk_row_purge_row_hash_mismatch:{row_num}")
        program = str(entry.get("program") or "").strip()
        if norm(actual[2]) != norm(program) or canonical_network(actual[4]) != "partnerize":
            raise RuntimeError(f"junk_row_purge_identity_mismatch:{row_num}")
        status = str(actual[10] or "").strip()
        if status not in JUNK_ROW_PURGE_ALLOWED_STATUS:
            raise RuntimeError(f"junk_row_purge_status_forbidden:{row_num}:{status}")
        if str(actual[14] or "").strip() or bool(actual[19]) or bool(actual[20]):
            raise RuntimeError(f"junk_row_purge_relationship_or_live_value_present:{row_num}")
        if not str(actual[7] or "").startswith("FIT GATE PENDING"):
            raise RuntimeError(f"junk_row_purge_fit_gate_not_pending:{row_num}")
        if entry.get("application_submission_receipt_present") is not False:
            raise RuntimeError(f"junk_row_purge_submission_receipt_not_disproven:{row_num}")
        source_update_id = str(entry.get("source_update_id") or "").strip()
        creation_path_text = str(entry.get("creation_receipt_path") or "")
        if (not source_update_id
                or (source_update_id not in str(actual[23] or "")
                    and source_update_id not in creation_path_text)):
            raise RuntimeError(f"junk_row_purge_source_update_not_bound:{row_num}")
        for path_field, sha_field in (
            ("creation_receipt_path", "creation_receipt_sha256"),
            ("audit_path", "audit_sha256"),
        ):
            raw_path = str(entry.get(path_field) or "").strip()
            digest = str(entry.get(sha_field) or "").strip()
            if not raw_path.startswith("reports/") or ".." in raw_path:
                raise RuntimeError(f"junk_row_purge_evidence_path_invalid:{row_num}:{path_field}")
            path = _REPO_ROOT / raw_path
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise RuntimeError(f"junk_row_purge_evidence_hash_mismatch:{row_num}:{path_field}")
        checked.append(dict(entry, row=row_num, program=program, status=status))
    return sorted(checked, key=lambda entry: entry["row"])


def run_junk_row_purge(plan_path: Path, out: Path, apply: bool) -> int:
    """Clear an exact audited batch through the one Sheet writer and its lock."""
    plan = json.loads(plan_path.read_text())
    svc = build("sheets", "v4", credentials=creds(), cache_discovery=False)
    values = svc.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=f"'{TAB}'!{EXPECTED_RANGE}",
        valueRenderOption="UNFORMATTED_VALUE").execute().get("values", [])
    if not values or pad(values[0]) != HEADERS:
        raise RuntimeError("canonical header mismatch; refusing junk purge")
    schema_preflight = require_checkbox_schema_pass(svc, SHEET_ID, TAB)
    rows = junk_row_purge_plan_rows(plan, values)
    baseline_sha = values_sha(values)
    target_rows = {int(entry["row"]) for entry in rows}
    nonblank_cells = sum(
        1 for entry in rows for value in pad(values[int(entry["row"]) - 1]) if value not in ("", None))
    receipt: dict[str, Any] = {
        "status": "PASS", "schema": JUNK_ROW_PURGE_WRITE_SCHEMA,
        "mode": "junk_row_purge", "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "sheet_id": SHEET_ID, "tab": TAB, "plan_path": str(plan_path),
        "plan_sha256": hashlib.sha256(plan_path.read_bytes()).hexdigest(),
        "authority_sha256": plan["authority_sha256"],
        "queue_update_ids": [str(plan.get("queue_update_id") or "")],
        "rows": sorted(target_rows), "rows_freed": [],
        "evidence": rows, "value_cells_planned": nonblank_cells,
        "full_sheet_values_precondition_sha256": baseline_sha,
        "schema_preflight_status": schema_preflight["status"],
        "tracking_clicks": 0, "provider_writes": 0,
    }
    if not apply:
        receipt.update({"dry_run": True, "no_value_write": True, "no_sheet_write": True,
                        "value_cells_written": 0})
    else:
        live = svc.spreadsheets().values().get(
            spreadsheetId=SHEET_ID, range=f"'{TAB}'!{EXPECTED_RANGE}",
            valueRenderOption="UNFORMATTED_VALUE").execute().get("values", [])
        if values_sha(live) != baseline_sha:
            raise RuntimeError("sheet_concurrency_drift_before_junk_purge")
        svc.spreadsheets().values().batchClear(
            spreadsheetId=SHEET_ID,
            body={"ranges": [f"'{TAB}'!A{row}:AA{row}" for row in sorted(target_rows)]},
        ).execute()
        reread = svc.spreadsheets().values().get(
            spreadsheetId=SHEET_ID, range=f"'{TAB}'!{EXPECTED_RANGE}",
            valueRenderOption="UNFORMATTED_VALUE").execute().get("values", [])
        not_blank = [row for row in sorted(target_rows)
                     if row_has_business_value(reread[row - 1] if len(reread) >= row else [])]
        collateral = []
        for row_index in range(max(len(values), len(reread))):
            if row_index + 1 in target_rows:
                continue
            before = pad(values[row_index] if row_index < len(values) else [])
            after = pad(reread[row_index] if row_index < len(reread) else [])
            for column, (old, new) in enumerate(zip(before, after)):
                if old != new:
                    collateral.append({"row": row_index + 1, "column": column,
                                       "before": old, "after": new})
        post_schema = require_checkbox_schema_pass(svc, SHEET_ID, TAB)
        receipt.update({
            "dry_run": False, "value_cells_written": nonblank_cells,
            "rows_freed": [{"row": entry["row"], "program": entry["program"]} for entry in rows
                           if entry["row"] not in not_blank],
            "readback_rows_not_blank": not_blank, "collateral_changes": collateral,
            "idempotent_effect": not not_blank, "post_write_values_sha256": values_sha(reread),
            "post_write_schema_status": post_schema["status"],
        })
        if not_blank or collateral or len(receipt["rows_freed"]) != len(rows):
            receipt["status"] = "FAIL"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    os.chmod(out, 0o600)
    print(json.dumps({key: receipt.get(key) for key in (
        "status", "mode", "rows", "rows_freed", "value_cells_planned",
        "value_cells_written", "dry_run")}, indent=2, sort_keys=True))
    return 0 if receipt["status"] == "PASS" else 2


def katalys_attempt_journal(rows: list[dict[str, Any]], values: list[list[Any]],
                            changes: list[tuple[int, int, Any]]) -> tuple[Path, dict[str, Any]]:
    """Keep crash-after-commit provenance across drain run directories.

    An exact target without an owned baseline/proof is observable, but cannot
    retrospectively prove collateral safety. Never acknowledge that as applied.
    The sole writer lock serializes this local journal with Sheet writes.
    """
    binding = hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    path = (_REPO_ROOT / "reports/glp-clinic-scout/affiliate-machine/sheet-sync/"
            "katalys-tracking-attempts" / (binding + ".json"))
    if path.exists():
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise RuntimeError("katalys_tracking_ready_attempt_journal_corrupt") from exc
        if (record.get("binding_sha256") != binding or record.get("schema") != "glp.katalys_tracking_attempt.v1"
                or record.get("baseline_sha256") != values_sha(record.get("baseline", []))):
            raise RuntimeError("katalys_tracking_ready_attempt_journal_invalid")
        if record.get("status") == "FAIL":
            raise RuntimeError("katalys_tracking_ready_prior_attempt_failed_requires_review")
        if record.get("status") not in {"STARTED", "PASS"}:
            raise RuntimeError("katalys_tracking_ready_attempt_journal_invalid")
        if record["status"] == "PASS":
            proof = record.get("verified_receipt") or {}
            if (proof.get("status") != "PASS" or proof.get("mode") != "katalys_tracking_ready"
                    or proof.get("collateral_changes") != [] or proof.get("readback_mismatches") != []
                    or proof.get("idempotent_rerun") is not True):
                raise RuntimeError("katalys_tracking_ready_attempt_proof_invalid")
        if record["status"] == "STARTED":
            planned = {(int(row["row"]), int(c)) for row in rows for c in row["cells"]}
            before = record["baseline"]
            for index in range(max(len(before), len(values))):
                old = pad(before[index] if index < len(before) else [])
                new = pad(values[index] if index < len(values) else [])
                if any(a != b and (index + 1, col) not in planned
                       for col, (a, b) in enumerate(zip(old, new))):
                    raise RuntimeError("katalys_tracking_ready_ambiguous_prior_collateral_requires_review")
        return path, record
    if not changes:
        raise RuntimeError("katalys_tracking_ready_ambiguous_replay_without_attempt_proof")
    record = {"schema": "glp.katalys_tracking_attempt.v1", "binding_sha256": binding,
              "baseline": values, "baseline_sha256": values_sha(values), "status": "STARTED"}
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation: corrupt/conflicting journals are never overwritten.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(record, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    return path, record


def run_sheet_sync_mode(mode: str, plan_path: Path, out: Path, apply: bool) -> int:
    """Apply one queue-drained narrow mode with CAS, readback and idempotency proof."""
    spec = SHEET_SYNC_MODES[mode]
    plan = json.loads(plan_path.read_text())
    rows = sheet_sync_plan_rows(mode, plan)
    svc = build("sheets", "v4", credentials=creds(), cache_discovery=False)
    meta = svc.spreadsheets().get(spreadsheetId=SHEET_ID).execute()
    props = next(
        sheet["properties"] for sheet in meta["sheets"]
        if sheet["properties"]["title"] == TAB
    )
    grid_before = dict(props["gridProperties"])
    values = svc.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=f"'{TAB}'!{EXPECTED_RANGE}", valueRenderOption="UNFORMATTED_VALUE"
    ).execute().get("values", [])
    if not values or pad(values[0]) != HEADERS:
        raise RuntimeError("canonical header mismatch; refusing mutation")
    schema_preflight = require_checkbox_schema_pass(svc, SHEET_ID, TAB)
    require_sheet_sync_preconditions(mode, values, rows)

    used = {row_num for row_num, row in enumerate(values[1:], 2) if row_has_business_value(row)}
    blank_rows = [row for row in range(2, int(grid_before["rowCount"]) + 1) if row not in used]
    target_rows: dict[str, int] = {}
    for entry in rows:
        if not entry.get("create"):
            continue
        program = str(entry["program"])
        reused = [
            item for item in sheet_sync_existing_matches(values, entry)
            if str(pad(values[item["row"] - 1])[10] or "") in spec.allowed_status
        ]
        if reused:
            target_rows[program] = reused[0]["row"]
            continue
        # Relevance gate on CREATES only (existing rows are never filtered).
        # 2026-08-26: the Partnerize enumeration mirrored our whole publisher
        # account into the Sheet -- Netgear Germany, Sports Direct DK, Indeed
        # AU, Etihad Holidays GCC, 32 rows in one drain. Enforced here at the
        # single writer so no future producer can bypass it by forgetting.
        verdict = program_relevance.create_verdict(
            program, fit_verified=bool(entry.get("fit_verified")))
        if not verdict["allow"]:
            raise RuntimeError(
                f"{spec.key}_program_not_relevant:{program}:"
                f"{verdict['reason']}:{verdict['detail']}")
        if not blank_rows:
            # The grid is full. Grow it rather than refusing every new provider.
            still_needed = sum(
                1 for item in rows
                if item.get("create") and str(item["program"]) not in target_rows)
            grown = grow_grid_for_creates(
                svc, int(props["sheetId"]), int(grid_before["rowCount"]),
                still_needed, spec.key)
            blank_rows.extend(grown)
            grid_before["rowCount"] = int(grid_before["rowCount"]) + len(grown)
        if not blank_rows:
            raise RuntimeError(f"{spec.key}_no_blank_grid_row_available")
        target_rows[program] = blank_rows.pop(0)
    require_sheet_sync_create_preconditions(mode, values, rows, target_rows)

    planned_rows = [
        (dict(entry, row=target_rows[str(entry["program"])]) if entry.get("create") else entry)
        for entry in rows
    ]
    created = sorted({
        int(item["row"]) for item, entry in zip(planned_rows, rows)
        if entry.get("create") and int(item["row"]) not in used
    })
    baseline_sha = values_sha(values)
    changes = sheet_sync_changed_cells(values, planned_rows)
    katalys_journal = (katalys_attempt_journal(planned_rows, values, changes)
                       if apply and spec.key == "katalys_tracking_ready" else None)
    receipt: dict[str, Any] = {
        "status": "PASS",
        "schema": spec.write_schema,
        "mode": spec.key,
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "sheet_id": SHEET_ID,
        "tab": TAB,
        "plan_path": str(plan_path),
        "plan_sha256": hashlib.sha256(plan_path.read_bytes()).hexdigest(),
        "authority_sha256": plan["authority_sha256"],
        "queue_update_ids": sorted(
            str(entry["queue_update_id"]) for entry in rows if entry.get("queue_update_id")
        ),
        "rows": sorted(int(item["row"]) for item in planned_rows),
        "created_rows": created,
        "mutable_columns": list(spec.mutable_columns),
        "evidence": [
            {"row": int(item["row"]), "program": item["program"], **item["evidence"]}
            for item in planned_rows
        ],
        "value_cells_planned": len(changes),
        "full_sheet_values_precondition_sha256": baseline_sha,
        "schema_preflight_status": schema_preflight["status"],
        "grid_before": grid_before,
        "tracking_clicks": 0,
        "provider_writes": 0,
    }
    if not apply:
        receipt.update({"dry_run": True, "no_value_write": True, "no_sheet_write": True})
    else:
        if created:
            source_row = next(
                (row_num for row_num, row in enumerate(values[1:], 2)
                 if offer_from_url(pad(row)[5]) == "1630"),
                None,
            )
            if not source_row:
                raise RuntimeError(f"{spec.key}_format_source_row_missing")
            svc.spreadsheets().batchUpdate(spreadsheetId=SHEET_ID, body={"requests": [
                {"copyPaste": {
                    "source": {"sheetId": props["sheetId"], "startRowIndex": source_row - 1,
                               "endRowIndex": source_row, "startColumnIndex": 0,
                               "endColumnIndex": len(HEADERS)},
                    "destination": {"sheetId": props["sheetId"], "startRowIndex": row - 1,
                                    "endRowIndex": row, "startColumnIndex": 0,
                                    "endColumnIndex": len(HEADERS)},
                    "pasteType": "PASTE_FORMAT", "pasteOrientation": "NORMAL",
                }} for row in created
            ] + schema_requests(props["sheetId"], created)}).execute()
        live = svc.spreadsheets().values().get(
            spreadsheetId=SHEET_ID, range=f"'{TAB}'!{EXPECTED_RANGE}", valueRenderOption="UNFORMATTED_VALUE"
        ).execute().get("values", [])
        if values_sha(live) != baseline_sha and not schema_materialization_is_safe(
            values, live, created
        ):
            raise RuntimeError("sheet_concurrency_drift_before_value_write")
        if spec.key in {"katalys_tracking_ready", "partner_email_hold"} or spec.key in provider_metadata.KINDS:
            require_sheet_sync_preconditions(mode, live, planned_rows)
        if changes:
            require_parent_row_protection(spec, changes, planned_rows)
            svc.spreadsheets().values().batchUpdate(
                spreadsheetId=SHEET_ID,
                body={"valueInputOption": "RAW", "data": [
                    {"range": f"'{TAB}'!{column_letter(column)}{row_num}",
                     "values": [[sheet_sync_write_value(column, value)]]}
                    for row_num, column, value in changes
                ]},
            ).execute()
        reread = svc.spreadsheets().values().get(
            spreadsheetId=SHEET_ID, range=f"'{TAB}'!{EXPECTED_RANGE}", valueRenderOption="UNFORMATTED_VALUE"
        ).execute().get("values", [])
        mismatches = []
        for item in planned_rows:
            row_num = int(item["row"])
            actual = pad(reread[row_num - 1] if len(reread) >= row_num else [])
            for column, expected in item["cells"].items():
                if not sheet_sync_cell_equal(int(column), actual[int(column)], expected):
                    mismatches.append({"row": row_num, "column": int(column),
                                       "expected": expected, "actual": actual[int(column)]})
        planned_cells = {
            (int(item["row"]), int(column)) for item in planned_rows for column in item["cells"]
        }
        collateral = []
        for row_index in range(max(len(values), len(reread))):
            before = pad(values[row_index] if row_index < len(values) else [])
            after = pad(reread[row_index] if row_index < len(reread) else [])
            for column, (old, new) in enumerate(zip(before, after)):
                if old == new or (row_index + 1, column) in planned_cells:
                    continue
                if (row_index + 1) in created and column in SHEET_SYNC_CHECKBOX_COLUMNS \
                        and old in ("", False) and new in ("", False):
                    continue
                collateral.append({"row": row_index + 1, "column": column,
                                   "before": old, "after": new})
        # The mode must still be satisfied by what it wrote, and the tracking
        # link is history no engine mode may erase.
        require_sheet_sync_preconditions(mode, reread, planned_rows)
        idempotent = not sheet_sync_changed_cells(reread, planned_rows)
        post_schema = require_checkbox_schema_pass(svc, SHEET_ID, TAB)
        receipt.update({
            "dry_run": False,
            "value_cells_written": len(changes),
            "readback_mismatches": mismatches,
            "collateral_changes": collateral,
            "idempotent_rerun": idempotent,
            "post_write_values_sha256": values_sha(reread),
            "post_write_schema_status": post_schema["status"],
            "tracking_links_preserved": [
                {"row": int(item["row"]),
                 "tracking_link_present": bool(str(pad(reread[int(item["row"]) - 1])[14]).strip())}
                for item in planned_rows
            ],
        })
        if mismatches or collateral or not idempotent:
            receipt["status"] = "FAIL"
    if katalys_journal:
        journal_path, journal_record = katalys_journal
        journal_record.update({"status": receipt["status"], "verified_receipt": receipt})
        temporary = journal_path.with_suffix(".pending")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            json.dump(journal_record, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, journal_path)
        receipt["attempt_journal"] = str(journal_path)
        receipt["historical_collateral_verified"] = receipt["status"] == "PASS"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    os.chmod(out, 0o600)
    print(json.dumps({key: value for key, value in receipt.items()
                      if key in ("status", "mode", "rows", "created_rows", "value_cells_planned",
                                 "value_cells_written", "idempotent_rerun", "dry_run")},
                     indent=2, sort_keys=True))
    return 0 if receipt["status"] == "PASS" else 2


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--classification")
    ap.add_argument("--applications")
    ap.add_argument("--relationships")
    ap.add_argument("--tracking-links")
    ap.add_argument("--preserve-baseline")
    ap.add_argument("--wire-in-plan", help="Run the narrow wire-in gate mode from a signed plan file.")
    ap.add_argument(
        "--approved-unlinked-disposition-plan",
        help="Record an evidence-bound terminal link-dark disposition for exact existing rows.",
    )
    ap.add_argument(
        "--network-paused-disposition-plan",
        help="Retract the live/wired claim on exact existing rows whose network paused the program.",
    )
    ap.add_argument(
        "--add-approved-row",
        help="Create a Sheet row for a portal-verified approval that has no row yet. "
             "Refuses if any row already claims the program.",
    )
    ap.add_argument(
        "--sheet-sync-mode",
        choices=sorted(SHEET_SYNC_MODES),
        help="Run one queue-drained narrow mode (see --sheet-sync-plan).",
    )
    ap.add_argument(
        "--sheet-sync-plan",
        help="Plan file for --sheet-sync-mode. One mode per plan; the plan's schema "
             "string must match the mode's, so a plan can never be run by the wrong mode.",
    )
    ap.add_argument("--repair-blank-schema-cells", action="store_true",
                    help="Fill typed cells a hand edit left empty (blank checkbox -> FALSE, "
                         "blank Vertical on a marketplace rail row -> general-wellness).")
    ap.add_argument("--repair-checkbox-values", action="store_true",
                    help="Rewrite string-typed checkbox cells as real booleans, preserving meaning.")
    ap.add_argument("--output", required=True)
    ap.add_argument(
        "--apply",
        action="store_true",
        help="Perform the planned Sheet writes; default is a zero-write plan.",
    )
    args = ap.parse_args()
    out = Path(args.output)
    if bool(args.sheet_sync_mode) != bool(args.sheet_sync_plan):
        ap.error("--sheet-sync-mode and --sheet-sync-plan must be given together")
    if (args.wire_in_plan or args.approved_unlinked_disposition_plan
            or args.network_paused_disposition_plan or args.add_approved_row
            or args.repair_checkbox_values or args.repair_blank_schema_cells
            or args.sheet_sync_mode):
        LOCK.parent.mkdir(parents=True, exist_ok=True)
        wire_lock = LOCK.open("a+")
        os.chmod(LOCK, 0o600)
        try:
            fcntl.flock(wire_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("portfolio_sheet_apply_lock_busy") from exc
        try:
            if args.sheet_sync_mode:
                return run_sheet_sync_mode(
                    args.sheet_sync_mode, Path(args.sheet_sync_plan), out, args.apply
                )
            if args.repair_blank_schema_cells:
                return run_blank_schema_repair(out, args.apply)
            if args.repair_checkbox_values:
                return run_checkbox_repair(out, args.apply)
            if args.approved_unlinked_disposition_plan:
                return run_approved_unlinked_disposition(
                    Path(args.approved_unlinked_disposition_plan), out, args.apply
                )
            if args.network_paused_disposition_plan:
                return run_network_paused_disposition(
                    Path(args.network_paused_disposition_plan), out, args.apply
                )
            if args.add_approved_row:
                return run_add_approved_row(Path(args.add_approved_row), out, args.apply)
            return run_wire_in(Path(args.wire_in_plan), out, args.apply)
        finally:
            fcntl.flock(wire_lock, fcntl.LOCK_UN)
            wire_lock.close()
    for required in ("classification", "applications", "relationships"):
        if not getattr(args, required):
            ap.error(f"--{required} is required unless a plan mode flag is given")
    baseline_rows = load_baseline_rows(args.preserve_baseline)
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    lock_handle = LOCK.open("a+")
    os.chmod(LOCK, 0o600)
    try:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise RuntimeError("portfolio_sheet_apply_lock_busy") from exc
    classification = json.loads(Path(args.classification).read_text())
    applications = json.loads(Path(args.applications).read_text())
    relationships = json.loads(Path(args.relationships).read_text())
    tracking_payload = json.loads(Path(args.tracking_links).read_text()) if args.tracking_links and Path(args.tracking_links).exists() else {}
    programs = [row for row in classification.get("programs", []) if row.get("decision") in {"APPLY", "ALREADY_ACTIVE", "DUPLICATE_ROUTE_REVIEW", "DISTINCT_ROUTE_KEEP_ACTIVE", "DUPLICATE_DO_NOT_USE"}]
    app_by_id = {int(row["offer_id"]): row for row in applications.get("programs", []) if row.get("offer_id") is not None}
    relationship_by_id = {int(row["offer_id"]): row for row in relationships.get("programs", []) if row.get("offer_id") is not None}
    track_by_id = {int(row["offer_id"]): row for row in tracking_payload.get("programs", []) if row.get("offer_id") is not None}
    if not programs or len({int(row["offer_id"]) for row in programs}) != len(programs):
        raise RuntimeError("classification target set empty or duplicated")
    parked_programs: list[dict[str, Any]] = []
    gated_programs: list[dict[str, Any]] = []
    for program in programs:
        gate = gate_batch_candidate(program, "affiliate_sheet_batch_writer.build_plan", persist=True)
        if gate["operational"]:
            gated_programs.append(program)
        else:
            parked_programs.append({"offer_id": program.get("offer_id"), "name": program.get("name"), "gate": gate})
    programs = gated_programs
    if not programs:
        receipt = {
            "schema": "affiliate_sheet_batch_writer_receipt_v1", "status": "PASS",
            "dry_run": bool(not args.apply), "parked_programs": parked_programs,
            "created": [], "updated": [], "value_cells_planned": 0,
            "no_value_write": True, "no_sheet_write": True,
            "external_effects": {"google_sheet_writes": 0, "provider_mutations": 0},
        }
        out.write_text(json.dumps(receipt, indent=2) + "\n")
        os.chmod(out, 0o600)
        print(json.dumps({"status": "PASS", "dry_run": bool(not args.apply),
                          "parked_count": len(parked_programs), "no_sheet_write": True,
                          "output": str(out)}))
        return 0
    policy_snapshot = load_snapshot()
    require_operator_ruling_clear(programs, policy_snapshot)

    svc = build("sheets", "v4", credentials=creds(), cache_discovery=False)
    meta = svc.spreadsheets().get(
        spreadsheetId=SHEET_ID,
        fields="sheets(properties(sheetId,title,gridProperties(rowCount,columnCount)))",
    ).execute()
    props = next(s["properties"] for s in meta["sheets"] if s["properties"]["title"] == TAB)
    grid_before = dict(props["gridProperties"])
    values = svc.spreadsheets().values().get(spreadsheetId=SHEET_ID, range=f"'{TAB}'!{EXPECTED_RANGE}", valueRenderOption="UNFORMATTED_VALUE").execute().get("values", [])
    if not values or pad(values[0]) != HEADERS:
        raise RuntimeError("canonical header mismatch; refusing mutation")
    schema_preflight = require_checkbox_schema_pass(svc, SHEET_ID, TAB)
    schema_repair = {"status": "SKIPPED_WRITER_FAILS_CLOSED", "repair_rows": []}
    baseline_values_sha256 = values_sha(values)
    before_nonblank = sum(1 for row in values[1:] if row_has_business_value(row))

    used = set()
    by_offer: dict[str, int] = {}
    katalys_name_rows: dict[str, list[int]] = {}
    duplicate_before: dict[str, list[int]] = {}
    for row_num, row in enumerate(values[1:], 2):
        p = pad(row)
        if row_has_business_value(p):
            used.add(row_num)
        offer = offer_from_url(p[5])
        if offer:
            if offer in by_offer:
                duplicate_before.setdefault(offer, [by_offer[offer]]).append(row_num)
            else:
                by_offer[offer] = row_num
        if str(p[4]).strip().lower() == "katalys" and p[2]:
            katalys_name_rows.setdefault(norm(p[2]), []).append(row_num)
    if duplicate_before:
        raise RuntimeError(f"preexisting duplicate Katalys offer IDs: {duplicate_before}")
    ambiguous = legacy_identity_collisions(programs, by_offer, katalys_name_rows)
    if ambiguous:
        raise RuntimeError(f"legacy_identity_mapping_required:{ambiguous}")
    empty_rows = [row for row in range(2, int(grid_before["rowCount"]) + 1) if row not in used]
    new_needed = sum(1 for program in programs if str(program["offer_id"]) not in by_offer)
    rows_added = max(0, new_needed - len(empty_rows))
    live_before_resize = svc.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=f"'{TAB}'!{EXPECTED_RANGE}", valueRenderOption="UNFORMATTED_VALUE"
    ).execute().get("values", [])
    if values_sha(live_before_resize) != baseline_values_sha256:
        raise RuntimeError("sheet_concurrency_drift_before_resize")
    if rows_added and args.apply:
        svc.spreadsheets().batchUpdate(spreadsheetId=SHEET_ID, body={"requests": [{"appendDimension": {"sheetId": props["sheetId"], "dimension": "ROWS", "length": rows_added}}]}).execute()
    if rows_added:
        empty_rows.extend(range(int(grid_before["rowCount"]) + 1, int(grid_before["rowCount"]) + rows_added + 1))

    assignments = []
    created = []
    updated = []
    for program in programs:
        offer = str(program["offer_id"])
        row_num = by_offer.get(offer)
        if row_num:
            updated.append({"offer_id": int(offer), "name": program.get("name"), "row": row_num})
            existing = pad(values[row_num - 1])
            if baseline_rows:
                baseline = baseline_rows.get(row_num)
                if not baseline or offer_from_url(pad(baseline)[5]) != offer:
                    raise RuntimeError(f"baseline_identity_mismatch:{offer}:row_{row_num}")
                baseline = pad(baseline)
                current = pad(values[row_num - 1])
                baseline[25] = current[25]
                baseline[26] = current[26]
                existing = baseline
        else:
            if program.get("decision") not in {"APPLY", "ALREADY_ACTIVE"}:
                raise RuntimeError(f"duplicate_route_review_target_missing_existing_row:{offer}")
            if not empty_rows:
                raise RuntimeError("no blank grid row available; refusing resize")
            row_num = empty_rows.pop(0)
            created.append({"offer_id": int(offer), "name": program.get("name"), "row": row_num})
            existing = [""] * len(HEADERS)
        row = make_row(program, app_by_id.get(int(offer)), relationship_by_id.get(int(offer)), existing, track_by_id.get(int(offer)))
        assignments.append((row_num, program, row))

    changed_assignments = changed_cells(values, assignments, [item["row"] for item in created])
    if not args.apply:
        receipt = {
            "status": "PASS",
            "dry_run": True,
            "captured_at_utc": datetime.now(timezone.utc).isoformat(),
            "sheet_id": SHEET_ID,
            "tab": TAB,
            "created": created,
            "updated": updated,
            "program_count_before": before_nonblank,
            "program_count_after": before_nonblank,
            "grid_before": grid_before,
            "grid_rows_planned": rows_added,
            "value_cells_planned": len(changed_assignments),
            "value_rows_planned": len({item[0] for item in changed_assignments}),
            "schema_write_requests_planned": len(created) + len(schema_requests(props["sheetId"], [item["row"] for item in created])),
            "no_value_write": True,
            "no_sheet_write": True,
            "schema_preflight_status": schema_preflight["status"],
            "full_sheet_values_precondition_sha256": baseline_values_sha256,
        }
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(receipt, indent=2) + "\n")
        os.chmod(out, 0o600)
        print(json.dumps({
            "status": "PASS",
            "dry_run": True,
            "created_count": len(created),
            "updated_count": len(updated),
            "grid_rows_planned": rows_added,
            "value_cells_planned": len(changed_assignments),
            "no_value_write": True,
            "no_sheet_write": True,
            "output": str(out),
        }, indent=2))
        return 0
    format_requests = []
    if created:
        healthrx_row = by_offer.get("1630")
        if not healthrx_row:
            raise RuntimeError("verified HealthRX format source row missing")
        for item in created:
            row = item["row"]
            format_requests.append({"copyPaste": {
                "source": {"sheetId": props["sheetId"], "startRowIndex": healthrx_row - 1, "endRowIndex": healthrx_row, "startColumnIndex": 0, "endColumnIndex": len(HEADERS)},
                "destination": {"sheetId": props["sheetId"], "startRowIndex": row - 1, "endRowIndex": row, "startColumnIndex": 0, "endColumnIndex": len(HEADERS)},
                "pasteType": "PASTE_FORMAT", "pasteOrientation": "NORMAL",
            }})
        format_requests.extend(schema_requests(props["sheetId"], [item["row"] for item in created]))
    if format_requests:
        svc.spreadsheets().batchUpdate(spreadsheetId=SHEET_ID, body={"requests": format_requests}).execute()

    live_before_write = svc.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=f"'{TAB}'!{EXPECTED_RANGE}", valueRenderOption="UNFORMATTED_VALUE"
    ).execute().get("values", [])
    if values_sha(live_before_write) != baseline_values_sha256 and not schema_materialization_is_safe(
        values, live_before_write, [item["row"] for item in created]
    ):
        raise RuntimeError("sheet_concurrency_drift_before_value_write")

    apply_value_assignments(svc, changed_assignments)

    reread = svc.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=f"'{TAB}'!{EXPECTED_RANGE}", valueRenderOption="UNFORMATTED_VALUE"
    ).execute().get("values", [])
    after_nonblank = sum(1 for row in reread[1:] if row_has_business_value(row))
    row_checks = []
    final_offer_rows: dict[str, list[int]] = {}
    for row_num, program, expected in assignments:
        actual = pad(reread[row_num - 1] if len(reread) >= row_num else [])
        mismatches = []
        for col, exp in enumerate(expected):
            got = actual[col]
            if isinstance(exp, bool):
                ok = str(got).upper() == str(exp).upper()
            else:
                ok = str(got) == str(exp)
            if not ok:
                mismatches.append({"column": HEADERS[col], "expected": exp, "actual": got})
        offer = str(program["offer_id"])
        final_offer_rows.setdefault(offer, []).append(row_num)
        row_checks.append({
            "offer_id": int(offer), "name": program.get("name"), "row": row_num,
            "status": actual[10], "tracking_link_present": bool(str(actual[14]).strip()),
            "verified_cells": len(HEADERS) - len(mismatches), "mismatches": mismatches,
        })
    # Independent duplicate scan across the full reread, not only assigned rows.
    full_offer_rows: dict[str, list[int]] = {}
    for row_num, row in enumerate(reread[1:], 2):
        offer = offer_from_url(pad(row)[5])
        if offer:
            full_offer_rows.setdefault(offer, []).append(row_num)
    duplicates = {offer: rows for offer, rows in full_offer_rows.items() if len(rows) > 1}
    comparison_rows = max(len(values), len(reread), max((row for row, _, _ in assignments), default=0))
    expected_after = expected_full_sheet_values(values, assignments, comparison_rows)
    observed_after = normalized_full_sheet_values(reread, comparison_rows)
    full_sheet_post_write_match = observed_after == expected_after
    full_sheet_expected_sha256 = values_sha(expected_after)
    full_sheet_observed_sha256 = values_sha(observed_after)

    min_row = min(row for row, _, _ in assignments)
    max_row = max(row for row, _, _ in assignments)
    grid = svc.spreadsheets().get(
        spreadsheetId=SHEET_ID,
        ranges=[f"'{TAB}'!A{min_row}:AA{max_row}"],
        includeGridData=True,
        fields="sheets(data(startRow,rowData(values(dataValidation))))",
    ).execute()
    data_block = grid["sheets"][0]["data"][0]
    start_row = data_block.get("startRow", min_row - 1)
    row_data = data_block.get("rowData", [])
    validation_checks = []
    for row_num, program, _ in assignments:
        rel = row_num - 1 - start_row
        cells = row_data[rel].get("values", []) if 0 <= rel < len(row_data) else []
        checks = {}
        for col in (1, 11, 19, 20):
            rule = cells[col].get("dataValidation", {}) if col < len(cells) else {}
            checks[HEADERS[col]] = rule.get("condition", {}).get("type") == "BOOLEAN"
        validation_checks.append({"offer_id": int(program["offer_id"]), "row": row_num, "checkbox_validation": checks})

    schema_audit = require_checkbox_schema_pass(svc, SHEET_ID, TAB)
    meta_after = svc.spreadsheets().get(
        spreadsheetId=SHEET_ID,
        fields="sheets(properties(title,gridProperties(rowCount,columnCount)))",
    ).execute()
    grid_after = next(s["properties"]["gridProperties"] for s in meta_after["sheets"] if s["properties"]["title"] == TAB)
    no_value_write = not changed_assignments
    no_sheet_write = zero_sheet_write(changed_assignments, created, rows_added, len(format_requests))
    second_plan_changes = changed_cells(reread, assignments, [item["row"] for item in created])
    second_plan_created = [
        {"offer_id": int(program["offer_id"]), "name": program.get("name")}
        for program in programs
        if str(program["offer_id"]) not in full_offer_rows
    ]
    second_blank_rows = max(0, int(grid_after["rowCount"]) - 1 - after_nonblank)
    second_plan_rows_added = max(0, len(second_plan_created) - second_blank_rows)
    second_plan_schema_requests = len(second_plan_created) * 7
    idempotent_rerun = zero_sheet_write(
        second_plan_changes,
        second_plan_created,
        second_plan_rows_added,
        second_plan_schema_requests,
    )
    all_cells = all(row["verified_cells"] == len(HEADERS) for row in row_checks)
    all_validations = all(all(row["checkbox_validation"].values()) for row in validation_checks)
    verified = write_verification_pass(
        all_cells,
        all_validations,
        schema_audit["status"],
        duplicates,
        full_sheet_post_write_match,
        idempotent_rerun,
        int(grid_after["rowCount"]) == int(grid_before["rowCount"]) + rows_added,
        int(grid_after["columnCount"]) == int(grid_before["columnCount"]),
    )
    status = "PASS" if verified else "FAIL"
    receipt = {
        "status": status,
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "sheet_id": SHEET_ID,
        "tab": TAB,
        "created": created,
        "updated": updated,
        "program_count_before": before_nonblank,
        "program_count_after": after_nonblank,
        "grid_before": grid_before,
        "grid_after": grid_after,
        "grid_rows_added": int(grid_after["rowCount"]) - int(grid_before["rowCount"]),
        "grid_columns_added": int(grid_after["columnCount"]) - int(grid_before["columnCount"]),
        "idempotent_rerun_pass": idempotent_rerun,
        "second_plan_value_cells": len(second_plan_changes),
        "second_plan_created_rows": len(second_plan_created),
        "second_plan_rows_added": second_plan_rows_added,
        "second_plan_schema_requests": second_plan_schema_requests,
        "no_value_write": no_value_write,
        "no_sheet_write": no_sheet_write,
        "value_cells_written": len(changed_assignments),
        "value_rows_written": len({item[0] for item in changed_assignments}),
        "schema_write_requests": len(format_requests),
        "schema_preflight_status": schema_preflight["status"],
        "duplicate_offer_ids": duplicates,
        "rows": row_checks,
        "validation_checks": validation_checks,
        "schema_repair": schema_repair,
        "full_sheet_values_precondition_sha256": baseline_values_sha256,
        "full_sheet_values_expected_post_write_sha256": full_sheet_expected_sha256,
        "full_sheet_values_observed_post_write_sha256": full_sheet_observed_sha256,
        "full_sheet_values_post_write_match": full_sheet_post_write_match,
        "full_sheet_checkbox_audit": schema_audit,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(receipt, indent=2) + "\n")
    os.chmod(out, 0o600)
    print(json.dumps({
        "status": status, "created_count": len(created), "updated_count": len(updated),
        "program_count_before": before_nonblank, "program_count_after": after_nonblank,
        "idempotent_rerun_pass": idempotent_rerun, "duplicate_offer_ids": duplicates,
        "full_sheet_values_post_write_match": full_sheet_post_write_match,
        "second_plan_value_cells": len(second_plan_changes),
        "second_plan_created_rows": len(second_plan_created),
        "second_plan_rows_added": second_plan_rows_added,
        "second_plan_schema_requests": second_plan_schema_requests,
        "no_value_write": no_value_write, "no_sheet_write": no_sheet_write,
        "value_cells_written": len(changed_assignments),
        "value_rows_written": len({item[0] for item in changed_assignments}),
        "schema_write_requests": len(format_requests),
        "grid_rows_added": receipt["grid_rows_added"], "grid_columns_added": receipt["grid_columns_added"],
        "full_sheet_checkbox_audit": schema_audit["status"],
        "output": str(out),
    }, indent=2))
    return 0 if status == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
