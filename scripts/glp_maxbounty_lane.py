#!/usr/bin/env python3
"""Owned fresh-browser MaxBounty enumeration and cross-network payout lane.

Scheduled runs always enumerate Browse -> Bookmarked, draft every eligible
application, refresh the coupon model, evaluate exact provider identities across
all supported rails, and queue all Sheet facts through sheet_update_queue. The
site-redesign + operator-image gate is checked before any application, tracking
link mint, or provider deployment capability is allowed to run. There is no
force flag and scheduled mode cannot disable the Sheet queue.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

BUSINESS = Path("/home/ubuntu/business")
SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT))
sys.path.insert(0, str(SOURCE_ROOT / "scripts"))
sys.path.insert(0, str(SOURCE_ROOT / "ops"))

from affiliate_payout_policy import (  # noqa: E402
    NETWORKS, collectible_eligibility, comparison_event, evaluate_portfolio,
    expected_earnings_per_100_clicks, network_key, provider_identity,
    resolve_provider_records, select_best_collectible,
)
from marketplace_adapters import maxbounty as mb  # noqa: E402
from ops import affiliate_apply_gate as application_gate  # noqa: E402

SCHEMA = "glp.maxbounty-lane.v1"
APPLICATION_ACTION_SCHEMA = "glp.maxbounty.application-action.v1"
SHEET_OUTBOX_SCHEMA = "glp.maxbounty.sheet-queue-outbox.v1"
APPLICATION_HANDOFF_SCHEMA = "glp.maxbounty.application-handoff-manifest.v1"
APPLICATION_RECONCILIATION_SCHEMA = "glp.maxbounty.application-reconciliation.v1"
MACHINE = "maxbounty_enumeration_and_payout"
WORKFLOW_ID = "iOJhvTtehlHGLUSs"
RUN_ROOT = BUSINESS / "reports/glp-clinic-scout/affiliate-machine/maxbounty"
COUPON_MODEL = BUSINESS / "data/affiliate-coupons.json"
PERFORMANCE_MODEL = BUSINESS / "data/affiliate-payout-performance.json"
QUEUE_TABLE = "hJiP3eJKBvtZ6bQU"
ERROR_CASES_TABLE = "jwrG1kHGYUIcOPWG"
HEARTBEATS_TABLE = "9Gevk0AdC7J6UbFn"
REGISTRY_TABLE = "ReulkaY4kEwCh9RY"
PARENT_NAMES = frozenset({"maxbounty", "maxbountynetwork"})
FORMULA = "effective_value = payout * lead_acceptance_rate * (1 - reversal_rate)"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def true(value: Any) -> bool:
    return value is True or str(value or "").strip().casefold() == "true"


def provider_review_slug(value: Any) -> str:
    """Derive the canonical review-page slug from a presented campaign name."""
    base = str(value or "").split(" - ", 1)[0]
    slug = re.sub(r"[^a-z0-9]+", "-", base.casefold()).strip("-")
    if not slug:
        raise RuntimeError("maxbounty_site_slug_unresolvable")
    return slug if slug.endswith("-review") else f"{slug}-review"


def official_hostname(value: Any) -> str:
    return (urlparse(str(value or "")).hostname or "").casefold().removeprefix("www.")


def snapshot_rows(snapshot: dict[str, Any]):
    """Yield governed rows from both live and legacy snapshot shapes."""
    import affiliate_sheet_batch_writer as writer  # noqa: PLC0415

    if isinstance(snapshot.get("rows"), list):
        for item in snapshot["rows"]:
            yield int(item["sheet_row"]), writer.pad(item["cells"])
        return
    for row_number, raw in enumerate((snapshot.get("values") or [])[1:], 2):
        yield row_number, writer.pad(raw)


def site_identity(snapshot: dict[str, Any], program: dict[str, Any],
                  explicit_slug: str = "") -> dict[str, str]:
    """Resolve one provider to an existing same-domain site or a stable new slug."""
    if explicit_slug:
        slug = explicit_slug
        basis = "operator_mission_explicit_site_slug"
    else:
        host = official_hostname(program.get("official_url"))
        candidates = []
        for row_number, row in snapshot_rows(snapshot):
            slug = str(row[9] or "").strip()
            if not slug or not host or official_hostname(row[6]) != host:
                continue
            score = (
                1 if str(row[20]).upper() == "TRUE" else 0,
                1 if str(row[19]).upper() == "TRUE" else 0,
                1 if str(row[10]).startswith(("approved", "live_wired")) else 0,
                1 if str(row[11]).upper() == "TRUE" else 0,
                -len(slug),
                -row_number,
            )
            candidates.append((score, slug))
        if candidates:
            slug = max(candidates)[1]
            basis = "same_official_domain_existing_site_slug"
        else:
            slug = provider_review_slug(program.get("name"))
            basis = "deterministic_provider_review_slug"
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
        raise RuntimeError(f"maxbounty_site_slug_invalid:{slug!r}")
    return {"seo_slug": slug, "suggested_glp_page": slug, "basis": basis}


def requires_site_identity(program: dict[str, Any]) -> bool:
    return (str(program.get("relationship") or "") == mb.REL_APPROVED
            and str(program.get("fit_classification") or "") == "core_glp1")


def require_program_site_identity(program: dict[str, Any]) -> None:
    if not requires_site_identity(program):
        return
    slug = str(program.get("seo_slug") or "").strip()
    suggested = str(program.get("suggested_glp_page") or "").strip()
    basis = str(program.get("basis") or "").strip()
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
        raise RuntimeError(f"maxbounty_approved_core_site_slug_invalid:{slug!r}")
    if suggested != slug:
        raise RuntimeError(
            f"maxbounty_approved_core_site_identity_mismatch:{suggested!r}:{slug!r}")
    if basis not in {
        "operator_mission_explicit_site_slug",
        "same_official_domain_existing_site_slug",
        "deterministic_provider_review_slug",
    }:
        raise RuntimeError(f"maxbounty_approved_core_site_identity_basis_invalid:{basis!r}")


def bind_required_site_identities(snapshot: dict[str, Any],
                                  programs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Bind every approved core campaign before its authenticated capture is hashed."""
    bound = []
    for source in programs:
        program = dict(source)
        if requires_site_identity(program):
            identity = site_identity(snapshot, program)
            for field in ("seo_slug", "suggested_glp_page"):
                supplied = str(program.get(field) or "").strip()
                if supplied and supplied != identity[field]:
                    raise RuntimeError(
                        f"maxbounty_site_identity_conflict:{program.get('program_id')}:"
                        f"{field}:{supplied}:{identity[field]}")
            program.update(identity)
            require_program_site_identity(program)
        bound.append(program)
    return bound


def repo_rel(path: Path) -> str:
    return str(Path(path).resolve().relative_to(BUSINESS))


def atomic_json(path: Path, payload: Any, mode: int = 0o600) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n"
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.chmod(mode)
    os.replace(temporary, path)
    path.chmod(mode)
    try:
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        pass
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_sheet_snapshot() -> dict[str, Any]:
    """Read the governed Sheet. This function has no write method or queue bypass."""
    from googleapiclient.discovery import build  # noqa: PLC0415
    import affiliate_sheet_batch_writer as writer  # noqa: PLC0415

    service = build("sheets", "v4", credentials=writer.creds(), cache_discovery=False)
    values = service.spreadsheets().values().get(
        spreadsheetId=writer.SHEET_ID,
        range=f"'{writer.TAB}'!{writer.EXPECTED_RANGE}",
        valueRenderOption="UNFORMATTED_VALUE",
    ).execute().get("values", [])
    if not values or writer.pad(values[0]) != writer.HEADERS:
        raise RuntimeError("maxbounty_sheet_header_mismatch")
    rows = []
    for row_num, values_row in enumerate(values[1:], 2):
        padded = writer.pad(values_row)
        if writer.row_has_business_value(padded):
            rows.append({"sheet_row": row_num, "cells": padded})
    parents = [row for row in rows if norm(row["cells"][2]) in PARENT_NAMES]
    if len(parents) != 1:
        raise RuntimeError(f"maxbounty_parent_row_missing_or_ambiguous:{len(parents)}")
    login_url = str(parents[0]["cells"][12] or "").strip()
    if not login_url.startswith("https://"):
        raise RuntimeError("maxbounty_parent_affiliate_login_missing_or_invalid")
    return {"headers": list(writer.HEADERS), "rows": rows,
            "parent_row": parents[0]["sheet_row"], "login_url": login_url}


def program_id_from_sheet(cells: list[Any]) -> str:
    for url in (cells[5], cells[14]):
        found = mb.program_id_from_url(url)
        if found:
            return found
        try:
            query = parse_qs(urlparse(str(url or "")).query)
        except ValueError:
            query = {}
        for key in ("offer_id", "campaign_id", "program_id", "mid", "oid"):
            value = str((query.get(key) or [""])[0]).strip()
            if value:
                return value
    notes = str(cells[23] or "")
    match = re.search(r"\b(?:offer|campaign|programme|program)\s+([A-Za-z0-9._:-]{2,64})\b",
                      notes, re.I)
    return match.group(1) if match else ""


def payout_from_terms(text: Any) -> dict[str, Any] | None:
    terms = " ".join(str(text or "").split())
    percent = re.search(r"(?<![0-9])([0-9]+(?:\.[0-9]+)?)\s*%", terms)
    if percent:
        return {"headline_payout": float(percent.group(1)), "currency": "PERCENT",
                "payout_model": "percent_revshare", "payout_terms": terms}
    parsed = mb.payout_details(terms)
    if parsed.get("headline_payout") is None:
        return None
    return parsed


def relationship_from_status(status: Any, signup: Any) -> str:
    text = str(status or "").casefold()
    if "reject" in text or "declin" in text or "denied" in text:
        return "rejected"
    if "pending" in text or "gated" in text or "not_related" in text:
        return "apply_needed"
    if text.startswith("approved") or "live_wired" in text:
        return "approved"
    if true(signup):
        return "unclassified"
    return "unclassified"


def sheet_payout_records(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for item in snapshot["rows"]:
        cells = item["cells"]
        network = network_key(cells[4])
        if network not in NETWORKS or norm(cells[2]) in PARENT_NAMES:
            continue
        payout = payout_from_terms(cells[16])
        if payout is None:
            continue
        program_id = program_id_from_sheet(cells)
        if not program_id:
            continue
        record = {
            "network": network,
            "program_id": program_id,
            "provider_name": str(cells[2] or "").strip(),
            "official_url": str(cells[6] or "").strip(),
            "relationship": relationship_from_status(cells[10], cells[11]),
            "wire_in_ready": true(cells[19]),
            "wired_live": true(cells[20]),
            "attribution_days": mb.parse_attribution_days(cells[17]),
            "sheet_row": item["sheet_row"],
            "page_slug": str(cells[9] or "").strip(),
            "suggested_page": str(cells[8] or "").strip(),
            **payout,
        }
        records.append(record)
    return records


def maxbounty_payout_records(programs: list[dict[str, Any]],
                             sheet_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sheet_by_id = {(row["network"], row["program_id"].casefold()): row for row in sheet_records}
    records = []
    for program in programs:
        if program.get("headline_payout") is None:
            continue
        prior = sheet_by_id.get(("maxbounty", str(program["program_id"]).casefold()), {})
        records.append({
            "network": "maxbounty",
            "program_id": str(program["program_id"]),
            "provider_name": str(program["name"]),
            "official_url": str(program.get("official_url") or ""),
            "relationship": program["relationship"],
            "wire_in_ready": bool(prior.get("wire_in_ready", False)),
            "wired_live": bool(prior.get("wired_live", False)),
            "attribution_days": program.get("attribution_days"),
            "sheet_row": prior.get("sheet_row"),
            "page_slug": prior.get("page_slug", ""),
            "suggested_page": prior.get("suggested_page", ""),
            "detail_url": program.get("detail_url"),
            "landing_pages": program.get("landing_pages") or [],
            "headline_payout": program["headline_payout"],
            "currency": program.get("currency") or "USD",
            "payout_model": program.get("payout_model") or "unknown",
            "payout_terms": program.get("payout_terms") or "",
        })
    return records


def merged_portfolio(snapshot: dict[str, Any], programs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sheet_records = sheet_payout_records(snapshot)
    fresh = maxbounty_payout_records(programs, sheet_records)
    fresh_ids = {row["program_id"].casefold() for row in fresh}
    return [row for row in sheet_records
            if row["network"] != "maxbounty" or row["program_id"].casefold() not in fresh_ids] + fresh


def target_best_collectible_selection(
    records: list[dict[str, Any]],
    performance_rows: list[dict[str, Any]],
    *,
    target_network: str,
    target_program_id: str,
    target_tracking_ready: bool,
) -> dict[str, Any]:
    """Run governed best-terms math for one newly retrieved target rail.

    Tracking retrieval makes the target economically rankable, but this receipt
    does not authorize deployment. Research and the existing wire-backlog gate
    remain mandatory downstream.
    """
    target = (network_key(target_network), str(target_program_id).casefold())
    prepared: list[dict[str, Any]] = []
    target_count = 0
    for raw in records:
        row = dict(raw)
        key = (network_key(row.get("network")), str(row.get("program_id") or "").casefold())
        if key == target:
            target_count += 1
            row["wire_in_ready"] = bool(target_tracking_ready or row.get("wire_in_ready"))
        prepared.append(row)
    if target_count != 1:
        raise RuntimeError(f"target_payout_record_match_count:{target_count}")

    resolved = resolve_provider_records(prepared)
    target_row = next(row for row in resolved if (
        network_key(row.get("network")), str(row.get("program_id") or "").casefold()
    ) == target)
    identity = str(target_row.get("provider_identity") or "")
    selection = select_best_collectible(resolved, performance_rows)
    providers = [row for row in selection.get("providers") or []
                 if str(row.get("provider_identity") or "") == identity]
    if len(providers) != 1:
        raise RuntimeError(f"target_payout_provider_match_count:{len(providers)}")
    provider = providers[0]
    winner = provider.get("selected_winner")
    winners_by_event = provider.get("winners_by_event") or []
    unknown_cross_event = len(winners_by_event) > 1 and any(
        row.get("expected_earnings_per_100_clicks") is None for row in winners_by_event)
    if unknown_cross_event:
        verdict = "INSUFFICIENT_DATA"
        reason = "cross_event_expected_earnings_per_100_clicks_unavailable"
    elif not winner:
        verdict = "INSUFFICIENT_DATA"
        reason = "no_collectible_approved_offer"
    elif (network_key(winner.get("network")), str(winner.get("program_id") or "").casefold()) == target:
        verdict = "WINNER"
        reason = "governed_best_collectible_offer"
    else:
        verdict = "NONWINNER"
        reason = "higher_value_collectible_sibling_exists"

    performance_index = {
        (network_key(row.get("network")), str(row.get("program_id") or "").casefold()): row
        for row in performance_rows
    }
    considered = []
    for row in resolved:
        if str(row.get("provider_identity") or "") != identity:
            continue
        key = (network_key(row.get("network")), str(row.get("program_id") or "").casefold())
        eligibility = collectible_eligibility(row)
        expected = expected_earnings_per_100_clicks(row, performance_index.get(key))
        considered.append({
            "network": key[0],
            "program_id": str(row.get("program_id") or ""),
            "relationship": eligibility["relationship"],
            "collectible": eligibility["eligible"],
            "ineligible_reasons": eligibility["reasons"],
            "comparison_event": comparison_event(row),
            "headline_payout": row.get("headline_payout"),
            "currency": str(row.get("currency") or "USD").upper(),
            "expected_earnings_per_100_clicks": expected.get(
                "expected_earnings_per_100_clicks"),
            "expected_earnings_basis": expected.get("basis"),
        })

    return {
        "schema": "glp.maxbounty.target-best-collectible-selection.v1",
        "target": {"network": target[0], "program_id": target_program_id},
        "provider_identity": identity,
        "verdict": verdict,
        "reason": reason,
        "selection_basis": provider.get("selection_basis"),
        "selected_winner": winner,
        "considered_rails": considered,
        "formula": (
            "same paid event: highest headline payout; different paid events: "
            "payout x governed conversion prior per 100 clicks, overridden only "
            "by qualified measured or network-reported performance"
        ),
        "deployment_gate": "RESEARCH_AND_EXISTING_WIRE_BACKLOG_REQUIRED",
        "effects": {"sheet_writes": 0, "provider_mutations": 0,
                    "tracking_links_opened": 0, "tracking_links_clicked": 0,
                    "site_deployments": 0},
    }


def load_performance() -> list[dict[str, Any]]:
    payload = json.loads(PERFORMANCE_MODEL.read_text())
    if payload.get("schema") != "glp.affiliate-payout-performance.v1":
        raise RuntimeError("affiliate_payout_performance_schema_invalid")
    rows = payload.get("records")
    if not isinstance(rows, list):
        raise RuntimeError("affiliate_payout_performance_records_invalid")
    return rows


def coupon_summary(program: dict[str, Any]) -> str:
    coupons = program.get("coupons") or []
    if not coupons:
        return "No coupon code exposed in captured Coupon section"
    return "; ".join(f"{row['code']}: {row['terms']}" for row in coupons)[:1800]


def cookie_summary(program: dict[str, Any]) -> str:
    days = program.get("attribution_days")
    return f"{int(days)} days" if days is not None else "Not exposed in captured campaign terms"


def update_coupon_model(programs: list[dict[str, Any]], capture_path: Path,
                        capture_sha: str, captured_at: str) -> dict[str, Any]:
    payload = json.loads(COUPON_MODEL.read_text())
    if payload.get("schema") != "glp.affiliate-coupons.v1":
        raise RuntimeError("affiliate_coupon_model_schema_invalid")
    providers = payload.setdefault("providers", {})
    for program in programs:
        key = f"maxbounty:{program['program_id']}"
        providers[key] = {
            "network": "MaxBounty",
            "network_program_id": str(program["program_id"]),
            "provider_name": str(program["name"]),
            "official_url": str(program.get("official_url") or ""),
            "coupons": program.get("coupons") or [],
            "coupon_section": str(program.get("coupon_section") or ""),
            "captured_at_utc": captured_at,
            "source_capture_path": repo_rel(capture_path),
            "source_capture_sha256": capture_sha,
        }
    payload["updated_at_utc"] = captured_at
    digest = atomic_json(COUPON_MODEL, payload)
    return {"providers_refreshed": len(programs),
            "providers_with_coupons": sum(bool(row.get("coupons")) for row in programs),
            "coupon_codes": sum(len(row.get("coupons") or []) for row in programs),
            "path": repo_rel(COUPON_MODEL), "sha256": digest}


def inventory_queue_row(program: dict[str, Any], capture_path: Path,
                        capture_sha: str, captured_at: str) -> dict[str, Any]:
    require_program_site_identity(program)
    evidence = {
        "portal_capture_path": repo_rel(capture_path),
        "portal_capture_sha256": capture_sha,
        "captured_at_utc": captured_at,
        "network_program_id": str(program["program_id"]),
        "relationship_button": str(program["relationship_button"]),
        "relationship_status_evidence": str(
            program.get("relationship_status_evidence") or program["relationship_button"]),
        "fit_classification": str(program["fit_classification"]),
        "vertical": str(program["vertical"]),
        "fit_reason": str(program["fit_reason"]),
        "coupon_summary": coupon_summary(program),
        "payout_terms": str(program.get("payout_terms") or "Not exposed in captured detail"),
        "cookie_window": cookie_summary(program),
        "detail_url": str(program["detail_url"]),
        "official_url": str(program.get("official_url") or program["detail_url"]),
        "suggested_glp_page": str(program.get("suggested_glp_page") or ""),
        "seo_slug": str(program.get("seo_slug") or ""),
        "site_identity_basis": str(program.get("basis") or ""),
    }
    identity = f"maxbounty-inventory-{program['program_id']}-{capture_sha[:16]}"
    return {
        "update_id": identity,
        "kind": "maxbounty_inventory",
        "network": "MaxBounty",
        "program_id": str(program["program_id"]),
        "program_name": str(program["name"])[:160],
        "evidence": json.dumps(evidence, sort_keys=True),
        "status": "queued",
        "queued_by": MACHINE,
        "queued_at_utc": captured_at,
        "resolved_at_utc": "",
        "note": ("Authenticated MaxBounty Browse > Bookmarked enumeration; relationship "
                 "derived from the exact campaign-detail button, never a name list.")
    }


def decision_basis(decision: dict[str, Any]) -> str:
    bases = {str((decision.get(side) or {}).get("economics", {}).get("basis") or "")
             for side in ("incumbent", "challenger")}
    bases.discard("")
    return next(iter(bases)) if len(bases) == 1 else "mixed"


def payout_queue_rows(evaluation: dict[str, Any], evaluation_path: Path,
                      evaluation_sha: str, evaluated_at: str,
                      records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    record_index = {(row["network"], row["program_id"]): row for row in records}
    rows = []
    for decision in evaluation.get("decisions") or []:
        inc = decision.get("incumbent") or {}
        chal = decision.get("challenger") or {}
        target = record_index.get((str(inc.get("network")), str(inc.get("program_id"))))
        if not target or not target.get("sheet_row"):
            continue
        inc_value = str((inc.get("economics") or {}).get("effective_value"))
        chal_value = str((chal.get("economics") or {}).get("effective_value"))
        if chal_value == "None":
            chal_value = "not_computed"
        evidence = {
            "payout_evaluation_path": repo_rel(evaluation_path),
            "payout_evaluation_sha256": evaluation_sha,
            "evaluated_at_utc": evaluated_at,
            "provider_identity": str(decision["provider_identity"]),
            "basis": decision_basis(decision),
            "formula": FORMULA,
            "incumbent_effective_value": inc_value,
            "challenger_effective_value": chal_value,
            "decision": str(decision["decision"]),
        }
        rows.append({
            "update_id": (f"payout-eval-{inc['network']}-{inc['program_id']}-"
                          f"{chal['network']}-{chal['program_id']}-{evaluation_sha[:12]}"),
            "kind": "payout_evaluation",
            "network": str(inc["network"]),
            "program_id": str(inc["program_id"]),
            "program_name": str(target["provider_name"])[:160],
            "sheet_row": int(target["sheet_row"]),
            "evidence": json.dumps(evidence, sort_keys=True),
            "status": "queued", "queued_by": MACHINE,
            "queued_at_utc": evaluated_at, "resolved_at_utc": "",
            "note": "Automatic cross-network payout policy evaluation from every enumeration pass.",
        })
    return rows


def queue_once(row: dict[str, Any], *, table: str = QUEUE_TABLE,
               dt: Any = None) -> str:
    if dt is None:
        import n8n_dt as dt  # noqa: PLC0415
    update_id = str(row["update_id"])
    if dt.rows(table, dt.eq("update_id", update_id), limit=5):
        return "already_present"
    try:
        dt.insert(table, row)
    except Exception:
        # insertRows is intentionally not retried after an ambiguous transport
        # failure. Read back the identity once; only an absent row is re-raised.
        if dt.rows(table, dt.eq("update_id", update_id), limit=5):
            return "present_after_ambiguous_insert"
        raise
    if not dt.rows(table, dt.eq("update_id", update_id), limit=5):
        raise RuntimeError(f"maxbounty_queue_readback_missing:{update_id}")
    return "inserted_verified"


def queue_all(rows: list[dict[str, Any]], dt: Any = None) -> dict[str, int]:
    counts = {"inserted_verified": 0, "already_present": 0,
              "present_after_ambiguous_insert": 0}
    for row in rows:
        outcome = queue_once(row, dt=dt)
        counts[outcome] += 1
    return counts


def draft_applications(programs: list[dict[str, Any]], run_dir: Path) -> dict[str, Any]:
    drafts = [mb.application_draft(row) for row in programs
              if row["relationship"] == mb.REL_APPLY_NEEDED
              and row["fit_classification"] == "core_glp1"]
    path = run_dir / "application-drafts.json"
    digest = atomic_json(path, {
        "schema": "glp.maxbounty.application-drafts.v1",
        "captured_at_utc": utc_now(),
        "traffic_type_constant": mb.TRAFFIC_TYPE,
        "leads_per_day": mb.LEADS_PER_DAY,
        "creative": mb.CREATIVE_ANSWER,
        "submission_gate": mb.mutation_gate_status(mb.load_policy()),
        "drafts": drafts,
        "effects": {"applications_submitted": 0, "tracking_links_minted": 0,
                    "provider_deploys": 0},
    })
    return {"count": len(drafts), "path": repo_rel(path), "sha256": digest}


def mutation_plan(evaluation: dict[str, Any], programs: list[dict[str, Any]]) -> dict[str, Any]:
    gate = mb.mutation_gate_status(mb.load_policy())
    by_id = {str(row["program_id"]): row for row in programs}
    actions = []
    for decision in evaluation.get("decisions") or []:
        challenger = decision.get("challenger") or {}
        if challenger.get("network") != "maxbounty":
            continue
        program = by_id.get(str(challenger.get("program_id")))
        if not program or program.get("fit_classification") != "core_glp1":
            continue
        if decision.get("decision") == "APPLY_TO_HIGHER_PAYING_CHALLENGER":
            actions.append({"action": "submit_application", "program_id": program["program_id"],
                            "status": "GATE_CLOSED" if not gate["open"] else "ELIGIBLE"})
        elif decision.get("decision") == "SWITCH_ATOMIC_AFTER_LINK_VALIDATION":
            actions.append({"action": "mint_validate_then_atomic_switch",
                            "program_id": program["program_id"],
                            "status": "GATE_CLOSED" if not gate["open"] else "ELIGIBLE",
                            "fallback": "KEEP_INCUMBENT"})
    return {"gate": gate, "actions": actions,
            "effects": {"applications_submitted": 0, "tracking_links_minted": 0,
                        "provider_deploys": 0}}


def highest_intent_landing_page(program: dict[str, Any], page_slug: Any) -> str:
    """Choose the portal destination with the strongest token match to the site page."""
    candidates = [str(value) for value in (program.get("landing_pages") or [])
                  if str(value).startswith("https://")]
    official = str(program.get("official_url") or "")
    if official.startswith("https://") and "maxbounty.com" not in (
            urlparse(official).hostname or ""):
        candidates.append(official)
    candidates = sorted(set(candidates))
    if not candidates:
        raise mb.MaxBountyError("maxbounty_high_intent_landing_page_not_exposed")
    tokens = {token for token in re.split(r"[^a-z0-9]+", str(page_slug).casefold())
              if len(token) >= 3}
    return max(candidates, key=lambda value: (
        sum(token in value.casefold() for token in tokens),
        "pricing" in value.casefold() or "weight" in value.casefold(),
        -len(value), value,
    ))


def _action_queue_row(result: dict[str, Any], program: dict[str, Any],
                      action_path: Path, action_sha: str) -> dict[str, Any]:
    program_id = str(program["program_id"])
    def create_cells(prefix: str) -> dict[str, Any]:
        cells = {
            "1": result["status"] == "SUBMITTED_VERIFIED",
            "2": str(program["name"]), "3": "Affiliate program", "4": "MaxBounty",
            "6": str(program.get("official_url") or program["detail_url"]),
        }
        missing = [key for key, field in (("7", "fit_reason"), ("25", "vertical"))
                   if not str(program.get(field) or "").strip()]
        if missing:
            # Older prebound application snapshots did not carry the researched
            # create cells. Only a pre-existing, action-bound outbox can supply
            # those facts; never infer a fit reason from a campaign name or URL.
            outbox_path = action_path.parent / f"sheet-queue-outbox-{program_id}.json"
            if not outbox_path.is_file():
                raise mb.MaxBountyError("maxbounty_handoff_create_facts_missing")
            outbox = json.loads(outbox_path.read_text(encoding="utf-8"))
            prior_row = outbox.get("sheet_queue_row") or {}
            expected_id = f"{prefix}-{program_id}-{action_sha[:16]}"
            if (outbox.get("schema") != SHEET_OUTBOX_SCHEMA
                    or str(outbox.get("program_id")) != program_id
                    or outbox.get("update_id") != expected_id
                    or outbox.get("source_action_receipt_path") != repo_rel(action_path)
                    or outbox.get("source_action_receipt_sha256") != action_sha
                    or prior_row.get("update_id") != expected_id
                    or prior_row.get("kind") != "application_pending"
                    or prior_row.get("network") != "MaxBounty"
                    or str(prior_row.get("program_id")) != program_id):
                raise mb.MaxBountyError("maxbounty_handoff_existing_outbox_source_drift")
            prior_evidence = json.loads(str(prior_row.get("evidence") or "{}"))
            if (prior_evidence.get("submission_receipt_path") != repo_rel(action_path)
                    or prior_evidence.get("submission_receipt_sha256") != action_sha
                    or not isinstance(prior_evidence.get("row_cells"), dict)):
                raise mb.MaxBountyError("maxbounty_handoff_existing_outbox_source_drift")
            for key in missing:
                value = prior_evidence["row_cells"].get(key)
                if not isinstance(value, str) or not value.strip():
                    raise mb.MaxBountyError("maxbounty_handoff_create_facts_missing")
                cells[key] = value
        for key, field in (("7", "fit_reason"), ("25", "vertical")):
            if key not in cells:
                cells[key] = str(program[field])
        return cells

    if result["status"] == "SUBMITTED_VERIFIED":
        evidence = {
            "submission_receipt_path": repo_rel(action_path),
            "submission_receipt_sha256": action_sha,
            "submitted_at_utc": str(result["submitted_at_utc"]),
            "source": "MaxBounty authenticated application confirmation",
            # Inventory normally creates this row first. Shipping the researched
            # create cells here too makes the action queue independently
            # drainable if queue ordering changes; the pending-mode writer still
            # owns and overwrites every status/live field.
            "row_cells": create_cells("maxbounty-application"),
        }
        kind = "application_pending"
        prefix = "maxbounty-application"
        note = "Gated MaxBounty provider action verified and queued through the one Sheet writer."
    elif result["status"] == mb.STATUS_PENDING_NO_ATTEMPT:
        attempt_path = Path(str(result.get("attempt_receipt") or ""))
        evidence = {
            # The shared writer's application-pending contract predates provider
            # relationship enumeration and names this field `submission_receipt`.
            # This action receipt explicitly says no submit occurred; the note
            # below preserves that distinction for every downstream consumer.
            "submission_receipt_path": repo_rel(action_path),
            "submission_receipt_sha256": action_sha,
            "submitted_at_utc": str(result["observed_at_utc"]),
            "source": "MaxBounty authenticated campaign relationship GET",
            "provider_observation_receipt_path": repo_rel(attempt_path),
            "provider_observation_receipt_sha256": str(
                result.get("attempt_receipt_sha256") or ""),
            "provider_application_attempts": 0,
            "provider_mutations": 0,
            "row_cells": create_cells("maxbounty-provider-pending"),
        }
        kind = "application_pending"
        prefix = "maxbounty-provider-pending"
        note = (
            "MaxBounty already reported this campaign under review. The cited receipt is the "
            "provider's pending relationship readback, not a submit receipt; this run made zero "
            "provider application attempts and queued the observed pending state through the one "
            "Sheet writer."
        )
    else:
        # The generic writer accepts evidence from reports/, not mutable config/.
        # Snapshot the policy next to the provider receipt, then bind it both to
        # this queue row and to the CURRENT policy in the writer. A stale open
        # snapshot can therefore never reopen a gate that was subsequently shut.
        gate_path = action_path.parent / "gate-policy-snapshot.json"
        gate_raw = mb.DEFAULT_GATE_POLICY.read_bytes()
        gate_path.write_bytes(gate_raw)
        gate_path.chmod(0o600)
        gate_sha = hashlib.sha256(gate_raw).hexdigest()
        evidence = {
            "mint_receipt_path": repo_rel(action_path),
            "mint_receipt_sha256": action_sha,
            "minted_at_utc": str(result["minted_at_utc"]),
            "network_program_id": program_id,
            "tracking_link": str(result["tracking_link"]),
            "tracking_link_sha256": str(result.get("tracking_link_sha256") or ""),
            "tracking_host": str(result.get("tracking_host") or ""),
            "rail_capture_requirements_sha256": str(
                result.get("rail_capture_requirements_sha256") or ""),
            "tracking_configuration": json.dumps(result["configuration"], sort_keys=True),
            "gate_policy_snapshot_path": repo_rel(gate_path),
            "gate_policy_snapshot_sha256": gate_sha,
        }
        if result.get("retrieval_authorization_path"):
            authorization_path = Path(str(result["retrieval_authorization_path"]))
            evidence.update({
                "retrieval_authorization_path": repo_rel(authorization_path),
                "retrieval_authorization_sha256": str(
                    result.get("retrieval_authorization_sha256") or ""),
                "authorization_mode": str(result.get("authorization_mode") or ""),
            })
        kind = "maxbounty_tracking_link"
        prefix = "maxbounty-link"
        note = "Gated MaxBounty provider action verified and queued through the one Sheet writer."
    return {
        "update_id": f"{prefix}-{program_id}-{action_sha[:16]}",
        "kind": kind, "network": "MaxBounty", "program_id": program_id,
        "program_name": str(program["name"])[:160],
        "evidence": json.dumps(evidence, sort_keys=True), "status": "queued",
        "queued_by": MACHINE,
        "queued_at_utc": str(result.get("submitted_at_utc")
                             or result.get("minted_at_utc")
                             or result.get("observed_at_utc") or utc_now()),
        "resolved_at_utc": "",
        "note": note,
    }


def _write_sheet_queue_outbox(run_dir: Path, queue_row: dict[str, Any],
                              action_path: Path, action_sha: str) -> dict[str, Any]:
    """Persist the single-writer queue handoff before the outer workflow can crash."""
    program_id = str(queue_row.get("program_id") or "")
    update_id = str(queue_row.get("update_id") or "")
    if not program_id or not update_id:
        raise mb.MaxBountyError("maxbounty_sheet_outbox_identity_missing")
    path = run_dir / f"sheet-queue-outbox-{program_id}.json"
    payload = {
        "schema": SHEET_OUTBOX_SCHEMA,
        "created_at_utc": str(queue_row.get("queued_at_utc") or utc_now()),
        "program_id": program_id,
        "update_id": update_id,
        "source_action_receipt_path": repo_rel(action_path),
        "source_action_receipt_sha256": action_sha,
        "sheet_queue_row": queue_row,
        "effects": {"sheet_direct_writes": 0, "sheet_queue_rows_prepared": 1,
                    "tracking_links_opened": 0, "tracking_links_clicked": 0},
    }
    expected = hashlib.sha256(
        (json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n").encode("utf-8")
    ).hexdigest()
    if path.is_file():
        outbox_sha = hashlib.sha256(path.read_bytes()).hexdigest()
        accepted_hashes = {expected}
        try:
            evidence = json.loads(str(queue_row.get("evidence") or "{}"))
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (TypeError, ValueError):
            evidence = {}
            existing = {}
        legacy_timestamp = str(existing.get("created_at_utc") or "")
        existing_row = existing.get("sheet_queue_row") or {}
        if (evidence.get("source") == "MaxBounty authenticated campaign relationship GET"
                and int(evidence.get("provider_application_attempts") or 0) == 0
                and int(evidence.get("provider_mutations") or 0) == 0
                and legacy_timestamp
                and existing_row.get("queued_at_utc") == legacy_timestamp):
            # Pending-observation outboxes produced before queued_at_utc was bound
            # to observed_at_utc used the one-time materialization clock instead.
            # Accept those immutable bytes only when replacing both timestamps in
            # the otherwise-current payload reproduces the exact existing hash.
            legacy_payload = {
                **payload,
                "created_at_utc": legacy_timestamp,
                "sheet_queue_row": {
                    **queue_row,
                    "queued_at_utc": legacy_timestamp,
                },
            }
            accepted_hashes.add(hashlib.sha256(
                (json.dumps(legacy_payload, indent=2, sort_keys=True, default=str) + "\n").encode(
                    "utf-8")
            ).hexdigest())
        if outbox_sha not in accepted_hashes:
            raise mb.MaxBountyError("maxbounty_sheet_outbox_existing_bytes_drift")
    else:
        outbox_sha = atomic_json(path, payload)
    return {"path": repo_rel(path), "sha256": outbox_sha,
            "schema": SHEET_OUTBOX_SCHEMA, "update_id": update_id}


def _prepare_application_handoff(run_dir: Path, *, queue_id: str,
                                 program: dict[str, Any], instruction_path: Path,
                                 instruction_sha: str) -> Path:
    """Bind all recovery inputs durably before any provider submit is possible."""
    program_id = str(program.get("program_id") or "")
    if not program_id:
        raise mb.MaxBountyError("maxbounty_handoff_program_identity_missing")
    path = run_dir / f"application-handoff-{program_id}.json"
    payload = {
        "schema": APPLICATION_HANDOFF_SCHEMA,
        "prepared_at_utc": utc_now(),
        "queue_id": str(queue_id),
        "program_id": program_id,
        "program_snapshot": program,
        "operator_instruction_path": repo_rel(instruction_path),
        "operator_instruction_sha256": instruction_sha,
        "provider_attempt_receipt_path": repo_rel(
            run_dir / f"provider-attempt-{program_id}.json"),
        "action_receipt_path": repo_rel(run_dir / f"application-{program_id}.json"),
        "sheet_queue_outbox_path": repo_rel(
            run_dir / f"sheet-queue-outbox-{program_id}.json"),
        "provider_mutations": 0,
        "sheet_direct_writes": 0,
    }
    atomic_json(path, payload)
    return path


def _bound_handoff_path(manifest: dict[str, Any], key: str,
                        run_dir: Path, expected_name: str) -> Path:
    value = Path(str(manifest.get(key) or ""))
    if not value.is_absolute():
        value = BUSINESS / value
    value = value.resolve()
    if value.parent != run_dir.resolve() or value.name != expected_name:
        raise mb.MaxBountyError(f"maxbounty_handoff_{key}_identity_invalid")
    return value


def materialize_application_handoff(path_value: str | Path) -> dict[str, Any]:
    """Recover action + outbox solely from a prebound manifest and verified attempt."""
    path = Path(path_value)
    if not path.is_absolute():
        path = BUSINESS / path
    path = path.resolve()
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("schema") != APPLICATION_HANDOFF_SCHEMA:
        raise mb.MaxBountyError("maxbounty_handoff_schema_invalid")
    run_dir = path.parent
    program_id = str(manifest.get("program_id") or "")
    if path.name != f"application-handoff-{program_id}.json":
        raise mb.MaxBountyError("maxbounty_handoff_manifest_identity_invalid")
    program = manifest.get("program_snapshot")
    if not isinstance(program, dict) or str(program.get("program_id") or "") != program_id:
        raise mb.MaxBountyError("maxbounty_handoff_program_snapshot_invalid")
    instruction_path = _bound_handoff_path(
        manifest, "operator_instruction_path", run_dir,
        f"queue-instructions-{program_id}.json")
    instruction_sha = str(manifest.get("operator_instruction_sha256") or "").lower()
    if (not re.fullmatch(r"[0-9a-f]{64}", instruction_sha)
            or hashlib.sha256(instruction_path.read_bytes()).hexdigest() != instruction_sha):
        raise mb.MaxBountyError("maxbounty_handoff_instruction_sha_invalid")
    attempt_path = _bound_handoff_path(
        manifest, "provider_attempt_receipt_path", run_dir,
        f"provider-attempt-{program_id}.json")
    action_path = _bound_handoff_path(
        manifest, "action_receipt_path", run_dir,
        f"application-{program_id}.json")
    outbox_path = _bound_handoff_path(
        manifest, "sheet_queue_outbox_path", run_dir,
        f"sheet-queue-outbox-{program_id}.json")
    if not attempt_path.is_file():
        return {"status": "HANDOFF_NOT_READY", "program_id": program_id}
    attempt_sha = hashlib.sha256(attempt_path.read_bytes()).hexdigest()
    attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
    if attempt.get("status") == mb.STATUS_PENDING_NO_ATTEMPT:
        detail = attempt.get("campaign_detail_state") or {}
        if (str(attempt.get("program_id") or "") != program_id
                or int(attempt.get("provider_application_attempts") or 0) != 0
                or int(attempt.get("provider_mutations") or 0) != 0
                or detail.get("classification") != "application_pending"
                or not detail.get("pending_markers_seen")):
            raise mb.MaxBountyError("maxbounty_handoff_pending_observation_contract_invalid")
        observed_at = str(attempt.get("observed_at_utc") or "")
        if not observed_at:
            raise mb.MaxBountyError("maxbounty_handoff_pending_observation_timestamp_missing")
        if action_path.is_file():
            action = json.loads(action_path.read_text(encoding="utf-8"))
            bound_attempt_path = Path(str(action.get("attempt_receipt") or ""))
            if not bound_attempt_path.is_absolute():
                bound_attempt_path = BUSINESS / bound_attempt_path
            if (action.get("status") != mb.STATUS_PENDING_NO_ATTEMPT
                    or str(action.get("program_id") or "") != program_id
                    or int(action.get("provider_application_attempts") or 0) != 0
                    or int(action.get("provider_mutations") or 0) != 0
                    or bound_attempt_path.resolve() != attempt_path
                    or str(action.get("attempt_receipt_sha256") or "") != attempt_sha):
                raise mb.MaxBountyError(
                    "maxbounty_handoff_pending_existing_action_contract_invalid")
            action_sha = hashlib.sha256(action_path.read_bytes()).hexdigest()
        else:
            action = {
                **attempt,
                "schema": APPLICATION_ACTION_SCHEMA,
                "attempt_receipt": repo_rel(attempt_path),
                "attempt_receipt_sha256": attempt_sha,
                "queue_id": str(manifest.get("queue_id") or ""),
                "operator_instruction_path": repo_rel(instruction_path),
                "operator_instruction_sha256": instruction_sha,
            }
            action_sha = atomic_json(action_path, action)
        queue_row = _action_queue_row(action, program, action_path, action_sha)
        outbox = _write_sheet_queue_outbox(run_dir, queue_row, action_path, action_sha)
        if Path(BUSINESS / outbox["path"]).resolve() != outbox_path:
            raise mb.MaxBountyError("maxbounty_handoff_outbox_materialized_path_mismatch")
        return {
            "status": "HANDOFF_MATERIALIZED",
            "program_id": program_id,
            "action_path": repo_rel(action_path),
            "action_sha256": action_sha,
            "queue_row": queue_row,
            "sheet_queue_outbox": outbox,
            "provider_application_attempts": 0,
            "provider_mutations": 0,
        }
    if attempt.get("status") != mb.STATUS_SUBMITTED:
        return {"status": "HANDOFF_NOT_VERIFIED", "program_id": program_id,
                "attempt_status": str(attempt.get("status") or "")}
    if (str(attempt.get("program_id") or "") != program_id
            or int(attempt.get("provider_application_attempts") or 0) != 1
            or int(attempt.get("gate_slot_consumed") or 0) != 1
            or int(attempt.get("provider_mutations") or 0) != 1):
        raise mb.MaxBountyError("maxbounty_handoff_verified_attempt_contract_invalid")
    submitted_at = str(attempt.get("submitted_at_utc")
                       or attempt.get("reconciled_at_utc") or "")
    if not submitted_at:
        raise mb.MaxBountyError("maxbounty_handoff_submitted_timestamp_missing")
    action = {
        "schema": APPLICATION_ACTION_SCHEMA,
        "status": mb.STATUS_SUBMITTED,
        "program_id": program_id,
        "program_name": str(program.get("name") or attempt.get("program_name") or ""),
        "submitted_at_utc": submitted_at,
        "answers": mb.application_draft(program),
        "ad_image_sha256": str(attempt.get("ad_image_sha256") or ""),
        "provider_mutations": 1,
        "provider_application_attempts": 1,
        "gate_slot_consumed": 1,
        "attempt_id": str(attempt.get("attempt_id") or ""),
        "attempt_receipt": repo_rel(attempt_path),
        "attempt_receipt_sha256": attempt_sha,
        "queue_id": str(manifest.get("queue_id") or ""),
        "operator_instruction_path": repo_rel(instruction_path),
        "operator_instruction_sha256": instruction_sha,
    }
    expected_action_sha = hashlib.sha256(
        (json.dumps(action, indent=2, sort_keys=True, default=str) + "\n").encode("utf-8")
    ).hexdigest()
    if action_path.is_file():
        action_sha = hashlib.sha256(action_path.read_bytes()).hexdigest()
        if action_sha != expected_action_sha:
            raise mb.MaxBountyError("maxbounty_handoff_action_existing_bytes_drift")
    else:
        action_sha = atomic_json(action_path, action)
    queue_row = _action_queue_row(action, program, action_path, action_sha)
    outbox = _write_sheet_queue_outbox(run_dir, queue_row, action_path, action_sha)
    if Path(BUSINESS / outbox["path"]).resolve() != outbox_path:
        raise mb.MaxBountyError("maxbounty_handoff_outbox_materialized_path_mismatch")
    return {
        "status": "HANDOFF_MATERIALIZED",
        "program_id": program_id,
        "action_path": repo_rel(action_path),
        "action_sha256": action_sha,
        "queue_row": queue_row,
        "sheet_queue_outbox": outbox,
    }


def reconcile_application_handoff(path_value: str | Path) -> dict[str, Any]:
    """Promote an ambiguous attempt only from a hash-bound provider GET readback."""
    path = Path(path_value)
    if not path.is_absolute():
        path = BUSINESS / path
    path = path.resolve()
    reconciliation = json.loads(path.read_text(encoding="utf-8"))
    if reconciliation.get("schema") != APPLICATION_RECONCILIATION_SCHEMA:
        raise mb.MaxBountyError("maxbounty_reconciliation_schema_invalid")
    program_id = str(reconciliation.get("program_id") or "")
    if path.name != f"provider-reconciliation-{program_id}.json":
        raise mb.MaxBountyError("maxbounty_reconciliation_identity_invalid")
    run_dir = path.parent

    handoff_path = _bound_handoff_path(
        reconciliation, "application_handoff_path", run_dir,
        f"application-handoff-{program_id}.json")
    handoff_sha = str(reconciliation.get("application_handoff_sha256") or "").lower()
    if (not re.fullmatch(r"[0-9a-f]{64}", handoff_sha)
            or hashlib.sha256(handoff_path.read_bytes()).hexdigest() != handoff_sha):
        raise mb.MaxBountyError("maxbounty_reconciliation_handoff_sha_invalid")
    handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
    if (handoff.get("schema") != APPLICATION_HANDOFF_SCHEMA
            or str(handoff.get("program_id") or "") != program_id):
        raise mb.MaxBountyError("maxbounty_reconciliation_handoff_contract_invalid")
    attempt_path = _bound_handoff_path(
        handoff, "provider_attempt_receipt_path", run_dir,
        f"provider-attempt-{program_id}.json")
    attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
    reconciliation_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    already = attempt.get("provider_readback_reconciliation") or {}
    if attempt.get("status") == mb.STATUS_SUBMITTED:
        if (already.get("manifest_sha256") == reconciliation_sha
                and already.get("manifest_path") == repo_rel(path)):
            return {"status": "RECONCILIATION_ALREADY_APPLIED", "program_id": program_id,
                    "attempt_path": repo_rel(attempt_path),
                    "attempt_sha256": hashlib.sha256(attempt_path.read_bytes()).hexdigest()}
        raise mb.MaxBountyError("maxbounty_reconciliation_attempt_already_terminal_different_proof")
    attempt_sha_before = str(reconciliation.get("attempt_sha256_before") or "").lower()
    if (not re.fullmatch(r"[0-9a-f]{64}", attempt_sha_before)
            or hashlib.sha256(attempt_path.read_bytes()).hexdigest() != attempt_sha_before):
        raise mb.MaxBountyError("maxbounty_reconciliation_attempt_sha_invalid")
    evidence_mode = str(reconciliation.get("evidence_mode") or "provider_get_readback")
    if evidence_mode not in {"provider_get_readback", "attempt_internal_success"}:
        raise mb.MaxBountyError("maxbounty_reconciliation_evidence_mode_invalid")
    if evidence_mode == "attempt_internal_success":
        provider_response = ((attempt.get("readback_evidence") or {}).get(
            "provider_response") or {})
    else:
        provider_response = attempt.get("provider_response") or {}
    if (attempt.get("schema") != "glp.maxbounty.provider-application-attempt.v1"
            or str(attempt.get("program_id") or "") != program_id
            or int(attempt.get("provider_application_attempts") or 0) != 1
            or int(attempt.get("gate_slot_consumed") or 0) != 1
            or provider_response.get("method") != "POST"
            or provider_response.get("path") != (
                f"/affiliates/api/campaign/{program_id}/requestApproval")
            or provider_response.get("request_seen") is not True):
        raise mb.MaxBountyError("maxbounty_reconciliation_attempt_contract_invalid")

    pre_submit_path = Path(str(attempt.get("pre_submit_receipt_path") or "")).resolve()
    if pre_submit_path.parent != run_dir.resolve():
        raise mb.MaxBountyError("maxbounty_reconciliation_pre_submit_path_invalid")
    pre_submit_sha = str(attempt.get("pre_submit_receipt_sha256") or "").lower()
    if (not re.fullmatch(r"[0-9a-f]{64}", pre_submit_sha)
            or hashlib.sha256(pre_submit_path.read_bytes()).hexdigest() != pre_submit_sha):
        raise mb.MaxBountyError("maxbounty_reconciliation_pre_submit_sha_invalid")
    pre_submit = json.loads(pre_submit_path.read_text(encoding="utf-8"))
    leads = str(((pre_submit.get("fields") or {}).get("leads_per_day") or {}).get("value") or "")
    image_sha = str(((pre_submit.get("fields") or {}).get("advertising_image") or {}).get(
        "sha256") or "").lower()
    asset = application_gate.approved_asset_record("maxbounty_advertising_image")
    if (pre_submit.get("schema") != "glp.maxbounty.application-pre-submit.v1"
            or pre_submit.get("status") != "PREPARED_VERIFIED_NOT_SUBMITTED"
            or str(pre_submit.get("program_id") or "") != program_id
            or (pre_submit.get("form_readback") or {}).get("verified") is not True
            or not leads.isdigit() or int(leads) < 25
            or (pre_submit.get("business_identity") or {}).get("legal_name") != "Tilton Group LLC"
            or not asset or image_sha != str(asset.get("sha256") or "").lower()
            or image_sha != str(attempt.get("ad_image_sha256") or "").lower()):
        raise mb.MaxBountyError("maxbounty_reconciliation_pre_submit_contract_invalid")

    reconciliation_evidence: dict[str, Any]
    if evidence_mode == "provider_get_readback":
        readback_path = Path(str(reconciliation.get("provider_readback_path") or ""))
        if readback_path.is_absolute() or ".." in readback_path.parts:
            raise mb.MaxBountyError("maxbounty_reconciliation_readback_path_invalid")
        readback_path = (BUSINESS / readback_path).resolve()
        try:
            readback_path.relative_to((BUSINESS / "reports").resolve())
        except ValueError as exc:
            raise mb.MaxBountyError("maxbounty_reconciliation_readback_outside_reports") from exc
        readback_sha = str(reconciliation.get("provider_readback_sha256") or "").lower()
        if (not re.fullmatch(r"[0-9a-f]{64}", readback_sha)
                or hashlib.sha256(readback_path.read_bytes()).hexdigest() != readback_sha):
            raise mb.MaxBountyError("maxbounty_reconciliation_readback_sha_invalid")
        readback = json.loads(readback_path.read_text(encoding="utf-8"))
        detail = readback.get("campaign_detail_before") or {}
        api = readback.get("campaign_api_safe_projection") or {}
        effects = readback.get("effects") or {}
        relationships = {
            str(item.get("path") or ""): str(item.get("value") or "")
            for item in api.get("relationship_fields") or [] if isinstance(item, dict)
        }
        pending = relationships.get("$.details.affiliate_campaign_status", "")
        if (readback.get("schema") != "glp.maxbounty.application-dry-run.v1"
                or readback.get("status") != "BLOCKED_NOT_APPLYABLE_NO_SUBMIT"
                or str(detail.get("program_id") or "") != program_id
                or detail.get("campaign_api_status") != 200
                or detail.get("campaign_api_method") != "GET"
                or detail.get("detail_url") != (
                    f"https://affiliates.maxbounty.com/campaign/{program_id}")
                or api.get("seen") is not True or api.get("http_status") != 200
                or not re.search(r"\b(?:pending|under review|submitted|received)\b", pending, re.I)
                or any(int(effects.get(key) or 0) != 0 for key in (
                    "provider_application_attempts", "provider_mutations", "sheet_writes",
                    "submit_control_clicks", "tracking_links_opened", "tracking_links_clicked"))):
            raise mb.MaxBountyError("maxbounty_reconciliation_readback_contract_invalid")
        reconciliation_evidence = {
            "evidence_mode": evidence_mode,
            "provider_readback_path": repo_rel(readback_path),
            "provider_readback_sha256": readback_sha,
            "campaign_api_body_sha256": str(api.get("body_sha256") or ""),
            "affiliate_campaign_status": pending[:500],
            "provider_request_replayed": False,
            "post_submit_modal_unavailable_due_to_pre_repair_consumer_ordering": True,
        }
    else:
        readback_evidence = attempt.get("readback_evidence") or {}
        canonical = attempt.get("canonical_readback_evidence") or {}
        modal = attempt.get("post_submit_modal_evidence") or {}
        relationship = canonical.get("campaign_api_relationship") or {}
        pending = str(relationship.get("affiliate_campaign_status") or "")
        dialog_texts = modal.get("dialog_texts") or []
        projection = provider_response.get("projection") or {}
        if (attempt.get("status") != "PROVIDER_ATTEMPT_AMBIGUOUS"
                or readback_evidence.get("verified") is not True
                or readback_evidence.get("verification_source") != "provider_post_response"
                or provider_response.get("response_seen") is not True
                or provider_response.get("http_status") != 200
                or projection.get("success") is not True
                or canonical.get("verified") is not True
                or canonical.get("campaign_api_status") != 200
                or canonical.get("campaign_api_method") != "GET"
                or canonical.get("approved_control_present") is not False
                or not re.search(r"\b(?:pending|under review|submitted|received)\b", pending, re.I)
                or not any("You have successfully submitted your request!" in str(text)
                           for text in dialog_texts)):
            raise mb.MaxBountyError("maxbounty_reconciliation_internal_success_invalid")
        reconciliation_evidence = {
            "evidence_mode": evidence_mode,
            "provider_response_body_sha256": str(provider_response.get("body_sha256") or ""),
            "campaign_api_body_sha256": str(canonical.get("campaign_api_body_sha256") or ""),
            "affiliate_campaign_status": pending[:500],
            "provider_success_message": str(projection.get("message") or "")[:500],
            "provider_request_replayed": False,
            "post_submit_modal_phrase_verified": True,
        }

    reconciled_at = utc_now()
    submitted_at = str(attempt.get("attempted_at_utc")
                       or provider_response.get("request_observed_at_utc") or reconciled_at)
    updated = {
        **attempt,
        "status": mb.STATUS_SUBMITTED,
        "submitted_at_utc": submitted_at,
        "reconciled_at_utc": reconciled_at,
        "provider_mutations": 1,
        "relationship_after": "application_pending",
        "readback": (
            "Provider POST response, success modal, and campaign API prove the request is under "
            "review. Any still-rendered apply control is non-authoritative."
            if evidence_mode == "attempt_internal_success" else
            "Provider campaign API reports request under review; any still-rendered apply "
            "control is non-authoritative."
        ),
        "journal_events": list(attempt.get("journal_events") or []) + [{
            "state": ("SUBMITTED_VERIFIED_FROM_INTERNAL_SUCCESS_EVIDENCE"
                      if evidence_mode == "attempt_internal_success" else
                      "SUBMITTED_VERIFIED_FROM_READ_ONLY_RECONCILIATION"),
            "at_utc": reconciled_at,
        }],
        "provider_readback_reconciliation": {
            "manifest_path": repo_rel(path),
            "manifest_sha256": reconciliation_sha,
            "attempt_sha256_before": attempt_sha_before,
            **reconciliation_evidence,
        },
    }
    attempt_sha = atomic_json(attempt_path, updated)
    return {"status": "RECONCILIATION_APPLIED", "program_id": program_id,
            "attempt_path": repo_rel(attempt_path), "attempt_sha256": attempt_sha,
            "provider_request_replayed": False}


def execute_mutation_plan(plan: dict[str, Any], evaluation: dict[str, Any],
                          programs: list[dict[str, Any]], login_url: str,
                          run_dir: Path, *, headless: bool = True,
                          session_factory: Callable | None = None) -> dict[str, Any]:
    """Execute eligible MaxBounty actions only after re-checking the hard gate.

    A switch is never performed here. The challenger link is stored link-dark;
    structural validation must pass before the atomic rollout machine can replace
    the incumbent. Any validation failure is an explicit KEEP_INCUMBENT outcome.
    """
    if not plan["gate"]["open"]:
        return {"status": "GATE_CLOSED", "actions": plan["actions"], "queue_rows": [],
                "effects": {"applications_submitted": 0, "tracking_links_minted": 0,
                            "provider_deploys": 0}}
    mb.require_mutation_gate()
    if not plan["actions"]:
        return {"status": "PASS_NO_ELIGIBLE_ACTIONS", "actions": [], "queue_rows": [],
                "effects": {"applications_submitted": 0, "tracking_links_minted": 0,
                            "provider_deploys": 0}}

    decisions = {
        (str((row.get("challenger") or {}).get("program_id")), row.get("decision")): row
        for row in evaluation.get("decisions") or []
    }
    by_id = {str(row["program_id"]): row for row in programs}
    if (session_factory is None or getattr(session_factory, 'fresh', False) is not True):
        raise mb.MaxBountyError('maxbounty_fresh_operation_executor_required')
    from marketplace_adapters import portal_lane  # schema only; no default session
    spec = dataclasses.replace(portal_lane.PORTALS["maxbounty"], login_url=login_url)
    outcomes: list[dict[str, Any]] = []
    queue_rows: list[dict[str, Any]] = []
    effects = {"applications_submitted": 0, "tracking_links_minted": 0,
               "provider_deploys": 0}
    with session_factory(spec, run_id=f"maxbounty-actions-{stamp()}", headless=headless) as session:
        login = session.login()
        if login.get("outcome") not in {"logged_in", "session_reused", "logged_in_via_sso"}:
            raise mb.MaxBountyError(f"maxbounty_action_login_blocked:{login.get('outcome')}")
        for action in plan["actions"]:
            program = by_id[str(action["program_id"])]
            if action["action"] == "submit_application":
                result = mb.submit_application(
                    session.driver, program,
                    attempt_receipt_path=run_dir / f"provider-attempt-{program['program_id']}.json")
                effects["applications_submitted"] += 1
                action_path = run_dir / f"application-{program['program_id']}.json"
                action_sha = atomic_json(action_path, result)
                queue_rows.append(_action_queue_row(result, program, action_path, action_sha))
                outcomes.append({"action": action["action"], "program_id": program["program_id"],
                                 "status": result["status"], "receipt": repo_rel(action_path),
                                 "sha256": action_sha})
                continue

            decision = decisions[(str(program["program_id"]),
                                  "SWITCH_ATOMIC_AFTER_LINK_VALIDATION")]
            incumbent = decision.get("incumbent") or {}
            page_slug = str(incumbent.get("page_slug") or incumbent.get("suggested_page") or "")
            configuration = mb.tracking_configuration(
                program, page_slug=page_slug,
                landing_page=highest_intent_landing_page(program, page_slug),
                creative=mb.CREATIVE_ANSWER)
            result = mb.mint_tracking_link(session.driver, program, configuration)
            effects["tracking_links_minted"] += 1
            action_path = run_dir / f"tracking-link-{program['program_id']}.json"
            action_sha = atomic_json(action_path, result)
            queue_rows.append(_action_queue_row(result, program, action_path, action_sha))
            validation = "PASS"
            try:
                from glp_affiliate_link_rails import validate_tracking_capture  # noqa: PLC0415
                validate_tracking_capture(result)
            except Exception as exc:  # fail closed; the incumbent is deliberately untouched
                validation = f"BLOCKED_KEEP_INCUMBENT:{type(exc).__name__}:{str(exc)[:300]}"
            outcomes.append({
                "action": action["action"], "program_id": program["program_id"],
                "status": result["status"], "validation": validation,
                "switch_executed": False, "incumbent_preserved": True,
                "receipt": repo_rel(action_path), "sha256": action_sha,
            })
    return {"status": "PASS", "actions": outcomes, "queue_rows": queue_rows,
            "effects": effects}


def execute_queued_application(
    program_id: str,
    *,
    queue_id: str,
    instructions: str,
    run_dir: Path,
    submission_packet: dict[str, Any] | None = None,
    headless: bool = True,
    session_factory: Callable | None = None,
    enumerate_fn: Callable | None = None,
) -> dict[str, Any]:
    """Submit exactly one operator-queued MaxBounty campaign, or refuse.

    This is the only application entry point for MaxBounty. Enumeration and
    payout comparison remain read-only. The hard image/site gate is re-checked
    here, after the queue identity and exact campaign id are known, so a queued
    row cannot bypass the operator's current display-image hold.
    """
    program_id = str(program_id or "").strip()
    instructions = " ".join(str(instructions or "").split())
    if not queue_id or not program_id or not instructions:
        raise mb.MaxBountyError("maxbounty_queue_identity_or_instructions_missing")
    gate = application_gate.gate_state()
    gate_block = application_gate.blocking_reason("maxbounty")
    if gate_block:
        return {
            "status": "GATE_CLOSED",
            "queue_id": queue_id,
            "program_id": program_id,
            "gate": gate,
            "blocker": gate_block,
            "queue_rows": [],
            "effects": {"applications_submitted": 0, "tracking_links_minted": 0,
                        "provider_deploys": 0, "provider_application_attempts": 0},
        }
    allowed_queue_ids = set((gate.get("scope") or {}).get("queue_ids_allowed") or [])
    if allowed_queue_ids and queue_id not in allowed_queue_ids:
        return {
            "status": "GATE_CLOSED",
            "queue_id": queue_id,
            "program_id": program_id,
            "gate": gate,
            "blocker": f"queue_id_outside_operator_scope:{queue_id}",
            "queue_rows": [],
            "effects": {"applications_submitted": 0, "tracking_links_minted": 0,
                        "provider_deploys": 0, "provider_application_attempts": 0},
        }
    asset = application_gate.approved_asset_record("maxbounty_advertising_image")
    if not asset:
        raise mb.GateClosed("maxbounty_hash_verified_application_asset_missing")
    packet = submission_packet or {}
    staged = packet.get("program_snapshot") if isinstance(packet, dict) else None
    if not isinstance(staged, dict):
        raise mb.MaxBountyError("maxbounty_staged_program_snapshot_missing")
    if str(staged.get("program_id") or "") != program_id:
        raise mb.MaxBountyError("maxbounty_staged_program_identity_mismatch")
    if staged.get("relationship") != mb.REL_APPLY_NEEDED:
        raise mb.MaxBountyError(
            f"maxbounty_staged_program_not_applyable:{staged.get('relationship')}")
    if staged.get("fit_classification") != "core_glp1":
        raise mb.MaxBountyError(
            f"maxbounty_staged_program_not_core_glp1:{staged.get('fit_classification')}")
    if not str(staged.get("application_url") or "").startswith("https://"):
        raise mb.MaxBountyError("maxbounty_staged_application_url_missing")
    snapshot = read_sheet_snapshot()
    # The authenticated detail page is still verified by submit_application,
    # but catalogue discovery is forbidden here: every field and exact campaign
    # identity comes from the queue packet staged while the global gate was shut.
    program = dict(staged)

    instruction_path = run_dir / f"queue-instructions-{program_id}.json"
    instruction_sha = atomic_json(instruction_path, {
        "schema": "glp.maxbounty.operator-queued-application.v1",
        "queue_id": queue_id,
        "program_id": program_id,
        "instructions": instructions,
        "captured_at_utc": utc_now(),
        "provider_mutation": False,
    })
    attempt_path = run_dir / f"provider-attempt-{program_id}.json"
    handoff_path = _prepare_application_handoff(
        run_dir, queue_id=queue_id, program=program,
        instruction_path=instruction_path, instruction_sha=instruction_sha)
    if (session_factory is None or getattr(session_factory, 'fresh', False) is not True):
        raise mb.MaxBountyError('maxbounty_fresh_operation_executor_required')
    from marketplace_adapters import portal_lane  # schema only; no default session
    spec = dataclasses.replace(portal_lane.PORTALS["maxbounty"],
                               login_url=snapshot["login_url"])
    with session_factory(spec, run_id=f"maxbounty-queued-{queue_id}",
                         headless=headless) as session:
        login = mb.login_with_dom_settle(session)
        if login.get("outcome") not in {
                "logged_in", "session_reused", "logged_in_via_sso",
                "session_reused_after_dom_settle"}:
            raise mb.MaxBountyError(
                f"maxbounty_queued_login_blocked:{login.get('outcome')}")
        try:
            result = mb.submit_application(
                session.driver,
                program,
                approved_image=Path(str(asset["absolute_path"])),
                pre_submit_check=lambda: application_gate.assert_apply_allowed("maxbounty"),
                attempt_receipt_path=attempt_path,
            )
        except Exception as exc:
            attempt = {}
            if attempt_path.is_file():
                try:
                    attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    attempt = {}
            provider_attempts = int(attempt.get("provider_application_attempts") or 0)
            gate_slot_consumed = int(attempt.get("gate_slot_consumed") or 0)
            if provider_attempts or gate_slot_consumed:
                return {
                    "status": ("PROVIDER_ATTEMPT_AMBIGUOUS" if provider_attempts
                               else "PROVIDER_ATTEMPT_RESERVED_RECONCILIATION_REQUIRED"),
                    "queue_id": queue_id,
                    "program_id": program_id,
                    "blocker": f"{type(exc).__name__}:{str(exc)[:500]}",
                    "attempt_receipt": repo_rel(attempt_path),
                    "attempt_receipt_sha256": hashlib.sha256(
                        attempt_path.read_bytes()).hexdigest(),
                    "queue_rows": [],
                    "effects": {"applications_submitted": 0, "tracking_links_minted": 0,
                                "provider_deploys": 0,
                                "provider_application_attempts": provider_attempts,
                                "gate_slots_consumed": gate_slot_consumed},
                }
            raise
    submitted = result["status"] == mb.STATUS_SUBMITTED
    provider_pending = result["status"] == mb.STATUS_PENDING_NO_ATTEMPT
    provider_attempts = int(result.get("provider_application_attempts") or 0)
    if submitted:
        handoff = materialize_application_handoff(handoff_path)
        if handoff.get("status") != "HANDOFF_MATERIALIZED":
            raise mb.MaxBountyError(
                f"maxbounty_verified_handoff_not_materialized:{handoff.get('status')}")
        action_path = BUSINESS / str(handoff["action_path"])
        action_sha = str(handoff["action_sha256"])
        queue_rows = [handoff["queue_row"]]
        outbox = handoff["sheet_queue_outbox"]
    else:
        action_path = run_dir / f"application-{program_id}.json"
        action_sha = atomic_json(action_path, {
            **result,
            "schema": APPLICATION_ACTION_SCHEMA,
            "queue_id": queue_id,
            "operator_instruction_path": repo_rel(instruction_path),
            "operator_instruction_sha256": instruction_sha,
        })
        if provider_pending:
            queue_row = _action_queue_row(result, program, action_path, action_sha)
            queue_rows = [queue_row]
            outbox = _write_sheet_queue_outbox(
                run_dir, queue_row, action_path, action_sha)
        else:
            queue_rows = []
            outbox = None
    return {
        "status": result["status"],
        "queue_id": queue_id,
        "program_id": program_id,
        "receipt": repo_rel(action_path),
        "sha256": action_sha,
        "queue_rows": queue_rows,
        "sheet_queue_outbox": outbox,
        "blocker": "" if submitted or provider_pending else result["status"],
        "effects": {"applications_submitted": 1 if submitted else 0,
                    "tracking_links_minted": 0,
                    "provider_deploys": 0,
                    "provider_application_attempts": provider_attempts},
    }


def fnv1a64(raw: str) -> str:
    value = 0xCBF29CE484222325
    for byte in raw.encode("utf-8"):
        value ^= byte
        value = (value * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return f"{value:016x}"


def error_signature(error_text: str) -> str:
    head = " ".join(str(error_text).split())[:200].casefold()
    head = re.sub(r"\b\d{4}-\d{2}-\d{2}[t ]\d{2}:\d{2}:\d{2}\S*", "<ts>", head)
    head = re.sub(r"\b[0-9a-f]{8,}\b", "<hex>", head)
    head = re.sub(r"\b\d{3,}\b", "<n>", head)
    return fnv1a64(f"{WORKFLOW_ID}|maxbounty_lane|{head}")


def route_error(error_text: str, receipt: str, dt: Any = None) -> dict[str, Any]:
    if dt is None:
        import n8n_dt as dt  # noqa: PLC0415
    signature = error_signature(error_text)
    existing = dt.rows(ERROR_CASES_TABLE, dt.eq("signature", signature), limit=10)
    open_existing = [row for row in existing
                     if str(row.get("status") or "").casefold() not in {"resolved", "closed"}]
    if open_existing:
        return {"routed": True, "deduped": True, "signature": signature,
                "case_id": open_existing[0].get("case_id")}
    now = utc_now()
    case_id = f"ec-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-maxbounty"
    row = {
        "case_id": case_id, "signature": signature, "source": MACHINE,
        "workflow_id": WORKFLOW_ID, "execution_id": "", "error_text": error_text[:1800],
        "status": "new", "attempts_today": 0, "first_seen_utc": now,
        "last_update_utc": now, "receipt": receipt[:400],
    }
    dt.insert(ERROR_CASES_TABLE, row)
    verified = bool(dt.rows(ERROR_CASES_TABLE, dt.eq("case_id", case_id), limit=5))
    if not verified:
        raise RuntimeError("maxbounty_error_case_readback_missing")
    return {"routed": True, "deduped": False, "verified": True,
            "signature": signature, "case_id": case_id}


def heartbeat(output_count: int, receipt: str, note: str, dt: Any = None) -> dict[str, Any]:
    if dt is None:
        import n8n_dt as dt  # noqa: PLC0415
    dt.insert(HEARTBEATS_TABLE, {"machine": MACHINE, "last_success_utc": utc_now(),
              "output_count": int(output_count), "receipt": receipt, "note": note[:400]})
    existing = dt.rows(REGISTRY_TABLE, dt.eq("machine", MACHINE), limit=5)
    if not existing:
        dt.insert(REGISTRY_TABLE, {
            "machine": MACHINE, "cadence_hours": 24, "grace_hours": 8,
            "active_days": "all", "owner": "glp/marketplace-enumeration",
            "config": json.dumps({"lane": "MaxBounty authenticated UI + payout policy",
                                  "bridge_action": "maxbounty_lane",
                                  "default_gate": "closed"}, sort_keys=True),
        })
    return {"heartbeat": "inserted", "registry": "present" if existing else "created"}


def coerce_flag(value: Any) -> bool:
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}


def compact(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: payload.get(key) for key in (
        "schema", "status", "captured_at_utc", "receipt", "enumeration", "coupon_model",
        "application_drafts", "payout_evaluation", "mutation_plan", "mutation_execution", "sheet_queue",
        "heartbeat", "error_handler", "effects") if key in payload}


def previous_approval_baseline(snapshot: dict[str, Any], snapshot_path: Path) -> dict[str, Any]:
    """Continuity from all bounded local captures plus fresh approved Sheet rows.

    No payout/vertical filter: unknown ID blocks instead of dropping a row. Never
    inspect a tracking URL for discovery; only exact portal detail hrefs and
    explicit numeric campaign identifiers in Notes can supply the identity.
    """
    ids = set()
    for row_number, cells in snapshot_rows(snapshot):
        if network_key(cells[4]) != "maxbounty" or norm(cells[2]) in PARENT_NAMES:
            continue
        if relationship_from_status(cells[10], cells[11]) != "approved":
            continue
        candidates = set(re.findall(r"\b(?:campaign|program|offer)\s+([0-9]+)\b", str(cells[23]), re.I))
        pid = mb.discovery.campaign_id(str(cells[5]))
        if pid:
            candidates.add(pid)
        if len(candidates) != 1:
            raise RuntimeError(f"maxbounty_sheet_approved_identity_missing_or_ambiguous:{row_number}")
        ids.update(candidates)
    return mb.discovery.approval_baseline(
        RUN_ROOT.glob("run-*/bookmarked-enumeration.json"), sorted(ids),
        {"source": str(snapshot_path), "sha256": mb.sha256_file(snapshot_path),
         "captured_at_utc": snapshot["captured_at_utc"]})


def main(argv: list[str] | None = None, *, fresh_driver=None, discovery_options=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("scheduled", "enumerate", "read-only", "draft", "application-dry-run"),
        default="scheduled",
    )
    parser.add_argument("--queue", choices=("on", "off"), default="on")
    parser.add_argument("--headless", default="1")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--output", default="")
    args = parser.parse_args(argv)
    if (args.mode != 'draft' and (fresh_driver is None
            or getattr(fresh_driver, 'fresh', False) is not True
            or not getattr(fresh_driver, 'owned_session', ''))):
        print(json.dumps({'schema': SCHEMA, 'status': 'BLOCKED',
            'reason': 'fresh_ai_browser_vault_stage_required',
            'enumeration': {'programs': None, 'discovered_links': None, 'completeness': 'UNKNOWN'},
            'effects': {'applications_submitted': 0, 'tracking_links_minted': 0,
                        'provider_deploys': 0, 'sheet_direct_writes': 0, 'tracking_clicks': 0}}))
        return 2
    if args.mode == "application-dry-run":
        if args.queue != "on" or args.output or not coerce_flag(args.headless):
            raise SystemExit(
                "maxbounty_application_dry_run_requires_queue_on_headless_and_no_output_override")
        raise mb.MaxBountyError('maxbounty_fresh_operation_executor_required')
    if args.mode == 'read-only' and args.queue != 'off':
        raise SystemExit('maxbounty_read_only_requires_queue_off')
    if args.mode == "scheduled" and args.queue != "on":
        raise SystemExit("maxbounty_scheduled_sheet_queue_may_not_be_disabled")

    run_dir = RUN_ROOT / f"run-{args.run_id or stamp()}"
    run_dir.mkdir(parents=True, exist_ok=True)
    output = Path(args.output) if args.output else run_dir / "run-receipt.json"
    captured_at = utc_now()
    payload: dict[str, Any] = {
        "schema": SCHEMA, "status": "RUNNING", "captured_at_utc": captured_at,
        "mode": args.mode,
        "effects": {"applications_submitted": 0, "tracking_links_minted": 0,
                    "provider_deploys": 0, "sheet_direct_writes": 0,
                    "tracking_clicks": 0},
    }
    try:
        snapshot = read_sheet_snapshot()
        if args.mode == "draft":
            enumeration = {"status": "NOT_RUN_DRAFT_MODE", "programs": [],
                           "enumerated": 0, "discovered_links": 0}
        else:
            snapshot["captured_at_utc"] = utc_now()
            snapshot_path = run_dir / "approval-baseline-sheet.json"
            atomic_json(snapshot_path, snapshot)
            baseline = previous_approval_baseline(snapshot, snapshot_path)
            enumeration = mb.login_and_enumerate(
                snapshot["login_url"], run_id=args.run_id or stamp(),
                headless=coerce_flag(args.headless), fresh_driver=fresh_driver,
                evidence_dir=run_dir / "search-category-pages", **baseline,
                **(discovery_options or {}))
        # Preserve raw coverage before identity enrichment or downstream effects.
        programs = list(enumeration.get("programs") or [])
        enumeration = dict(enumeration)
        enumeration["programs"] = programs
        capture_path = run_dir / "bookmarked-enumeration.json"
        capture_sha = atomic_json(capture_path, enumeration)
        payload["enumeration"] = {
            "status": enumeration.get("status"),
            "programs": enumeration.get("enumerated"),
            "discovered_links": enumeration.get("discovered_links"),
            "approved": sum(row.get("relationship") == mb.REL_APPROVED for row in programs),
            "apply_needed": sum(row.get("relationship") == mb.REL_APPLY_NEEDED for row in programs),
            "pending": sum(row.get("relationship") == mb.REL_PENDING for row in programs),
            "denied": sum(row.get("relationship") == mb.REL_DENIED for row in programs),
            "capacity_blocked": sum(
                row.get("relationship") == mb.REL_CAPACITY_BLOCKED for row in programs),
            "core_glp1": sum(row.get("fit_classification") == "core_glp1" for row in programs),
            "sister_vertical": sum(row.get("fit_classification") == "sister_vertical" for row in programs),
            "coupons_found": sum(len(row.get("coupons") or []) for row in programs),
            "capture_path": repo_rel(capture_path), "capture_sha256": capture_sha,
            "blockers": enumeration.get("blockers") or [],
            "retired_404": (enumeration.get("coverage") or {}).get("retired_404", []),
        }
        if enumeration.get('complete') is not True:
            for key in ('approved', 'apply_needed', 'pending', 'denied', 'capacity_blocked',
                        'core_glp1', 'sister_vertical', 'coupons_found'):
                payload['enumeration'][key] = None
        if args.mode != "draft" and enumeration.get("status") not in {"PASS", "COMPLETE"}:
            raise RuntimeError(
                f"maxbounty_enumeration_not_complete:{enumeration.get('status')}:"
                f"{enumeration.get('enumerated')}/{enumeration.get('discovered_links')}")

        if args.mode == 'read-only':
            mb.discovery.require_search_coverage(enumeration)
            payload['status'] = 'PASS_READ_ONLY'
            payload['read_method'] = enumeration.get('read_method')
            payload['coverage'] = enumeration.get('coverage')
            payload['receipt'] = repo_rel(output)
            atomic_json(output, payload)
            print(json.dumps(compact(payload), sort_keys=True))
            return 0
        if args.mode != "draft":
            mb.discovery.require_search_coverage(enumeration)
        programs = bind_required_site_identities(snapshot, programs)
        enumeration["programs"] = programs
        capture_sha = atomic_json(capture_path, enumeration)
        payload["enumeration"]["capture_sha256"] = capture_sha

        payload["coupon_model"] = update_coupon_model(
            programs, capture_path, capture_sha, captured_at)
        payload["application_drafts"] = draft_applications(programs, run_dir)

        portfolio = merged_portfolio(snapshot, programs)
        policy = mb.load_policy()
        payout_cfg = policy.get("payout_policy") or {}
        evaluation = evaluate_portfolio(
            portfolio, load_performance(),
            min_settled=int(payout_cfg.get("measured_min_settled_conversions") or 20),
            switch_ratio=str(payout_cfg.get("switch_advantage_ratio") or "1.10"))
        evaluation["evaluated_at_utc"] = captured_at
        evaluation["records_evaluated"] = len(portfolio)
        evaluation_path = run_dir / "payout-evaluation.json"
        evaluation_sha = atomic_json(evaluation_path, evaluation)
        payload["payout_evaluation"] = {
            "records": len(portfolio), "provider_groups": evaluation["provider_groups"],
            "decisions": len(evaluation["decisions"]),
            "identity_violations": len(evaluation["identity_violations"]),
            "path": repo_rel(evaluation_path), "sha256": evaluation_sha,
        }
        payload["mutation_plan"] = mutation_plan(evaluation, programs)

        payload["mutation_execution"] = {
            "status": "QUEUE_ONLY_NO_OPERATOR_ITEM", "queue_rows": [],
            "effects": {"applications_submitted": 0, "tracking_links_minted": 0,
                        "provider_deploys": 0},
        }
        for key in ("applications_submitted", "tracking_links_minted", "provider_deploys"):
            payload["effects"][key] = int(
                (payload["mutation_execution"].get("effects") or {}).get(key) or 0)

        queue_rows = [inventory_queue_row(row, capture_path, capture_sha, captured_at)
                      for row in programs]
        queue_rows += payout_queue_rows(
            evaluation, evaluation_path, evaluation_sha, captured_at, portfolio)
        queue_rows += payload["mutation_execution"].get("queue_rows") or []
        if args.queue == "on":
            payload["sheet_queue"] = {"rows": len(queue_rows), **queue_all(queue_rows)}
        else:
            queue_path = run_dir / "sheet-queue-preview.json"
            queue_sha = atomic_json(queue_path, {"rows": queue_rows})
            payload["sheet_queue"] = {"rows": len(queue_rows), "queued": 0,
                                      "mode": "read_only_preview",
                                      "path": repo_rel(queue_path), "sha256": queue_sha}
        payload["status"] = "PASS"
        payload["receipt"] = repo_rel(output)
        if args.mode == "scheduled":
            payload["heartbeat"] = heartbeat(
                len(programs), payload["receipt"],
                f"{len(programs)} bookmarked programs; {payload['enumeration']['approved']} "
                f"approved; {payload['enumeration']['apply_needed']} apply-needed; "
                f"{len(queue_rows)} Sheet facts queued")
        atomic_json(output, payload)
        print(json.dumps(compact(payload), sort_keys=True))
        return 0
    except Exception as exc:  # one machine-readable failure, secrets never included
        payload["status"] = "FAIL"
        payload["error"] = f"{type(exc).__name__}:maxbounty_reader_or_consumer_failed"
        payload["receipt"] = repo_rel(output)
        atomic_json(output, payload)
        if args.mode == "scheduled":
            try:
                payload["error_handler"] = route_error(payload["error"], payload["receipt"])
                atomic_json(output, payload)
            except Exception as route_exc:
                payload["error_handler"] = {
                    "routed": False,
                    "error": f"{type(route_exc).__name__}:maxbounty_error_route_failed",
                }
                atomic_json(output, payload)
        print(json.dumps(compact(payload) | {"error": payload["error"]}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
