#!/usr/bin/env python3
"""Select one strategy-safe operator-GO topic without network access.

The selector joins the ranked topic feed to the immutable commercial cluster
queue.  Rank score is never authority.  Current GSC striking-distance signals
are only a preference inside the already-safe set; otherwise cluster queue
position is deterministic.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, "/home/ubuntu/business/systems/analytics-brain")
from current_content_base import verify_resolution  # noqa: E402
from machine_config import canonical, load_config  # noqa: E402
from weekly_strategy_consumer import load_strategy as load_cogny_strategy, preference as cogny_preference, preference_score as cogny_preference_score
from selection_receipt import load_selection_receipt, sha256_file, write_immutable_receipt  # noqa: E402
from decision_policy import (  # noqa: E402
    consumer_policy, require_allowed, write_consumer_decision,
)

BUSINESS = Path("/home/ubuntu/business")
DEFAULT_CONFIG = BUSINESS / "systems/seo-blog-machine/config/verticals/glp-clinic-scout.json"
DEFAULT_QUEUE = BUSINESS / "reports/glp-clinic-scout/seo-blog-machine-20260730/topic-queues/latest.json"
# Active strategy inputs are maintained source, not age-retained report output.
DEFAULT_STRATEGY_QUEUE = BUSINESS / "ops/glp/topic-supply-inputs/strategy-queue.json"
DEFAULT_RUNS = BUSINESS / "reports/glp-clinic-scout/seo-machine"
DEFAULT_LEDGER = DEFAULT_RUNS / "published-topic-ledger.json"
DEFAULT_HOLD_LEDGER = DEFAULT_RUNS / "article-hold-ledger.json"
DEFAULT_BUFFER_ROOT = DEFAULT_RUNS / "article-buffer"
DEFAULT_KEYWORD_GAP_OVERLAY = DEFAULT_RUNS / "keyword-gap/current-strategy-overlay.json"
KEYWORD_GAP_OVERLAY_SCHEMA = "glp-keyword-gap-strategy-overlay-v1"
ALLOWED_ROUTE = re.compile(r"^/(?:guides|best|reviews|cost|tools|compare)/[a-z0-9]+(?:-[a-z0-9]+)*/$")


def normalize(value: str) -> str:
    text = re.sub(r"\bglp\s*-?\s*1\b", "glp1", str(value or "").lower())
    return " ".join(re.findall(r"[a-z0-9]+", text))


SEMANTIC_SINGULARS = {
    "companies": "company",
    "programs": "program",
    "providers": "provider",
    "services": "service",
    "websites": "website",
}

BROAD_PROVIDER_TERMS = {"company", "program", "provider", "service", "website"}
BROAD_PROVIDER_COMPARATORS = {"best", "rated", "top"}


def semantic_query_signature(value: str) -> str:
    """Collapse known broad-provider variants before reserving supply."""
    tokens = [SEMANTIC_SINGULARS.get(token, token) for token in normalize(value).split()]
    if (
        "glp1" in tokens
        and any(token in BROAD_PROVIDER_TERMS for token in tokens)
        and any(token in BROAD_PROVIDER_COMPARATORS for token in tokens)
    ):
        return "broad glp1 provider comparison"
    return " ".join(sorted(tokens))


def canonical_route(value: Any) -> str:
    route = str(value or "")
    if not ALLOWED_ROUTE.fullmatch(route):
        raise RuntimeError(f"strategy_route_invalid:{route}")
    return route


def file_spec(path: Path) -> dict[str, str]:
    source = path.resolve(strict=True)
    if not source.is_file():
        raise RuntimeError(f"selection_input_not_file:{source}")
    return {"path": str(source), "sha256": sha256_file(source)}


def read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{label}_invalid:{path}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{label}_not_object:{path}")
    return payload


def has_operator_go(row: dict[str, Any]) -> bool:
    directive = row.get("competitor_gap") or {}
    return bool(
        row.get("evidence_state") == "READY_FOR_RESEARCH"
        and directive.get("operator_ready_for_research") is True
        and isinstance(directive.get("operator_directive_id"), str)
        and directive["operator_directive_id"].strip()
        and isinstance(directive.get("target_route"), str)
    )


def records_at(value: dict[str, Any], key: str) -> list[dict[str, Any]]:
    current: Any = value
    for part in key.split("."):
        current = current[part]
    if not isinstance(current, list):
        raise RuntimeError(f"catalog_records_invalid:{key}")
    return current


def current_catalog_routes(config: dict[str, Any]) -> set[str]:
    routes: set[str] = set()
    for catalog in config["paths"]["content_catalogs"]:
        source = Path(catalog["_resolved_path"])
        for row in records_at(read_object(source, "content_catalog"), catalog["records_key"]):
            for field in catalog.get("route_fields") or []:
                if row.get(field):
                    route = "/" + urlsplit(str(row[field])).path.strip("/") + "/"
                    routes.add(route)
                    break
    return routes


def sitemap_routes(path: Path, expected_host: str) -> set[str]:
    text = path.read_text(encoding="utf-8")
    rows: set[str] = set()
    for location in re.findall(r"<loc>\s*([^<]+?)\s*</loc>", text, flags=re.I):
        parsed = urlsplit(location.strip())
        if parsed.hostname != expected_host:
            raise RuntimeError(f"sitemap_foreign_host:{parsed.hostname}")
        rows.add("/" + parsed.path.strip("/") + "/" if parsed.path != "/" else "/")
    if not rows:
        raise RuntimeError("sitemap_routes_empty")
    return rows


def ledger_terminals(published: dict[str, Any], held: dict[str, Any]) -> tuple[set[str], set[str]]:
    if published.get("schema") != "glp-opus-published-topic-ledger-v1":
        raise RuntimeError("published_topic_ledger_invalid")
    if held.get("schema") != "glp-opus-held-topic-ledger-v1":
        raise RuntimeError("held_topic_ledger_invalid")
    topic_ids: set[str] = set()
    routes: set[str] = set()
    for row in published.get("entries") or []:
        if row.get("status") == "PASS_LIVE":
            if row.get("topic_id"):
                topic_ids.add(str(row["topic_id"]))
            if row.get("route") or row.get("target_route"):
                routes.add(str(row.get("route") or row.get("target_route")))
    for row in held.get("entries") or []:
        if row.get("status") == "HOLD_OPUS_NONCONVERGENCE" and row.get("topic_id"):
            topic_ids.add(str(row["topic_id"]))
    return topic_ids, routes


def buffered_ready_entries(
    root: Path,
) -> tuple[set[str], set[str], set[str], list[dict[str, str]]]:
    """Return immutable ready-buffer reservations so nightly refill never duplicates work."""
    topic_ids: set[str] = set()
    routes: set[str] = set()
    query_signatures: set[str] = set()
    specs: list[dict[str, str]] = []
    entries_root = root.resolve() / "entries"
    if not entries_root.is_dir():
        return topic_ids, routes, query_signatures, specs
    quarantined: set[str] = set()
    quarantine_root = root.resolve() / "quarantined"
    for path in sorted(quarantine_root.glob("*.json")) if quarantine_root.is_dir() else []:
        payload = read_object(path, "article_buffer_quarantine")
        entry_id = str(payload.get("entry_id") or "")
        if (
            payload.get("schema") != "glp-seo-article-buffer-quarantine-v1"
            or not entry_id
            or path.stem != entry_id
        ):
            raise RuntimeError(f"article_buffer_quarantine_invalid:{path}")
        quarantined.add(entry_id)
        specs.append(file_spec(path))
    for path in sorted(entries_root.glob("*/entry.json")):
        payload = read_object(path, "article_buffer_entry")
        if payload.get("schema") != "glp-seo-article-buffer-entry-v1":
            raise RuntimeError(f"article_buffer_entry_schema_invalid:{path}")
        if payload.get("status") != "READY_UNPUBLISHED":
            continue
        if str(payload.get("entry_id") or "") in quarantined:
            continue
        topic_id = str(payload.get("topic_id") or "")
        route = str(payload.get("target_route") or "")
        query_signature = semantic_query_signature(str(payload.get("target_query") or ""))
        if not topic_id or not ALLOWED_ROUTE.fullmatch(route):
            raise RuntimeError(f"article_buffer_entry_identity_invalid:{path}")
        topic_ids.add(topic_id)
        routes.add(route)
        if query_signature:
            query_signatures.add(query_signature)
        specs.append(file_spec(path))
    return topic_ids, routes, query_signatures, specs


def terminal_receipts(root: Path) -> tuple[set[str], set[str], list[dict[str, str]]]:
    topic_ids: set[str] = set()
    routes: set[str] = set()
    specs: list[dict[str, str]] = []
    for path in sorted(root.rglob("10-final-publication-receipt.json")) if root.is_dir() else []:
        try:
            payload = read_object(path, "final_publication_receipt")
        except RuntimeError:
            continue
        if payload.get("status") != "PASS_LIVE_GSC_READBACK_PASS":
            continue
        if payload.get("topic_id"):
            topic_ids.add(str(payload["topic_id"]))
        route = payload.get("target_route")
        if not route and payload.get("target_url"):
            route = urlsplit(str(payload["target_url"])).path
        if route:
            routes.add("/" + str(route).strip("/") + "/")
        specs.append(file_spec(path))
    return topic_ids, routes, specs


# Striking-distance bands (operator authority: reports/glp-clinic-scout/
# seo-machine/OPERATOR-AUTHORITY-AUTONOMOUS-RELEASE-20260813.md — ruthless
# optimization for relevant traffic). page_one: nearly ranking, highest value.
# striking: positions 40-90 with real impressions — the pages Google already
# half-trusts; targeted content moves them fastest.
PAGE_ONE_BAND = {"position_min": 5.0, "position_max": 15.0, "minimum_impressions": 100, "weight_multiplier": 3.0}
STRIKING_BAND = {"position_min": 40.0, "position_max": 90.0, "minimum_impressions": 1, "weight_multiplier": 1.0}
QUERY_STOPWORDS = {"a", "an", "and", "are", "for", "how", "is", "of", "or", "the",
                   "to", "what", "where", "with"}


# ---------------------------------------------------------------------------
# Content-class balance (bridge, strategy glp-content-mix-v1, 2026-08-15).
# The binding strategy (CONTENT-STRATEGY-BINDING-20260814.md) fixes the
# net-new mix; selection soft-suppresses classes over their weight band and
# boosts under-served ones. Soft (0.25x), never zero: when the queue only has
# over-weight topics the day still publishes — cadence beats mix purity.
# ---------------------------------------------------------------------------
CLASS_SUPPRESS_FACTOR = 0.25
CLASS_BOOST_CAP = 2.0
CLASS_LOOKBACK_DAYS = 28


def content_class(query: str, route: str = "") -> str:
    text = f"{normalize(query)} {route.lower()}"
    if "/reviews/" in route or " review" in f" {text}":
        return "provider_review"
    if any(tok in text for tok in (" vs ", "best ", "compare", "alternative", "-vs-")):
        return "commercial_comparison"
    if any(tok in text for tok in ("cost", "price", "pricing", "coupon", "cheap",
                                   "savings", "insurance", "afford")):
        return "cost_pricing"
    if any(tok in text for tok in ("how get", "how to get", "buy ", "online", "legit",
                                   "verify", "prescription", "telehealth", "pharmacy")):
        return "medication_guide"
    return "education_condition"


def recent_class_shares(ledger_path, as_of=None) -> dict[str, float]:
    """Trailing published-class shares from the route ledger. Pure, read-only."""
    import datetime as _dt
    try:
        routes = json.loads(Path(ledger_path).read_text()).get("routes", {})
    except (OSError, ValueError):
        return {}
    cutoff = ((as_of or _dt.datetime.now(_dt.timezone.utc))
              - _dt.timedelta(days=CLASS_LOOKBACK_DAYS))
    counts: dict[str, int] = {}
    for route, row in routes.items():
        seen = str(row.get("seen_at_utc") or "")
        try:
            when = _dt.datetime.fromisoformat(seen.replace("Z", "+00:00"))
        except ValueError:
            continue
        if when >= cutoff:
            cls = content_class(route.replace("-", " "), route)
            counts[cls] = counts.get(cls, 0) + 1
    total = sum(counts.values())
    return {cls: count / total for cls, count in counts.items()} if total else {}


def class_balance_multiplier(cls: str, shares: dict[str, float],
                             weights: dict[str, int], tolerance_pct: float) -> float:
    if not weights or cls not in weights:
        return 1.0
    target = weights[cls] / 100.0
    actual = shares.get(cls, 0.0)
    if actual > target + tolerance_pct / 100.0:
        return CLASS_SUPPRESS_FACTOR
    if actual < target and actual > 0:
        return min(CLASS_BOOST_CAP, target / actual)
    if actual == 0.0:
        return CLASS_BOOST_CAP
    return 1.0


def query_tokens(value: str) -> frozenset[str]:
    return frozenset(token for token in normalize(value).split() if token not in QUERY_STOPWORDS)


DEMAND_SIDECAR = (BUSINESS
                  / "reports/glp-clinic-scout/seo-machine/topic-data-score/demand-multipliers.json")
TARGETING_POLICY = (BUSINESS
                    / "reports/glp-clinic-scout/seo-machine/targeting-policy/current-targeting-policy.json")
TARGETING_POLICY_SCHEMA = "glp-seo-targeting-policy-v1"


def load_targeting_policy(
    configured_weights: dict[str, int],
    path: Path = TARGETING_POLICY,
) -> tuple[dict[str, int], dict[str, Any]]:
    """Load the bounded weekly class mix; fail open to configured weights.

    The weekly controller may reorient the mix, but it can never stop topic
    selection. Schema, receipt, age, weight-total, source-path, and source-hash
    failures all preserve the static configuration and are receipted.
    """
    fallback = {str(key): int(value) for key, value in configured_weights.items()}
    meta: dict[str, Any] = {"path": str(path), "state": "absent", "applied": False}
    try:
        if not path.is_file():
            return fallback, meta
        document = json.loads(path.read_text(encoding="utf-8"))
        if (document.get("schema") != TARGETING_POLICY_SCHEMA
                or document.get("status") != "PASS_TARGETING_POLICY"):
            meta["state"] = "schema_or_status_mismatch"
            return fallback, meta
        receipt_sha256 = str(document.get("receipt_sha256") or "")
        unsigned = dict(document)
        unsigned.pop("receipt_sha256", None)
        actual_receipt_sha256 = hashlib.sha256(
            (json.dumps(unsigned, sort_keys=True, separators=(",", ":"),
                        ensure_ascii=False) + "\n").encode("utf-8")
        ).hexdigest()
        if not receipt_sha256 or receipt_sha256 != actual_receipt_sha256:
            meta["state"] = "policy_receipt_unverified"
            return fallback, meta
        generated = dt.datetime.fromisoformat(str(document.get("generated_at_utc")).replace("Z", "+00:00"))
        if generated.tzinfo is None:
            generated = generated.replace(tzinfo=dt.timezone.utc)
        age_hours = (dt.datetime.now(dt.timezone.utc) - generated).total_seconds() / 3600.0
        max_age = float(document.get("max_age_hours") or 192)
        meta.update({"generated_at_utc": document.get("generated_at_utc"),
                     "age_hours": round(age_hours, 2), "max_age_hours": max_age,
                     "decision": document.get("decision")})
        if age_hours < -1 or age_hours > max_age:
            meta["state"] = "stale"
            return fallback, meta
        weights = document.get("class_weights")
        if not isinstance(weights, dict) or set(weights) != set(fallback):
            meta["state"] = "weight_keys_invalid"
            return fallback, meta
        active = {str(key): int(value) for key, value in weights.items()}
        if sum(active.values()) != 100 or any(value < 5 or value > 60 for value in active.values()):
            meta["state"] = "weight_values_invalid"
            return fallback, meta
        source = document.get("source_report") or {}
        source_path = Path(str(source.get("path") or ""))
        expected_hash = str(source.get("sha256") or "")
        if (not source_path.is_absolute() or not source_path.is_file()
                or not expected_hash or sha256_file(source_path) != expected_hash):
            meta["state"] = "source_report_unverified"
            return fallback, meta
        meta.update({"state": "applied", "applied": True,
                     "class_weights": active,
                     "policy_file": {
                         "path": str(path.resolve()),
                         "sha256": sha256_file(path),
                         "receipt_sha256": receipt_sha256,
                     },
                     "source_report": {"path": str(source_path), "sha256": expected_hash}})
        return active, meta
    except Exception as exc:  # noqa: BLE001 — never block the publication rail
        meta.update({"state": "error", "error": str(exc)[:200]})
        return fallback, meta


def load_demand_multipliers(path: Path = DEMAND_SIDECAR) -> tuple[dict[str, float], dict[str, Any]]:
    """Measured-demand multipliers written by ``scripts/glp_topic_data_score.py``.

    These bounded multipliers come from cached first-party GSC/Bing query
    aggregates and privacy-safe site-level context. No model scraping or user,
    conversion, revenue, or health-path GA4 join is performed. Unknown metric
    rows stay out of the measured table and use the existing neutral fallback.

    FAILS OPEN, always. Missing file, unreadable JSON, wrong schema, or a stale
    write all yield ``{}`` and the pre-existing ordering, because a scoring
    outage must never be able to stop the M/W/F publish. The reason is recorded
    in the receipt so a quiet degradation is still a visible one.
    """
    meta: dict[str, Any] = {"path": str(path), "state": "absent", "count": 0}
    try:
        if not path.is_file():
            return {}, meta
        doc = json.loads(path.read_text(encoding="utf-8"))
        if doc.get("schema") != "glp-topic-demand-multiplier-v1":
            meta["state"] = "schema_mismatch"
            return {}, meta
        written = dt.datetime.fromisoformat(str(doc.get("written_utc")))
        if written.tzinfo is None:
            written = written.replace(tzinfo=dt.timezone.utc)
        age_hours = (dt.datetime.now(dt.timezone.utc) - written).total_seconds() / 3600.0
        max_age = float(doc.get("max_age_hours") or 96)
        meta.update({"written_utc": doc.get("written_utc"), "age_hours": round(age_hours, 2),
                     "degraded": doc.get("degraded") or []})
        if age_hours > max_age:
            meta["state"] = "stale"
            return {}, meta
        out: dict[str, float] = {}
        for row in doc.get("multipliers") or []:
            key = normalize(str(row.get("keyword") or ""))
            raw_value = row.get("multiplier")
            if raw_value is None or isinstance(raw_value, bool):
                continue
            value = float(raw_value)
            if key and 0.1 <= value <= 5.0:
                out[key] = value
        meta.update({"state": "applied", "count": len(out)})
        return out, meta
    except Exception as exc:  # noqa: BLE001 — fail open, never raise into selection
        meta.update({"state": "error", "error": str(exc)[:200]})
        return {}, meta


def demand_multiplier(target_query: str, table: dict[str, float]) -> float:
    if not table:
        return 1.0
    return float(table.get(normalize(target_query), 1.0))


def gsc_preference(database: Path, lookback_days: int = 28) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    uri = f"file:{database.resolve()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        table = "observability_gsc_query_daily"
        if table not in tables:
            raise RuntimeError("gsc_query_table_missing")
        latest = connection.execute(f"SELECT MAX(metric_date) FROM {table}").fetchone()[0]
        if not latest:
            return {}, {"window_start": None, "window_end": None, "rows": 0, "digest": hashlib.sha256(b"[]").hexdigest()}
        end = dt.date.fromisoformat(str(latest))
        start = end - dt.timedelta(days=lookback_days - 1)
        raw = [dict(row) for row in connection.execute(
            """SELECT metric_date, query_text, impressions, clicks, position
               FROM observability_gsc_query_daily
               WHERE metric_date BETWEEN ? AND ? AND query_text IS NOT NULL
               ORDER BY metric_date, query_text""",
            (start.isoformat(), end.isoformat()),
        )]
    finally:
        connection.close()
    grouped: dict[str, dict[str, Any]] = {}
    for row in raw:
        key = normalize(str(row.get("query_text") or ""))
        if not key:
            continue
        item = grouped.setdefault(key, {"impressions": 0, "clicks": 0, "weighted_position_total": 0.0})
        impressions = int(row.get("impressions") or 0)
        item["impressions"] += impressions
        item["clicks"] += int(row.get("clicks") or 0)
        item["weighted_position_total"] += float(row.get("position") or 0) * max(impressions, 1)
    for item in grouped.values():
        divisor = max(int(item["impressions"]), 1)
        item["position"] = round(float(item.pop("weighted_position_total")) / divisor, 4)
        page_one = (item["impressions"] >= PAGE_ONE_BAND["minimum_impressions"]
                    and PAGE_ONE_BAND["position_min"] <= item["position"] <= PAGE_ONE_BAND["position_max"])
        striking = (item["impressions"] >= STRIKING_BAND["minimum_impressions"]
                    and STRIKING_BAND["position_min"] <= item["position"] <= STRIKING_BAND["position_max"])
        item["band"] = "page_one" if page_one else ("striking_distance" if striking else None)
        item["striking_distance_preference"] = page_one or striking
        multiplier = PAGE_ONE_BAND["weight_multiplier"] if page_one else (
            STRIKING_BAND["weight_multiplier"] if striking else 0.0)
        item["query_weight"] = round(item["impressions"] * multiplier, 4)
    digest = hashlib.sha256(json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return grouped, {"window_start": start.isoformat(), "window_end": end.isoformat(), "rows": len(raw), "digest": digest}


def topic_gsc_signal(target_query: str, gsc_rows: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Weighted striking-distance signal for one candidate topic.

    Exact-query stats plus a hub-gap component: banded queries whose token set
    overlaps the topic's query (>=50% full weight, >=25% half weight) prove
    demand the hub does not yet serve; the topic covering them inherits that
    weight so selection chases real search demand, not queue order alone.
    """
    topic_key = normalize(target_query)
    tokens = query_tokens(target_query)
    exact = dict(gsc_rows.get(topic_key) or {})
    weight = float(exact.get("query_weight") or 0.0)
    matched: list[dict[str, Any]] = []
    hub_gap_weight = 0.0
    for key, row in gsc_rows.items():
        if key == topic_key or not row.get("band"):
            continue
        row_tokens = query_tokens(key)
        union = tokens | row_tokens
        if not union or not tokens:
            continue
        overlap = len(tokens & row_tokens) / len(union)
        if overlap >= 0.5:
            factor = 1.0
        elif overlap >= 0.25:
            factor = 0.5
        else:
            continue
        contribution = round(float(row.get("query_weight") or 0.0) * factor, 4)
        if contribution <= 0:
            continue
        hub_gap_weight += contribution
        matched.append({"query": key, "band": row["band"], "impressions": row["impressions"],
                        "position": row["position"], "overlap": round(overlap, 3),
                        "weight_contribution": contribution})
    matched.sort(key=lambda row: (-row["weight_contribution"], row["query"]))
    total = round(weight + hub_gap_weight, 4)
    return {
        "impressions": int(exact.get("impressions") or 0),
        "clicks": int(exact.get("clicks") or 0),
        "position": exact.get("position"),
        "band": exact.get("band"),
        "striking_distance_preference": total > 0,
        "exact_query_weight": round(weight, 4),
        "hub_gap_weight": round(hub_gap_weight, 4),
        "weight": total,
        "matched_queries": matched[:10],
        "matched_query_count": len(matched),
    }


def select_strategy_safe(
    topic_rows: list[dict[str, Any]],
    strategy_rows: list[dict[str, Any]],
    map_cluster_ids: set[str],
    live_routes: set[str],
    terminal_topic_ids: set[str],
    terminal_routes: set[str],
    gsc_rows: dict[str, dict[str, Any]],
    buffered_topic_ids: set[str] | None = None,
    buffered_routes: set[str] | None = None,
    buffered_query_signatures: set[str] | None = None,
    class_weights: dict[str, int] | None = None,
    class_shares: dict[str, float] | None = None,
    class_tolerance_pct: float = 5.0,
    demand_table: dict[str, float] | None = None,
    excluded_topic_ids: set[str] | None = None,
    dynamic_cluster_ids: set[str] | None = None,
    cogny_strategy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Pure selector: strategy safety first, GSC preference second, then the
    strategy's content-class balance and the measured-demand multiplier (both
    soft multipliers, never hard gates)."""
    buffered_topic_ids = buffered_topic_ids or set()
    buffered_routes = buffered_routes or set()
    buffered_query_signatures = buffered_query_signatures or set()
    excluded_topic_ids = excluded_topic_ids or set()
    dynamic_cluster_ids = dynamic_cluster_ids or set()
    strategy_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    ambiguous_keys: set[tuple[str, str]] = set()
    pillar_routes: dict[str, str] = {}
    for strategy in strategy_rows:
        route_value = str(strategy.get("target_route") or "")
        if not ALLOWED_ROUTE.fullmatch(route_value):
            continue
        route = canonical_route(route_value)
        cluster = str(strategy.get("cluster_id") or "")
        if cluster not in map_cluster_ids and cluster not in dynamic_cluster_ids:
            raise RuntimeError(f"strategy_map_join_missing:{cluster}")
        key = (normalize(str(strategy.get("target_query") or "")), route)
        if not key[0]:
            raise RuntimeError(f"strategy_queue_join_key_invalid:{key}")
        if key in strategy_by_key:
            ambiguous_keys.add(key)
        else:
            strategy_by_key[key] = strategy
        if strategy.get("depth_stage") == "pillar":
            if cluster in pillar_routes:
                raise RuntimeError(f"strategy_cluster_pillar_duplicate:{cluster}")
            pillar_routes[cluster] = route

    eligible: list[dict[str, Any]] = []
    awaiting: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for row in topic_rows:
        directive = row.get("competitor_gap") or {}
        route_value = directive.get("target_route")
        if not isinstance(route_value, str) or not route_value:
            rejected.append({"topic_id": row.get("topic_id"), "target_query": row.get("target_query"), "reason": "competitor_gap_target_route_missing"})
            continue
        try:
            route = canonical_route(route_value)
        except RuntimeError:
            rejected.append({"topic_id": row.get("topic_id"), "target_query": row.get("target_query"), "target_route": route_value, "reason": "competitor_gap_target_route_invalid"})
            continue
        join_key = (normalize(str(row.get("target_query") or "")), route)
        if join_key in ambiguous_keys:
            rejected.append({"topic_id": row.get("topic_id"), "target_query": row.get("target_query"), "target_route": route, "reason": "strategy_queue_join_ambiguous"})
            continue
        strategy = strategy_by_key.get(join_key)
        if strategy is None:
            rejected.append({"topic_id": row.get("topic_id"), "target_query": row.get("target_query"), "target_route": route, "reason": "strategy_queue_join_missing"})
            continue
        topic_id = str(row.get("topic_id") or "")
        candidate_class = content_class(str(row.get("target_query") or ""), route)
        if candidate_class == "provider_review" and route.startswith("/guides/"):
            rejected.append({
                "topic_id": topic_id,
                "target_query": row.get("target_query"),
                "target_route": route,
                "queue_position": strategy["queue_position"],
                "reason": "onboarding_owned_review_surface",
                "canonical_surface": "/reviews/",
            })
            continue
        if topic_id in excluded_topic_ids:
            rejected.append({
                "topic_id": topic_id,
                "target_query": row.get("target_query"),
                "target_route": route,
                "queue_position": strategy["queue_position"],
                "reason": "run_cooldown_exclusion",
            })
            continue
        if topic_id in buffered_topic_ids or route in buffered_routes:
            rejected.append({"topic_id": topic_id, "target_query": row.get("target_query"), "target_route": route, "queue_position": strategy["queue_position"], "reason": "buffered_ready_exclusion"})
            continue
        if semantic_query_signature(str(row.get("target_query") or "")) in buffered_query_signatures:
            rejected.append({"topic_id": topic_id, "target_query": row.get("target_query"), "target_route": route, "queue_position": strategy["queue_position"], "reason": "buffered_semantic_query_exclusion"})
            continue
        if topic_id in terminal_topic_ids or route in terminal_routes or route in live_routes:
            rejected.append({"topic_id": topic_id, "target_query": row.get("target_query"), "target_route": route, "queue_position": strategy["queue_position"], "reason": "terminal_or_live_exclusion"})
            continue
        if not has_operator_go(row):
            awaiting.append({
                "topic_id": topic_id,
                "target_query": row.get("target_query"),
                "target_route": route,
                "queue_position": strategy["queue_position"],
                "cluster_id": strategy["cluster_id"],
                "decision": "GO_OR_HOLD_REQUIRED",
            })
            continue
        cluster = str(strategy["cluster_id"])
        pillar_route = pillar_routes.get(cluster)
        requires_predecessor = strategy.get("requires_pillar_predecessor") is True
        predecessor_satisfied = not requires_predecessor or bool(pillar_route and pillar_route in live_routes)
        predecessor = {
            "required": requires_predecessor,
            "pillar_route": pillar_route,
            "satisfied": predecessor_satisfied,
            "basis": "not_required" if not requires_predecessor else "current_content_base_or_sitemap_live",
        }
        if not predecessor_satisfied:
            rejected.append({
                "topic_id": topic_id,
                "target_query": row.get("target_query"),
                "target_route": route,
                "queue_position": strategy["queue_position"],
                "cluster_id": cluster,
                "reason": "pillar_predecessor_absent",
                "predecessor_proof": predecessor,
                "rank_score": row.get("rank_score") or 0,
            })
            continue
        gsc = topic_gsc_signal(str(row.get("target_query") or ""), gsc_rows)
        eligible.append({
            "topic_id": topic_id,
            "target_query": row["target_query"],
            "target_route": route,
            "suggested_route": route,
            "operator_directive_id": directive["operator_directive_id"],
            "queue_position": int(strategy["queue_position"]),
            "cluster_id": cluster,
            "depth_stage": strategy["depth_stage"],
            "current_route_status": strategy.get("current_route_status"),
            "strategy_safe": True,
            "predecessor_proof": predecessor,
            "gsc_preference": gsc,
            "rank_score_observability_only": row.get("rank_score") or 0,
            "content_class": candidate_class,
            "opportunity_class": directive.get("opportunity_class"),
            "opportunity_class_rank": int(directive.get("class_rank", 2)),
            "topic_provenance": {
                "schema": "glp-seo-topic-provenance-v1",
                "keyword": row["target_query"],
                "selection_reason": str(directive.get("rationale") or ""),
                "gap_source": {
                    "type": ("strategy_competitor_gap" if directive.get("opportunity_type") == "competitor_gap_new_topic" else "keyword_gap"),
                    "opportunity_type": directive.get("opportunity_type"),
                    "opportunity_class": directive.get("opportunity_class"),
                    "source_opportunity_id": directive.get("source_opportunity_id"),
                    "operator_directive_id": directive.get("operator_directive_id"),
                },
                "evidence": directive.get("evidence") or [],
                "keyword_evidence": row.get("keyword_evidence") or {},
                "analytics": {"gsc_preference": gsc},
                "source_artifacts": {},
            },
        })
    shares = class_shares or {}
    weights = class_weights or {}
    table = demand_table or {}
    for row in eligible:
        row["cogny_strategy_preference"] = cogny_preference(cogny_strategy, row["target_query"], row["target_route"], "new_article")
        row["class_balance_multiplier"] = class_balance_multiplier(
            row["content_class"], shares, weights, class_tolerance_pct)
        row["demand_multiplier"] = demand_multiplier(str(row.get("target_query") or ""), table)
    gap_candidates = [row for row in eligible if row.get("opportunity_class") in {"B", "C"}]
    if gap_candidates:
        minimum_gap_rank = min(int(row["opportunity_class_rank"]) for row in gap_candidates)
        selection_pool = [row for row in gap_candidates
                          if int(row["opportunity_class_rank"]) == minimum_gap_rank]
        preferred = [row for row in selection_pool
                     if float(row["gsc_preference"].get("weight") or 0) > 0]
    else:
        selection_pool = eligible
        preferred = [row for row in eligible if float(row["gsc_preference"].get("weight") or 0) > 0]
    # When NO candidate carries a GSC weight — the normal case on a site with no
    # rankings yet — the demand sidecar becomes the primary ordering signal
    # instead of falling straight through to queue position. That is the whole
    # point of measuring demand we do not yet serve.
    ordered = sorted(
        preferred or selection_pool,
        key=lambda row: (
            int(row.get("opportunity_class_rank", 2)),
            -cogny_preference_score(cogny_strategy, row["target_query"], row["target_route"], "new_article"),
            -float(row["gsc_preference"].get("weight") or 0)
            * float(row.get("class_balance_multiplier") or 1.0)
            * float(row.get("demand_multiplier") or 1.0),
            -float(row.get("demand_multiplier") or 1.0),
            int(row["queue_position"]),
            row["topic_id"],
        ),
    )
    return {
        "eligible": sorted(eligible, key=lambda row: (
            int(row.get("opportunity_class_rank", 2)), row["queue_position"], row["topic_id"])),
        "selected": ordered[:1],
        "awaiting_go": sorted(awaiting, key=lambda row: (row["queue_position"], row["topic_id"])),
        "rejected": sorted(rejected, key=lambda row: (int(row.get("queue_position") or 999999), str(row.get("topic_id") or ""))),
        "selection_mode": (
            f"keyword_gap_class_{(preferred or selection_pool)[0]['opportunity_class'].lower()}_first"
            if gap_candidates else
            "gsc_striking_distance_weighted" if preferred else "deterministic_cluster_queue_fallback"),
        "class_balance": {"strategy_version": "glp-content-mix-v1" if weights else None,
                          "weights": weights, "recent_shares": shares,
                          "tolerance_pct": class_tolerance_pct},
    }


def load_keyword_gap_overlay(path: Path) -> tuple[list[dict[str, Any]], set[str], dict[str, Any] | None]:
    """Validate the operator-directed dynamic strategy overlay before joining it."""
    if not path.is_file():
        return [], set(), None
    resolved = path.resolve(strict=True)
    payload = read_object(resolved, "keyword_gap_strategy_overlay")
    if payload.get("schema") != KEYWORD_GAP_OVERLAY_SCHEMA:
        raise RuntimeError("keyword_gap_strategy_overlay_schema_invalid")
    unsigned = dict(payload)
    expected_self = unsigned.pop("overlay_sha256", None)
    if expected_self != hashlib.sha256(canonical(unsigned).encode("utf-8")).hexdigest():
        raise RuntimeError("keyword_gap_strategy_overlay_self_hash_invalid")
    if payload.get("operator_directive_id") != "operator-keyword-gap-20260823":
        raise RuntimeError("keyword_gap_strategy_overlay_directive_invalid")
    source = Path(str(payload.get("source_opportunity_artifact") or "")).resolve(strict=True)
    opportunity = read_object(source, "keyword_gap_opportunity_artifact")
    if opportunity.get("artifact_sha256") != payload.get("source_opportunity_sha256"):
        raise RuntimeError("keyword_gap_strategy_overlay_source_hash_invalid")
    rows = list(payload.get("queue") or [])
    clusters: set[str] = set()
    for row in rows:
        cluster = str(row.get("cluster_id") or "")
        if (
            row.get("opportunity_class") not in {"B", "C"}
            or not ALLOWED_ROUTE.fullmatch(str(row.get("target_route") or ""))
            or row.get("depth_stage") != "pillar"
            or row.get("requires_pillar_predecessor") is not False
            or not cluster.startswith("keyword-gap-")
        ):
            raise RuntimeError("keyword_gap_strategy_overlay_row_invalid")
        clusters.add(cluster)
    return rows, clusters, file_spec(resolved)


def redirected_review_routes(path: Path) -> set[str]:
    """Existing native review aliases are not unpublished article opportunities."""
    routes = set()
    for line in path.read_text().splitlines():
        columns = line.split()
        if len(columns) >= 2 and re.fullmatch(r"/reviews/[a-z0-9]+(?:-[a-z0-9]+)*/", columns[0]):
            routes.add(columns[0])
    return routes


def build_selection_receipt(args: argparse.Namespace) -> dict[str, Any]:
    if args.limit != 1:
        raise RuntimeError("selection_limit_must_equal_one")
    policy = consumer_policy("strategy_selector", decision_class="search")
    require_allowed(policy)
    search_policy = {**policy, "mode": "PASS_SEARCH_ONLY", "revenue_blind": True}
    config = load_config(args.config)
    resolution = config["_current_content_base_resolution"]
    queue_path = args.queue.resolve(strict=True)
    strategy_path = args.strategy_queue.resolve(strict=True)
    ledger_path = args.ledger.resolve(strict=True)
    hold_path = args.hold_ledger.resolve(strict=True)
    database_path = args.database.resolve(strict=True)
    queue = read_object(queue_path, "topic_queue")
    strategy = read_object(strategy_path, "strategy_queue")
    if strategy.get("schema") != "glp-seo-cluster-rotation-queue-v1" or strategy.get("status") != "READY_FOR_DAILY_CANDIDATE_WAVE":
        raise RuntimeError("strategy_queue_contract_invalid")
    map_path = Path(str(strategy.get("map_path") or "")).resolve(strict=True)
    strategy_map = read_object(map_path, "strategy_map")
    if strategy_map.get("schema") != "glp-seo-commercial-cluster-map-v1" or strategy_map.get("status") != "PASS_MAP_READY_QUEUE_ROTATION":
        raise RuntimeError("strategy_map_contract_invalid")
    map_clusters = {str(row.get("id")) for row in strategy_map.get("clusters") or [] if row.get("id")}
    overlay_rows, overlay_clusters, overlay_spec = load_keyword_gap_overlay(args.keyword_gap_overlay)
    strategy_rows = list(strategy.get("queue") or [])
    existing_keys = {
        (normalize(str(row.get("target_query") or "")), canonical_route(str(row.get("target_route") or "")))
        for row in strategy_rows if ALLOWED_ROUTE.fullmatch(str(row.get("target_route") or ""))
    }
    strategy_rows.extend(
        row for row in overlay_rows
        if (normalize(str(row.get("target_query") or "")),
            canonical_route(str(row.get("target_route") or ""))) not in existing_keys
    )
    published = read_object(ledger_path, "published_topic_ledger")
    held = read_object(hold_path, "held_topic_ledger")
    terminal_ids, terminal_routes = ledger_terminals(published, held)
    buffered_ids, buffered_routes, buffered_query_signatures, buffered_specs = buffered_ready_entries(
        args.article_buffer_root
    )
    receipt_ids, receipt_routes, terminal_specs = terminal_receipts(args.runs.resolve())
    terminal_ids |= receipt_ids
    terminal_routes |= receipt_routes
    release_root = Path(resolution["pointer"]["release_root"])
    sitemap_path = (release_root / "public/sitemap.xml").resolve(strict=True)
    redirect_path = (release_root / "public/_redirects").resolve(strict=True)
    live_routes = current_catalog_routes(config) | sitemap_routes(sitemap_path, config["vertical"]["domain"])
    live_routes |= redirected_review_routes(redirect_path)
    gsc_rows, gsc_window = gsc_preference(database_path)
    demand_table, demand_meta = load_demand_multipliers(getattr(args, "demand_sidecar", DEMAND_SIDECAR))
    configured_class_weights = {
        str(key): int(value)
        for key, value in ((config.get("topic_feed") or {}).get("class_weights") or {}).items()
    }
    active_class_weights, targeting_meta = load_targeting_policy(configured_class_weights)
    cogny_strategy, cogny_meta = load_cogny_strategy()
    selected = select_strategy_safe(
        list(queue.get("queue") or []),
        strategy_rows,
        map_clusters,
        live_routes,
        terminal_ids,
        terminal_routes,
        gsc_rows,
        buffered_ids,
        buffered_routes,
        buffered_query_signatures,
        class_weights=active_class_weights,
        class_shares=recent_class_shares(
            "/home/ubuntu/business/reports/glp-clinic-scout/seo-machine/published-route-ledger.json"),
        class_tolerance_pct=float((config.get("topic_feed") or {}).get("class_share_tolerance_pct") or 5),
        demand_table=demand_table,
        excluded_topic_ids=set(args.exclude_topic_id or []),
        dynamic_cluster_ids=overlay_clusters,
        cogny_strategy=cogny_strategy,
    )
    overlay_source_spec = None
    if overlay_spec is not None:
        overlay_payload = read_object(
            Path(overlay_spec["path"]), "keyword_gap_strategy_overlay_provenance"
        )
        source_path = Path(
            str(overlay_payload.get("source_opportunity_artifact") or "")
        ).resolve(strict=True)
        overlay_source_spec = {
            **file_spec(source_path),
            "artifact_sha256": str(
                overlay_payload.get("source_opportunity_sha256") or ""
            ),
        }
    for row in selected["eligible"]:
        provenance = row.get("topic_provenance")
        if not isinstance(provenance, dict):
            continue
        provenance["selection_mode"] = selected["selection_mode"]
        provenance["analytics"]["gsc_window"] = gsc_window
        if provenance["gap_source"]["type"] == "strategy_competitor_gap":
            provenance["source_artifacts"] = {
                "strategy_map": {"path": str(map_path), "sha256": sha256_file(map_path)},
                "strategy_queue": {"path": str(strategy_path), "sha256": sha256_file(strategy_path)},
            }
        else:
            provenance["source_artifacts"] = {
                "keyword_gap_strategy_overlay": overlay_spec,
                "keyword_gap_opportunity_artifact": overlay_source_spec,
            }
    for row in selected["eligible"]:
        content_class_name = str(row.get("content_class") or "")
        row["weekly_targeting_policy"] = {
            "state": targeting_meta.get("state"),
            "applied": targeting_meta.get("applied") is True,
            "configured_weight": configured_class_weights.get(content_class_name),
            "active_weight": active_class_weights.get(content_class_name),
            "decision": targeting_meta.get("decision"),
            "policy_sha256": (targeting_meta.get("policy_file") or {}).get("sha256"),
            "policy_receipt_sha256": (
                targeting_meta.get("policy_file") or {}).get("receipt_sha256"),
        }
        provenance = row.get("topic_provenance")
        if isinstance(provenance, dict):
            provenance.setdefault("source_artifacts", {})["weekly_targeting_policy"] = (
                targeting_meta.get("policy_file") if targeting_meta.get("applied") else {
                    "path": targeting_meta.get("path"), "state": targeting_meta.get("state")
                }
            )
    queue_sha = sha256_file(queue_path)
    for row in selected["eligible"]:
        row["queue_sha256"] = queue_sha
    for row in selected["selected"]:
        row["queue_sha256"] = queue_sha
    # No current-week advice means no new dispatch, never fallback to old
    # rotation. Keep prior admission/identity/provider guards as prerequisites.
    from seo_weekly_governance import CADENCE, new_candidates
    governed = new_candidates(selected["eligible"], cogny_strategy)
    selected["eligible"] = governed
    selected["selected"] = governed[:args.limit]
    selected["weekly_governance"] = {"cadence": CADENCE, "strategy": cogny_meta,
        "governing": True, "no_op_allowed": True,
        "reason": None if governed else "current_week_has_no_admitted_new_topic"}
    status = "PASS_STRATEGY_SAFE_SELECTION" if selected["selected"] else "PASS_EMPTY_STRATEGY_SAFE_SET"
    # Empty supply is a valid immutable no-op receipt, not an exception that
    # loses the strategy SHA. Consumers must not author from an empty selection.
    # GSC is a preference inside the strategy-safe set, not an eligibility
    # gate. A populated zero-weight signal truthfully means that none of the
    # safe candidates matched the exact-query or hub-gap relevance rules; in
    # that case deterministic cluster order is the intended fallback. Fail
    # only when a selected topic was not evaluated at all.
    if int(gsc_window.get("rows") or 0) > 0 and any(
        not isinstance(row.get("gsc_preference"), dict)
        or not row["gsc_preference"]
        for row in selected["selected"]
    ):
        raise RuntimeError(
            "selection_gsc_preference_empty_with_gsc_data_present:"
            f"rows={gsc_window['rows']}:window_end={gsc_window.get('window_end')}"
        )
    verify_resolution(resolution)
    analytics_decisions = []
    for row in selected["selected"]:
        gsc = row.get("gsc_preference") or {}
        demand_available = demand_meta.get("state") == "applied"
        analytics_decisions.append(write_consumer_decision(
            consumer="strategy_selector",
            decision_type="topic_selection",
            subject=str(row.get("topic_id") or row.get("target_query") or ""),
            action="select_for_generation",
            pre_signal_score=float(row.get("rank_score_observability_only") or 0),
            components=[
                {"signal": "gsc_striking_distance", "available": True,
                 "delta": float(gsc.get("weight") or 0)},
                {"signal": "measured_demand_multiplier", "available": demand_available,
                 "delta": (float(row.get("demand_multiplier") or 1.0) - 1.0)
                          if demand_available else None,
                 "reason": None if demand_available else f"demand_sidecar_{demand_meta.get('state')}"},
                {"signal": "content_class_balance", "available": True,
                 "delta": float(row.get("class_balance_multiplier") or 1.0) - 1.0},
                {"signal": "weekly_targeting_policy",
                 "available": targeting_meta.get("applied") is True,
                 "delta": ((float(active_class_weights.get(str(row.get("content_class") or ""), 0))
                            - float(configured_class_weights.get(str(row.get("content_class") or ""), 0))) / 100.0)
                          if targeting_meta.get("applied") is True else None,
                 "reason": None if targeting_meta.get("applied") is True
                           else f"targeting_policy_{targeting_meta.get('state')}"},
                {"signal": "normalized_revenue", "available": False, "delta": None,
                 "reason": "search_selection_is_revenue_blind"},
            ],
            policy=search_policy,
        ))
    return {
        "schema": "glp-opus-content-selection-receipt-v2",
        "status": status,
        "generated_at_utc": args.generated_at_utc or dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "selection_mode": selected["selection_mode"],
        "data_quality_mode": "PASS_SEARCH_ONLY",
        "revenue_blind": True,
        "analytics_decisions": analytics_decisions,
        "demand_signal": demand_meta,
        "cogny_weekly_strategy": cogny_meta,
        "weekly_governance": selected["weekly_governance"],
        "targeting_policy": targeting_meta,
        "eligible_count": len(selected["eligible"]),
        "selected": selected["selected"],
        "eligible": selected["eligible"],
        "topic_card_candidates": selected["awaiting_go"][:5] if not selected["eligible"] else [],
        "rejections": selected["rejected"],
        "rules": {
            "strategy_safe_before_gsc": True,
            "run_cooldown_excluded_topic_ids": sorted(set(args.exclude_topic_id or [])),
            "gsc_preference": {
                "authority_receipt": "/home/ubuntu/business/reports/glp-clinic-scout/seo-machine/OPERATOR-AUTHORITY-AUTONOMOUS-RELEASE-20260813.md",
                "bands": {"page_one": PAGE_ONE_BAND, "striking_distance": STRIKING_BAND},
                "hub_gap_token_overlap": {"full_weight": 0.5, "half_weight": 0.25},
                "weighted_ranking": True,
                "missing_preference_with_gsc_data_is_failure": True,
                "populated_zero_weight_uses_deterministic_fallback": True,
            },
            "weekly_targeting_policy": {
                "configured_weights": configured_class_weights,
                "active_weights": active_class_weights,
                "state": targeting_meta.get("state"),
                "fail_open": True,
            },
            "rank_score_is_authority": False,
            "auto_generate": config["publishing"]["auto_generate"],
            "auto_publish": config["publishing"]["auto_publish"],
        },
        "inputs": {
            "topic_queue": file_spec(queue_path),
            "strategy_queue": file_spec(strategy_path),
            "strategy_map": file_spec(map_path),
            "keyword_gap_strategy_overlay": overlay_spec,
            "published_ledger": file_spec(ledger_path),
            "hold_ledger": file_spec(hold_path),
            "article_buffer_ready_entries": buffered_specs,
            "current_content_base_state": resolution["state"],
            "current_content_base_resolution": resolution,
            "sitemap_readback": file_spec(sitemap_path),
            "review_alias_redirect_readback": file_spec(redirect_path),
            "observability_database": file_spec(database_path),
            "observability_window": gsc_window,
            "terminal_receipts": terminal_specs,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    parser.add_argument("--strategy-queue", type=Path, default=DEFAULT_STRATEGY_QUEUE)
    parser.add_argument("--runs", type=Path, default=DEFAULT_RUNS)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--hold-ledger", type=Path, default=DEFAULT_HOLD_LEDGER)
    parser.add_argument("--article-buffer-root", type=Path, default=DEFAULT_BUFFER_ROOT)
    parser.add_argument("--keyword-gap-overlay", type=Path, default=DEFAULT_KEYWORD_GAP_OVERLAY)
    parser.add_argument("--database", type=Path, default=BUSINESS / "reports/glp-clinic-scout/operator-national-wow-20260723/data/glp-observability.sqlite")
    parser.add_argument("--demand-sidecar", type=Path, default=DEMAND_SIDECAR)
    parser.add_argument("--output-receipt", type=Path, required=True)
    parser.add_argument("--generated-at-utc")
    parser.add_argument("--limit", type=int, default=1)
    parser.add_argument(
        "--exclude-topic-id",
        action="append",
        default=[],
        help="Temporarily skip a topic already attempted in this refill run; repeatable.",
    )
    args = parser.parse_args()
    if args.output_receipt.exists():
        output = args.output_receipt.resolve()
        load_selection_receipt(output)
    else:
        payload = build_selection_receipt(args)
        output = write_immutable_receipt(args.output_receipt, payload)
    persisted = read_object(output, "persisted_selection_receipt")
    print(json.dumps({
        "status": persisted["status"],
        "eligible_count": persisted["eligible_count"],
        "selected": persisted["selected"],
        "selection_mode": persisted["selection_mode"],
        "selection_receipt": str(output),
        "selection_receipt_sha256": sha256_file(output),
        "receipt_sha256": persisted["receipt_sha256"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
