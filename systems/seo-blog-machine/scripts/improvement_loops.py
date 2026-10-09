#!/usr/bin/env python3
"""Deterministic, proposal-only SEO improvement loops A-E.

The output is evidence and queue data only. Every content patch must re-enter the
same brief gate, per-article manifest, and O-2 weekly approval-wave chain.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, "/home/ubuntu/business/systems/analytics-brain")
from decision_policy import (  # noqa: E402
    consumer_policy, require_allowed, write_consumer_decision,
)

try:
    from phase23 import canonical, digest, normalized, read_json, write_json
except ModuleNotFoundError:  # package import during pytest
    from scripts.phase23 import canonical, digest, normalized, read_json, write_json

try:
    from analytics_feedback import (
        canonical_feedback_route,
        load_feedback,
        load_route_aliases,
        loop_b_priority_score,
    )
    from machine_config import load_config
except ModuleNotFoundError:  # package import during pytest
    from scripts.analytics_feedback import (
        canonical_feedback_route,
        load_feedback,
        load_route_aliases,
        loop_b_priority_score,
    )
    from scripts.machine_config import load_config

RELEASE_CHAIN = [
    "source_bound_patch_brief",
    "phase23_machine_gate",
    "per_article_release_manifest",
    "o2_weekly_approval_wave",
    "parent_owned_deploy",
    "post_publish_live_readback",
]


def proposal_id(loop: str, page: str, evidence: Any) -> str:
    return f"loop-{loop.lower()}-{digest({'loop': loop, 'page': page, 'evidence': evidence})[:20]}"


def loop_a(page: dict[str, Any], as_of: dt.date) -> list[dict[str, Any]]:
    """Weekly striking-distance: position 5-20 and >=50 impressions."""
    weekly = page.get("gsc_weekly") or {}
    position = weekly.get("position")
    impressions = int(weekly.get("impressions") or 0)
    query = str(weekly.get("query") or "").strip()
    if position is None or not 5 <= float(position) <= 20 or impressions < 50 or not query:
        return []
    evidence = {"query": query, "position": float(position), "impressions": impressions, "as_of": as_of.isoformat()}
    return [{
        "proposal_id": proposal_id("A", page["path"], evidence),
        "loop": "A_STRIKING_DISTANCE_WEEKLY",
        "page": page["path"],
        "evidence": evidence,
        "proposed_changes": [
            {"target": "title_h1_or_question_h2", "operation": "add_query_naturally", "query": query},
            {"target": "question_h2", "operation": "add_immediate_answer_block", "word_range": [40, 60]},
            {"target": "related_pages", "operation": "add_exact_anchor_internal_links", "count_range": [2, 3], "anchor": query},
        ],
        "evaluation_window_days": [14, 28],
        "release_chain": RELEASE_CHAIN,
    }]


def consecutive_click_declines(values: list[int]) -> bool:
    return len(values) >= 4 and all(values[index] > values[index + 1] for index in range(len(values) - 1))


def build_loop_b_snapshot(
    database: str | Path,
    as_of: dt.date,
    route_aliases: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Read monthly page clicks from the observability mirror without writes."""
    path = Path(database)
    if not path.is_file():
        raise ValueError("loop_b_database_missing")
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='observability_page_daily'"
        ).fetchone()
        if not table:
            raise ValueError("loop_b_page_daily_missing")
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(observability_page_daily)")}
        required = {"metric_date", "page_path", "gsc_clicks"}
        if not required <= columns:
            raise ValueError("loop_b_page_daily_columns_missing:" + ",".join(sorted(required - columns)))
        page_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(observability_page_daily)")}
        optional = [name for name in ("gsc_impressions", "gsc_position", "gsc_ctr") if name in page_columns]
        rows = [dict(row) for row in connection.execute(
            "SELECT metric_date,page_path,gsc_clicks"
            + ("," + ",".join(optional) if optional else "")
            + " FROM observability_page_daily "
            "WHERE metric_date<=? ORDER BY metric_date,page_path",
            (as_of.isoformat(),),
        )]
        tables = {str(row[0]) for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        query_rows: list[dict[str, Any]] = []
        if "observability_gsc_query_daily" in tables:
            query_columns = {str(row[1]) for row in connection.execute(
                "PRAGMA table_info(observability_gsc_query_daily)"
            )}
            required_query = {"metric_date", "page_path", "query_text", "impressions", "clicks", "position"}
            if required_query <= query_columns:
                query_rows = [dict(row) for row in connection.execute(
                    "SELECT metric_date,page_path,query_text,impressions,clicks,position "
                    "FROM observability_gsc_query_daily WHERE metric_date<=? "
                    "AND query_text IS NOT NULL ORDER BY metric_date,page_path,query_text",
                    (as_of.isoformat(),),
                )]
    finally:
        connection.close()
    daily: dict[tuple[str, str], int] = {}
    aliases = route_aliases or {}
    for row in rows:
        route = canonical_feedback_route(str(row["page_path"]), aliases)
        key = (str(row["metric_date"]), route)
        daily[key] = max(daily.get(key, 0), int(row.get("gsc_clicks") or 0))
    monthly: dict[tuple[str, str], int] = {}
    for (metric_date, route), clicks in daily.items():
        key = (metric_date[:7], route)
        monthly[key] = monthly.get(key, 0) + clicks
    pages: dict[str, list[dict[str, Any]]] = {}
    for (month, route), clicks in sorted(monthly.items()):
        pages.setdefault(route, []).append({"month": month, "clicks": clicks})
    weekly_start = as_of - dt.timedelta(days=6)
    query_signal: dict[str, dict[str, Any]] = {}
    for row in query_rows:
        if dt.date.fromisoformat(str(row["metric_date"])) < weekly_start:
            continue
        route = canonical_feedback_route(str(row["page_path"]), aliases)
        query = str(row.get("query_text") or "").strip()
        impressions = int(row.get("impressions") or 0)
        if not query or impressions <= 0:
            continue
        current = query_signal.get(route)
        candidate = {
            "query": query, "impressions": impressions,
            "clicks": int(row.get("clicks") or 0), "position": float(row.get("position") or 0),
        }
        if current is None or candidate["impressions"] > current["impressions"]:
            query_signal[route] = candidate

    generic = {"guides", "best", "reviews", "cost", "tools", "compare", "glp", "1", "online"}
    route_tokens = {
        route: {token for token in re.findall(r"[a-z0-9]+", route.lower()) if token not in generic}
        for route in pages
    }
    page_rows = []
    for route, values in sorted(pages.items()):
        related = []
        for other, other_tokens in route_tokens.items():
            if route == other:
                continue
            union = route_tokens[route] | other_tokens
            similarity = len(route_tokens[route] & other_tokens) / len(union) if union else 0.0
            if similarity >= 0.2:
                related.append({
                    "path": other,
                    "anchor": " ".join(other.strip("/").split("/")[-1].split("-")),
                    "similarity": round(similarity, 4),
                })
        related.sort(key=lambda row: (-row["similarity"], row["path"]))
        page_rows.append({
            "path": route,
            "monthly_clicks": values,
            "gsc_weekly": query_signal.get(route) or {},
            "related_pages": related[:10],
        })
    first_date = min((dt.date.fromisoformat(str(row["metric_date"])) for row in rows), default=as_of)
    site_age_months = max(0, months_between(first_date, as_of))
    return {
        "schema": "seo-machine-improvement-snapshot-v1",
        "as_of": as_of.isoformat(),
        "site_age_months": site_age_months,
        "pages": page_rows,
        "source_digest": digest({"pages": rows, "queries": query_rows}),
        "source_coverage": {
            "page_rows": len(rows), "query_rows": len(query_rows),
            "weekly_query_routes": len(query_signal),
            "related_link_routes": sum(len(row["related_pages"]) >= 5 for row in page_rows),
        },
    }


def refresh_priority(
    monthly: list[dict[str, Any]],
    feedback_page: dict[str, Any],
    settings: dict[str, Any],
) -> dict[str, Any]:
    """Combine bounded click decay and engagement/conversion evidence."""
    values = [int(row.get("clicks") or 0) for row in monthly[-4:] if isinstance(row, dict)]
    first = values[0] if values else 0
    last = values[-1] if values else 0
    decline_fraction = min(1.0, max(0.0, (first - last) / max(first, 1)))
    decay_max = float(settings.get("decay_priority_points_max", 75.0))
    engagement_max = float(settings.get("engagement_priority_points_max", 25.0))
    signal = loop_b_priority_score(feedback_page, settings) if feedback_page else None
    signal_cap = float(settings["loop_b_score_cap"])
    decay = decay_max * decline_fraction
    engagement = engagement_max * signal / signal_cap if signal is not None else None
    return {
        "priority_score": round(min(100.0, decay + (engagement or 0.0)), 3),
        "priority_components": {
            "decay": round(decay, 3),
            "engagement_conversion": round(engagement, 3) if engagement is not None else None,
        },
        "loop_b_signal": signal,
        "unavailable_signals_excluded": ([] if signal is not None else ["engagement_conversion"]),
    }


def loop_b(
    page: dict[str, Any],
    as_of: dt.date,
    feedback_page: dict[str, Any] | None = None,
    feedback_settings: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Monthly scan / quarterly sweep after 3+ consecutive monthly declines."""
    monthly = page.get("monthly_clicks") or []
    values = [int(row.get("clicks") or 0) for row in monthly[-4:] if isinstance(row, dict)]
    if not consecutive_click_declines(values):
        return []
    settings = feedback_settings or {
        "loop_b_score_cap": 25.0,
        "weights": {"engaged_session": 1.0, "affiliate_click": 4.0, "capture_complete": 3.0},
    }
    feedback = feedback_page or {}
    feedback_available = feedback_page is not None
    analytics_evidence = {
        "engaged_sessions": int(feedback.get("engaged_sessions") or 0) if feedback_available else None,
        "affiliate_clicks": int(feedback.get("affiliate_clicks") or 0) if feedback_available else None,
        "capture_views": int(feedback.get("capture_views") or 0) if feedback_available else None,
        "capture_completes": int(feedback.get("capture_completes") or 0) if feedback_available else None,
        "measurement_state": "measured" if feedback_available else "unavailable",
        "source_digest": str(feedback.get("source_digest") or digest({"page": page["path"], "analytics": "not_supplied"})),
    }
    priority = refresh_priority(monthly, analytics_evidence, settings)
    evidence = {
        "months": monthly[-4:], "consecutive_declines": 3, "as_of": as_of.isoformat(),
        "analytics_evidence": analytics_evidence,
    }
    return [{
        "proposal_id": proposal_id("B", page["path"], evidence),
        "loop": "B_REFRESH_DECAY_MONTHLY_QUARTERLY",
        "page": page["path"],
        "evidence": evidence,
        "priority_score": priority["priority_score"],
        "priority_components": priority["priority_components"],
        "unavailable_signals_excluded": priority["unavailable_signals_excluded"],
        "analytics_evidence": analytics_evidence,
        "scan_cadence": "monthly",
        "sweep_cadence": "quarterly",
        "proposed_changes": [
            "reverify_pricing_monthly_against_first_party_sources",
            "update_mutable_data_and_source_receipts",
            "refresh_paa_and_40_60_word_answer_blocks",
            "redate_only_after_substantive_content_hash_change",
        ],
        "release_chain": RELEASE_CHAIN,
    }]


def loop_c(page: dict[str, Any], as_of: dt.date) -> list[dict[str, Any]]:
    """Position-adjusted CTR test with 21-28 day same-position evaluation."""
    ctr = page.get("ctr_test") or {}
    current = ctr.get("actual_ctr")
    expected = ctr.get("expected_ctr")
    position = ctr.get("position")
    if current is None or expected is None or position is None:
        return []
    active = ctr.get("active_experiment")
    if active:
        started = dt.date.fromisoformat(active["started_on"])
        age = (as_of - started).days
        if age < 21:
            return []
        if age > 28:
            return [{
                "proposal_id": proposal_id("C", page["path"], {"active": active, "age": age}),
                "loop": "C_TITLE_CTR_TEST",
                "page": page["path"],
                "evidence": {"age_days": age, "evaluation_window_missed": True},
                "action": "HOLD_RECALIBRATE_EXPERIMENT_WINDOW",
                "release_chain": RELEASE_CHAIN,
            }]
        baseline_position = float(active["baseline_position"])
        same_position = abs(float(position) - baseline_position) <= 1.0
        if not same_position:
            return [{
                "proposal_id": proposal_id("C", page["path"], {"active": active, "position": position}),
                "loop": "C_TITLE_CTR_TEST", "page": page["path"],
                "evidence": {"same_position": False, "baseline_position": baseline_position, "current_position": float(position), "age_days": age},
                "action": "HOLD_UNTIL_SAME_POSITION_COMPARISON",
                "release_chain": RELEASE_CHAIN,
            }]
        winner = float(current) > float(active["baseline_ctr"])
        return [{
            "proposal_id": proposal_id("C", page["path"], {"active": active, "actual_ctr": current, "position": position}),
            "loop": "C_TITLE_CTR_TEST", "page": page["path"],
            "evidence": {"same_position": True, "baseline_ctr": active["baseline_ctr"], "current_ctr": float(current), "age_days": age},
            "action": "KEEP_WINNER" if winner else "REVERT_LOSER_TO_PREVIOUS_TITLE",
            "restore_title": None if winner else active.get("previous_title"),
            "release_chain": RELEASE_CHAIN,
        }]
    if float(current) >= float(expected):
        return []
    evidence = {"actual_ctr": float(current), "expected_ctr": float(expected), "position": float(position), "as_of": as_of.isoformat()}
    return [{
        "proposal_id": proposal_id("C", page["path"], evidence),
        "loop": "C_TITLE_CTR_TEST", "page": page["path"], "evidence": evidence,
        "action": "PROPOSE_TITLE_REWRITE_AND_LOG_BASELINE",
        "evaluation_window_days": [21, 28],
        "same_position_tolerance": 1.0,
        "revert_losers": True,
        "release_chain": RELEASE_CHAIN,
    }]


def _related_candidates(page: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [row for row in page.get("related_pages") or [] if row.get("path") and row.get("path") != page.get("path")]
    rows.sort(key=lambda row: (-float(row.get("similarity") or 0), str(row.get("path"))))
    deduped: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    seen_anchors: set[str] = set()
    for row in rows:
        anchor = normalized(row.get("anchor") or "")
        if row["path"] in seen_paths or not anchor or anchor in seen_anchors:
            continue
        seen_paths.add(row["path"])
        seen_anchors.add(anchor)
        deduped.append({"from_path": row["path"], "anchor": row["anchor"], "similarity": row.get("similarity")})
    return deduped[:10]


def loop_d(page: dict[str, Any], as_of: dt.date) -> list[dict[str, Any]]:
    """On-publish + weekly related-link insertion and money-page inlink floor."""
    related = _related_candidates(page)
    proposals: list[dict[str, Any]] = []
    if len(related) >= 5:
        evidence = {"related_candidates": related, "as_of": as_of.isoformat()}
        proposals.append({
            "proposal_id": proposal_id("D", page["path"], evidence),
            "loop": "D_INTERNAL_LINKS_PUBLISH_WEEKLY", "page": page["path"],
            "trigger": "on_publish_and_weekly",
            "evidence": evidence,
            "action": "INSERT_FROM_5_TO_10_MOST_RELATED_POSTS",
            "anchor_policy": "varied_keyword_descriptive_no_generic_anchor",
            "release_chain": RELEASE_CHAIN,
        })
    if page.get("page_kind") == "money_page" and int(page.get("internal_inlinks") or 0) < 10:
        evidence = {"internal_inlinks": int(page.get("internal_inlinks") or 0), "minimum": 10, "as_of": as_of.isoformat()}
        proposals.append({
            "proposal_id": proposal_id("D", page["path"], evidence),
            "loop": "D_INTERNAL_LINKS_PUBLISH_WEEKLY", "page": page["path"],
            "evidence": evidence, "action": "ALERT_MONEY_PAGE_INLINK_FLOOR",
            "release_chain": RELEASE_CHAIN,
        })
    return proposals


def months_between(start: dt.date, end: dt.date) -> int:
    return (end.year - start.year) * 12 + end.month - start.month - (1 if end.day < start.day else 0)


def loop_e(page: dict[str, Any], as_of: dt.date, site_age_months: int) -> list[dict[str, Any]]:
    """Quarterly from month six; consolidate only at 12+ months and zero signals."""
    if site_age_months < 6 or as_of.month not in {1, 4, 7, 10}:
        return []
    published = page.get("published_on")
    if not published or months_between(dt.date.fromisoformat(published), as_of) < 12:
        return []
    if int(page.get("trailing_12m_clicks") or 0) > 1 or int(page.get("external_links") or 0) != 0 or int(page.get("conversions") or 0) != 0:
        return []
    target = page.get("consolidation_target")
    if not target:
        return [{
            "proposal_id": proposal_id("E", page["path"], {"target": None, "as_of": as_of.isoformat()}),
            "loop": "E_PRUNE_CONSOLIDATE_QUARTERLY_MONTH6_PLUS", "page": page["path"],
            "evidence": {"age_months": months_between(dt.date.fromisoformat(published), as_of), "clicks": page.get("trailing_12m_clicks", 0), "external_links": 0, "conversions": 0},
            "action": "HOLD_NO_DEFINITIVE_CONSOLIDATION_TARGET",
            "delete_allowed": False,
            "release_chain": RELEASE_CHAIN,
        }]
    evidence = {"age_months": months_between(dt.date.fromisoformat(published), as_of), "clicks": page.get("trailing_12m_clicks", 0), "external_links": 0, "conversions": 0, "target": target}
    return [{
        "proposal_id": proposal_id("E", page["path"], evidence),
        "loop": "E_PRUNE_CONSOLIDATE_QUARTERLY_MONTH6_PLUS", "page": page["path"],
        "evidence": evidence,
        "action": "CONSOLIDATE_INTO_DEFINITIVE_GUIDE_THEN_301",
        "sequence": ["merge_unique_value_into_target", "gate_target_patch", "operator_approve", "deploy_target", "verify_target", "add_301", "verify_301"],
        "delete_allowed": False,
        "release_chain": RELEASE_CHAIN,
    }]


def run_loops(
    snapshot: dict[str, Any],
    analytics_feedback: dict[str, Any] | None = None,
    feedback_settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if snapshot.get("schema") != "seo-machine-improvement-snapshot-v1":
        raise ValueError("improvement_snapshot_schema_invalid")
    as_of = dt.date.fromisoformat(snapshot["as_of"])
    site_age_months = int(snapshot.get("site_age_months") or 0)
    feedback = analytics_feedback or {"eligible": False, "pages": {}, "source_digest": digest({"analytics": "not_supplied"})}
    proposals: list[dict[str, Any]] = []
    for page in sorted(snapshot.get("pages") or [], key=lambda row: row.get("path", "")):
        proposals.extend(loop_a(page, as_of))
        proposals.extend(loop_b(page, as_of, feedback.get("pages", {}).get(page.get("path")), feedback_settings))
        proposals.extend(loop_c(page, as_of))
        proposals.extend(loop_d(page, as_of))
        proposals.extend(loop_e(page, as_of, site_age_months))
    proposals.sort(key=lambda row: (
        row["loop"],
        -float(row.get("priority_score") or 0) if row["loop"].startswith("B_") else 0.0,
        row["page"],
        row["proposal_id"],
    ))
    counts = {letter: sum(1 for row in proposals if row["loop"].startswith(letter + "_")) for letter in "ABCDE"}
    pages = list(snapshot.get("pages") or [])
    diagnostics = {
        "pages_evaluated": len(pages),
        "A_weekly_signal_present": sum(bool((page.get("gsc_weekly") or {}).get("query")) for page in pages),
        "B_four_month_history": sum(len(page.get("monthly_clicks") or []) >= 4 for page in pages),
        "C_ctr_test_signal_present": sum(bool(page.get("ctr_test")) for page in pages),
        "D_five_related_pages": sum(len(page.get("related_pages") or []) >= 5 for page in pages),
        "E_site_age_months": site_age_months,
        "zero_proposals_is_pass": False,
    }
    payload = {
        "schema": "seo-machine-improvement-proposals-v1",
        "status": "PASS_PROPOSAL_ONLY" if proposals else "HOLD_NO_ELIGIBLE_PROPOSALS",
        "as_of": as_of.isoformat(),
        "cadence": {
            "A": "weekly", "B": "monthly scan and quarterly sweep", "C": "21-28 day experiment evaluation",
            "D": "on publish and weekly", "E": "quarterly from site month 6",
        },
        "loop_counts": counts,
        "proposals": proposals,
        "proposal_diagnostics": diagnostics,
        "analytics_feedback": {
            "eligible": bool(feedback.get("eligible")),
            "status": feedback.get("status", "NOT_SUPPLIED"),
            "source_digest": feedback["source_digest"],
        },
        "truth_metrics": ["gsc_clicks", "engaged_sessions", "affiliate_clicks", "capture_views", "capture_completes"],
        "ignored_noisy_metrics": ["engagement_rate", "average_engagement_time", "bounce_rate"],
        "effects": {"page_mutations": 0, "publishes": 0, "redirects": 0, "deployments": 0, "gsc_mutations": 0},
    }
    payload["receipt_sha256"] = digest(payload)
    return payload


def optimizer_to_topics(optimizer: dict[str, Any], minimum_impressions: int = 1) -> dict[str, Any]:
    """Feed high-impression uncovered optimizer queries into B1, never directly to generation."""
    rows: list[dict[str, Any]] = []
    for item in optimizer.get("candidates") or optimizer.get("proposals") or []:
        evidence = item.get("evidence") or {}
        query = str(evidence.get("query") or item.get("query") or "").strip()
        impressions = int(evidence.get("impressions") or item.get("impressions") or 0)
        kind = str(item.get("action_type") or item.get("kind") or "")
        uncovered = evidence.get("covered") is False or "uncovered" in kind
        if not query or impressions < minimum_impressions or not uncovered:
            continue
        key = normalized(query)
        rows.append({
            "topic_id": digest({"source": "optimizer", "query": key})[:20],
            "target_query": query,
            "normalized_query": key,
            "opportunity_type": "optimizer_uncovered_query",
            "evidence_state": "READY_FOR_RESEARCH",
            "rank_score": round(math.log1p(impressions) * 10 + 25, 3),
            "optimizer_evidence": evidence,
            "generator_state": "requires_primary_source_research_pack",
            "publication_state": "HELD_O2_WEEKLY_WAVE",
        })
    deduped = {row["normalized_query"]: row for row in sorted(rows, key=lambda row: (-row["rank_score"], row["normalized_query"]))}
    output = sorted(deduped.values(), key=lambda row: (-row["rank_score"], row["normalized_query"]))
    payload = {
        "schema": "seo-machine-optimizer-topic-feed-v1",
        "status": "PASS_REPORT_ONLY",
        "summary": {"ready_for_research": len(output), "page_mutations": 0, "publishes": 0},
        "queue": output,
    }
    payload["receipt_sha256"] = digest(payload)
    return payload


def held_loop_receipt(as_of: dt.date, feedback: dict[str, Any], reason: str | None = None) -> dict[str, Any]:
    payload = {
        "schema": "seo-machine-improvement-proposals-v1",
        "status": "HELD_ANALYTICS_FEEDBACK",
        "as_of": as_of.isoformat(),
        "analytics_feedback": {
            "status": feedback.get("status", "HELD_LOOP_B_SNAPSHOT"),
            "eligible": False,
            "source_digest": feedback.get("source_digest", digest({"reason": reason})),
            "details": feedback.get("details") or {},
        },
        "reason": reason,
        "loop_counts": {letter: 0 for letter in "ABCDE"},
        "proposals": [],
        "effects": {"page_mutations": 0, "publishes": 0, "redirects": 0, "deployments": 0, "gsc_mutations": 0},
    }
    payload["receipt_sha256"] = digest(payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    loops = sub.add_parser("run")
    loops.add_argument("--snapshot", type=Path, required=True)
    loops.add_argument("--output", type=Path, required=True)
    database_loops = sub.add_parser("run-db")
    database_loops.add_argument("--config", type=Path, required=True)
    database_loops.add_argument("--output", type=Path, required=True)
    database_loops.add_argument("--as-of", type=dt.date.fromisoformat, default=dt.date.today())
    feed = sub.add_parser("optimizer-to-topic")
    feed.add_argument("--optimizer", type=Path, required=True)
    feed.add_argument("--minimum-impressions", type=int, default=1)
    feed.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "run":
        policy = consumer_policy("improvement_loops", decision_class="search")
        require_allowed(policy)
        payload = run_loops(read_json(args.snapshot))
        return_code = 0 if payload["proposals"] else 2
    elif args.command == "run-db":
        policy = consumer_policy("improvement_loops", decision_class="search")
        require_allowed(policy)
        config = load_config(args.config)
        aliases = load_route_aliases(config)
        settings = {
            **config["topic_feed"]["analytics_feedback"],
            **config["improvement_loops"]["B"],
        }
        feedback = load_feedback(
            config["paths"]["local_database"],
            args.as_of.isoformat(),
            settings,
            aliases,
        )
        if settings.get("required") is True and not feedback.get("eligible"):
            payload = held_loop_receipt(args.as_of, feedback)
            return_code = 2
        else:
            try:
                snapshot = build_loop_b_snapshot(config["paths"]["local_database"], args.as_of, aliases)
            except (sqlite3.Error, ValueError) as exc:
                payload = held_loop_receipt(args.as_of, feedback, str(exc))
                return_code = 2
            else:
                payload = run_loops(snapshot, feedback, settings)
                return_code = 0 if payload["proposals"] else 2
    else:
        payload = optimizer_to_topics(read_json(args.optimizer), args.minimum_impressions)
        return_code = 0
    if args.command in {"run", "run-db"}:
        search_policy = {**policy, "mode": "PASS_SEARCH_ONLY", "revenue_blind": True}
        decisions = []
        for proposal in payload.get("proposals") or []:
            decisions.append(write_consumer_decision(
                consumer="improvement_loops",
                decision_type="page_improvement_proposal",
                subject=str(proposal.get("page") or ""),
                action=str(proposal.get("action") or proposal.get("loop") or "propose_update"),
                pre_signal_score=0.0,
                components=[
                    {"signal": "proposal_priority", "available": True,
                     "delta": float(proposal.get("priority_score") or 1.0)},
                    {"signal": "normalized_revenue", "available": False, "delta": None,
                     "reason": "proposal_only_search_decision_is_revenue_blind"},
                ],
                policy=search_policy,
            ))
        payload["data_quality_mode"] = "PASS_SEARCH_ONLY"
        payload["revenue_blind"] = True
        payload["analytics_decisions"] = decisions
        payload.pop("receipt_sha256", None)
        payload["receipt_sha256"] = digest(payload)
    write_json(args.output, payload)
    print(json.dumps({"status": payload["status"], "output": str(args.output), "receipt_sha256": payload["receipt_sha256"]}, sort_keys=True))
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
