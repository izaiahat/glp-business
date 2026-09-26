#!/usr/bin/env python3
"""Weekly proof that the GLP SEO machine publishes pages Google indexes and ranks.

This is the registered data collector behind the Sunday weekly SEO control job.
It keeps publication, indexation, rankings, and traffic as separate evidence gates.
It never deploys the site, writes Sheets, submits URLs, or requests affiliate
tracking links.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import json
import os
import re
import stat
import statistics
import sys
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

BUSINESS = Path("/home/ubuntu/business")
REPORT_ROOT = BUSINESS / "reports/glp-clinic-scout/seo-machine/weekly-ranking-report"
SEO_ROOT = BUSINESS / "reports/glp-clinic-scout/seo-machine"
LEDGER = SEO_ROOT / "published-route-ledger.json"
R2_ROOT = SEO_ROOT / "r2-update-carrier"
DISCORD_CHANNEL = "1487300132054765629"
DISCORD_TOKEN = Path.home() / ".hermes/secrets/boring-ai/discord_bot_token"
SITE = "https://glpclinicscout.com"
GSC_PROPERTY = "sc-domain:glpclinicscout.com"
PT = ZoneInfo("America/Los_Angeles")
ARTICLE_PREFIXES = ("/guides/", "/reviews/", "/compare/", "/best/")
ARTICLE_INDEXES = {"/guides/", "/reviews/"}
VERDICT_DEADLINE = dt.date(2026, 9, 28)

sys.path.insert(0, str(BUSINESS))
from ops.glp.cogny_client import TOOL, connect  # noqa: E402
from ops.glp.seo_data_rails import DataForSEO, GoogleRail, PostHog  # noqa: E402


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    raw = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def metric(value: Any, source: str, date_range: str | None = None, *,
           classification: str = "MEASURED", unit: str | None = None,
           note: str | None = None) -> dict[str, Any]:
    row: dict[str, Any] = {
        "value": value,
        "classification": classification,
        "source": source,
    }
    if date_range:
        row["date_range"] = date_range
    if unit:
        row["unit"] = unit
    if note:
        row["note"] = note
    return row


def report_windows(report_date: dt.date) -> dict[str, tuple[dt.date, dt.date]]:
    # The durable job runs Sunday evening, so that day's production belongs to
    # the current week. Historical Monday reports retain their original
    # Monday-through-Sunday window. GSC is always held three days back so a
    # partial Search Console day is never presented as complete.
    if report_date.weekday() == 6:  # Sunday
        publication_current = (report_date - dt.timedelta(days=6), report_date)
        publication_prior = (report_date - dt.timedelta(days=13), report_date - dt.timedelta(days=7))
    else:
        publication_current = (report_date - dt.timedelta(days=7), report_date - dt.timedelta(days=1))
        publication_prior = (report_date - dt.timedelta(days=14), report_date - dt.timedelta(days=8))
    return {
        "publication_current": publication_current,
        "publication_prior": publication_prior,
        "gsc_current": (report_date - dt.timedelta(days=9), report_date - dt.timedelta(days=3)),
        "gsc_prior": (report_date - dt.timedelta(days=16), report_date - dt.timedelta(days=10)),
    }


def date_range_text(window: tuple[dt.date, dt.date]) -> str:
    return f"{window[0].isoformat()}..{window[1].isoformat()}"


def published_articles(document: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    seeded: list[dict[str, Any]] = []
    new: list[dict[str, Any]] = []
    for route, value in sorted((document.get("routes") or {}).items()):
        if not route.startswith(ARTICLE_PREFIXES) or route in ARTICLE_INDEXES:
            continue
        row = {"route": route, **value}
        (seeded if value.get("seed") else new).append(row)
    return seeded, new


def successful_r2_refreshes(root: Path) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for path in root.rglob("10-final-publication-receipt.json"):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not (
            document.get("schema") == "glp-opus-article-final-publication-v1"
            and document.get("status") == "PASS_LIVE_GSC_READBACK_PASS"
            and document.get("operation") == "r2_refresh"
        ):
            continue
        receipt_hash = str(document.get("receipt_sha256") or sha256(path))
        rows[receipt_hash] = {
            "topic_id": document.get("topic_id"),
            "target_query": document.get("target_query"),
            "live_url": document.get("live_url"),
            "article_id": document.get("article_id"),
            "completed_at_utc": document.get("completed_at_utc"),
            "status": document.get("status"),
            "receipt_path": str(path),
            "receipt_sha256": receipt_hash,
        }
    return sorted(rows.values(), key=lambda row: str(row.get("completed_at_utc") or ""))


def unwrap_cogny_rows(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict) and "result" in value:
        value = value["result"]
    if isinstance(value, str):
        value = json.loads(value)
    if isinstance(value, dict) and "d" in value:
        value = value["d"]
    return value if isinstance(value, list) else []


def clean_bing_rows(rows: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    key = "query" if kind == "query" else "page"
    return [{
        "date_provider": row.get("Date"),
        key: row.get("Query"),
        "impressions": row.get("Impressions"),
        "clicks": row.get("Clicks"),
        "avg_position": row.get("AvgImpressionPosition"),
        "avg_click_position": row.get("AvgClickPosition"),
    } for row in rows]


def bing_date(value: str | None) -> str | None:
    match = re.search(r"Date\((\d+)", value or "")
    if not match:
        return None
    return dt.datetime.fromtimestamp(int(match.group(1)) / 1000, dt.timezone.utc).date().isoformat()


def aggregate_bing(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for row in rows:
        day = bing_date(row.get("date_provider")) or "unknown"
        agg = output.setdefault(day, {"rows": 0, "impressions": 0, "clicks": 0,
                                      "position_numerator": 0.0})
        impressions = int(row.get("impressions") or 0)
        agg["rows"] += 1
        agg["impressions"] += impressions
        agg["clicks"] += int(row.get("clicks") or 0)
        agg["position_numerator"] += float(row.get("avg_position") or 0) * impressions
    for agg in output.values():
        numerator = agg.pop("position_numerator")
        agg["weighted_average_position"] = (
            numerator / agg["impressions"] if agg["impressions"] else None
        )
    return output


def aggregate_gsc(rows: list[dict[str, Any]], allowed_urls: set[str]) -> dict[str, Any]:
    """Aggregate either page-only or query+page rows without losing anonymized queries."""
    selected = [row for row in rows
                if (row.get("keys") or []) and row["keys"][-1] in allowed_urls]
    impressions = sum(float(row.get("impressions") or 0) for row in selected)
    clicks = sum(float(row.get("clicks") or 0) for row in selected)
    return {
        "rows": len(selected),
        "impressions": int(impressions),
        "clicks": int(clicks),
        "ctr": clicks / impressions if impressions else 0.0,
        "weighted_average_position": (
            sum(float(row.get("position") or 0) * float(row.get("impressions") or 0)
                for row in selected) / impressions if impressions else None
        ),
        "pages_with_impressions": len({row["keys"][-1] for row in selected
                                       if row.get("impressions")}),
        "pages_with_clicks": len({row["keys"][-1] for row in selected if row.get("clicks")}),
    }


def key_map(rows: list[dict[str, Any]], article_urls: set[str], kind: str) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for row in rows:
        keys = row.get("keys") or []
        if not keys or keys[-1] not in article_urls:
            continue
        if kind == "query" and len(keys) < 2:
            continue
        key = keys[0] if kind == "query" else keys[-1]
        agg = output.setdefault(key, {"impressions": 0.0, "clicks": 0.0,
                                      "position_numerator": 0.0})
        impressions = float(row.get("impressions") or 0)
        agg["impressions"] += impressions
        agg["clicks"] += float(row.get("clicks") or 0)
        agg["position_numerator"] += float(row.get("position") or 0) * impressions
    for agg in output.values():
        numerator = agg.pop("position_numerator")
        agg["weighted_average_position"] = (
            numerator / agg["impressions"] if agg["impressions"] else None
        )
        agg["impressions"] = int(agg["impressions"])
        agg["clicks"] = int(agg["clicks"])
    return output


def mover_rows(current: list[dict[str, Any]], prior: list[dict[str, Any]],
               article_urls: set[str], kind: str) -> list[dict[str, Any]]:
    current_map = key_map(current, article_urls, kind)
    prior_map = key_map(prior, article_urls, kind)
    rows = []
    empty = {"impressions": 0, "clicks": 0, "weighted_average_position": None}
    for key in sorted(set(current_map) | set(prior_map)):
        cur = current_map.get(key, empty)
        old = prior_map.get(key, empty)
        cur_position = cur["weighted_average_position"]
        old_position = old["weighted_average_position"]
        rows.append({
            "key": key,
            "current": cur,
            "prior": old,
            "impressions_delta": cur["impressions"] - old["impressions"],
            "clicks_delta": cur["clicks"] - old["clicks"],
            "position_improvement": (
                old_position - cur_position
                if old_position is not None and cur_position is not None else None
            ),
        })
    return rows


def top_impression_movers(rows: list[dict[str, Any]], gain: bool) -> list[dict[str, Any]]:
    fallback = -999 if gain else 999
    return sorted(
        rows,
        key=lambda row: (
            row["impressions_delta"], row["clicks_delta"],
            row["position_improvement"] if row["position_improvement"] is not None else fallback,
        ),
        reverse=gain,
    )[:10]


def normalize_dataforseo(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for item in items:
        keyword_data = item.get("keyword_data") or {}
        element = item.get("ranked_serp_element") or {}
        serp = element.get("serp_item") or {}
        keyword_info = keyword_data.get("keyword_info") or {}
        output.append({
            "keyword": keyword_data.get("keyword"),
            "position": serp.get("rank_absolute"),
            "url": serp.get("url"),
            "search_volume": keyword_info.get("search_volume"),
            "keyword_difficulty": element.get("keyword_difficulty"),
            "last_updated_time": element.get("last_updated_time"),
            "is_new": (serp.get("rank_changes") or {}).get("is_new"),
        })
    return output


def content_family(url: str, query: str = "") -> str:
    """Mirror the selector's commercial page classes for measured reporting."""
    path = urlparse(str(url)).path.lower()
    text = (str(query).lower() + " " + path.replace("-", " ")).strip()
    # Page-family evidence describes the canonical surface, not merely a
    # review-intent query. Onboarding owns /reviews/; a guide that contains
    # "review" remains a guide and must not inflate the review thesis.
    if path.startswith("/reviews/"):
        return "provider_review"
    if path.startswith(("/best/", "/compare/")) or " vs " in " " + text or " compare" in text:
        return "commercial_comparison"
    if any(token in text for token in (
        "cost", "price", "pricing", "coupon", "savings", "insurance", "afford",
    )):
        return "cost_pricing"
    if any(token in text for token in (
        "how to get", "buy ", "online", "legit", "verify", "prescription",
        "telehealth", "pharmacy", "availability", "access",
    )):
        return "medication_guide"
    return "education_condition"


def page_family_analysis(
    *,
    gsc_rows: list[dict[str, Any]],
    gsc_query_rows: list[dict[str, Any]],
    dfs_rows: list[dict[str, Any]],
    ga4: dict[str, Any],
    posthog: dict[str, Any],
    article_urls: set[str],
    gsc_window: str,
    captured: str,
) -> dict[str, Any]:
    families = {
        family: {
            "gsc": {"queries": set(), "pages": set(), "impressions": 0, "clicks": 0,
                    "position_numerator": 0.0, "rows": [], "page_rows": [],
                    "named_query_impressions": 0, "named_query_clicks": 0},
            "dataforseo": {"positions": [], "search_volume": 0, "rows": []},
            "ga4": {"sessions": 0, "views": 0, "pages": set()},
            "posthog": {"views": 0, "affiliate_clicks": 0, "intent_events": 0,
                        "pages": set()},
        }
        for family in (
            "provider_review", "commercial_comparison", "cost_pricing",
            "medication_guide", "education_condition",
        )
    }
    # Page-dimension totals retain clicks/impressions from anonymized queries.
    for row in gsc_rows:
        keys = row.get("keys") or []
        if not keys or keys[-1] not in article_urls:
            continue
        url = str(keys[-1])
        family = content_family(url)
        target = families[family]["gsc"]
        impressions = int(row.get("impressions") or 0)
        clicks = int(row.get("clicks") or 0)
        position = float(row.get("position") or 0)
        target["pages"].add(url)
        target["impressions"] += impressions
        target["clicks"] += clicks
        target["position_numerator"] += position * impressions
        target["page_rows"].append({
            "url": url, "impressions": impressions, "clicks": clicks,
            "position": position,
        })
    # Query+page rows name targets but omit anonymized-query traffic by design.
    for row in gsc_query_rows:
        keys = row.get("keys") or []
        if len(keys) < 2 or keys[-1] not in article_urls:
            continue
        query, url = str(keys[0]), str(keys[-1])
        family = content_family(url, query)
        target = families[family]["gsc"]
        impressions = int(row.get("impressions") or 0)
        clicks = int(row.get("clicks") or 0)
        target["queries"].add(query)
        target["named_query_impressions"] += impressions
        target["named_query_clicks"] += clicks
        target["rows"].append({
            "query": query, "url": url, "impressions": impressions,
            "clicks": clicks, "position": float(row.get("position") or 0),
        })
    for row in dfs_rows:
        url = str(row.get("url") or "")
        if url not in article_urls:
            continue
        family = content_family(url, str(row.get("keyword") or ""))
        target = families[family]["dataforseo"]
        position = float(row.get("position") or 999)
        target["positions"].append(position)
        target["search_volume"] += int(row.get("search_volume") or 0)
        target["rows"].append(row)
    article_paths = {urlparse(url).path for url in article_urls}
    for row in ga4.get("rows") or []:
        path = str((row.get("dimensions") or [""])[0])
        if path not in article_paths:
            continue
        family = content_family(path)
        target = families[family]["ga4"]
        metrics = row.get("metrics") or []
        target["sessions"] += int(float(metrics[0] or 0)) if metrics else 0
        target["views"] += int(float(metrics[1] or 0)) if len(metrics) > 1 else 0
        target["pages"].add(path)
    for row in posthog.get("rows") or []:
        path = str(row.get("path") or "")
        if path not in article_paths:
            continue
        family = content_family(path)
        target = families[family]["posthog"]
        target["views"] += int(row.get("views") or 0)
        target["affiliate_clicks"] += int(row.get("clicks") or 0)
        target["intent_events"] += int(row.get("intent_events") or 0)
        target["pages"].add(path)

    output: dict[str, Any] = {}
    all_dfs: list[tuple[float, str, str]] = []
    for family, signals in families.items():
        gsc = signals["gsc"]
        dfs = signals["dataforseo"]
        positions = dfs["positions"]
        for row in dfs["rows"]:
            all_dfs.append((float(row.get("position") or 999), family,
                            str(row.get("keyword") or "")))
        post = signals["posthog"]
        page_rows = sorted(
            gsc["page_rows"],
            key=lambda row: (-row["clicks"], -row["impressions"], row["position"], row["url"]),
        )
        top_page_clicks = page_rows[0]["clicks"] if page_rows else 0
        output[family] = {
            "gsc": {
                "classification": "MEASURED",
                "date_range": gsc_window,
                "source": "GSC Search Analytics page totals plus query+page target detail; direct user-OAuth sc-domain property",
                "query_count": len(gsc["queries"]),
                "page_count": len(gsc["pages"]),
                "impressions": gsc["impressions"],
                "clicks": gsc["clicks"],
                "named_query_impressions": gsc["named_query_impressions"],
                "named_query_clicks": gsc["named_query_clicks"],
                "query_privacy_note": "Named query rows omit anonymized queries; page totals are authoritative for traffic.",
                "clicking_pages": sum(row["clicks"] > 0 for row in page_rows),
                "top_page_click_concentration": (
                    round(top_page_clicks / gsc["clicks"], 6) if gsc["clicks"] else None
                ),
                "top_pages_by_clicks": page_rows[:10],
                "weighted_average_position": (
                    round(gsc["position_numerator"] / gsc["impressions"], 4)
                    if gsc["impressions"] else None
                ),
                "top_queries_by_position": sorted(
                    gsc["rows"], key=lambda row: (row["position"], -row["impressions"], row["query"])
                )[:10],
            },
            "dataforseo": {
                "classification": "SOURCE-REPORTED",
                "captured_at_utc": captured,
                "keyword_count": len(positions),
                "best_position": min(positions) if positions else None,
                "median_position": statistics.median(positions) if positions else None,
                "top_20": sum(position <= 20 for position in positions),
                "top_50": sum(position <= 50 for position in positions),
                "summed_search_volume_not_traffic": dfs["search_volume"],
                "top_keywords": sorted(
                    dfs["rows"], key=lambda row: (row.get("position") or 999,
                                                    -(row.get("search_volume") or 0))
                )[:10],
            },
            "ga4": {
                "classification": "SOURCE-REPORTED",
                "date_range": gsc_window,
                "source": "GA4 direct user-OAuth, production hostname filter",
                "sessions": signals["ga4"]["sessions"],
                "views": signals["ga4"]["views"],
                "page_count": len(signals["ga4"]["pages"]),
            },
            "posthog": {
                "classification": "SOURCE-REPORTED",
                "date_range": "rolling 14 days",
                "source": "PostHog HogQL host/path-filtered",
                "views": post["views"],
                "affiliate_clicks": post["affiliate_clicks"],
                "intent_events": post["intent_events"],
                "page_count": len(post["pages"]),
                "affiliate_click_rate": (
                    round(post["affiliate_clicks"] / post["views"], 6)
                    if post["views"] else None
                ),
            },
        }
    top_12 = sorted(all_dfs)[:12]
    review_top_12 = sum(family == "provider_review" for _, family, _ in top_12)
    review = output["provider_review"]
    non_review_best = [
        family["dataforseo"]["best_position"] for key, family in output.items()
        if key != "provider_review" and family["dataforseo"]["best_position"] is not None
    ]
    rankability_supported = bool(
        review["dataforseo"]["keyword_count"] >= 5
        and review["dataforseo"]["best_position"] is not None
        and non_review_best
        and review["dataforseo"]["best_position"] + 5 <= min(non_review_best)
        and review["gsc"]["impressions"] >= 25
        and review["gsc"]["weighted_average_position"] is not None
    )
    acquisition_supported = bool(
        review["gsc"]["clicks"] >= 5
        and review["gsc"]["clicking_pages"] >= 2
        and review["gsc"]["impressions"] >= 100
    )
    conversion_proven = bool(
        review["posthog"]["views"] >= 25
        and review["posthog"]["affiliate_clicks"] >= 2
    )
    return {
        "classification": "MEASURED_AND_SOURCE_REPORTED",
        "families": output,
        "hypothesis_test": {
            "status": (
                "SUPPORTED_FOR_RANKABILITY_AND_CONVERSION" if rankability_supported and conversion_proven
                else "SUPPORTED_FOR_RANKABILITY_NOT_YET_CONVERSION" if rankability_supported
                else "NOT_SUPPORTED"
            ),
            "provider_review_rankability_supported": rankability_supported,
            "provider_review_acquisition_supported": acquisition_supported,
            "provider_review_conversion_proven": conversion_proven,
            "provider_reviews_in_best_12_dataforseo_keywords": review_top_12,
            "best_12_dataforseo_keywords": [
                {"position": position, "family": family, "keyword": keyword}
                for position, family, keyword in top_12
            ],
            "acquisition_caveat": (
                "GSC page totals retain anonymized-query traffic. Review acquisition is measured, "
                "but click distribution remains concentrated and is not revenue attribution."
            ),
            "conversion_caveat": (
                "PostHog sample is too small to claim provider-review conversion. "
                "Rankability and Google acquisition are measured; conversion is not yet proven."
            ),
        },
    }


def verdict_for(report_date: dt.date, current: dict[str, Any], prior: dict[str, Any],
                top20: int, indexed: int, indexable: int,
                current_production_events: int) -> dict[str, Any]:
    if current_production_events == 0:
        status, risk = "NOT_WORKING", "not_publishing"
        explanation = "The machine produced no new routes or successful R2 refreshes this week."
    elif indexable >= 20 and indexed / indexable < 0.5:
        status, risk = "NOT_WORKING", "published_but_not_indexed"
        explanation = "Fewer than half of indexable published URLs are indexed by Google."
    elif top20 is None:
        status, risk = "TOO_EARLY", "independent_rank_measurement_unknown"
        explanation = "Independent rank evidence is unavailable; no zero ranking or content-stop conclusion is inferred."
    elif current["clicks"] >= 5 and current["clicks"] > prior["clicks"] and top20 > 0:
        status, risk = "WORKING", "ranking_progress"
        explanation = "Clicks are growing and at least one independently measured keyword is top 20."
    elif report_date >= VERDICT_DEADLINE and (current["clicks"] <= prior["clicks"] or top20 == 0):
        status, risk = "NOT_WORKING", "indexed_but_not_ranking_or_growing"
        explanation = "The four-cycle proving window expired without both click growth and a top-20 ranking."
    else:
        status, risk = "TOO_EARLY", "indexed_but_mostly_ranking_too_low"
        explanation = (
            "Google is indexing and testing the pages, but the latest complete week does not yet show "
            "useful traffic. Continue only through the four-cycle proving window while leading indicators improve."
        )
    return {
        "status": status,
        "failure_risk": risk,
        "plain_language": explanation,
        "working_now_for_traffic": status == "WORKING",
        "next_verdict_date": VERDICT_DEADLINE.isoformat(),
    }


def dataforseo_collection(module_input: Path | None = None) -> dict[str, Any]:
    if module_input is None:
        return DataForSEO().ranked_keywords(
            ["glpclinicscout.com"], page_size=1000, max_pages_per_target=3, max_cost_usd=1.0)
    manifest = json.loads(Path(module_input).read_text())
    binding = manifest['inputs']['footprint']
    path = Path(binding['path'])
    if path.is_symlink() or sha256(path) != binding['sha256']:
        raise ValueError('shared_dfs_snapshot_binding_invalid')
    snapshot = json.loads(path.read_text())
    result = snapshot['result']
    own = result.get('domains', {}).get('glpclinicscout.com', {})
    if result.get('ok') is True and own.get('complete') is not True:
        result = {**result, 'ok': False, 'state': 'UNKNOWN_INCOMPLETE_OWN_DOMAIN'}
    return {**result, 'shared_snapshot': binding, 'captured_at_utc': snapshot['captured_at_utc'], 'new_collection_cost_usd': 0}


def collect(report_date: dt.date, module_input: Path | None = None) -> dict[str, Any]:
    windows = report_windows(report_date)
    ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
    seeded, new_routes = published_articles(ledger)
    article_routes = [row["route"] for row in seeded + new_routes]
    article_urls = {SITE + route for route in article_routes}
    r2_rows = successful_r2_refreshes(R2_ROOT)

    google = GoogleRail()
    google._access_token()  # cache once before concurrent URL inspections
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        inspections = list(executor.map(google.gsc_url_inspection, sorted(article_urls)))
    if len(inspections) != len(article_urls) or not all(row.get("ok") for row in inspections):
        raise RuntimeError("gsc_url_inspection_incomplete")

    def gsc_rows(window: tuple[dt.date, dt.date], dimensions: list[str]) -> dict[str, Any]:
        result = google.gsc_search_analytics(
            start_date=window[0].isoformat(), end_date=window[1].isoformat(),
            dimensions=dimensions, row_limit=25000, fetch_all=True,
        )
        if not result.get("ok") or not result.get("complete"):
            raise RuntimeError("gsc_search_analytics_incomplete:" + str(result.get("error")))
        return result

    gsc_current = gsc_rows(windows["gsc_current"], ["page"])
    gsc_prior = gsc_rows(windows["gsc_prior"], ["page"])
    gsc_current_query = gsc_rows(windows["gsc_current"], ["query", "page"])
    gsc_prior_query = gsc_rows(windows["gsc_prior"], ["query", "page"])
    ga4 = google.ga4_run_report(
        start_date=windows["gsc_current"][0].isoformat(),
        end_date=windows["gsc_current"][1].isoformat(),
        dimensions=["pagePath"], metrics=["sessions", "screenPageViews"],
        limit=250000, fetch_all=True,
    )
    posthog = PostHog().page_conversion(days=14)

    dfs_result = dataforseo_collection(module_input)
    dfs_measured = dfs_result.get('ok') is True
    if not dfs_measured and module_input is None:
        raise RuntimeError("dataforseo_ranked_keywords_failed:" + str(dfs_result.get("error")))

    cogny = connect(timeout=180, retries=2)
    bing_query = clean_bing_rows(unwrap_cogny_rows(
        cogny.call(TOOL["bing_query_stats"], {"site_url": SITE + "/"})
    ), "query")
    bing_page = clean_bing_rows(unwrap_cogny_rows(
        cogny.call(TOOL["bing_page_stats"], {"site_url": SITE + "/"})
    ), "page")

    indexed = {row["url"] for row in inspections if row.get("verdict") == "PASS"}
    redirected = {row["url"] for row in inspections
                  if row.get("coverage_state") == "Page with redirect"}
    not_indexed = [row for row in inspections
                   if row.get("verdict") != "PASS"
                   and row.get("coverage_state") != "Page with redirect"]
    current_metrics = aggregate_gsc(gsc_current["rows"], indexed)
    prior_metrics = aggregate_gsc(gsc_prior["rows"], indexed)
    all_current = aggregate_gsc(gsc_current["rows"], article_urls)
    all_prior = aggregate_gsc(gsc_prior["rows"], article_urls)
    query_movers = mover_rows(gsc_current_query["rows"], gsc_prior_query["rows"], article_urls, "query")
    page_movers = mover_rows(gsc_current["rows"], gsc_prior["rows"], article_urls, "page")

    dfs_domain = (dfs_result.get("domains") or {}).get("glpclinicscout.com") or {}
    dfs_rows = normalize_dataforseo(dfs_domain.get("items") or []) if dfs_measured else []
    top20 = sum((row.get("position") or 999) <= 20 for row in dfs_rows) if dfs_measured else None

    def in_window(value: str | None, window: tuple[dt.date, dt.date]) -> bool:
        if not value:
            return False
        day = dt.date.fromisoformat(value[:10])
        return window[0] <= day <= window[1]

    new_current = [row for row in new_routes
                   if in_window(row.get("seen_at_utc"), windows["publication_current"])]
    new_prior = [row for row in new_routes
                 if in_window(row.get("seen_at_utc"), windows["publication_prior"])]
    r2_current = [row for row in r2_rows
                  if in_window(row.get("completed_at_utc"), windows["publication_current"])]
    r2_prior = [row for row in r2_rows
                if in_window(row.get("completed_at_utc"), windows["publication_prior"])]
    production_events = len(new_current) + len(r2_current)

    path_set = {urlparse(url).path for url in article_urls}
    ga4_sessions = ga4_views = ga4_pages = 0
    for row in ga4.get("rows") or []:
        if (row.get("dimensions") or [None])[0] in path_set:
            ga4_pages += 1
            ga4_sessions += int(float(row["metrics"][0]))
            ga4_views += int(float(row["metrics"][1]))
    posthog_views = posthog_clicks = posthog_intents = posthog_pages = 0
    for row in posthog.get("rows") or []:
        if row.get("path") in path_set:
            posthog_pages += 1
            posthog_views += int(row.get("views") or 0)
            posthog_clicks += int(row.get("clicks") or 0)
            posthog_intents += int(row.get("intent_events") or 0)

    captured = utc_now()
    family_analysis = page_family_analysis(
        gsc_rows=gsc_current["rows"], gsc_query_rows=gsc_current_query["rows"],
        dfs_rows=dfs_rows, ga4=ga4, posthog=posthog,
        article_urls=article_urls, gsc_window=date_range_text(windows["gsc_current"]),
        captured=captured,
    )
    verdict = verdict_for(report_date, current_metrics, prior_metrics, top20,
                          len(indexed), len(article_urls) - len(redirected), production_events)
    report: dict[str, Any] = {
        "schema": "glp.weekly-seo-ranking-report.v1",
        "report_date": report_date.isoformat(),
        "captured_at_utc": captured,
        "verdict": verdict,
        "headline": {
            "published_article_urls": metric(len(article_urls), str(LEDGER), unit="URLS",
                                               note=(f"{len(seeded)} were already present when the ledger "
                                                     f"was seeded; {len(new_routes)} were new afterward.")),
            "google_indexed": metric(len(indexed),
                                     "GSC URL Inspection direct user-OAuth sc-domain property",
                                     captured, unit="URLS"),
            "google_clicks_latest_complete_week_indexed_pages": metric(
                current_metrics["clicks"], "GSC Search Analytics direct user-OAuth",
                date_range_text(windows["gsc_current"]), unit="CLICKS"),
        },
        "publishing": {
            "ledger_total_routes": metric(len(ledger.get("routes") or {}), str(LEDGER), unit="ROUTES"),
            "article_routes": metric(len(article_urls), str(LEDGER), unit="URLS"),
            "seeded_article_routes_original_publish_date_unknown": metric(
                len(seeded), str(LEDGER), unit="URLS",
                note="seen_at_utc is ledger seed observation, not original publication time"),
            "new_article_routes_after_seed": metric(len(new_routes), str(LEDGER), unit="URLS"),
            "new_routes_current_publication_week": metric(
                len(new_current), str(LEDGER), date_range_text(windows["publication_current"]), unit="URLS"),
            "new_routes_prior_publication_week": metric(
                len(new_prior), str(LEDGER), date_range_text(windows["publication_prior"]), unit="URLS"),
            "new_route_rows": new_routes,
            "r2_refresh_events": metric(len(r2_rows), "R2 final publication receipts",
                                        unit="REFRESH_EVENTS"),
            "r2_unique_existing_urls_refreshed": metric(
                len({row["live_url"] for row in r2_rows}), "R2 final publication receipts", unit="URLS"),
            "r2_refreshes_current_publication_week": metric(
                len(r2_current), "R2 final publication receipts",
                date_range_text(windows["publication_current"]), unit="REFRESH_EVENTS"),
            "r2_refreshes_prior_publication_week": metric(
                len(r2_prior), "R2 final publication receipts",
                date_range_text(windows["publication_prior"]), unit="REFRESH_EVENTS"),
            "combined_page_production_events_current_week": metric(
                production_events, "published-route ledger plus R2 final receipts",
                date_range_text(windows["publication_current"]), unit="NEW_OR_REFRESH_EVENTS"),
            "r2_refresh_rows": r2_rows,
        },
        "google_indexation": {
            "property": GSC_PROPERTY,
            "authority": "GSC URL Inspection direct user-OAuth",
            "inspection_count": metric(len(inspections), "GSC URL Inspection", captured, unit="URLS"),
            "indexed": metric(len(indexed), "GSC verdict=PASS", captured, unit="URLS"),
            "indexed_rate_all_ledger_article_urls": metric(
                len(indexed) / len(article_urls), "GSC URL Inspection + route ledger", captured,
                unit="RATIO"),
            "redirect_sources_not_expected_to_index": metric(
                len(redirected), "GSC coverageState=Page with redirect", captured, unit="URLS"),
            "indexable_non_redirect_urls": metric(
                len(article_urls) - len(redirected), "GSC URL Inspection + route ledger", captured,
                unit="URLS"),
            "indexed_rate_excluding_redirect_sources": metric(
                len(indexed) / (len(article_urls) - len(redirected)),
                "GSC URL Inspection + route ledger", captured, unit="RATIO"),
            "not_indexed_excluding_redirects": metric(
                len(not_indexed), "GSC URL Inspection", captured, unit="URLS"),
            "coverage_state_counts": {
                key: metric(value, "GSC URL Inspection", captured, unit="URLS")
                for key, value in sorted(Counter(row.get("coverage_state") or "ERROR"
                                                  for row in inspections).items())
            },
            "inspection_rows": inspections,
        },
        "google_ranking": {
            "source": "GSC Search Analytics direct user-OAuth; page totals preserve anonymized-query traffic, query+page rows name targets",
            "current_window": date_range_text(windows["gsc_current"]),
            "prior_window": date_range_text(windows["gsc_prior"]),
            "indexed_pages_current": current_metrics,
            "indexed_pages_prior": prior_metrics,
            "week_over_week": {
                "impressions_delta": metric(
                    current_metrics["impressions"] - prior_metrics["impressions"],
                    "derived from GSC indexed-page windows", unit="IMPRESSIONS"),
                "clicks_delta": metric(
                    current_metrics["clicks"] - prior_metrics["clicks"],
                    "derived from GSC indexed-page windows", unit="CLICKS"),
                "average_position_improvement": metric(
                    prior_metrics["weighted_average_position"] - current_metrics["weighted_average_position"],
                    "derived from impression-weighted GSC positions; positive is better", unit="POSITIONS"),
                "pages_with_impressions_delta": metric(
                    current_metrics["pages_with_impressions"] - prior_metrics["pages_with_impressions"],
                    "derived from GSC indexed-page windows", unit="PAGES"),
            },
            "all_ledger_articles_current": all_current,
            "all_ledger_articles_prior": all_prior,
            "query_gains_top10_by_impressions": {
                "classification": "MEASURED", "source": "GSC query+page",
                "rows": top_impression_movers(query_movers, True)},
            "query_losses_top10_by_impressions": {
                "classification": "MEASURED", "source": "GSC query+page",
                "rows": top_impression_movers(query_movers, False)},
            "page_gains_top10_by_impressions": {
                "classification": "MEASURED", "source": "GSC query+page",
                "rows": top_impression_movers(page_movers, True)},
            "page_losses_top10_by_impressions": {
                "classification": "MEASURED", "source": "GSC query+page",
                "rows": top_impression_movers(page_movers, False)},
            "all_query_movers": {"classification": "MEASURED", "source": "GSC", "rows": query_movers},
            "all_page_movers": {"classification": "MEASURED", "source": "GSC", "rows": page_movers},
        },
        "page_family_analysis": family_analysis,
        "dataforseo_context": {
            "classification": "SOURCE-REPORTED",
            "captured_at_utc": captured,
            "target": "glpclinicscout.com",
            "actual_cost_usd": dfs_result.get("cost_usd"),
            "ranked_keywords": len(dfs_rows),
            "top_3": sum((row.get("position") or 999) <= 3 for row in dfs_rows),
            "top_10": sum((row.get("position") or 999) <= 10 for row in dfs_rows),
            "top_20": top20,
            "top_50": sum((row.get("position") or 999) <= 50 for row in dfs_rows),
            "positions_51_to_100": sum(51 <= (row.get("position") or 0) <= 100
                                       for row in dfs_rows),
            "summed_search_volume_not_traffic": sum(row.get("search_volume") or 0
                                                     for row in dfs_rows),
            "rows": dfs_rows,
            "interpretation": (
                "Search volume is demand context, not site traffic. High-volume terms ranking "
                "near the bottom of the first 100 are not useful traffic yet."
            ),
        },
        "bing": {
            "classification": "SOURCE-REPORTED",
            "source": "Cogny Bing Webmaster Tools",
            "site_url": SITE + "/",
            "query_weekly": aggregate_bing(bing_query),
            "page_weekly": aggregate_bing(bing_page),
            "query_rows": bing_query,
            "page_rows": bing_page,
        },
        "downstream_corroboration": {
            "ga4_article_sessions": metric(
                ga4_sessions, "GA4 direct user-OAuth, production hostname filter",
                date_range_text(windows["gsc_current"]), classification="SOURCE-REPORTED",
                unit="SESSIONS"),
            "ga4_article_pageviews": metric(
                ga4_views, "GA4 direct user-OAuth, production hostname filter",
                date_range_text(windows["gsc_current"]), classification="SOURCE-REPORTED",
                unit="VIEWS"),
            "posthog_article_views": metric(
                posthog_views, "PostHog HogQL host/path-filtered", "rolling 14 days",
                classification="SOURCE-REPORTED", unit="VIEWS"),
            "posthog_article_affiliate_clicks": metric(
                posthog_clicks, "PostHog HogQL affiliate_click events", "rolling 14 days",
                classification="SOURCE-REPORTED", unit="AFFILIATE_CLICKS"),
            "warning": "GA4/PostHog corroborate visits only; they do not prove Google indexation or ranking.",
        },
        "evidence": [],
        "effects": {
            "site_deploys": 0,
            "sheet_writes": 0,
            "tracking_links_opened_or_clicked": 0,
            "dataforseo_actual_cost_usd": dfs_result.get("cost_usd"),
        },
        "skills_mcps_data_rails": {
            "skills": [
                "dogfood/operator-review-reporting",
                "marketing/content-strategy",
                "dogfood/loopos-production-loops",
                "email/google-workspace-oauth-operations",
                "research/grounded-citations",
                "mcp/mcp-operations",
                "social-media/discord-rest-messaging",
            ],
            "mcps": ["Cogny Bing Webmaster Tools", "n8n MCP/fail-closed native fallback"],
            "data_rails": [
                str(LEDGER), "R2 final publication receipts",
                "GSC direct user-OAuth sc-domain property", "Cogny Bing Webmaster Tools",
                "DataForSEO", "GA4 direct user-OAuth", "PostHog",
            ],
        },
    }
    for path in (LEDGER, BUSINESS / "ops/glp/seo_data_rails.py"):
        report["evidence"].append({"path": str(path), "sha256": sha256(path),
                                   "bytes": path.stat().st_size})
    report['dataforseo_context']['captured_at_utc'] = dfs_result.get('captured_at_utc', captured)
    report['dataforseo_context']['shared_snapshot'] = dfs_result.get('shared_snapshot')
    report['effects']['dataforseo_actual_cost_usd'] = dfs_result.get('new_collection_cost_usd', dfs_result.get('cost_usd'))
    if not dfs_measured:
        dfs = report['dataforseo_context']
        dfs['classification'] = 'UNKNOWN_NOT_ZERO'
        dfs['state'] = dfs_result.get('state', 'UNKNOWN_PROVIDER_OR_TRANSPORT')
        for key in ('ranked_keywords','top_3','top_10','top_20','top_50','positions_51_to_100','summed_search_volume_not_traffic'):
            dfs[key] = None
        for family in report['page_family_analysis']['families'].values():
            fields = family['dataforseo']
            fields['classification'] = 'UNKNOWN_NOT_ZERO'
            for key in ('keyword_count','best_position','median_position','top_20','top_50','summed_search_volume_not_traffic'):
                fields[key] = None
        report['page_family_analysis']['hypothesis_test'].update(status='UNKNOWN_DFS_RANKABILITY', provider_reviews_in_best_12_dataforseo_keywords=None)
    return report


def render_discord(report: dict[str, Any]) -> str:
    headline = report["headline"]
    rank = report["google_ranking"]
    current = rank["indexed_pages_current"]
    prior = rank["indexed_pages_prior"]
    publication = report["publishing"]
    indexation = report["google_indexation"]
    dfs = {k: 'UNKNOWN' if v is None else v for k, v in report["dataforseo_context"].items()}
    verdict = report["verdict"]
    gains = rank["query_gains_top10_by_impressions"]["rows"]
    losses = rank["query_losses_top10_by_impressions"]["rows"]
    gain = gains[0] if gains else None
    loss = losses[0] if losses else None
    movement = []
    if gain:
        movement.append(f"gain: ‘{gain['key']}’ {gain['impressions_delta']:+d} impressions")
    if loss:
        movement.append(f"loss: ‘{loss['key']}’ {loss['impressions_delta']:+d} impressions")
    movement_text = "; ".join(movement) or "no query movement measured"
    indexed = headline["google_indexed"]["value"]
    total = headline["published_article_urls"]["value"]
    clicks = headline["google_clicks_latest_complete_week_indexed_pages"]["value"]
    indexable = indexation["indexable_non_redirect_urls"]["value"]
    production = publication["combined_page_production_events_current_week"]["value"]
    message = f"""{'🟢' if verdict['status'] == 'WORKING' else '🟡' if verdict['status'] == 'TOO_EARLY' else '🔴'} **SEO MACHINE VERDICT: {verdict['status'].replace('_', ' ')}**
**{total} article URLs published · {indexed} Google-indexed · {clicks} Google click{'s' if clicks != 1 else ''} in the latest complete week.**

**Google WoW ({rank['current_window']} vs {rank['prior_window']})**
• Indexed-page impressions: **{current['impressions']} vs {prior['impressions']}** ({current['impressions']-prior['impressions']:+d})
• Clicks: **{current['clicks']} vs {prior['clicks']}** ({current['clicks']-prior['clicks']:+d})
• Avg position: **{current['weighted_average_position']:.1f} vs {prior['weighted_average_position']:.1f}** ({prior['weighted_average_position']-current['weighted_average_position']:+.1f} better)
• Indexed pages with impressions: **{current['pages_with_impressions']}/{indexed}**
• DataForSEO: **{dfs['ranked_keywords']} ranked keywords, {dfs['top_20']} top-20, {dfs['top_50']} top-50**. Impressions/search volume are not traffic.
• Targeting evidence: **{report['page_family_analysis']['hypothesis_test']['provider_reviews_in_best_12_dataforseo_keywords']}/12 best-ranked keywords are provider reviews**; review pages earned **{report['page_family_analysis']['families']['provider_review']['gsc']['clicks']} Google clicks** vs **{sum(family['gsc']['clicks'] for key, family in report['page_family_analysis']['families'].items() if key != 'provider_review')}** elsewhere. Conversion is not yet proven.

**Publishing:** **{production} events** this week = {publication['new_routes_current_publication_week']['value']} new + {publication['r2_refreshes_current_publication_week']['value']} R2 refreshes.
**Indexing:** {indexed}/{total} overall; **{indexed}/{indexable} indexable URLs** after excluding redirects. {indexation['not_indexed_excluding_redirects']['value']} are not indexed.
**Movers:** {movement_text}.

**Plain answer:** {verdict['plain_language']}
-# Full report: {REPORT_ROOT / (report['report_date'] + '.json')}"""
    if len(message) > 1900:
        raise RuntimeError(f"discord_report_too_long:{len(message)}")
    return message


def load_discord_token() -> str:
    fd = os.open(DISCORD_TOKEN, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) % 64 != 0:
            raise RuntimeError("discord_token_file_not_secure_regular")
        token = os.read(fd, 8192).decode("utf-8").strip()
    finally:
        os.close(fd)
    if not token:
        raise RuntimeError("discord_token_file_empty")
    return token


def post_discord_once(report_date: dt.date, message: str) -> dict[str, Any]:
    receipt_path = REPORT_ROOT / f"{report_date.isoformat()}-discord-receipt.json"
    intent_path = REPORT_ROOT / f"{report_date.isoformat()}-discord-intent.json"
    write_json(intent_path, {
        "schema": "glp.weekly-seo-ranking-discord-intent.v1",
        "channel_id": DISCORD_CHANNEL,
        "content": message,
        "content_sha256": hashlib.sha256(message.encode()).hexdigest(),
        "created_at_utc": utc_now(),
    })
    token = load_discord_token()
    headers = {
        "Authorization": "Bot " + token,
        "Content-Type": "application/json",
        "User-Agent": "DiscordBot (https://discordapp.com, 10)",
    }
    base = f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL}/messages"
    prior = (json.loads(receipt_path.read_text(encoding="utf-8"))
             if receipt_path.is_file() else None)
    if prior and prior.get("message_id"):
        message_id = str(prior["message_id"])
        request = urllib.request.Request(
            f"{base}/{message_id}", headers=headers, method="GET")
        with urllib.request.urlopen(request, timeout=30) as response:
            existing = json.loads(response.read())
        if existing.get("content") == message:
            return {"status": "already_posted", "message_id": message_id,
                    "receipt": str(receipt_path)}
        request = urllib.request.Request(
            f"{base}/{message_id}", data=json.dumps({"content": message}).encode(),
            headers=headers, method="PATCH")
        with urllib.request.urlopen(request, timeout=30) as response:
            posted = json.loads(response.read())
        delivery_mode = "corrected_in_place"
    else:
        request = urllib.request.Request(
            base, data=json.dumps({"content": message}).encode(), headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=30) as response:
            posted = json.loads(response.read())
        message_id = posted["id"]
        delivery_mode = "new_message"
    request = urllib.request.Request(f"{base}/{message_id}", headers=headers, method="GET")
    with urllib.request.urlopen(request, timeout=30) as response:
        readback = json.loads(response.read())
    receipt = {
        "schema": "glp.weekly-seo-ranking-discord-delivery.v1",
        "status": "PASS" if readback.get("content") == message else "FAIL",
        "channel_id": DISCORD_CHANNEL,
        "message_id": message_id,
        "delivery_mode": delivery_mode,
        "content_sha256": hashlib.sha256(message.encode()).hexdigest(),
        "exact_readback": readback.get("content") == message,
        "intent_path": str(intent_path),
        "secret_printed": False,
        "posted_at_utc": posted.get("edited_timestamp") or posted.get("timestamp"),
    }
    write_json(receipt_path, receipt)
    if receipt["status"] != "PASS":
        raise RuntimeError("discord_exact_readback_failed")
    return {"status": delivery_mode, "message_id": message_id,
            "receipt": str(receipt_path)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-date")
    parser.add_argument("--module-input", type=Path)
    parser.add_argument("--post-discord", action="store_true")
    args = parser.parse_args(argv)
    report_date = (dt.date.fromisoformat(args.report_date) if args.report_date
                   else dt.datetime.now(PT).date())
    report = collect(report_date, args.module_input)
    report_path = REPORT_ROOT / f"{report_date.isoformat()}.json"
    write_json(report_path, report)
    discord = (post_discord_once(report_date, render_discord(report))
               if args.post_discord else {"status": "not_requested", "message_id": None})
    summary = {
        "machine": "glp_weekly_seo_ranking_report",
        "status": "PASS",
        "report_date": report_date.isoformat(),
        "verdict": report["verdict"]["status"],
        "published": report["headline"]["published_article_urls"]["value"],
        "indexed": report["headline"]["google_indexed"]["value"],
        "clicks": report["headline"]["google_clicks_latest_complete_week_indexed_pages"]["value"],
        "report": str(report_path),
        "report_sha256": sha256(report_path),
        "discord_status": discord["status"],
        "message_id": discord.get("message_id"),
        "receipt": discord.get("receipt") or str(report_path),
    }
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
