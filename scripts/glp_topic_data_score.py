#!/usr/bin/env python3
"""Score topic candidates and refresh routes from first-party demand receipts.

This is the registered n8n bridge ``topic_data_score`` action: the cron bridge
runs it after every ``topic_supply_mint`` (``scripts/cron_bridge_action.py``)
and the strategy selector (``select_opus_content_queue.load_demand_multipliers``)
folds the sidecar it writes into topic ordering, fail-open and unchanged.

It reads only receipts the system already collects, never a provider:

* the analytics-brain canonical source cache (``glp-analytics-source-cache-v1``)
  for GSC query/page rows on the exact ``sc-domain`` property, Bing Webmaster
  query/page stats for the exact site, and GA4 page sessions on the production
  host; each source carries its own collector state, identity and watermark;
* the weekly GSC sitewide aggregate (``glp.weekly-gsc-sitewide-aggregate.v1``)
  for the exact property, finalized (``data_state=final``) day window that
  anchors the score and its denominator.

No DataForSEO, Cogny, or model scraping is consulted. A metric that no receipt
reports stays ``null`` with an ``UNKNOWN_*`` state; it is never written as zero.
Effects: local files only. No network, sends, or deploys.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import re
import tempfile
from pathlib import Path
from typing import Any

BUSINESS = Path("/home/ubuntu/business")
SEO_ROOT = BUSINESS / "reports/glp-clinic-scout/seo-machine"
DEFAULT_SOURCE_CACHE = BUSINESS / "reports/glp-clinic-scout/analytics/current/canonical-source-cache.json"
DEFAULT_WEEKLY_REPORT_ROOT = SEO_ROOT / "weekly-ranking-report"
DEFAULT_STRATEGY_QUEUE = BUSINESS / "ops/glp/topic-supply-inputs/strategy-queue.json"
DEFAULT_TOPIC_QUEUE = BUSINESS / "reports/glp-clinic-scout/seo-blog-machine-20260730/topic-queues/latest.json"
DEFAULT_OUTPUT_DIR = SEO_ROOT / "topic-data-score"
SIDECAR_NAME = "demand-multipliers.json"
SIDECAR_SCHEMA = "glp-topic-demand-multiplier-v1"
RECEIPT_SCHEMA = "glp-topic-first-party-demand-score-v1"
MODEL = "first-party-receipts-v1"
SOURCE_CACHE_SCHEMA = "glp-analytics-source-cache-v1"
AGGREGATE_SCHEMA = "glp.weekly-gsc-sitewide-aggregate.v1"
STRATEGY_QUEUE_SCHEMA = "glp-seo-cluster-rotation-queue-v1"
TOPIC_QUEUE_SCHEMA = "seo-blog-machine-topic-queue-v1"
# Exact identities the receipts must carry; anything else is UNKNOWN, not data.
GSC_PROPERTY = "sc-domain:glpclinicscout.com"
CONSUMER_PROPERTY = "https://glpclinicscout.com/"
BING_SITE = "https://glpclinicscout.com"
PRODUCTION_HOST = "glpclinicscout.com"
EXPECTED_SOURCE_SCHEMAS = {"gsc": "gsc-searchanalytics-v3", "bing": "bing-webmaster-v1", "ga4": "ga4-data-v1beta"}
AGGREGATE_SCOPES = {"consumer_https_apex": CONSUMER_PROPERTY, "domain_including_partners": GSC_PROPERTY}
AGGREGATE_FIELDS = ("clicks", "impressions", "ctr", "position")
# Bounds the selector enforces on a multiplier row (0.1..5.0); sidecar max age it reads.
MULTIPLIER_MIN, MULTIPLIER_MAX = 0.1, 5.0
SIDECAR_MAX_AGE_HOURS = 96.0
# A cached source older than this is UNKNOWN here even if the collector has not
# re-marked it; the collector's own tighter per-source thresholds still apply first.
SOURCE_MAX_AGE_HOURS = 168.0
AGGREGATE_MAX_AGE_DAYS = 21
# Same striking-distance band and hub-gap overlap rule the selector applies.
STRIKING_POSITION = (11.0, 40.0)
STRIKING_BONUS = 0.5
OVERLAP_FULL, OVERLAP_HALF = 0.5, 0.25
ALLOWED_ROUTE = re.compile(r"^/(?:guides|best|reviews|cost|tools|compare)/[a-z0-9]+(?:-[a-z0-9]+)*/$")
QUERY_STOPWORDS = {"a", "an", "and", "are", "for", "how", "is", "of", "or", "the",
                   "to", "what", "where", "with"}


def normalize(value: Any) -> str:
    """Identical to the selector's topic key so sidecar rows join exactly."""
    text = re.sub(r"\bglp\s*-?\s*1\b", "glp1", str(value or "").lower())
    return " ".join(re.findall(r"[a-z0-9]+", text))


def query_tokens(value: str) -> frozenset[str]:
    return frozenset(token for token in normalize(value).split() if token not in QUERY_STOPWORDS)


def canonical(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def payload_hash(value: dict[str, Any], field: str = "receipt_sha256") -> str:
    unsigned = dict(value)
    unsigned.pop(field, None)
    return hashlib.sha256(canonical(unsigned)).hexdigest()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_utc(value: Any) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def finite_number(value: Any) -> float | None:
    """A reported numeric metric; booleans, text and non-finite values are unknown."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def read_object(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def file_spec(path: Path) -> dict[str, str]:
    return {"path": str(path), "sha256": sha256_file(path)}


# ---------------------------------------------------------------------------
# Receipt readers. Each returns tables plus a state; UNKNOWN states carry a reason.
# ---------------------------------------------------------------------------

def load_source_cache(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    meta: dict[str, Any] = {"path": str(path), "state": "UNKNOWN_ABSENT"}
    if not path.is_file():
        return {}, meta
    payload = read_object(path)
    if payload is None:
        meta["state"] = "UNKNOWN_UNREADABLE"
        return {}, meta
    if payload.get("schema") != SOURCE_CACHE_SCHEMA:
        meta.update(state="UNKNOWN_SCHEMA", observed_schema=payload.get("schema"))
        return {}, meta
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("sources"), dict):
        meta["state"] = "UNKNOWN_DATA_SHAPE"
        return {}, meta
    meta.update(state="PRESENT", sha256=sha256_file(path),
                updated_at_utc=payload.get("updated_at_utc"), last_phase=payload.get("last_phase"))
    return data, meta


def source_state(sources: dict[str, Any], name: str, *, now: dt.datetime) -> dict[str, Any]:
    """Admit one cached source only on its own healthy state and exact identity."""
    source = sources.get(name)
    if not isinstance(source, dict):
        return {"state": "UNKNOWN_SOURCE_ABSENT"}
    state = str(source.get("state") or "absent")
    if state not in ("healthy", "measured_zero"):
        return {"state": f"UNKNOWN_SOURCE_{state.upper()}", "reason": source.get("reason")}
    if source.get("observed_schema") != EXPECTED_SOURCE_SCHEMAS[name]:
        return {"state": "UNKNOWN_SOURCE_SCHEMA_DRIFT", "observed_schema": source.get("observed_schema")}
    if source.get("complete") is not True:
        return {"state": "UNKNOWN_SOURCE_INCOMPLETE"}
    identity: dict[str, Any]
    if name == "gsc":
        window = source.get("collection_window") or {}
        if source.get("property") != GSC_PROPERTY:
            return {"state": "UNKNOWN_PROPERTY_MISMATCH", "observed_property": source.get("property")}
        if not (isinstance(window, dict) and window.get("start") and window.get("end")):
            return {"state": "UNKNOWN_WINDOW_MISSING"}
        identity = {"property": GSC_PROPERTY, "window": {"start": window["start"], "end": window["end"]}}
    elif name == "bing":
        if source.get("site") != BING_SITE:
            return {"state": "UNKNOWN_SITE_MISMATCH", "observed_site": source.get("site")}
        identity = {"site": BING_SITE}
    else:
        if source.get("host_scope") != PRODUCTION_HOST:
            return {"state": "UNKNOWN_HOST_MISMATCH", "observed_host": source.get("host_scope")}
        identity = {"host": PRODUCTION_HOST}
    readback = parse_utc(source.get("last_successful_readback"))
    if readback is None:
        return {"state": "UNKNOWN_READBACK_MISSING"}
    age_hours = round((now - readback).total_seconds() / 3600.0, 2)
    if age_hours < -1 or age_hours > SOURCE_MAX_AGE_HOURS:
        return {"state": "UNKNOWN_STALE", "age_hours": age_hours, "max_age_hours": SOURCE_MAX_AGE_HOURS}
    return {
        "state": "SOURCE_REPORTED", **identity,
        "collector_state": state, "actual_rows": source.get("actual_rows"),
        "watermark": source.get("watermark"), "watermark_kind": source.get("watermark_kind"),
        "last_successful_readback": source.get("last_successful_readback"), "age_hours": age_hours,
    }


def _bucket(table: dict[str, dict[str, Any]], key: str) -> dict[str, Any]:
    return table.setdefault(key, {"impressions": 0, "clicks": 0, "position_numerator": 0.0,
                                  "position_weight": 0, "rows": 0, "unknown_metric_rows": 0})


def _accumulate(bucket: dict[str, Any], impressions: Any, clicks: Any, position: Any) -> None:
    imp, clk = finite_number(impressions), finite_number(clicks)
    if imp is None or clk is None:
        bucket["unknown_metric_rows"] += 1
        return
    bucket["rows"] += 1
    bucket["impressions"] += int(imp)
    bucket["clicks"] += int(clk)
    pos = finite_number(position)
    if pos is not None and pos > 0 and imp > 0:
        bucket["position_numerator"] += pos * imp
        bucket["position_weight"] += int(imp)


def _finish(table: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    for bucket in table.values():
        numerator = bucket.pop("position_numerator")
        weight = bucket.pop("position_weight")
        if bucket["rows"] == 0:
            # Every provider row for this key carried null metrics: unknown, not zero.
            bucket.update(impressions=None, clicks=None, position=None, unknown=True)
            continue
        bucket["position"] = round(numerator / weight, 4) if weight else None
        bucket["unknown"] = False
    return table


def gsc_tables(rows: list[Any]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Query/page aggregation of the cached GSC page+query rows (exact property)."""
    queries: dict[str, dict[str, Any]] = {}
    pages: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        path = str(row.get("path") or "")
        if path:
            bucket = _bucket(pages, path)
            _accumulate(bucket, row.get("impressions"), row.get("clicks"), row.get("position"))
            bucket.setdefault("queries", set()).add(normalize(row.get("query")) or "(anonymized)")
        key = normalize(row.get("query"))
        if key:
            bucket = _bucket(queries, key)
            _accumulate(bucket, row.get("impressions"), row.get("clicks"), row.get("position"))
            bucket.setdefault("pages", set()).add(path or "/")
    for bucket in pages.values():
        bucket["query_count"] = len(bucket.pop("queries", set()))
    for bucket in queries.values():
        bucket["pages"] = sorted(bucket.pop("pages", set()))
    return _finish(queries), _finish(pages)


def bing_tables(query_rows: list[Any], page_rows: list[Any]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    queries: dict[str, dict[str, Any]] = {}
    pages: dict[str, dict[str, Any]] = {}
    for row in query_rows:
        if isinstance(row, dict) and normalize(row.get("query")):
            _accumulate(_bucket(queries, normalize(row.get("query"))),
                        row.get("impressions"), row.get("clicks"), row.get("avg_position"))
    for row in page_rows:
        if isinstance(row, dict) and row.get("path"):
            _accumulate(_bucket(pages, str(row["path"])),
                        row.get("impressions"), row.get("clicks"), row.get("position"))
    return _finish(queries), _finish(pages)


def ga4_pages(rows: list[Any]) -> dict[str, dict[str, Any]]:
    """Production-host page_view rows: page views, sessions and engaged sessions.

    Rows are per report minute, so a session spanning minutes counts once per
    minute; this is an engagement diagnostic for refresh ordering, not a user
    count or denominator.
    """
    pages: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("host") != PRODUCTION_HOST or row.get("event_name") != "page_view":
            continue
        path = str(row.get("path") or "")
        views, sessions, engaged = (finite_number(row.get(key)) for key in ("event_count", "sessions", "engaged_sessions"))
        if not path or views is None or sessions is None or engaged is None:
            continue
        bucket = pages.setdefault(path, {"page_views": 0, "sessions": 0, "engaged_sessions": 0, "rows": 0})
        bucket["page_views"] += int(views)
        bucket["sessions"] += int(sessions)
        bucket["engaged_sessions"] += int(engaged)
        bucket["rows"] += 1
    return pages


def latest_sitewide_aggregate(root: Path) -> Path | None:
    if not root.is_dir():
        return None
    candidates = sorted(path for path in root.glob("*-gsc-sitewide-aggregate.json")
                        if re.fullmatch(r"\d{4}-\d{2}-\d{2}-gsc-sitewide-aggregate\.json", path.name))
    return candidates[-1] if candidates else None


def load_sitewide_aggregate(path: Path | None, *, now: dt.datetime) -> dict[str, Any]:
    """Exact-property, finalized-window site totals; every gap stays UNKNOWN."""
    if path is None:
        return {"state": "UNKNOWN_ABSENT", "path": None}
    meta: dict[str, Any] = {"path": str(path)}
    payload = read_object(path)
    if payload is None or payload.get("schema") != AGGREGATE_SCHEMA:
        return {**meta, "state": "UNKNOWN_SCHEMA"}
    scopes = payload.get("scopes")
    if not isinstance(scopes, dict) or set(scopes) != set(AGGREGATE_SCOPES):
        return {**meta, "state": "UNKNOWN_SCOPES"}
    try:
        report_date = dt.date.fromisoformat(path.name[:10])
    except ValueError:
        return {**meta, "state": "UNKNOWN_REPORT_DATE"}
    age_days = (now.date() - report_date).days
    output: dict[str, Any] = {}
    window: dict[str, Any] | None = None
    for label, property_uri in AGGREGATE_SCOPES.items():
        item = scopes[label]
        context = item.get("context") if isinstance(item, dict) else None
        if (not isinstance(context, dict) or context.get("property") != property_uri
                or context.get("data_state") != "final" or context.get("dimensions") != []
                or context.get("aggregation_type") != "byProperty" or context.get("search_type") != "web"
                or not context.get("start_date") or not context.get("end_date")):
            return {**meta, "state": "UNKNOWN_CONTEXT:" + label}
        scope_window = {"start": context["start_date"], "end": context["end_date"],
                        "days": context.get("window_days"), "data_state": "final",
                        "timezone": context.get("timezone")}
        if window is not None and window != scope_window:
            return {**meta, "state": "UNKNOWN_WINDOW_MISMATCH"}
        window = scope_window
        totals = item.get("totals") if isinstance(item.get("totals"), dict) else {}
        reported = item.get("state") == "SOURCE_REPORTED" and all(
            finite_number(totals.get(key)) is not None and finite_number(totals.get(key)) >= 0
            for key in AGGREGATE_FIELDS)
        output[label] = {
            "property": property_uri,
            "state": item.get("state") if isinstance(item.get("state"), str) else "UNKNOWN_STATE",
            "totals": {key: totals.get(key) for key in AGGREGATE_FIELDS} if reported else dict.fromkeys(AGGREGATE_FIELDS),
            "missing_dates": list(item.get("missing_dates") or []),
            "captured_at_utc": item.get("captured_at_utc"),
        }
    meta.update(sha256=sha256_file(path), report_date=report_date.isoformat(), age_days=age_days,
                window=window, scopes=output)
    if age_days < 0 or age_days > AGGREGATE_MAX_AGE_DAYS:
        return {**meta, "state": "UNKNOWN_STALE", "max_age_days": AGGREGATE_MAX_AGE_DAYS}
    return {**meta, "state": "SOURCE_REPORTED"}


# ---------------------------------------------------------------------------
# Candidates: strategy queue rows plus the ranked topic queue, one row per key.
# ---------------------------------------------------------------------------

def load_candidates(strategy_queue: Path, topic_queue: Path | None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    meta: dict[str, Any] = {"strategy_queue": {"path": str(strategy_queue)}, "topic_queue": {"path": str(topic_queue) if topic_queue else None},
                            "duplicates_removed": [], "skipped": []}
    strategy = read_object(strategy_queue)
    if strategy is None or strategy.get("schema") != STRATEGY_QUEUE_SCHEMA:
        raise RuntimeError(f"strategy_queue_contract_invalid:{strategy_queue}")
    meta["strategy_queue"].update(sha256=sha256_file(strategy_queue), rows=len(strategy.get("queue") or []))
    raw: list[dict[str, Any]] = []
    for row in strategy.get("queue") or []:
        if not isinstance(row, dict):
            continue
        raw.append({
            "origin": "strategy_queue", "keyword": str(row.get("target_query") or ""),
            "target_route": str(row.get("target_route") or ""), "cluster_id": row.get("cluster_id"),
            "queue_position": row.get("queue_position"), "depth_stage": row.get("depth_stage"),
            "current_route_status": row.get("current_route_status"), "topic_id": None,
        })
    if topic_queue is not None and topic_queue.is_file():
        topics = read_object(topic_queue)
        if topics is None or topics.get("schema") != TOPIC_QUEUE_SCHEMA:
            meta["topic_queue"]["state"] = "UNKNOWN_SCHEMA"
        else:
            meta["topic_queue"].update(state="PRESENT", sha256=sha256_file(topic_queue), rows=len(topics.get("queue") or []))
            for row in topics.get("queue") or []:
                if not isinstance(row, dict):
                    continue
                directive = row.get("competitor_gap") if isinstance(row.get("competitor_gap"), dict) else {}
                raw.append({
                    "origin": "topic_queue", "keyword": str(row.get("target_query") or ""),
                    "target_route": str(directive.get("target_route") or ""), "cluster_id": row.get("cluster_id"),
                    "queue_position": None, "depth_stage": None, "current_route_status": None,
                    "topic_id": row.get("topic_id"),
                })
    elif topic_queue is not None:
        meta["topic_queue"]["state"] = "UNKNOWN_ABSENT"
    candidates: list[dict[str, Any]] = []
    seen: dict[str, dict[str, Any]] = {}
    for row in raw:
        key = normalize(row["keyword"])
        if not key:
            meta["skipped"].append({"origin": row["origin"], "reason": "empty_query", "topic_id": row["topic_id"]})
            continue
        if not ALLOWED_ROUTE.fullmatch(row["target_route"]):
            row["target_route"] = None
        if key in seen:
            meta["duplicates_removed"].append({"key": key, "origin": row["origin"], "topic_id": row["topic_id"],
                                               "kept_origin": seen[key]["origin"]})
            continue
        row["key"] = key
        seen[key] = row
        candidates.append(row)
    return candidates, meta


# ---------------------------------------------------------------------------
# Scoring: exact-key aggregation plus the selector's hub-gap overlap rule.
# ---------------------------------------------------------------------------

def demand_for(key: str, table: dict[str, dict[str, Any]]) -> dict[str, Any]:
    tokens = query_tokens(key)
    exact = table.get(key)
    if exact is not None and exact.get("unknown"):
        return {"state": "UNKNOWN_METRICS_NULL", "exact_observed": True, "exact_impressions": None,
                "exact_clicks": None, "exact_position": None, "related_impressions_weighted": None,
                "related_query_count": None, "related_queries": [], "weighted_impressions": None}
    related: list[dict[str, Any]] = []
    related_weight = 0.0
    for other, row in table.items():
        if other == key or not tokens or row.get("unknown"):
            continue
        other_tokens = query_tokens(other)
        union = tokens | other_tokens
        if not union:
            continue
        overlap = len(tokens & other_tokens) / len(union)
        factor = 1.0 if overlap >= OVERLAP_FULL else 0.5 if overlap >= OVERLAP_HALF else 0.0
        if factor <= 0 or int(row["impressions"]) <= 0:
            continue
        contribution = round(int(row["impressions"]) * factor, 4)
        related_weight += contribution
        related.append({"query": other, "impressions": row["impressions"], "clicks": row["clicks"],
                        "position": row["position"], "overlap": round(overlap, 3), "weight": contribution})
    related.sort(key=lambda row: (-row["weight"], row["query"]))
    exact_impressions = int(exact["impressions"]) if exact else 0
    return {
        "state": "SOURCE_REPORTED",
        "exact_observed": exact is not None,
        "exact_impressions": exact_impressions,
        "exact_clicks": int(exact["clicks"]) if exact else 0,
        "exact_position": exact["position"] if exact else None,
        "related_impressions_weighted": round(related_weight, 4),
        "related_query_count": len(related),
        "related_queries": related[:10],
        "weighted_impressions": round(exact_impressions + related_weight, 4),
    }


def clamp_multiplier(value: float) -> float:
    return round(min(MULTIPLIER_MAX, max(MULTIPLIER_MIN, value)), 4)


def score_topics(candidates: list[dict[str, Any]], tables: dict[str, Any], states: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for candidate in candidates:
        gsc = demand_for(candidate["key"], tables["gsc_queries"]) if states["gsc"]["state"] == "SOURCE_REPORTED" else None
        bing = demand_for(candidate["key"], tables["bing_queries"]) if states["bing"]["state"] == "SOURCE_REPORTED" else None
        reported = [row for row in (gsc, bing) if row is not None and row["weighted_impressions"] is not None]
        if reported:
            weighted = round(sum(row["weighted_impressions"] for row in reported), 4)
            bonus = STRIKING_BONUS if (gsc and gsc["exact_impressions"] > 0 and gsc["exact_position"] is not None
                                       and STRIKING_POSITION[0] <= gsc["exact_position"] <= STRIKING_POSITION[1]) else 0.0
            multiplier: float | None = clamp_multiplier(1.0 + math.log10(1.0 + weighted) + bonus)
            state = "SOURCE_REPORTED" if weighted > 0 else "SOURCE_REPORTED_NO_OBSERVED_DEMAND"
        else:
            weighted, bonus, multiplier = None, None, None
            state = ("UNKNOWN_METRICS_NULL" if any(row is not None for row in (gsc, bing))
                     else "UNKNOWN_NO_FIRST_PARTY_SOURCE")
        rows.append({
            "keyword": candidate["keyword"], "key": candidate["key"], "target_route": candidate["target_route"],
            "origin": candidate["origin"], "topic_id": candidate["topic_id"], "cluster_id": candidate["cluster_id"],
            "queue_position": candidate["queue_position"], "depth_stage": candidate["depth_stage"],
            "current_route_status": candidate["current_route_status"],
            "state": state, "multiplier": multiplier, "weighted_impressions": weighted, "striking_bonus": bonus,
            "gsc": gsc, "bing": bing,
        })
    rows.sort(key=lambda row: (row["multiplier"] is None, -(row["multiplier"] or 0.0),
                               row["queue_position"] is None, row["queue_position"] or 0, row["key"]))
    rank = 0
    for row in rows:
        if row["multiplier"] is not None:
            rank += 1
            row["rank"] = rank
        else:
            row["rank"] = None
    return rows


def _page_metric(table: dict[str, dict[str, Any]], route: str, reported: bool, fields: tuple[str, ...]) -> dict[str, Any] | None:
    if not reported:
        return None
    row = table.get(route)
    if row is None:
        return {"state": "SOURCE_REPORTED_NO_ROWS", **{field: 0 for field in fields if field != "position"},
                **({"position": None} if "position" in fields else {})}
    if row.get("unknown"):
        return {"state": "UNKNOWN_METRICS_NULL", **dict.fromkeys(fields)}
    return {"state": "SOURCE_REPORTED", **{field: row.get(field) for field in fields}}


def score_refreshes(candidates: list[dict[str, Any]], tables: dict[str, Any], states: dict[str, Any]) -> list[dict[str, Any]]:
    """Route-keyed refresh demand across GSC pages, Bing pages and GA4 engagement."""
    reported = {name: states[name]["state"] == "SOURCE_REPORTED" for name in ("gsc", "bing", "ga4")}
    routes: set[str] = set()
    for name, table in (("gsc", tables["gsc_pages"]), ("bing", tables["bing_pages"]), ("ga4", tables["ga4_pages"])):
        if reported[name]:
            routes |= {route for route in table if ALLOWED_ROUTE.fullmatch(route)}
    queue_refresh = {row["target_route"]: row for row in candidates
                     if row["target_route"] and row["current_route_status"] == "refresh"}
    routes |= set(queue_refresh)
    rows: list[dict[str, Any]] = []
    for route in sorted(routes):
        gsc = _page_metric(tables["gsc_pages"], route, reported["gsc"], ("impressions", "clicks", "position", "query_count"))
        bing = _page_metric(tables["bing_pages"], route, reported["bing"], ("impressions", "clicks", "position"))
        ga4 = _page_metric(tables["ga4_pages"], route, reported["ga4"], ("page_views", "sessions", "engaged_sessions"))
        search = [row for row in (gsc, bing) if row is not None and row["state"] != "UNKNOWN_METRICS_NULL"]
        if ga4 is not None and ga4["state"] == "UNKNOWN_METRICS_NULL":
            ga4_known = None
        else:
            ga4_known = ga4
        if search or ga4_known is not None:
            impressions = sum(int(row["impressions"]) for row in search) if search else None
            engagement = None
            if ga4_known is not None:
                engagement = (round(min(1.0, int(ga4_known["engaged_sessions"]) / int(ga4_known["sessions"])), 4)
                              if int(ga4_known["sessions"]) > 0 else 0.0)
            score: float | None = round(1.0 + math.log10(1.0 + (impressions or 0)) + (engagement or 0.0), 4)
            observed = (impressions or 0) > 0 or (ga4_known is not None and int(ga4_known["sessions"]) > 0)
            state = "SOURCE_REPORTED" if observed else "SOURCE_REPORTED_NO_OBSERVED_DEMAND"
        else:
            impressions, engagement, score, state = None, None, None, "UNKNOWN_NO_FIRST_PARTY_SOURCE"
        queue_row = queue_refresh.get(route)
        rows.append({
            "route": route, "state": state, "refresh_score": score, "search_impressions": impressions,
            "engagement_fraction": engagement, "gsc": gsc, "bing": bing, "ga4": ga4,
            "queue_keyword": queue_row["keyword"] if queue_row else None,
            "queue_position": queue_row["queue_position"] if queue_row else None,
        })
    rows.sort(key=lambda row: (row["refresh_score"] is None, -(row["refresh_score"] or 0.0), row["route"]))
    rank = 0
    for row in rows:
        if row["refresh_score"] is not None:
            rank += 1
            row["rank"] = rank
        else:
            row["rank"] = None
    return rows


# ---------------------------------------------------------------------------
# Writers: atomic sidecar replace, immutable dated receipt.
# ---------------------------------------------------------------------------

def write_atomic(path: Path, payload: dict[str, Any]) -> Path:
    target = path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False).encode("utf-8") + b"\n"
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def write_immutable(path: Path, payload: dict[str, Any]) -> Path:
    target = path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False).encode("utf-8") + b"\n"
    if target.exists():
        if target.read_bytes() != rendered:
            raise RuntimeError(f"topic_score_receipt_immutable_conflict:{target}")
        return target
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o444)
        os.link(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def run(*, source_cache: Path, weekly_report_root: Path, strategy_queue: Path, topic_queue: Path | None,
        output_dir: Path, now: dt.datetime | None = None) -> dict[str, Any]:
    now = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc).replace(microsecond=0)
    candidates, candidate_meta = load_candidates(strategy_queue, topic_queue)
    data, cache_meta = load_source_cache(source_cache)
    sources = data.get("sources") if isinstance(data.get("sources"), dict) else {}
    states = {name: source_state(sources, name, now=now) for name in ("gsc", "bing", "ga4")}
    if cache_meta["state"] != "PRESENT":
        states = {name: {"state": "UNKNOWN_SOURCE_CACHE_" + cache_meta["state"].removeprefix("UNKNOWN_")} for name in states}
    gsc_queries, gsc_pages = gsc_tables(list(data.get("gsc_rows") or []))
    bing_queries, bing_pages = bing_tables(list(data.get("bing_query_rows") or []), list(data.get("bing_rows") or []))
    tables = {"gsc_queries": gsc_queries, "gsc_pages": gsc_pages, "bing_queries": bing_queries,
              "bing_pages": bing_pages, "ga4_pages": ga4_pages(list(data.get("ga4_rows") or []))}
    sitewide = load_sitewide_aggregate(latest_sitewide_aggregate(weekly_report_root), now=now)
    topics = score_topics(candidates, tables, states)
    refreshes = score_refreshes(candidates, tables, states)
    degraded = [f"{name}:{state['state']}" for name, state in states.items() if state["state"] != "SOURCE_REPORTED"]
    if sitewide["state"] != "SOURCE_REPORTED":
        degraded.append("gsc_sitewide_aggregate:" + sitewide["state"])
    scored = [row for row in topics if row["multiplier"] is not None]
    status = ("PASS_FIRST_PARTY_DEMAND_SCORE" if any(state["state"] == "SOURCE_REPORTED" for state in states.values())
              else "DEGRADED_NO_FIRST_PARTY_SOURCE")
    written = now.isoformat()
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    receipt_path = output_dir / "first-party" / f"{stamp}-receipt.json"
    receipt = {
        "schema": RECEIPT_SCHEMA, "machine": "topic_data_score", "status": status, "model": MODEL,
        "generated_at_utc": written, "effects": {"network_calls": 0, "provider_calls": 0, "sends": 0, "deploys": 0,
                                                "model_scraping": False},
        "inputs": {"source_cache": cache_meta, "gsc_sitewide_aggregate": sitewide, **candidate_meta},
        "sources": states, "degraded": degraded,
        "candidates": len(candidates), "multipliers_written": len(scored),
        "topics": topics, "refreshes": refreshes,
        "rules": {"topic_key": "normalize(target_query), identical to the selector join key",
                  "multiplier": "clamp(1 + log10(1 + weighted first-party impressions) + striking bonus, 0.1, 5.0); null when no source reported",
                  "hub_gap_overlap": {"full_weight": OVERLAP_FULL, "half_weight": OVERLAP_HALF},
                  "striking_bonus": {"position": list(STRIKING_POSITION), "bonus": STRIKING_BONUS},
                  "refresh_score": "1 + log10(1 + page impressions) + GA4 engaged-session fraction; null when no source reported",
                  "unknown_never_zero": True},
    }
    receipt["receipt_sha256"] = payload_hash(receipt)
    receipt_file = write_immutable(receipt_path, receipt)
    sidecar = {
        "schema": SIDECAR_SCHEMA, "written_utc": written, "max_age_hours": SIDECAR_MAX_AGE_HOURS,
        "generator": "scripts/glp_topic_data_score.py", "model": MODEL, "status": status, "degraded": degraded,
        "sources": {name: {"state": state["state"], **{key: state[key] for key in ("property", "site", "host", "window", "watermark") if key in state}}
                    for name, state in states.items()},
        "finalized_window": {"state": sitewide["state"], "window": sitewide.get("window"),
                             "scopes": sitewide.get("scopes"), "report_date": sitewide.get("report_date")},
        "multipliers": [{key: row[key] for key in ("keyword", "key", "multiplier", "state", "rank", "weighted_impressions",
                                                   "target_route", "origin", "queue_position", "cluster_id")}
                        for row in topics],
        "refreshes": [{key: row[key] for key in ("route", "refresh_score", "state", "rank", "search_impressions",
                                                 "engagement_fraction", "queue_keyword")}
                      for row in refreshes],
        "receipt": file_spec(receipt_file),
    }
    sidecar_file = write_atomic(output_dir / SIDECAR_NAME, sidecar)
    return {
        "schema": RECEIPT_SCHEMA, "machine": "topic_data_score", "status": status, "model": MODEL,
        "multipliers_written": len(scored), "candidates": len(candidates),
        "duplicates_removed": len(candidate_meta["duplicates_removed"]),
        "top_pick": scored[0]["keyword"] if scored else None,
        "refreshes_scored": sum(1 for row in refreshes if row["refresh_score"] is not None),
        "degraded": degraded, "sources": {name: state["state"] for name, state in states.items()},
        "finalized_window": sitewide.get("window") if sitewide["state"] == "SOURCE_REPORTED" else None,
        "sidecar": file_spec(sidecar_file), "receipt": file_spec(receipt_file),
        "receipt_sha256": receipt["receipt_sha256"], "effects": receipt["effects"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source-cache", type=Path, default=DEFAULT_SOURCE_CACHE)
    parser.add_argument("--weekly-report-root", type=Path, default=DEFAULT_WEEKLY_REPORT_ROOT)
    parser.add_argument("--strategy-queue", type=Path, default=DEFAULT_STRATEGY_QUEUE)
    parser.add_argument("--topic-queue", type=Path, default=DEFAULT_TOPIC_QUEUE)
    parser.add_argument("--no-topic-queue", action="store_true", help="score strategy-queue rows only")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--now", help="ISO-8601 UTC instant for deterministic receipts (tests)")
    args = parser.parse_args(argv)
    now = parse_utc(args.now) if args.now else None
    if args.now and now is None:
        parser.error("--now must be an ISO-8601 instant")
    result = run(source_cache=args.source_cache, weekly_report_root=args.weekly_report_root,
                 strategy_queue=args.strategy_queue, topic_queue=None if args.no_topic_queue else args.topic_queue,
                 output_dir=args.output_dir, now=now)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
