#!/usr/bin/env python3
"""Refresh exact-query Firecrawl evidence for strategy topics short on supply.

The measured keyword corpus remains the primary demand source. This companion
only supplies current competitor URLs for exact strategy queries that otherwise
have fewer than two distinct evidence URLs. It writes no site content and never
prints the Firecrawl credential.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

BUSINESS = Path("/home/ubuntu/business")
DEFAULT_FEED = BUSINESS / (
    "reports/glp-clinic-scout/seo-blog-machine-20260730/competitor-gap-feed-current.json"
)
DEFAULT_OUTPUT = BUSINESS / (
    "reports/glp-clinic-scout/seo-machine/topic-supply/firecrawl-gap-evidence-current.json"
)
SCHEMA = "glp-topic-supply-firecrawl-evidence-v1"
SEARCH_ENDPOINT = "https://api.firecrawl.dev/v2/search"
MAX_TARGETS = 40
RESULT_LIMIT = 3
REFRESH_AFTER_HOURS = 168
PAGE_TWO_REPORT = BUSINESS / 'reports/glp-clinic-scout/seo-machine/page-two/firecrawl-serp-pack-current.json'


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_utc(value: dt.datetime | None = None) -> str:
    return (value or now_utc()).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"json_object_required:{path}")
    return value


def api_key() -> str:
    value = os.environ.get("FIRECRAWL_API_KEY", "").strip()
    if value:
        return value
    env_path = Path.home() / ".hermes/.env"
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("FIRECRAWL_API_KEY="):
                value = line.split("=", 1)[1].strip().strip('"').strip("'")
                if value:
                    return value
    raise RuntimeError("firecrawl_api_key_missing")


def canonical_url(value: Any) -> str | None:
    try:
        parsed = urlsplit(str(value or "").strip())
    except ValueError:
        return None
    if parsed.scheme != "https" or not parsed.hostname:
        return None
    if parsed.hostname.lower() in {"glpclinicscout.com", "www.glpclinicscout.com"}:
        return None
    return urlunsplit(("https", parsed.netloc.lower(), parsed.path or "/", "", ""))


def normalized_results(payload: dict[str, Any]) -> tuple[str | None, list[dict[str, Any]]]:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    rows = data.get("web") if isinstance(data, dict) else None
    search_id = payload.get("id") or (data.get("id") if isinstance(data, dict) else None)
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        url = canonical_url(row.get("url"))
        if not url or url in seen:
            continue
        seen.add(url)
        results.append({
            "url": url,
            "host": urlsplit(url).hostname,
            "title": str(row.get("title") or "")[:500],
            "description": str(row.get("description") or "")[:1200],
            "markdown": str(row.get("markdown") or ""),
            "metadata": row.get("metadata"),
        })
    return str(search_id) if search_id else None, results


def firecrawl_search(key: str, query: str) -> tuple[str | None, list[dict[str, Any]]]:
    body = json.dumps({"query": query, "limit": RESULT_LIMIT, "sources": ["web"],
                       "location": "United States", "country": "US", "tbs": "qdr:w",
                       "scrapeOptions": {"formats": ["markdown"], "onlyMainContent": True, "proxy": "basic"}}).encode()
    request = urllib.request.Request(
        SEARCH_ENDPOINT,
        data=body,
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        import sys
        sys.path.insert(0, str(BUSINESS / 'scripts'))
        from firecrawl_client import firecrawl_urlopen
        with firecrawl_urlopen(request, caller='seo.refresh_topic_supply_firecrawl', timeout=90) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"firecrawl_http_{exc.code}") from exc
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise RuntimeError("firecrawl_search_failed")
    return normalized_results(payload)


def target_map(feed: dict[str, Any], prior: dict[str, Any] | None) -> dict[str, str]:
    targets: dict[str, str] = {}
    for row in feed.get("research_backlog") or []:
        if not isinstance(row, dict) or row.get("reason") not in {"insufficient_measured_competitor_evidence", "HOLD_INSUFFICIENT_MEASURED_EVIDENCE"}:
            continue
        query = str(row.get("target_query") or row.get("keyword") or "").strip()
        route = str(row.get("target_route") or "").strip()
        if query and route:
            targets[query] = route
    if isinstance(prior, dict) and prior.get("schema") == SCHEMA:
        for row in prior.get("query_plan", prior.get("targets")) or []:
            if not isinstance(row, dict):
                continue
            query = str(row.get("target_query") or "").strip()
            route = str(row.get("target_route") or "").strip()
            if query and route:
                targets.setdefault(query, route)
    return dict(list(sorted(targets.items()))[:MAX_TARGETS])


def prior_is_fresh(prior: dict[str, Any] | None, targets: dict[str, str]) -> bool:
    if not isinstance(prior, dict) or prior.get("schema") != SCHEMA:
        return False
    # Attempted scope is not competitor evidence. Only explicit empty responses,
    # never unknown/failed retrievals, can complete the reusable weekly plan.
    failures = prior.get("failures") or []
    if any(not isinstance(row, dict) or row.get("reason") != "no_web_results" for row in failures):
        return False
    prior_targets = {
        str(row.get("target_query")): str(row.get("target_route"))
        for row in prior.get("query_plan", prior.get("targets")) or [] if isinstance(row, dict)
    }
    if "query_plan" not in prior:
        # Legacy genuine packs retained empty responses only in failures. Resolve
        # their routes from current input without rewriting or redating receipts.
        for row in failures:
            query = str(row.get("target_query") or "").strip()
            if query in targets:
                prior_targets[query] = targets[query]
    if prior_targets != targets:
        return False
    try:
        captured = dt.datetime.fromisoformat(str(prior.get("captured_at_utc")).replace("Z", "+00:00"))
    except ValueError:
        return False
    return (now_utc() - captured).total_seconds() < REFRESH_AFTER_HOURS * 3600


def atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--feed", type=Path, default=DEFAULT_FEED)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--page-two-report", type=Path, default=PAGE_TWO_REPORT)
    parser.add_argument("--force", action="store_true", help="Refresh the existing target set now, ignoring the weekly cache")
    args = parser.parse_args()

    feed = read_json(args.feed)
    prior = read_json(args.output) if args.output.is_file() else None
    targets = target_map(feed, prior)
    if not targets:
        print(json.dumps({"status": "PASS_NO_TARGETS", "provider_calls": 0, "targets": 0}))
        return 0
    if not args.force and prior_is_fresh(prior, targets):
        print(json.dumps({
            "status": "PASS_FRESH_REUSE",
            "provider_calls": 0,
            "targets": len(targets),
            "output": str(args.output),
        }, sort_keys=True))
        return 0

    key = api_key()
    rows: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    calls = 0
    for query, route in targets.items():
        try:
            search_id, results = firecrawl_search(key, query)
            calls += 1
            if not results:
                failures.append({"target_query": query, "reason": "no_web_results"})
                continue
            rows.append({
                "target_query": query,
                "target_route": route,
                "search_id": search_id,
                "results": results,
            })
        except RuntimeError as exc:
            calls += 1
            failures.append({"target_query": query, "reason": str(exc)})

    payload = {
        "schema": SCHEMA,
        "status": "PASS" if rows else "BLOCKED_NO_RESULTS",
        "captured_at_utc": iso_utc(),
        "provider": "firecrawl-v2-search",
        "provider_calls": calls,
        "weekly_query_cap": MAX_TARGETS,
        "query_plan": [{"target_query": query, "target_route": route} for query, route in targets.items()],
        "search_options": {"location": "United States", "country": "US", "tbs": "qdr:w", "scrape_top": RESULT_LIMIT},
        "scope": "Exact-query SERP research, not a claim of GSC position or ranking improvement",
        "targets": rows,
        "failures": failures,
        "effects": {"site_writes": 0, "publishes": 0, "deployments": 0},
    }
    if not rows:
        print(json.dumps({
            "status": payload["status"], "provider_calls": calls,
            "targets": 0, "failures": len(failures),
        }, sort_keys=True))
        return 1
    atomic_write(args.output, payload)
    atomic_write(args.page_two_report, payload)
    print(json.dumps({
        "status": payload["status"],
        "provider_calls": calls,
        "targets": len(rows),
        "results": sum(len(row["results"]) for row in rows),
        "failures": len(failures),
        "output": str(args.output),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
