#!/usr/bin/env python3
"""Render one ranked Monday affiliate-discovery card; never mutate anything."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


def load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def rank_direct(row: dict[str, Any]) -> tuple[int, int, int, str]:
    tier = str((row.get("glp_evidence") or {}).get("tier") or "")
    tier_score = {"explicit_glp1": 4, "clinical_weight_loss": 3,
                  "compounding_pharmacy": 3, "telehealth_clinical": 2}.get(tier, 0)
    terms = 1 if str(row.get("commission_terms") or "").strip() else 0
    signup = 1 if row.get("has_signup_path") else 0
    return (-tier_score, -terms, -signup, str(row.get("provider") or "").casefold())


def direct_candidate_is_reviewable(row: dict[str, Any]) -> bool:
    """A search snippet is not a program; require a real signup/program surface."""
    if not row.get("has_signup_path"):
        return False
    url = str(row.get("program_url") or "")
    host = (urlparse(url).hostname or "").lower()
    if not host or host.endswith((".com.ar", ".ru", ".cn", ".in", ".pk")):
        return False
    provider = str(row.get("provider") or "").strip()
    return len(provider) >= 3 and provider.casefold() not in {"unknown", "affiliate", "program"}


def ranked_candidates(discovery_dir: Path) -> list[dict[str, Any]]:
    direct = load(discovery_dir / "direct-programs.json")
    markets = load(discovery_dir / "marketplaces.json")
    rows: list[dict[str, Any]] = []
    for row in sorted(direct.get("accepted") or [], key=rank_direct):
        if not direct_candidate_is_reviewable(row):
            continue
        rows.append({
            "kind": "direct_program",
            "name": str(row.get("provider") or row.get("name") or "Unknown"),
            "network": str(row.get("network") or "direct"),
            "payout_terms": str(row.get("commission_terms") or "not published"),
            "fit": str((row.get("glp_evidence") or {}).get("tier") or "unverified"),
            "application_url": str(row.get("program_url") or ""),
            "evidence_source": str(row.get("evidence_source") or ""),
        })
    for row in sorted(markets.get("candidates") or [],
                      key=lambda item: (-int(item.get("glp_signals") or 0),
                                        str(item.get("network") or ""))):
        rows.append({
            "kind": "new_marketplace",
            "name": str(row.get("display") or row.get("name") or row.get("network") or "Unknown"),
            "network": str(row.get("network") or ""),
            "payout_terms": str(row.get("commission_terms") or "network terms require review"),
            "fit": f"{int(row.get('glp_signals') or 0)} observed GLP signal(s)",
            "application_url": str(row.get("signup_url") or ""),
            "evidence_source": "Firecrawl marketplace search",
        })
    return rows


def marketplace_scan_resolution(discovery_dir: Path,
                                marketplace_receipt: dict[str, Any]) -> dict[str, Any]:
    coverage = dict(marketplace_receipt.get("coverage") or {})
    missing = [str(value) for value in (coverage.get("missing") or [])]
    five_routes = load(discovery_dir / "five-route-receipts.json")
    platform_route = (five_routes.get("routes") or {}).get("platform_sweeps") or {}
    receipts = {str(row.get("platform") or ""): row
                for row in (platform_route.get("subroutes") or [])}
    fallback = []
    unattempted = []
    for network in missing:
        row = receipts.get(network)
        if not row or not row.get("scan_attempted"):
            unattempted.append(network)
            continue
        fallback.append({
            "network": network,
            "status": str(row.get("status") or ""),
            "reason": str(row.get("reason") or ""),
            "hits_emitted": int(row.get("hits_emitted") or 0),
            "public_fallback": bool(row.get("public_fallback")),
        })
    expected = int(coverage.get("expected") or 0)
    completed = int(coverage.get("completed") or 0)
    return {
        "expected": expected,
        "authenticated_completed": completed,
        "attempted": min(expected, completed + len(fallback)),
        "fallback_scans": fallback,
        "unattempted": unattempted,
    }


def compose(discovery_dir: Path, marketplace_receipt: dict[str, Any]) -> dict[str, Any]:
    summary = load(discovery_dir / "summary.json")
    candidates = ranked_candidates(discovery_dir)
    coverage = marketplace_receipt.get("coverage") or {}
    resolution = marketplace_scan_resolution(discovery_dir, marketplace_receipt)
    execution_id = str(marketplace_receipt.get("execution_id") or "not recorded")
    lines = [
        f"🔎 **GLP Affiliate Monday Discovery** — {len(candidates)} candidate(s) for review",
        "",
        (f"joined marketplace scans attempted: **{resolution['attempted']}/{resolution['expected']}** "
         f"· marketplace execution: **{execution_id}** · direct web: **Firecrawl**"),
    ]
    source_coverage = summary.get("source_coverage") or {}
    lines.append(f"Discovery: **{summary.get('status', 'UNKNOWN')}** · "
                 f"pending reads: **{len(source_coverage.get('pending') or [])}** · "
                 f"unavailable reads: **{len(source_coverage.get('unavailable') or [])}**")
    lines.append("Marketplace coverage uses prior local receipts; it is not a fresh catalog scan.")
    if resolution["fallback_scans"]:
        names = ", ".join(
            f"{row['network']} ({'public fallback' if row['public_fallback'] else 'bounded attempt'}; "
            f"{row['hits_emitted']} hit(s))" for row in resolution["fallback_scans"])
        lines.append(
            f"authenticated catalogs completed: **{resolution['authenticated_completed']}/{resolution['expected']}** "
            f"· constrained scans: **{names}**")
    if resolution["unattempted"]:
        lines.append(f"unattempted coverage: **{', '.join(resolution['unattempted'])}**")
    for position, row in enumerate(candidates[:10], start=1):
        lines.append(
            f"**{position}. {row['name']}** · {row['network']} · {row['payout_terms']} · fit: {row['fit']}")
        if row["application_url"]:
            lines.append(row["application_url"])
    if not candidates:
        lines.append("No new candidates in the completed source reads; check coverage above.")
    admission = summary.get("application_admission") or {}
    outcomes = admission.get("outcomes") or []
    counts = {status: sum(row.get("status") == status for row in outcomes)
              for status in ("admitted", "already_admitted", "already_queued", "deferred", "reconcile_required")}
    lines.insert(5, f"Queue: **{counts['admitted']} admitted** · "
                 f"**{counts['already_admitted'] + counts['already_queued']} already queued** · "
                 f"**{counts['deferred']} deferred** · **{counts['reconcile_required']} need reconciliation**")
    lines.extend(["", f"Application queue admission: {admission.get('status', 'disabled')}. "
                  "No applications submitted, marketplaces joined, or site changes made here."])
    card = "\n".join(lines)
    if len(card) > 1900:
        card = card[:1888].rstrip() + "\n…"
    return {
        "schema": "affiliate-monday-discovery-card-v1",
        "captured_at_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": summary.get("status", "UNKNOWN"),
        "candidate_count": len(candidates),
        "ranked_candidates": candidates,
        "marketplace_execution": marketplace_receipt,
        "marketplace_scan_resolution": resolution,
        "direct_discovery_receipt": str(discovery_dir / "summary.json"),
        "direct_queries_or_hits": int(summary.get("direct_hits") or 0),
        "card": card,
        "effects": {"applications_submitted": 0, "marketplaces_joined": 0,
                    "affiliate_deploys": 0, "sheet_writes": 0,
                    "tracking_links_repointed": 0},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--discovery-dir", required=True)
    parser.add_argument("--marketplace-receipt", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    payload = compose(Path(args.discovery_dir), load(Path(args.marketplace_receipt)))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.chmod(output, 0o600)
    print(payload["card"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
