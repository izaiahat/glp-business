#!/usr/bin/env python3
"""A4 evidence selection, refresh-tier validation, and exact R2 replacement."""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

from weekly_strategy_consumer import (
    DEFAULT_PATH as WEEKLY_STRATEGY_PATH,
    load_strategy as load_weekly_strategy,
    preference as weekly_preference,
)

sys.path.insert(0, "/home/ubuntu/business/systems/analytics-brain")
from decision_policy import (  # noqa: E402
    consumer_policy, require_allowed, write_consumer_decision,
)

OBSERVABILITY_DB = Path(
    "/home/ubuntu/business/reports/glp-clinic-scout/"
    "operator-national-wow-20260723/data/glp-observability.sqlite"
)
KEYWORD_GAP_OPPORTUNITIES = Path(
    "/home/ubuntu/business/reports/glp-clinic-scout/seo-machine/"
    "KEYWORD-GAP-MACHINE-20260823/latest-opportunities.json"
)
DAILY_UPDATE_QUOTAS = (2, 1, 2, 1, 2, 1)
WEEKLY_TIER_UNITS = {"P1": 2, "P2": 3, "P3": 2, "P4": 2}
NET_NEW_WEEKLY_TARGET = 3
R2_MINIMUM_DELTA = 0.30
R2_ROUTE_GOVERNANCE_FIELDS = (
    "risk_tier",
    "surface_taxonomy",
    "comparison_required",
    "comparison_spec",
    "offer_module",
    "has_affiliate_cta",
    "affiliate_link",
    "affiliate_links",
    "inline_ftc_disclosure",
    "inline_ftc_disclosure_text",
    "ftc_disclosure_placement",
    "suppress_cta",
)
META_FIELDS = {
    "updated_at",
    "date_modified",
    "dateModified",
    "operator_release_receipt",
    "status",
    "batch_gate_passed",
    "inbound_link_plan",
    "r2_contract",
}
AUTHORITATIVE_HOSTS = {
    "fda.gov",
    "www.fda.gov",
    "nih.gov",
    "www.nih.gov",
    "cdc.gov",
    "www.cdc.gov",
    "clinicaltrials.gov",
}


def _canonical(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


def payload_hash(value: dict[str, Any], field: str = "receipt_sha256") -> str:
    unsigned = dict(value)
    unsigned.pop(field, None)
    return hashlib.sha256(_canonical(unsigned)).hexdigest()


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_route(value: Any) -> str:
    route = str(value or "")
    if not re.fullmatch(r"/(?:guides|best|reviews|cost|tools|compare)/[a-z0-9]+(?:-[a-z0-9]+)*/", route):
        raise RuntimeError(f"update_route_invalid:{route}")
    return route


def _strict_date(value: Any) -> dt.date | None:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None


def article_age_days(article: dict[str, Any], as_of: dt.date) -> int | None:
    values = [
        _strict_date(article.get(key))
        for key in ("date_modified", "dateModified", "updated_at", "published_on")
    ]
    dates = [value for value in values if value is not None]
    return (as_of - max(dates)).days if dates else None


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _required_columns(connection: sqlite3.Connection, table: str, required: set[str]) -> None:
    columns = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
    if not required <= columns:
        raise RuntimeError(f"update_observability_columns_missing:{table}:{','.join(sorted(required - columns))}")


def load_observability(
    database: Path = OBSERVABILITY_DB,
    *,
    window_end: dt.date | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Read the actual GSC/affiliate mirror in SQLite read-only mode."""
    source = database.resolve(strict=True)
    before_sha = sha(source)
    connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        required_tables = {"observability_page_daily", "observability_gsc_query_daily"}
        if not required_tables <= _tables(connection):
            raise RuntimeError("update_observability_tables_missing")
        _required_columns(connection, "observability_page_daily", {
            "metric_date", "page_path", "gsc_clicks", "gsc_impressions",
            "gsc_ctr", "gsc_position", "affiliate_clicks",
        })
        _required_columns(connection, "observability_gsc_query_daily", {
            "metric_date", "page_path", "query_text", "clicks", "impressions", "ctr", "position",
        })
        latest = connection.execute(
            "SELECT MAX(metric_date) FROM observability_page_daily"
        ).fetchone()[0]
        if latest is None:
            return {}, {"database": {"path": str(source), "sha256": before_sha}, "rows": 0}
        latest_date = dt.date.fromisoformat(str(latest))
        end = min(window_end, latest_date) if window_end is not None else latest_date
        current_start = end - dt.timedelta(days=27)
        previous_start = end - dt.timedelta(days=55)
        previous_end = end - dt.timedelta(days=28)
        page_rows = [dict(row) for row in connection.execute(
            """SELECT metric_date,page_path,gsc_clicks,gsc_impressions,gsc_ctr,gsc_position,affiliate_clicks
               FROM observability_page_daily
               WHERE metric_date BETWEEN ? AND ?
               ORDER BY metric_date,page_path""",
            (previous_start.isoformat(), end.isoformat()),
        )]
        query_rows = [dict(row) for row in connection.execute(
            """SELECT metric_date,page_path,query_text,clicks,impressions,ctr,position
               FROM observability_gsc_query_daily
               WHERE metric_date BETWEEN ? AND ? AND query_text IS NOT NULL
               ORDER BY metric_date,page_path,query_text""",
            (current_start.isoformat(), end.isoformat()),
        )]
    finally:
        connection.close()
    if sha(source) != before_sha:
        raise RuntimeError("update_observability_database_drift")

    routes: dict[str, dict[str, Any]] = {}
    for row in page_rows:
        route = str(row.get("page_path") or "")
        try:
            route = canonical_route(route)
        except RuntimeError:
            continue
        day = dt.date.fromisoformat(str(row["metric_date"]))
        item = routes.setdefault(route, {
            "current_clicks": 0,
            "previous_clicks": 0,
            "impressions": 0,
            "affiliate_clicks": 0,
            "weighted_position": 0.0,
            "position_weight": 0,
            "queries": {},
        })
        clicks = int(row.get("gsc_clicks") or 0)
        if current_start <= day <= end:
            impressions = int(row.get("gsc_impressions") or 0)
            item["current_clicks"] += clicks
            item["impressions"] += impressions
            item["affiliate_clicks"] += int(row.get("affiliate_clicks") or 0)
            position = row.get("gsc_position")
            if position is not None and float(position) > 0:
                item["weighted_position"] += float(position) * max(impressions, 1)
                item["position_weight"] += max(impressions, 1)
        elif previous_start <= day <= previous_end:
            item["previous_clicks"] += clicks
    for row in query_rows:
        route = str(row.get("page_path") or "")
        if route not in routes:
            continue
        query = " ".join(str(row.get("query_text") or "").split())
        if not query:
            continue
        q = routes[route]["queries"].setdefault(query, {
            "query": query,
            "clicks": 0,
            "impressions": 0,
            "weighted_position": 0.0,
            "position_weight": 0,
        })
        impressions = int(row.get("impressions") or 0)
        q["clicks"] += int(row.get("clicks") or 0)
        q["impressions"] += impressions
        position = row.get("position")
        if position is not None and float(position) > 0:
            q["weighted_position"] += float(position) * max(impressions, 1)
            q["position_weight"] += max(impressions, 1)
    for item in routes.values():
        item["position"] = round(item.pop("weighted_position") / max(item.pop("position_weight"), 1), 4)
        item["ctr"] = round(item["current_clicks"] / max(item["impressions"], 1), 6)
        previous = item["previous_clicks"]
        item["click_decay_fraction"] = round(
            max(0.0, (previous - item["current_clicks"]) / previous), 6
        ) if previous > 0 else 0.0
        queries = list(item["queries"].values())
        for query in queries:
            query["position"] = round(
                query.pop("weighted_position") / max(query.pop("position_weight"), 1), 4
            )
            query["ctr"] = round(query["clicks"] / max(query["impressions"], 1), 6)
        queries.sort(key=lambda row: (-row["impressions"], row["position"], row["query"]))
        item["queries"] = queries
    evidence = {
        "database": {"path": str(source), "sha256": before_sha},
        "window_start": current_start.isoformat(),
        "window_end": end.isoformat(),
        "previous_window_start": previous_start.isoformat(),
        "previous_window_end": previous_end.isoformat(),
        "page_rows": len(page_rows),
        "query_rows": len(query_rows),
    }
    evidence["source_digest"] = hashlib.sha256(_canonical({"page": page_rows, "query": query_rows})).hexdigest()
    return routes, evidence


def select_update_candidates(
    articles: Iterable[dict[str, Any]],
    metrics: dict[str, dict[str, Any]],
    *,
    as_of: dt.date,
) -> dict[str, Any]:
    """Apply P1→P4 once per route; class-A 11–40 pages lead R2 selection.

    Positions 41–90 remain deep-loss P5 spoke work. This boundary is deliberate:
    an impression-bearing page at #12 (or #40) is cheaper to improve than a new
    generic page near #90, so it must reach the updater rather than the carrier.
    """
    by_route: dict[str, dict[str, Any]] = {}
    for article in articles:
        route = canonical_route(article.get("canonical_path"))
        if route in by_route:
            raise RuntimeError(f"update_article_route_duplicate:{route}")
        by_route[route] = article
    selected: list[dict[str, Any]] = []
    spokes: list[dict[str, Any]] = []
    for route, metric in sorted(metrics.items()):
        article = by_route.get(route)
        if article is None:
            continue
        position = float(metric.get("position") or 0)
        age = article_age_days(article, as_of)
        base = {
            "route": route,
            "article_id": article.get("article_id"),
            "position": position,
            "impressions": int(metric.get("impressions") or 0),
            "ctr": float(metric.get("ctr") or 0),
            "current_clicks": int(metric.get("current_clicks") or 0),
            "previous_clicks": int(metric.get("previous_clicks") or 0),
            "click_decay_fraction": float(metric.get("click_decay_fraction") or 0),
            "affiliate_clicks_28d": int(metric.get("affiliate_clicks") or 0),
            "page_age_days": age,
            "gsc_queries": metric.get("queries") or [],
            "dataforseo_relevance_score": float(metric.get("dataforseo_relevance_score") or 0),
            "dataforseo_search_volume": int(metric.get("dataforseo_search_volume") or 0),
            "dataforseo_keyword_difficulty": metric.get("dataforseo_keyword_difficulty"),
            "dataforseo_measured_query_count": int(metric.get("dataforseo_measured_query_count") or 0),
            "keyword_gap_opportunity_ids": list(metric.get("keyword_gap_opportunity_ids") or []),
        }
        class_a_queries = [
            row for row in base["gsc_queries"]
            if isinstance(row, dict)
            and int(row.get("impressions") or 0) > 0
            and 11 <= float(row.get("position") or 0) <= 40
        ]
        if class_a_queries:
            class_a_impressions = sum(int(row["impressions"]) for row in class_a_queries)
            class_a_clicks = sum(int(row.get("clicks") or 0) for row in class_a_queries)
            base.update({
                "position": round(sum(
                    float(row["position"]) * int(row["impressions"])
                    for row in class_a_queries
                ) / class_a_impressions, 4),
                "impressions": class_a_impressions,
                "current_clicks": class_a_clicks,
                "ctr": round(class_a_clicks / class_a_impressions, 6),
                "gsc_queries": class_a_queries,
            })
            position = float(base["position"])
        elif 41 <= position <= 90:
            spokes.append({
                **base,
                "priority": "P5",
                "refresh_tier": None,
                "action": "net_new_spoke",
                "r2_edit_forbidden": True,
                "reason": "deep_loss_cluster_support_required",
            })
            continue
        priority = None
        if class_a_queries:
            priority = "P1"
            base["opportunity_class"] = "A"
            base["selection_basis"] = "keyword_gap_striking_distance_11_40"
            base["route_owner"] = "r2_updater"
            base["carrier_excluded"] = True
        elif base["affiliate_clicks_28d"] >= 5:
            priority = "P1"
        elif 5 <= position <= 20 and base["impressions"] >= 50:
            priority = "P2"
        elif 0 < position <= 10 and base["ctr"] < 0.02 and base["impressions"] >= 100:
            priority = "P3"
        elif base["click_decay_fraction"] >= 0.30 and age is not None and age >= 90:
            priority = "P4"
        if priority:
            selected.append({
                **base,
                "priority": priority,
                "refresh_tier": "R2",
                "action": "substantive_refresh",
                "refresh_deadline_days": 21 if priority == "P1" else None,
                "required_inbound_links": 2 if priority == "P1" else 1,
            })
    order = {name: index for index, name in enumerate(("P1", "P2", "P3", "P4"))}
    selected.sort(key=lambda row: (
        order[row["priority"]],
        0 if row.get("opportunity_class") == "A" else 1,
        0 if row.get("opportunity_class") == "A" and row.get("dataforseo_measured_query_count") else 1,
        -float(row.get("dataforseo_relevance_score") or 0)
        if row.get("opportunity_class") == "A" else 0,
        -int(row.get("dataforseo_search_volume") or 0)
        if row.get("opportunity_class") == "A" else 0,
        row["position"] if row.get("opportunity_class") == "A" or row["priority"] == "P2"
        else -row["affiliate_clicks_28d"],
        -int(row.get("impressions") or 0),
        row["route"],
    ))
    spokes.sort(key=lambda row: (row["position"], row["route"]))
    return {"r2_candidates": selected, "p5_net_new_spokes": spokes}


def select_refresh_cadence_reserve(
    articles: Iterable[dict[str, Any]],
    metrics: dict[str, dict[str, Any]],
    *,
    as_of: dt.date,
    excluded_routes: set[str],
) -> list[dict[str, Any]]:
    """Select observed existing underperformers when strict P1-P4 has gaps.

    The operator's standing freshness authority permits substantive rewrites of
    published underperformers. Reserve rows still require real GSC query truth;
    routes with no observed query are never invented or refreshed here.
    """
    by_route = {canonical_route(row.get("canonical_path")): row for row in articles}
    reserve: list[dict[str, Any]] = []
    for route, metric in metrics.items():
        article = by_route.get(route)
        if article is None or route in excluded_routes:
            continue
        queries = [
            dict(row) for row in metric.get("queries") or []
            if isinstance(row, dict) and str(row.get("query") or "").strip()
        ]
        position = float(metric.get("position") or 0)
        if not queries or not 0 < position <= 90:
            continue
        reserve.append({
            "route": route,
            "article_id": article.get("article_id"),
            "position": position,
            "impressions": int(metric.get("impressions") or 0),
            "ctr": float(metric.get("ctr") or 0),
            "current_clicks": int(metric.get("current_clicks") or 0),
            "previous_clicks": int(metric.get("previous_clicks") or 0),
            "click_decay_fraction": float(metric.get("click_decay_fraction") or 0),
            "affiliate_clicks_28d": int(metric.get("affiliate_clicks") or 0),
            "page_age_days": article_age_days(article, as_of),
            "gsc_queries": queries,
            "priority": "P4",
            "refresh_tier": "R2",
            "action": "substantive_freshness_refresh",
            "selection_basis": "observed_existing_article_underperformance",
            "operator_freshness_cadence_authority": True,
            "required_inbound_links": 1,
            "r2_edit_forbidden": False,
        })
    reserve.sort(key=lambda row: (
        1 if float(row["position"]) >= 31 else 0,
        float(row["position"]),
        -int(row["impressions"]),
        str(row["route"]),
    ))
    return reserve


def weekly_plan(
    candidates: Iterable[dict[str, Any]], *, cogny_strategy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # Advice can order already-admitted rows, never add routes or change tiers.
    rows = [dict(row) for row in candidates]
    if cogny_strategy is not None:
        for row in rows:
            queries = [q.get("query", "") for q in row.get("gsc_queries", [])] or [""]
            matches = [weekly_preference(cogny_strategy, q, row.get("route"), "r2_refresh") for q in queries]
            matches = [value for value in matches if value is not None]
            row["cogny_strategy_preference"] = max(matches, key=lambda value: value["score"]) if matches else None
    selected: list[dict[str, Any]] = []
    capacities: dict[str, dict[str, int]] = {}
    for tier in ("P1", "P2", "P3", "P4"):
        eligible = [row for row in rows if row.get("priority") == tier]
        if tier == "P1":
            eligible.sort(key=lambda row: (
                0 if row.get("opportunity_class") == "A" else 1,
                0 if row.get("opportunity_class") == "A" and row.get("dataforseo_measured_query_count") else 1,
                -float(row.get("dataforseo_relevance_score") or 0)
                if row.get("opportunity_class") == "A" else 0,
                -int(row.get("dataforseo_search_volume") or 0)
                if row.get("opportunity_class") == "A" else 0,
                float(row.get("position") or float("inf"))
                if row.get("opportunity_class") == "A"
                else -int(row.get("affiliate_clicks_28d") or 0),
                -int(row.get("impressions") or 0),
                str(row.get("route") or ""),
            ))
        elif tier == "P2":
            eligible.sort(key=lambda row: (float(row.get("position") or float("inf")), str(row.get("route") or "")))
        elif tier == "P3":
            eligible.sort(key=lambda row: (float(row.get("ctr") or 0), -int(row.get("impressions") or 0), str(row.get("route") or "")))
        else:
            eligible.sort(key=lambda row: (-float(row.get("click_decay_fraction") or 0), str(row.get("route") or "")))
        if cogny_strategy is not None:
            # Stable soft ordering retains the previous metric order on ties.
            # Measured class-A admission remains ahead of unmeasured P1 rows.
            eligible.sort(key=lambda row: (
                0 if tier == "P1" and row.get("opportunity_class") == "A" else 1,
                0 if tier == "P1" and row.get("opportunity_class") == "A" and row.get("dataforseo_measured_query_count") else 1,
                -float((row.get("cogny_strategy_preference") or {}).get("score", 0)),
            ))
        cap = WEEKLY_TIER_UNITS[tier]
        chosen = eligible[:cap]
        selected.extend(chosen)
        capacities[tier] = {
            "required": cap,
            "eligible": len(eligible),
            "selected": len(chosen),
            "deficit": max(0, cap - len(chosen)),
        }
    requested = sum(WEEKLY_TIER_UNITS.values())
    return {
        "schema": "glp-seo-r2-weekly-plan-v1",
        "status": "PASS_WEEKLY_R2_PLAN" if len(selected) == requested else "R2_CAPACITY_DEFICIT",
        "required": requested,
        "selected_count": len(selected),
        "capacity_deficit": requested - len(selected),
        "tier_capacity": capacities,
        "selected": selected,
        "daily_quotas": list(DAILY_UPDATE_QUOTAS),
        "net_new_weekly_target": NET_NEW_WEEKLY_TARGET,
    }


def daily_plan(candidates: Iterable[dict[str, Any]], update_day_index: int) -> dict[str, Any]:
    if not 0 <= update_day_index < len(DAILY_UPDATE_QUOTAS):
        raise RuntimeError("update_day_index_invalid")
    week = weekly_plan(candidates)
    start = sum(DAILY_UPDATE_QUOTAS[:update_day_index])
    quota = DAILY_UPDATE_QUOTAS[update_day_index]
    chosen = week["selected"][start:start + quota]
    return {
        "schema": "glp-seo-r2-daily-plan-v1",
        "status": "PASS_DAILY_R2_PLAN" if len(chosen) == quota else "R2_CAPACITY_DEFICIT",
        "update_day_index": update_day_index,
        "quota": quota,
        "selected_count": len(chosen),
        "capacity_deficit": quota - len(chosen),
        "selected": chosen,
        "weekly_capacity": week,
    }


def validate_r2_execution_contract(
    contract: dict[str, Any],
    *,
    route: str,
) -> dict[str, Any]:
    """Fail closed on deep-loss or incomplete packet-level R2 authority."""
    target = canonical_route(route)
    if not isinstance(contract, dict):
        raise RuntimeError("r2_execution_contract_invalid")
    priority = contract.get("priority")
    if priority not in WEEKLY_TIER_UNITS:
        raise RuntimeError("r2_priority_invalid")
    raw_position = contract.get("position")
    if isinstance(raw_position, bool) or not isinstance(raw_position, (int, float)):
        raise RuntimeError("r2_position_invalid")
    position = float(raw_position)
    cadence_authority = contract.get("operator_freshness_cadence_authority") is True
    if cadence_authority and (
        priority != "P4"
        or contract.get("selection_basis") != "observed_existing_article_underperformance"
        or contract.get("action") != "substantive_freshness_refresh"
    ):
        raise RuntimeError("r2_freshness_cadence_authority_invalid")
    if (41 <= position <= 90 or contract.get("r2_edit_forbidden") is True) and not cadence_authority:
        raise RuntimeError("r2_deep_loss_edit_forbidden")
    query_values = contract.get("gsc_queries")
    if not isinstance(query_values, list):
        raise RuntimeError("r2_contract_gsc_queries_missing")
    queries = [
        " ".join(str(row.get("query") if isinstance(row, dict) else row).split())
        for row in query_values
    ]
    if not queries or any(not query for query in queries):
        raise RuntimeError("r2_contract_gsc_queries_missing")
    current_routes = {canonical_route(value) for value in contract.get("current_routes") or []}
    if target not in current_routes:
        raise RuntimeError("r2_target_not_current")
    striking = {canonical_route(value) for value in contract.get("striking_distance_routes") or []}
    if not striking:
        raise RuntimeError("r2_striking_distance_routes_missing")
    # Every inbound plan is required to point at the article being refreshed.
    # A global striking-distance set that omits this target creates an authority
    # no packet can satisfy: review can pass, but the release builder rejects the
    # exact same target at publication time.  Validate that contradiction when
    # authority is minted, not after a full review run.
    if target not in striking:
        raise RuntimeError("r2_target_not_striking_distance_authorized")
    required_inbound = contract.get("required_inbound_links")
    expected_inbound = 2 if priority == "P1" else 1
    if required_inbound != expected_inbound:
        raise RuntimeError("r2_required_inbound_links_invalid")
    return {
        **contract,
        "priority": priority,
        "position": position,
        "gsc_queries": queries,
        "current_routes": sorted(current_routes),
        "striking_distance_routes": sorted(striking),
        "required_inbound_links": expected_inbound,
    }


def r1_data_sync(
    article: dict[str, Any],
    data_changes: dict[str, Any],
    *,
    material: bool,
    as_of: dt.date,
) -> dict[str, Any]:
    if not isinstance(data_changes, dict) or not data_changes:
        raise RuntimeError("r1_data_changes_empty")
    updated = copy.deepcopy(article)
    before = copy.deepcopy(article)
    updated.update(data_changes)
    if material:
        if "date_modified" in updated:
            updated["date_modified"] = as_of.isoformat()
        if "dateModified" in updated:
            updated["dateModified"] = as_of.isoformat()
        updated["updated_at"] = as_of.isoformat()
    else:
        for field in ("date_modified", "dateModified", "updated_at"):
            if field in before:
                updated[field] = before[field]
            else:
                updated.pop(field, None)
    return {
        "refresh_tier": "R1",
        "status": "PASS_R1_DATA_SYNC",
        "counted_r2_unit": False,
        "material": material,
        "date_modified_changed": any(before.get(key) != updated.get(key) for key in ("date_modified", "dateModified", "updated_at")),
        "article": updated,
    }


def plan_r3(route: str, authorization_packet: dict[str, Any] | None = None) -> dict[str, Any]:
    source_route = canonical_route(route)
    if authorization_packet is None:
        return {
            "refresh_tier": "R3",
            "status": "HOLD_R3_AUTHORIZED_MERGE_PACKET_REQUIRED",
            "action": "merge_plus_301",
            "executable": False,
        }
    if (
        authorization_packet.get("schema") != "glp-seo-authorized-merge-packet-v1"
        or authorization_packet.get("status") != "AUTHORIZED_MERGE_PLUS_301"
        or authorization_packet.get("receipt_sha256") != payload_hash(authorization_packet)
        or authorization_packet.get("source_route") != source_route
        or not authorization_packet.get("target_route")
    ):
        raise RuntimeError("r3_authorization_packet_invalid")
    target_route = canonical_route(authorization_packet["target_route"])
    if target_route == source_route:
        raise RuntimeError("r3_authorization_packet_invalid")
    return {
        "refresh_tier": "R3",
        "status": "READY_R3_AUTHORIZED_MERGE_PACKET",
        "action": "merge_plus_301",
        "executable": True,
        "authorization": authorization_packet,
    }


def _substantive_projection(article: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in article.items() if key not in META_FIELDS}


def substantive_delta_fraction(before: dict[str, Any], after: dict[str, Any]) -> float:
    token = re.compile(r"[a-z0-9]+")
    left = Counter(token.findall(json.dumps(_substantive_projection(before), sort_keys=True).lower()))
    right = Counter(token.findall(json.dumps(_substantive_projection(after), sort_keys=True).lower()))
    changed = sum((left - right).values()) + sum((right - left).values())
    return changed / max(sum(left.values()), sum(right.values()), 1)


def _section_key(section: dict[str, Any]) -> str:
    return str(section.get("id") or section.get("heading") or section.get("h2") or "").strip().lower()


def _section_text(section: dict[str, Any]) -> str:
    values = [section.get("heading"), section.get("h2"), section.get("title"), section.get("answer")]
    values.extend(section.get("paragraphs") or [])
    return " ".join(str(value or "") for value in values)


def _query_answering_sections(
    before: dict[str, Any],
    after: dict[str, Any],
    queries: Iterable[Any],
) -> list[dict[str, str]]:
    old = {_section_key(row) for row in before.get("sections") or [] if isinstance(row, dict)}
    query_rows = [
        " ".join(str(query.get("query") if isinstance(query, dict) else query).lower().split())
        for query in queries
        if str(query.get("query") if isinstance(query, dict) else query).strip()
    ]
    matched: list[dict[str, str]] = []
    for section in after.get("sections") or []:
        if not isinstance(section, dict) or not _section_key(section) or _section_key(section) in old:
            continue
        text = _section_text(section).lower()
        text_tokens = set(re.findall(r"[a-z0-9]+", text))
        if len(text.split()) < 8:
            continue
        for query in query_rows:
            query_tokens = set(re.findall(r"[a-z0-9]+", query))
            overlap = len(query_tokens & text_tokens) / max(len(query_tokens), 1)
            if query in text or overlap >= 0.6:
                matched.append({"section": _section_key(section), "query": query})
                break
    return matched


def _source_identity(source: Any) -> str:
    if not isinstance(source, dict):
        return str(source)
    return str(source.get("url") or source.get("source_id") or source.get("id") or "")


def _authoritative(source: Any) -> bool:
    if not isinstance(source, dict):
        return False
    kind = str(
        source.get("source_type")
        or source.get("authority_type")
        or source.get("type")
        or source.get("source_class")
        or ""
    ).lower()
    if (
        source.get("primary") is True
        or source.get("authoritative") is True
        or kind in {"primary", "authoritative", "government", "label", "prescribing_information"}
        or kind.startswith(("primary_", "first_party_"))
    ):
        return True
    host = (urlsplit(str(source.get("url") or "")).hostname or "").lower()
    return host in AUTHORITATIVE_HOSTS or host.endswith(".gov")


def _routes_in(value: Any) -> set[str]:
    routes: set[str] = set()
    if isinstance(value, dict):
        for item in value.values():
            routes |= _routes_in(item)
    elif isinstance(value, list):
        for item in value:
            routes |= _routes_in(item)
    elif isinstance(value, str):
        routes |= set(re.findall(r"/(?:guides|best|reviews|cost|tools|compare)/[a-z0-9]+(?:-[a-z0-9]+)*/", value))
    return routes


def preserve_r2_route_governance(
    before: dict[str, Any],
    after: dict[str, Any],
) -> dict[str, Any]:
    """Keep risk, monetization, and disclosure authority unchanged in R2.

    R2 authorizes a source-bound content refresh, not a risk reclassification or
    monetization ruling. The predecessor therefore owns these route-level fields
    exactly, including their absence. Deep copies prevent a nested comparison or
    offer specification from aliasing either input.
    """
    preserved = copy.deepcopy(after)
    for field in R2_ROUTE_GOVERNANCE_FIELDS:
        if field in before:
            preserved[field] = copy.deepcopy(before[field])
        else:
            preserved.pop(field, None)
    return preserved


def semantic_materiality(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Structural evidence for an independently judged material answer change.

    Token churn and dates are diagnostics only. This does not replace claim
    review: acceptance still needs the exact subject-bound five-gate receipt.
    """
    old_sources = {_source_identity(row) for row in before.get("sources") or []}
    new_sources = [_source_identity(row) for row in after.get("sources") or []
                   if _authoritative(row) and _source_identity(row) not in old_sources]
    old_sections = {_canonical(row) for row in before.get("sections") or []}
    changed = [row for row in after.get("sections") or [] if _canonical(row) not in old_sections
               and len(re.findall(r"\w+", json.dumps(row))) >= 12]
    return {"material": bool(changed and new_sources),
            "basis": "evidence_backed_answer_change" if changed and new_sources else "no_material_evidence_change",
            "changed_sections": [row.get("heading") or row.get("h2") for row in changed],
            "new_primary_sources": new_sources,
            "independent_semantic_review_required": True}


def validate_r2_refresh(
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    gsc_queries: Iterable[Any],
    current_routes: set[str],
    striking_distance_routes: set[str],
    inbound_link_plan: Iterable[dict[str, Any]] | None = None,
    minimum_inbound_links: int = 1,
    priority: str | None = None,
) -> dict[str, Any]:
    route = canonical_route(before.get("canonical_path"))
    if after.get("canonical_path") != route:
        raise RuntimeError("r2_canonical_route_changed")
    if not before.get("article_id") or after.get("article_id") != before.get("article_id"):
        raise RuntimeError("r2_article_id_changed")
    delta = substantive_delta_fraction(before, after)  # diagnostics, never a quota
    materiality = semantic_materiality(before, after)
    if not materiality["material"]:
        raise RuntimeError("r2_no_semantically_material_change")
    query_sections = _query_answering_sections(before, after, gsc_queries)
    if not query_sections:
        raise RuntimeError("r2_gsc_query_answering_section_missing")
    old_sources = {_source_identity(row) for row in before.get("sources") or []}
    new_sources = [
        row for row in after.get("sources") or []
        if _source_identity(row) and _source_identity(row) not in old_sources and _authoritative(row)
    ]
    if not new_sources:
        raise RuntimeError("r2_new_primary_authoritative_source_missing")
    old_links = _routes_in(_substantive_projection(before))
    new_links = {
        link for link in _routes_in(_substantive_projection(after)) - old_links
        if link != route and link in current_routes
    }
    if not new_links:
        raise RuntimeError("r2_new_outbound_internal_link_missing")
    plans = list(inbound_link_plan if inbound_link_plan is not None else after.get("inbound_link_plan") or [])
    valid_inbound = []
    for plan in plans:
        source = str(plan.get("source_route") or "")
        target = str(plan.get("target_route") or "")
        if (
            source in current_routes
            and source != target
            and target in striking_distance_routes
            and target in current_routes
            and (priority != "P1" or target == route)
            and str(plan.get("context_sentence") or "").strip()
        ):
            valid_inbound.append({"source_route": source, "target_route": target})
    unique_inbound = {
        (row["source_route"], row["target_route"]): row for row in valid_inbound
    }
    valid_inbound = list(unique_inbound.values())
    if not isinstance(minimum_inbound_links, int) or isinstance(minimum_inbound_links, bool) or minimum_inbound_links < 1:
        raise RuntimeError("r2_minimum_inbound_links_invalid")
    if len(valid_inbound) < minimum_inbound_links:
        raise RuntimeError("r2_striking_distance_inbound_link_missing")
    return {
        "status": "PASS_R2_REFRESH_CONTRACT",
        "refresh_tier": "R2",
        "priority": priority,
        "canonical_route": route,
        "article_id": after["article_id"],
        "semantic_materiality": materiality,
        "substantive_delta_fraction": round(delta, 6),
        "query_answering_sections": query_sections,
        "new_authoritative_sources": [_source_identity(row) for row in new_sources],
        "new_outbound_internal_links": sorted(new_links),
        "striking_distance_inbound_links": valid_inbound,
    }


def replace_article_and_ownership(
    catalogs: dict[str, dict[str, Any]],
    ownership: dict[str, Any],
    replacement: dict[str, Any],
    *,
    opus_receipt_sha256: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any], dict[str, Any]]:
    route = canonical_route(replacement.get("canonical_path"))
    article_id = str(replacement.get("article_id") or "")
    if not article_id:
        raise RuntimeError("r2_article_id_missing")
    updated_catalogs = copy.deepcopy(catalogs)
    matches: list[tuple[str, int, dict[str, Any]]] = []
    for name, payload in updated_catalogs.items():
        articles = payload.get("articles")
        if not isinstance(articles, list):
            raise RuntimeError(f"r2_catalog_articles_invalid:{name}")
        for index, article in enumerate(articles):
            if article.get("canonical_path") == route:
                matches.append((name, index, article))
    if len(matches) != 1:
        raise RuntimeError(f"r2_exact_route_match_count_invalid:{len(matches)}")
    name, index, previous = matches[0]
    if previous.get("article_id") != article_id:
        raise RuntimeError("r2_existing_article_id_mismatch")
    if any(
        row.get("article_id") == article_id and row.get("canonical_path") != route
        for payload in updated_catalogs.values()
        for row in payload["articles"]
    ):
        raise RuntimeError("r2_article_id_owned_by_other_route")
    updated_catalogs[name]["articles"][index] = copy.deepcopy(replacement)

    updated_ownership = copy.deepcopy(ownership)
    entries_key = "entries" if isinstance(updated_ownership.get("entries"), list) else "keywords"
    entries = updated_ownership.get(entries_key)
    if not isinstance(entries, list):
        raise RuntimeError("r2_ownership_shape_invalid")
    ownership_matches = [idx for idx, row in enumerate(entries) if row.get("url") == route]
    if len(ownership_matches) != 1:
        raise RuntimeError(f"r2_exact_ownership_match_count_invalid:{len(ownership_matches)}")
    ownership_index = ownership_matches[0]
    old_owner = entries[ownership_index]
    owner_article_id = old_owner.get("article_id", old_owner.get("owner"))
    if owner_article_id not in (None, article_id):
        raise RuntimeError("r2_ownership_article_id_mismatch")
    replacement_status = replacement.get("status")
    if replacement_status is not None:
        if not isinstance(replacement_status, str) or not replacement_status.strip():
            raise RuntimeError("r2_replacement_status_invalid")
        owner_status = replacement_status
    elif "status" in old_owner:
        raise RuntimeError("r2_replacement_status_missing")
    else:
        owner_status = "PASS_OPUS_SEO_DRAFT"
    new_owner = dict(old_owner)
    if "article_id" in old_owner:
        new_owner["article_id"] = article_id
    elif "owner" in old_owner:
        new_owner["owner"] = article_id
    new_owner.update({
        "primary_keyword": replacement["primary_keyword"],
        "risk_tier": replacement["risk_tier"],
        "status": owner_status,
        "url": route,
        "batch_gate_passed": True,
        "operator_release_receipt": opus_receipt_sha256,
    })
    entries[ownership_index] = new_owner
    return updated_catalogs, updated_ownership, {
        "catalog": name,
        "article_index": index,
        "ownership_index": ownership_index,
        "replaced_count": 1,
        "ownership_updated_count": 1,
        "previous": previous,
    }


def materialize_national_article_ownership(
    ownership: dict[str, Any],
    national_articles: Iterable[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Persist the national ownership rows already derived by the renderer.

    The flagship renderer historically derived these rows in memory from
    ``content-national-wow-additions.json``. R2 operates on the persisted map,
    so a national route rendered with an effective owner had no source row for
    the exact-one replacement guard. Materialize that same derivation into the
    candidate map before replacement; never synthesize from a route alone.
    """
    updated = copy.deepcopy(ownership)
    entries_key = "entries" if isinstance(updated.get("entries"), list) else "keywords"
    entries = updated.get(entries_key)
    if not isinstance(entries, list) or not all(isinstance(row, dict) for row in entries):
        raise RuntimeError("r2_ownership_shape_invalid")

    existing_urls: dict[str, list[int]] = {}
    existing_keywords: dict[str, list[int]] = {}
    for index, row in enumerate(entries):
        url = str(row.get("url") or "")
        keyword = str(row.get("primary_keyword") or "").strip().casefold()
        if url:
            existing_urls.setdefault(url, []).append(index)
        if keyword:
            existing_keywords.setdefault(keyword, []).append(index)
    duplicate_urls = sorted(url for url, indexes in existing_urls.items() if len(indexes) != 1)
    if duplicate_urls:
        raise RuntimeError(
            f"r2_exact_ownership_match_count_invalid:{len(existing_urls[duplicate_urls[0]])}"
        )
    if any(len(indexes) != 1 for indexes in existing_keywords.values()):
        raise RuntimeError("r2_keyword_ownership_collision")

    materialized_routes: list[str] = []
    preserved_routes: list[str] = []
    for article in national_articles:
        route = canonical_route(article.get("canonical_path"))
        article_id = str(article.get("article_id") or "")
        owner = str(article.get("owner_key") or "")
        keyword = str(article.get("primary_keyword") or "").strip()
        risk_tier = str(article.get("risk_tier") or "")
        status = str(article.get("status") or "")
        if not article_id or not owner or not keyword or not risk_tier or not status:
            raise RuntimeError(f"r2_national_ownership_source_invalid:{route}")
        matches = existing_urls.get(route, [])
        if len(matches) > 1:
            raise RuntimeError(f"r2_exact_ownership_match_count_invalid:{len(matches)}")
        if matches:
            row = entries[matches[0]]
            stored_owner = row.get("article_id", row.get("owner"))
            if stored_owner != article_id:
                raise RuntimeError("r2_ownership_article_id_mismatch")
            if (
                str(row.get("primary_keyword") or "").strip() != keyword
                or str(row.get("risk_tier") or "") != risk_tier
                or str(row.get("status") or "") != status
            ):
                raise RuntimeError(f"r2_national_ownership_identity_invalid:{route}")
            preserved_routes.append(route)
            continue
        # Missing national ownership may only be bootstrapped from the original
        # article-id-owned catalog shape. Once an exact persisted route owner
        # exists, owner_key remains a route alias and the map above is the
        # authoritative identity source.
        if owner != article_id:
            raise RuntimeError(f"r2_national_ownership_source_invalid:{route}")
        normalized_keyword = keyword.casefold()
        if normalized_keyword in existing_keywords:
            raise RuntimeError("r2_keyword_ownership_collision")
        entries.append({
            "owner": owner,
            "primary_keyword": keyword,
            "risk_tier": risk_tier,
            "status": status,
            "url": route,
        })
        index = len(entries) - 1
        existing_urls[route] = [index]
        existing_keywords[normalized_keyword] = [index]
        materialized_routes.append(route)

    return updated, {
        "schema": "glp-r2-national-ownership-materialization-v1",
        "status": "PASS_EXACT_NATIONAL_OWNERSHIP",
        "entries_key": entries_key,
        "materialized_count": len(materialized_routes),
        "materialized_routes": materialized_routes,
        "preserved_count": len(preserved_routes),
        "total_entries": len(entries),
    }


def load_release_articles(release_root: Path) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    root = release_root.resolve(strict=True)
    relpaths = (
        "data/blog/content-batch.json",
        "data/blog/content-national-wow-additions.json",
    )
    catalogs: dict[str, dict[str, Any]] = {}
    rows: list[dict[str, Any]] = []
    for relative in relpaths:
        path = root / relative
        payload = json.loads(path.read_text(encoding="utf-8"))
        catalogs[relative] = payload
        rows.extend(payload.get("articles") or [])
    return rows, catalogs


def apply_keyword_gap_class_a(
    metrics: dict[str, dict[str, Any]],
    *,
    opportunity_path: Path,
    current_routes: set[str],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Overlay weekly query-page class A truth onto R2 observability.

    The observability mirror can lag the domain-property pull by several days.
    The weekly machine already has the query, current page, impressions and
    position required by R2, so the updater consumes that validated artifact.
    Only current release routes enter; net-new carrier routes never do.
    """
    meta: dict[str, Any] = {
        "path": str(opportunity_path), "state": "absent", "routes": 0, "queries": 0,
    }
    if not opportunity_path.is_file():
        return metrics, meta
    payload = json.loads(opportunity_path.read_text(encoding="utf-8"))
    if (
        payload.get("schema") != "glp-keyword-gap-opportunities-v1"
        or payload.get("operator_directive_id") != "operator-keyword-gap-20260823"
    ):
        raise RuntimeError("r2_keyword_gap_class_a_contract_invalid")
    unsigned = dict(payload)
    expected = unsigned.pop("artifact_sha256", None)
    actual = hashlib.sha256(json.dumps(
        unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")).hexdigest()
    if expected != actual:
        raise RuntimeError("r2_keyword_gap_class_a_self_hash_invalid")

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in (payload.get("classes") or {}).get("A") or []:
        route = str(row.get("target_route") or "")
        if (
            row.get("queue_state") != "QUEUED_R2_REFRESH"
            or row.get("route_owner") != "r2_updater"
            or route not in current_routes
        ):
            continue
        route = canonical_route(route)
        query = " ".join(str(row.get("keyword") or "").split())
        impressions = int(row.get("gsc_impressions") or 0)
        position = float(row.get("our_position") or 0)
        if not query or impressions <= 0 or not 11 <= position <= 40:
            raise RuntimeError("r2_keyword_gap_class_a_row_invalid")
        grouped.setdefault(route, []).append({
            "query": query,
            "clicks": int(row.get("gsc_clicks") or 0),
            "impressions": impressions,
            "position": position,
            "ctr": round(int(row.get("gsc_clicks") or 0) / impressions, 6),
            "opportunity_id": row.get("opportunity_id"),
            "opportunity_score": float(row.get("score") or 0),
            "search_volume": row.get("search_volume"),
            "keyword_difficulty": row.get("keyword_difficulty"),
            "our_dataforseo_position": row.get("our_dataforseo_position"),
            "competitor_position": row.get("competitor_position"),
            "dataforseo_measured": any(
                row.get(field) is not None
                for field in (
                    "search_volume", "keyword_difficulty",
                    "our_dataforseo_position", "competitor_position",
                )
            ),
        })

    merged = {route: dict(row) for route, row in metrics.items()}
    for route, queries in grouped.items():
        # The first query becomes the authority's target_query. Prefer measured
        # DataForSEO white-space value over raw GSC impression volume so packet
        # research and substantive rewrites answer the best gap, not merely the
        # loudest already-observed phrase.
        queries.sort(key=lambda row: (
            0 if row["dataforseo_measured"] else 1,
            -float(row.get("opportunity_score") or 0),
            -int(row.get("search_volume") or 0),
            -int(row["impressions"]),
            row["position"],
            row["query"],
        ))
        impressions = sum(int(row["impressions"]) for row in queries)
        clicks = sum(int(row["clicks"]) for row in queries)
        weighted_position = sum(float(row["position"]) * int(row["impressions"])
                                for row in queries)
        previous = merged.get(route) or {}
        measured = [row for row in queries if row["dataforseo_measured"]]
        difficulty = [float(row["keyword_difficulty"]) for row in measured
                      if row.get("keyword_difficulty") is not None]
        merged[route] = {
            **previous,
            "current_clicks": clicks,
            "previous_clicks": int(previous.get("previous_clicks") or 0),
            "impressions": impressions,
            "affiliate_clicks": int(previous.get("affiliate_clicks") or 0),
            "position": round(weighted_position / impressions, 4),
            "ctr": round(clicks / impressions, 6),
            "click_decay_fraction": float(previous.get("click_decay_fraction") or 0),
            "queries": queries,
            "keyword_gap_class_a_overlay": True,
            "dataforseo_relevance_score": max(float(row["opportunity_score"]) for row in queries),
            "dataforseo_search_volume": sum(int(row.get("search_volume") or 0) for row in measured),
            "dataforseo_keyword_difficulty": min(difficulty) if difficulty else None,
            "dataforseo_measured_query_count": len(measured),
            "keyword_gap_opportunity_ids": sorted(str(row["opportunity_id"]) for row in queries),
        }
    meta.update({
        "state": "applied", "routes": len(grouped),
        "queries": sum(len(rows) for rows in grouped.values()),
        "sha256": sha(opportunity_path.resolve(strict=True)),
        "artifact_sha256": expected,
        "as_of": payload.get("as_of"),
        "dataforseo_measured_queries": sum(
            sum(1 for row in rows if row["dataforseo_measured"])
            for rows in grouped.values()
        ),
        "ranking_model": (
            "class_a_dataforseo_measured_first_then_opportunity_score_"
            "then_search_volume_then_gsc_position"
        ),
    })
    return merged, meta


def build_selection_receipt(
    *,
    database: Path,
    release_root: Path,
    as_of: dt.date | None = None,
    keyword_gap_opportunities: Path = KEYWORD_GAP_OPPORTUNITIES,
    weekly_strategy_path: Path = WEEKLY_STRATEGY_PATH,
) -> dict[str, Any]:
    policy = consumer_policy("existing_article_updates", decision_class="search")
    require_allowed(policy)
    search_policy = {**policy, "mode": "PASS_SEARCH_ONLY", "revenue_blind": True}
    metrics, evidence = load_observability(database, window_end=as_of)
    end = dt.date.fromisoformat(evidence["window_end"]) if evidence.get("window_end") else (as_of or dt.date.today())
    articles, _catalogs = load_release_articles(release_root)
    article_routes = {canonical_route(row.get("canonical_path")) for row in articles}
    metrics, class_a_evidence = apply_keyword_gap_class_a(
        metrics,
        opportunity_path=keyword_gap_opportunities,
        current_routes=article_routes,
    )
    candidates = select_update_candidates(articles, metrics, as_of=end)
    # Strategy freshness is wall-clock based, not the lagged GSC window end.
    strategy, strategy_meta = load_weekly_strategy(weekly_strategy_path, as_of=as_of)
    week = weekly_plan(candidates["r2_candidates"], cogny_strategy=strategy)
    strict_selected = list(week["selected"])
    reserve = select_refresh_cadence_reserve(
        articles,
        metrics,
        as_of=end,
        excluded_routes={row["route"] for row in strict_selected},
    )
    # The review governs eligibility; arbitrary freshness reserves are not
    # dispatch orders. Targets are aspirational, not forced date bumps.
    from seo_weekly_governance import CADENCE, refresh_candidates
    strict_selected = refresh_candidates([*strict_selected, *reserve], strategy)[:CADENCE["meaningful_updates_max_per_week"]]
    week["required"] = CADENCE["meaningful_updates_min_per_week"]
    week["cadence"] = CADENCE
    reserve_selected = []
    week["strict_selected_count"] = len(strict_selected)
    week["strict_capacity_deficit"] = week["capacity_deficit"]
    week["cadence_reserve"] = {
        "authority": "operator_freshness_cadence_2026-08-15",
        "eligible": len(reserve),
        "selected": len(reserve_selected),
        "routes": [row["route"] for row in reserve_selected],
    }
    week["selected"] = [*strict_selected, *reserve_selected]
    week["selected_count"] = len(week["selected"])
    week["capacity_deficit"] = max(0, week["required"] - week["selected_count"])
    week["status"] = "PASS_WEEKLY_R2_PLAN" if week["capacity_deficit"] == 0 else "PASS_WEEKLY_REVIEW_NOOP_ALLOWED"
    reserve_routes = {row["route"] for row in reserve_selected}
    analytics_decisions = []
    for row in week["selected"]:
        analytics_decisions.append(write_consumer_decision(
            consumer="existing_article_updates",
            decision_type="existing_article_update",
            subject=str(row["route"]),
            action=str(row["action"]),
            pre_signal_score=float({"P1": 4, "P2": 3, "P3": 2, "P4": 1}[row["priority"]]),
            components=[
                {"signal": "gsc_impressions", "available": True,
                 "delta": round(min(2.0, int(row.get("impressions") or 0) / 100.0), 6)},
                {"signal": "click_decay", "available": True,
                 "delta": round(float(row.get("click_decay_fraction") or 0), 6)},
                {"signal": "tracked_affiliate_clicks", "available": True,
                 "delta": round(min(2.0, int(row.get("affiliate_clicks_28d") or 0) / 5.0), 6)},
                {"signal": "normalized_revenue", "available": False, "delta": None,
                 "reason": "search_update_decision_is_revenue_blind"},
            ],
            policy=search_policy,
        ))
    payload = {
        "schema": "glp-seo-existing-article-update-selection-v1",
        "status": week["status"],
        "as_of": end.isoformat(),
        "data_quality_mode": "PASS_SEARCH_ONLY",
        "revenue_blind": True,
        "analytics_decisions": analytics_decisions,
        "observability": evidence,
        "keyword_gap_class_a": class_a_evidence,
        "cogny_weekly_strategy": {
            **strategy_meta,
            "consumer": "existing_article_updates.weekly_plan",
            "selected_preference_count": sum(bool(row.get("cogny_strategy_preference")) for row in week["selected"]),
            "selected_routes": [row["route"] for row in week["selected"]],
            "advice_only": True,
        },
        "current_release": {"path": str(release_root.resolve()), "articles": len(articles)},
        "weekly_plan": week,
        "p5_net_new_spokes": [
            row for row in candidates["p5_net_new_spokes"]
            if row["route"] not in reserve_routes
        ],
        "effects": {"page_mutations": 0, "deployments": 0, "redirects": 0},
    }
    payload["receipt_sha256"] = payload_hash(payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, default=OBSERVABILITY_DB)
    parser.add_argument("--release-root", type=Path, required=True)
    parser.add_argument("--as-of", type=dt.date.fromisoformat)
    parser.add_argument("--keyword-gap-opportunities", type=Path,
                        default=KEYWORD_GAP_OPPORTUNITIES)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    payload = build_selection_receipt(
        database=args.database,
        release_root=args.release_root,
        as_of=args.as_of,
        keyword_gap_opportunities=args.keyword_gap_opportunities,
    )
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(json.dumps({
        "status": payload["status"],
        "selected": payload["weekly_plan"]["selected_count"],
        "capacity_deficit": payload["weekly_plan"]["capacity_deficit"],
        "p5_spokes": len(payload["p5_net_new_spokes"]),
        "receipt_sha256": payload["receipt_sha256"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
