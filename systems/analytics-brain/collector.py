#!/usr/bin/env python3
"""Collect GLP analytics sources and emit the five canonical brain outputs.

Live provider reads are disabled until ``ops/glp/analytics-brain-control.json``
is explicitly activated at launch. Fixture runs ignore that switch and are the
pre-launch integration path. This script never reads the site candidate tree.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import hashlib
from html import unescape
from html.parser import HTMLParser
import importlib.util
import json
import re
import sqlite3
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
BUSINESS = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(BUSINESS))

from analytics_brain import (  # noqa: E402
    BING_SITE,
    GSC_PROPERTY,
    PRODUCTION_HOST,
    apply_fact_model,
    build_outputs,
    fact_model_statements,
    record_source_facts,
    utc_now,
    write_outputs,
)

CONTROL = BUSINESS / "ops/glp/analytics-brain-control.json"
DEPLOY_TRIGGER = BUSINESS / "ops/glp/analytics-brain-state/deploy-trigger.json"
DEFAULT_DB = BUSINESS / "reports/glp-clinic-scout/operator-national-wow-20260723/data/glp-observability.sqlite"
DEFAULT_OUTPUT = BUSINESS / "reports/glp-clinic-scout/analytics/current"
MIGRATION = HERE / "migrations/001_analytics_brain_additive.sql"
SITEMAP_URL = f"https://{PRODUCTION_HOST}/sitemap.xml"
USER_AGENT = "glp-analytics-brain/1.0 (+https://glpclinicscout.com)"
GA4_MEASUREMENT_ID = "G-2WQ42JY0WK"
# Authenticated Analytics Admin dataStreams readback (GLP44, 2026-10-06):
# this property also carries Partners stream 15224515823. Neither its traffic
# nor its hosts establish consumer facts or consumer-stream host pollution.
GA4_PROPERTY_ID = "541440529"
GA4_CONSUMER_STREAM_ID = "15060837778"
GTM_CONTAINER_ID = "GTM-PWR49VVK"
SOURCE_DATA_KEYS = {
    "gsc": ("gsc_rows",),
    "bing": ("bing_rows", "bing_query_rows"),
    "ga4": ("ga4_rows", "ga4_events"),
    "posthog": ("posthog_rows", "posthog_events"),
    "d1_clicks": ("d1_clicks", "browser_clicks", "d1_noncommercial_clicks"),
    "network_revenue": ("revenue_events", "noncommercial_revenue_events"),
    "dataforseo": (),
}
ROUTE_DATA_KEYS = ("sitemap_routes", "sitemap_complete", "sitemap_sources", "static_routes")
EXPECTED_SCHEMAS = {
    "gsc": "gsc-searchanalytics-v3",
    "bing": "bing-webmaster-v1",
    "ga4": "ga4-data-v1beta",
    "posthog": "posthog-hogql-v1",
    "d1_clicks": "affiliate-click-map-v1",
    "dataforseo": "glp-topic-data-score-v1",
    "network_revenue": "analytics-revenue-ledger-v1",
}


def _utc_days_ago(days: int) -> str:
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).date().isoformat()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _http_text(url: str, timeout: int = 45) -> tuple[int, str]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return int(response.status), response.read().decode("utf-8", "replace")


class _PublicRouteRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlsplit(newurl)
        if (parsed.scheme != "https" or parsed.hostname != PRODUCTION_HOST
                or parsed.path == "/go" or parsed.path.startswith("/go/")):
            raise RuntimeError("public_route_redirect_outside_editorial_scope")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _http_page(url: str, timeout: int = 45) -> tuple[int, str, dict[str, str]]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    opener = urllib.request.build_opener(_PublicRouteRedirects())
    with opener.open(request, timeout=timeout) as response:
        headers = {str(key).lower(): str(value) for key, value in response.headers.items()}
        headers["_collector_resolved_path"] = urlsplit(response.geturl()).path
        return int(response.status), response.read().decode("utf-8", "replace"), headers


def sitemap_routes(url: str = SITEMAP_URL) -> tuple[list[str], list[str]]:
    """Read every URL from a sitemap tree. There is intentionally no route cap."""
    pending = [url]
    visited: set[str] = set()
    routes: set[str] = set()
    sitemap_urls: list[str] = []
    while pending:
        current = pending.pop(0)
        if current in visited:
            continue
        visited.add(current)
        _status, text = _http_text(current)
        sitemap_urls.append(current)
        locations = [value.strip() for value in re.findall(r"<loc>\s*([^<]+?)\s*</loc>", text, re.I)]
        if "<sitemapindex" in text.lower():
            for child in locations:
                parsed = urlsplit(child)
                if parsed.hostname != PRODUCTION_HOST:
                    raise RuntimeError(f"sitemap_foreign_host:{parsed.hostname}")
                if child not in visited:
                    pending.append(child)
            continue
        for location in locations:
            parsed = urlsplit(location)
            if parsed.hostname != PRODUCTION_HOST:
                raise RuntimeError(f"sitemap_foreign_host:{parsed.hostname}")
            route = "/" + parsed.path.strip("/")
            routes.add("/" if route == "/" else route + "/")
    if not routes:
        raise RuntimeError("sitemap_routes_empty")
    return sorted(routes), sitemap_urls


def _slot_ids(html: str) -> list[str]:
    values: set[str] = set()
    for pattern in (
        r"data-slot-id\s*=\s*['\"]([^'\"]+)['\"]",
        r"data-sponsor-slot\s*=\s*['\"]([^'\"]+)['\"]",
        r"data-placement-id\s*=\s*['\"]([^'\"]+)['\"]",
    ):
        values.update(re.findall(pattern, html, re.I))
    return sorted(values)


def _internal_html_routes(html: str) -> list[str]:
    routes: set[str] = set()
    for href in re.findall(r"<a\b[^>]*\bhref\s*=\s*['\"]([^'\"#]+)", html, re.I):
        parsed = urlsplit(urljoin(f"https://{PRODUCTION_HOST}/", href))
        if parsed.hostname != PRODUCTION_HOST:
            continue
        path = parsed.path or "/"
        if path.startswith(("/go/", "/api/", "/cdn-cgi/")):
            continue
        final = path.rsplit("/", 1)[-1]
        if "." in final:
            continue
        route = "/" + path.strip("/")
        routes.add("/" if route == "/" else route + "/")
    return sorted(routes)


# 2026-08-27 declared excluded hosts: our own subdomains and the operator's
# review tunnels legitimately carry the tag but are NOT production traffic.
# Facts already filter to the exact production host; these are recorded and
# excluded from the pollution verdict. A host outside this set still fails.
#: 2026-08-28: Cloudflare Pages PREVIEW deployments (`<hash>.glp-clinic-scout.pages.dev`
#: and branch aliases) are OUR OWN review infrastructure. They ship the real
#: analytics wiring on purpose — a preview whose tag is stripped cannot prove the
#: production build's instrumentation — so they emit real GA4 events from a
#: non-production host. Execution 2002 correctly flagged 14 such events
#: (4136ff89 / cfa10eb2, the paid-acquisition funnel previews) as
#: FAIL_HOST_POLLUTION. Same category as the operator's trycloudflare review
#: tunnels already declared below: recorded, excluded from the pollution verdict,
#: never counted as production traffic. The project name is pinned — an arbitrary
#: *.pages.dev host is still someone else's site and still fails.
DECLARED_EXCLUDED_HOST_RE = re.compile(
    r"^(?:www\.glpclinicscout\.com|partners\.glpclinicscout\.com"
    r"|[a-z0-9-]+\.trycloudflare\.com"
    r"|[a-z0-9-]+\.glp-clinic-scout\.pages\.dev|glp-clinic-scout\.pages\.dev"
    r"|localhost|127\.0\.0\.1)$")


def _non_allowlisted(host_distribution: dict[str, int]) -> int:
    return sum(value for host, value in host_distribution.items()
               if host != PRODUCTION_HOST and not DECLARED_EXCLUDED_HOST_RE.match(str(host or "")))


def _csp(html: str, headers: dict[str, str]) -> str:
    header = headers.get("content-security-policy") or ""
    meta = re.search(
        r"<meta\b[^>]*http-equiv\s*=\s*['\"]Content-Security-Policy['\"][^>]*content\s*=\s*['\"]([^'\"]+)",
        html,
        re.I,
    )
    return header or (meta.group(1) if meta else "")


def inspect_route(route: str, control: dict[str, Any]) -> dict[str, Any]:
    url = urljoin(f"https://{PRODUCTION_HOST}/", route.lstrip("/"))
    try:
        status, html, headers = _http_page(url)
    except Exception as exc:  # noqa: BLE001 - recorded per route
        return {
            "path": route,
            "http_status": None,
            "error": f"{type(exc).__name__}:{str(exc)[:200]}",
            "ga4_static_present": False,
            "ga4_direct_loader_present": False,
            "gtm_static_present": False,
            "posthog_static_present": False,
            "clarity_static_present": False,
            "csp_present": False,
            "csp_gtm_allowed": False,
            "csp_posthog_allowed": False,
            "consent_default_denied_present": False,
            "sponsored_anchor_count": 0,
            "client_marker_count": 0,
            "first_party_redirect_count": 0,
            "sponsor_slot_ids": [],
            "internal_routes": [],
            "affiliate_route_expected": route in set(control.get("affiliate_routes") or []),
            "representative_probe_required": route in set(control.get("representative_probe_routes") or []),
            "static_contract_exempt": route in set(control.get("static_exempt_routes") or []),
        }
    sponsored_tags = re.findall(
        r"<a\b[^>]*\brel\s*=\s*['\"][^'\"]*\bsponsored\b[^'\"]*['\"][^>]*>", html, re.I
    )
    sponsored = len(sponsored_tags)
    # Retain hrefs as data only. Permission for a direct issued link does not
    # authorize /go, another program's target, or an arbitrary tracking URL.
    sponsored_targets = []
    for tag in sponsored_tags:
        attrs = {name.lower(): unescape(value) for name, _quote, value in
                 re.findall(r"([\w:-]+)\s*=\s*(['\"])(.*?)\2", tag, re.S)}
        sponsored_targets.append({"program_id": attrs.get("data-program"),
                                  "href": attrs.get("href"),
                                  "link_mode": attrs.get("data-link-mode"),
                                  "network": attrs.get("data-network")})
    # 2026-08-27 launch as-built grammar: sponsored anchors carry the CR-5
    # marker set (data-analytics-event="affiliate_click" + data-program +
    # data-network + element/rank/page-type + baked sub slots) and link
    # transparent-direct to the network with baked identity; /go/ remains only
    # on redirect-carrying surfaces (quiz held routes). The pre-launch grammar
    # (data-link-position markers, universal /go/ hrefs) stays accepted so an
    # older release never reads as uninstrumented.
    client_markers = sum(
        (bool(re.search(r"\bdata-program\s*=", tag, re.I))
         and bool(re.search(r"\bdata-link-position\s*=", tag, re.I)))
        or (bool(re.search(r"\bdata-analytics-event\s*=\s*['\"]affiliate_click", tag, re.I))
            and bool(re.search(r"\bdata-program\s*=", tag, re.I)))
        for tag in sponsored_tags
    )
    first_party_redirects = sum(
        bool(re.search(r"\bhref\s*=\s*['\"](?:https://glpclinicscout\.com)?/go/", tag, re.I))
        or (bool(re.search(r"\bdata-analytics-event\s*=\s*['\"]affiliate_click", tag, re.I))
            and bool(re.search(r"\bdata-network\s*=", tag, re.I)))
        for tag in sponsored_tags
    )
    csp = _csp(html, headers)
    gtm_present = GTM_CONTAINER_ID in html
    # 2026-08-27: the measurement ID string legitimately appears in the page
    # (GCS_ANALYTICS event-routing config + the inline synthetic ga-disable
    # guard). "Direct loader" means the gtag.js LOADER itself — GTM is the only
    # sanctioned loader after the cutover.
    direct_ga4 = "googletagmanager.com/gtag/js" in html
    # 2026-08-27: operative consent model is the operator-approved default-on
    # policy (analytics-default-on-20260801): the consent DEFAULT lives in the
    # GTM container at Consent Initialization, ad_* stays denied via the
    # consent.js update path, and an inline synthetic guard precedes the GTM
    # loader. Accept that as-built shape alongside the original inline
    # default-denied grammar.
    consent_default_denied = bool(
        (re.search(r"consent.{0,120}default", html, re.I | re.S)
         and re.search(r"analytics_storage.{0,80}denied", html, re.I | re.S))
        or ("ga-disable-" + GA4_MEASUREMENT_ID in html and "/assets/consent.js" in html)
    )
    return {
        "path": route,
        "http_status": status,
        "resolved_path": headers.get("_collector_resolved_path", route),
        "ga4_static_present": gtm_present or direct_ga4,
        "ga4_direct_loader_present": direct_ga4,
        "gtm_static_present": gtm_present,
        "posthog_static_present": bool(re.search(r"posthog", html, re.I)),
        "clarity_static_present": bool(re.search(r"clarity\.ms|clarity\s*\(", html, re.I)),
        "csp_present": bool(csp),
        "csp_gtm_allowed": "googletagmanager.com" in csp,
        "csp_posthog_allowed": "us.i.posthog.com" in csp and "us-assets.i.posthog.com" in csp,
        "consent_default_denied_present": consent_default_denied,
        "sponsored_anchor_count": sponsored,
        "sponsored_anchor_targets": sponsored_targets,
        "listing_contract": _listing_contract(html),
        "client_marker_count": client_markers,
        "first_party_redirect_count": first_party_redirects,
        "sponsor_slot_ids": _slot_ids(html),
        "internal_routes": _internal_html_routes(html),
        "affiliate_route_expected": route in set(control.get("affiliate_routes") or []),
        "representative_probe_required": route in set(control.get("representative_probe_routes") or []),
        "static_contract_exempt": route in set(control.get("static_exempt_routes") or []),
    }


def inspect_routes(routes: list[str], control: dict[str, Any]) -> list[dict[str, Any]]:
    pending = sorted(set(routes))
    inspected: dict[str, dict[str, Any]] = {}
    while pending:
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            rows = list(pool.map(lambda route: inspect_route(route, control), pending))
        for row in rows:
            inspected[str(row["path"])] = row
        discovered = {
            path for row in rows for path in row.get("internal_routes") or []
            if path not in inspected
        }
        pending = sorted(discovered)
    return [inspected[path] for path in sorted(inspected)]


def _rail_module():
    path = BUSINESS / "ops/glp/seo_data_rails.py"
    spec = importlib.util.spec_from_file_location("analytics_brain_rails", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("seo_data_rails_import_failed")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _d1_client():
    path = BUSINESS / "reports/glp-clinic-scout/operator-national-wow-20260723/scripts/glp_observability_collect.py"
    spec = importlib.util.spec_from_file_location("analytics_brain_d1", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("d1_client_import_failed")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    values = module.load_env(module.D1_SECRET)
    token = values.get("CLOUDFLARE_API_TOKEN") or values.get("CF_API_TOKEN")
    if not token:
        raise RuntimeError("d1_token_missing")
    return module.D1Client(token)


def record_d1_fact_model(outputs: dict[str, dict[str, Any]], client: Any | None = None) -> int:
    """Write the same source-specific facts to D1 with parameterized statements."""
    remote = client or _d1_client()
    statements = fact_model_statements(outputs)
    remote.batch(statements)
    return len(statements)


def _source(state: str, rows: int | None, *, host_scope: str | None = None,
            watermark: str | None = None, reason: str | None = None,
            schema: str, expected_rows: int | None = None) -> dict[str, Any]:
    return {
        "state": state,
        "actual_rows": rows,
        "expected_rows": expected_rows,
        "watermark": watermark,
        "host_scope": host_scope,
        "host_filter": ({"dimension": "hostName", "exact": PRODUCTION_HOST}
                        if host_scope == PRODUCTION_HOST else None),
        "expected_schema": schema,
        "observed_schema": schema if state in {"healthy", "measured_zero"} else None,
        "last_successful_readback": utc_now() if state in {"healthy", "measured_zero"} else None,
        "reason": reason,
        "metric_value": None,
    }


def _collect_gsc(data: dict[str, Any], sources: dict[str, Any], google: Any) -> None:
    prior_success = (sources.get("gsc") or {}).get("last_successful_readback")
    attempted_at = utc_now()
    try:
        gsc_doc = google.gsc_search_analytics(
            property_uri=GSC_PROPERTY,
            start_date=_utc_days_ago(30),
            end_date=_utc_days_ago(3),
            dimensions=["page", "query"],
            row_limit=25000,
            fetch_all=True,
        )
        if not gsc_doc.get("ok") or gsc_doc.get("complete") is not True:
            raise RuntimeError(str(gsc_doc.get("error") or "gsc_read_failed"))
        gsc_rows = []
        for row in gsc_doc.get("rows") or []:
            keys = row.get("keys") or []
            page = urlsplit(str(keys[0] if keys else "")).path or "/"
            gsc_rows.append({
                "path": page,
                "query": keys[1] if len(keys) > 1 else None,
                "clicks": int(row.get("clicks") or 0),
                "impressions": int(row.get("impressions") or 0),
                "ctr": row.get("ctr"),
                "position": row.get("position"),
            })
        # A requested end date is not a provider reporting watermark. Verify the
        # latest returned date independently, as recommended by Google's API doc.
        dates_doc = google.gsc_search_analytics(
            property_uri=GSC_PROPERTY, start_date=_utc_days_ago(30),
            end_date=_utc_days_ago(3), dimensions=["date"], row_limit=25000, fetch_all=True,
        )
        if not dates_doc.get("ok") or dates_doc.get("complete") is not True:
            raise RuntimeError(str(dates_doc.get("error") or "gsc_date_read_failed"))
        observed_dates = [dt.date.fromisoformat(str(row["keys"][0])).isoformat()
                          for row in dates_doc.get("rows") or [] if row.get("keys")]
        data["gsc_rows"] = gsc_rows
        gsc_state = "healthy" if gsc_rows else "measured_zero"
        sources["gsc"] = {
            **_source(gsc_state, len(gsc_rows), watermark=max(observed_dates, default=None),
                      schema=EXPECTED_SCHEMAS["gsc"]),
            "watermark_kind": "provider_report_date" if observed_dates else "unknown",
            "property": GSC_PROPERTY,
            "pages_fetched": int(gsc_doc.get("pages") or 0),
            "complete": True,
            "collection_window": {"start": _utc_days_ago(30), "end": _utc_days_ago(3)},
        }
    except Exception as exc:  # noqa: BLE001
        sources["gsc"] = _source("error", None, reason=str(exc)[:300],
                                 schema=EXPECTED_SCHEMAS["gsc"])
        sources["gsc"]["last_successful_readback"] = prior_success
    sources["gsc"].update(last_attempt_at=attempted_at, prior_last_successful_readback=prior_success)


def _ga4_report_rows(doc: dict[str, Any], dimensions: list[str],
                     metrics: list[str]) -> list[dict[str, Any]]:
    """Missing stream/schema/count evidence is an error, including empty reports."""
    if (doc.get("dimension_names") != dimensions or doc.get("metric_names") != metrics
            or not isinstance(doc.get("rows"), list)):
        raise RuntimeError("ga4_report_shape_invalid")
    for row in doc["rows"]:
        if (not isinstance(row, dict) or not isinstance(row.get("dimensions"), list)
                or len(row["dimensions"]) != len(dimensions)
                or any(not isinstance(value, str) or not value for value in row["dimensions"])
                or not isinstance(row.get("metrics"), list)
                or len(row["metrics"]) != len(metrics)
                or any(not isinstance(value, str) or not re.fullmatch(r"[0-9]+", value)
                       for value in row["metrics"])):
            raise RuntimeError("ga4_report_row_invalid")
        if not re.fullmatch(r"[0-9]+", row["dimensions"][dimensions.index("streamId")]):
            raise RuntimeError("ga4_stream_id_invalid")
    return doc["rows"]


def _collect_ga4(data: dict[str, Any], sources: dict[str, Any], google: Any) -> None:
    data["ga4_rows"], data["ga4_events"] = [], []
    try:
        fact_dimensions = ["dateHourMinute", "streamId", "hostName", "pagePath", "eventName"]
        fact_metrics = ["eventCount", "sessions", "engagedSessions"]
        ga4_doc = google.ga4_run_report(
            property_id=GA4_PROPERTY_ID,
            start_date=_utc_days_ago(2),
            end_date="today",
            # Five separately approved ordinary routes; no click identity or
            # health-path enumeration is needed for this aggregate collector.
            # The rail ANDs its unchanged exact production-host filter with this.
            dimensions=fact_dimensions,
            dimension_filter={"andGroup": {"expressions": [
                {"filter": {"fieldName": "streamId", "stringFilter": {
                    "matchType": "EXACT", "value": GA4_CONSUMER_STREAM_ID, "caseSensitive": True}}},
                {"filter": {"fieldName": "pagePath", "inListFilter": {
                    "values": sorted(GA4_APPROVED_NEUTRAL_PATHS), "caseSensitive": True}}}]}},
            metrics=fact_metrics,
            limit=250000,
            fetch_all=True,
        )
        if (not ga4_doc.get("ok") or ga4_doc.get("complete") is not True
                or ga4_doc.get("host_filtered") is not True):
            raise RuntimeError(str(ga4_doc.get("error") or "ga4_read_failed"))
        fact_rows = _ga4_report_rows(ga4_doc, fact_dimensions, fact_metrics)
        host_doc = google.ga4_run_report(
            property_id=GA4_PROPERTY_ID,
            start_date=_utc_days_ago(2), end_date="today", dimensions=["streamId", "hostName"],
            metrics=["eventCount"], limit=250000, host_filter=None, fetch_all=True,
        )
        if not host_doc.get("ok") or host_doc.get("complete") is not True:
            raise RuntimeError(str(host_doc.get("error") or "ga4_host_distribution_read_failed"))
        # Keep every property/other-stream hostname diagnostic without treating
        # another stream as consumer pollution or adding hosts to the allowlist.
        property_hosts: dict[str, int] = {}
        stream_hosts: dict[str, dict[str, int]] = {}
        for row in _ga4_report_rows(host_doc, ["streamId", "hostName"], ["eventCount"]):
            stream, host = row["dimensions"]
            count = int(row["metrics"][0])
            property_hosts[host] = property_hosts.get(host, 0) + count
            hosts = stream_hosts.setdefault(stream, {})
            hosts[host] = hosts.get(host, 0) + count
        host_distribution = stream_hosts.get(GA4_CONSUMER_STREAM_ID, {})
        other_stream_hosts = {stream: hosts for stream, hosts in stream_hosts.items()
                              if stream != GA4_CONSUMER_STREAM_ID}
        non_allowlisted = _non_allowlisted(host_distribution)
        ga4_rows = []
        for row in fact_rows:
            observed_at, stream, host, page, event_name = row["dimensions"]
            metrics = row["metrics"]
            if (stream != GA4_CONSUMER_STREAM_ID or host != PRODUCTION_HOST
                    or page not in GA4_APPROVED_NEUTRAL_PATHS):
                raise RuntimeError("ga4_neutral_route_shape_invalid")
            record = {"host": host, "path": page, "event_name": event_name,
                      "provider_time": observed_at,
                      "event_count": int(metrics[0]),
                      "sessions": int(metrics[1]),
                      "engaged_sessions": int(metrics[2])}
            ga4_rows.append(record)
        data["ga4_rows"] = ga4_rows
        ga4_state = "healthy" if ga4_rows else "measured_zero"
        event_watermark = max((str(row.get("provider_time") or "") for row in ga4_rows), default="") or None
        sources["ga4"] = {
            **_source(ga4_state, len(ga4_rows), host_scope=PRODUCTION_HOST,
                      watermark=event_watermark or utc_now(), schema=EXPECTED_SCHEMAS["ga4"]),
            "event_watermark": event_watermark,
            "pages_fetched": int(ga4_doc.get("pages") or 0),
            "complete": True,
            "provider_host_filter": ga4_doc.get("host_filter"),
            "host_distribution": host_distribution,
            "non_allowlisted_event_count": non_allowlisted,
            "collection_window": {"scope": "exact_consumer_stream_host_and_approved_neutral_routes",
                                  "consumer_stream_id": GA4_CONSUMER_STREAM_ID,
                                  "property": GA4_PROPERTY_ID,
                                  "host_audit": {
                                      "scope": "whole_property_partitioned_by_stream",
                                      "property_host_distribution": property_hosts,
                                      "stream_host_distribution": stream_hosts,
                                      "other_stream_host_distribution": other_stream_hosts,
                                      "property_event_count": sum(property_hosts.values()),
                                      "consumer_stream_event_count": sum(host_distribution.values()),
                                      "other_stream_event_count": sum(
                                          sum(hosts.values()) for hosts in other_stream_hosts.values())},
                                  "neutral_route_paths": sorted(GA4_APPROVED_NEUTRAL_PATHS),
                                  "click_identity": "not_collected_by_aggregate_contract"},
        }
    except Exception as exc:  # noqa: BLE001
        sources["ga4"] = _source("error", None, host_scope=PRODUCTION_HOST,
                                 reason=str(exc)[:300], schema=EXPECTED_SCHEMAS["ga4"])


def _bing_report_date(value: Any) -> str | None:
    if not value:
        return None
    text = str(value)
    match = re.fullmatch(r"/Date\((-?\d+)(?:[+-]\d{4})?\)/", text)
    try:
        if match:
            return dt.datetime.fromtimestamp(int(match.group(1)) / 1000, dt.timezone.utc).date().isoformat()
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except (ValueError, OverflowError, OSError):
        return None


def _collect_bing(data: dict[str, Any], sources: dict[str, Any], rails: Any) -> None:
    prior_success = (sources.get("bing") or {}).get("last_successful_readback")
    attempted_at = utc_now()
    try:
        bing = rails.BingWebmaster(site=BING_SITE)
        # Same authenticated rail and two read-only endpoints. The shared
        # convenience transforms discard Date, so retain it here without
        # changing the shared SEO/scout rail or adding extra provider calls.
        pages = bing._get("GetPageStats", {"siteUrl": BING_SITE})
        queries = bing._get("GetQueryStats", {"siteUrl": BING_SITE})
        if not isinstance(pages, list) or not isinstance(queries, list):
            raise RuntimeError("bing_stats_response_shape_invalid")
        bing_rows = [{
            "path": urlsplit(str(row.get("Query") or "")).path or "/",
            "impressions": int(row.get("Impressions") or 0),
            "clicks": int(row.get("Clicks") or 0),
            "position": row.get("AvgImpressionPosition"),
        } for row in pages]
        query_rows = [{"query": row.get("Query"), "impressions": row.get("Impressions"),
                       "clicks": row.get("Clicks"), "avg_position": row.get("AvgImpressionPosition"),
                       "avg_click_position": row.get("AvgClickPosition")} for row in queries]
        watermarks = {name: max((date for row in rows if (date := _bing_report_date(row.get("Date")))), default=None)
                      for name, rows in (("pages", pages), ("queries", queries))}
        # Conservative common reporting horizon. Missing nonempty-stream dates
        # remain unknown rather than relabeling the collection clock as data.
        complete_dates = all(not rows or watermarks[name] for name, rows in (("pages", pages), ("queries", queries)))
        watermark = min((v for v in watermarks.values() if v), default=None) if complete_dates else None
        data["bing_rows"] = bing_rows
        data["bing_query_rows"] = query_rows
        total_rows = len(bing_rows) + len(query_rows)
        sources["bing"] = {
            **_source("healthy" if total_rows else "measured_zero", total_rows,
                      watermark=watermark, schema=EXPECTED_SCHEMAS["bing"]),
            "watermark_kind": "provider_report_date" if watermark else "unknown",
            "reporting_watermarks": watermarks,
            "site": BING_SITE,
            "page_rows": len(bing_rows),
            "query_rows": len(query_rows),
            "complete": True,
        }
    except Exception as exc:  # noqa: BLE001
        sources["bing"] = _source("error", None, reason=str(exc)[:300], schema=EXPECTED_SCHEMAS["bing"])
        sources["bing"]["last_successful_readback"] = prior_success
    sources["bing"].update(last_attempt_at=attempted_at, prior_last_successful_readback=prior_success)


def _collect_posthog(data: dict[str, Any], sources: dict[str, Any], rails: Any) -> None:
    # The approved producer exports no click identity or health-route identity.
    # Never query those fields to establish their intentional absence. Clear old
    # populations before dispatch so a failed read cannot retain broader rows.
    data["posthog_rows"], data["posthog_events"] = [], []
    try:
        posthog = rails.PostHog()
        paths = ", ".join(repr(path) for path in POSTHOG_APPROVED_NEUTRAL_PATHS)
        query_started = dt.datetime.now(dt.timezone.utc)
        aggregate = posthog.hogql(
            "SELECT properties.$pathname, countIf(event = '$pageview'), "
            "countIf(event = 'affiliate_click') FROM events "
            "WHERE timestamp > now() - INTERVAL 3 DAY "
            "AND ifNull(toString(properties.is_test), '') NOT IN ('true', '1') "
            "AND properties.$host = 'glpclinicscout.com' "
            f"AND properties.$pathname IN ({paths}) "
            "GROUP BY properties.$pathname ORDER BY properties.$pathname LIMIT 7"
        )
        query_finished = dt.datetime.now(dt.timezone.utc)
        if not aggregate.get("ok"):
            raise RuntimeError(str(aggregate.get("error") or "posthog_read_failed"))
        route_query_window = {
            "start_utc": (query_started - dt.timedelta(days=3)).isoformat().replace("+00:00", "Z"),
            "end_utc": query_finished.isoformat().replace("+00:00", "Z"),
            "basis": "capture_before_query_minus_3_days",
        }
        route_rows = []
        for row in aggregate.get("rows") or []:
            if len(row) != 3 or row[0] not in POSTHOG_APPROVED_NEUTRAL_PATHS:
                raise RuntimeError("posthog_neutral_route_shape_invalid")
            route_rows.append({"path": row[0], "host": PRODUCTION_HOST,
                               "views": int(row[1] or 0), "affiliate_clicks": int(row[2] or 0)})
        # Fixed server-side buckets retain missing/off-host evidence without
        # returning raw hosts, URLs, arbitrary paths, identities or quiz values.
        buckets = posthog.hogql(
            "SELECT if(properties.$host = 'glpclinicscout.com', "
            "'production_host', 'off_host_or_unknown') AS host_bucket, "
            f"multiIf(properties.$pathname IN ({paths}), 'neutral_route', "
            "empty(ifNull(properties.$pathname, '')), 'route_unknown', 'other_route') AS route_bucket, "
            "count(), max(timestamp) FROM events "
            "WHERE timestamp > now() - INTERVAL 3 DAY "
            "AND ifNull(toString(properties.is_test), '') NOT IN ('true', '1') "
            "GROUP BY host_bucket, route_bucket ORDER BY host_bucket, route_bucket LIMIT 6"
        )
        if not buckets.get("ok"):
            raise RuntimeError(str(buckets.get("error") or "posthog_scope_read_failed"))
        scope_counts = {}
        for row in buckets.get("rows") or []:
            if (len(row) != 4 or row[0] not in {"production_host", "off_host_or_unknown"}
                    or row[1] not in {"neutral_route", "route_unknown", "other_route"}):
                raise RuntimeError("posthog_scope_shape_invalid")
            key = row[0] + "/" + row[1]
            if key in scope_counts:
                raise RuntimeError("posthog_scope_duplicate_bucket")
            scope_counts[key] = {"event_count": int(row[2] or 0),
                                 "event_watermark": str(row[3]) if row[3] else None}
        eligible = scope_counts.get("production_host/neutral_route") or {}
        event_count = eligible.get("event_count", 0)
        watermark = eligible.get("event_watermark")
        data["posthog_rows"] = route_rows
        sources["posthog"] = {
            **_source("healthy" if event_count else "measured_zero", len(route_rows),
                      host_scope=PRODUCTION_HOST, watermark=watermark or utc_now(),
                      schema=EXPECTED_SCHEMAS["posthog"]),
            "event_watermark": watermark,
            "route_query_window": route_query_window,
            "project": "508172",
            "provider_host_filter": {"property": "$host", "exact": PRODUCTION_HOST},
            "host_distribution": {PRODUCTION_HOST: event_count} if event_count else {},
            # This field means pollution of selected production rows, not the
            # excluded traffic belonging to another business in this project.
            "non_allowlisted_event_count": 0,
            "collection_window": {"scope": "exact_host_and_approved_neutral_routes",
                                  "scope_counts": scope_counts,
                                  "missing_route_identity": "unknown_not_homepage_or_consent_proof"},
            "complete": True,
        }
    except Exception as exc:  # noqa: BLE001
        sources["posthog"] = _source("error", None, host_scope=PRODUCTION_HOST,
                                     reason=str(exc)[:300], schema=EXPECTED_SCHEMAS["posthog"])


def _noncommercial_kind(row: dict[str, Any]) -> str | None:
    """Recognize explicit provenance only; unmarked historical rows stay unknown.

    A Katalys network label, browser UA, absent referrer or denied consent is
    NOT a probe marker. The historical liveness job calls the network directly
    without a durable click identity, so its probes cannot be retroactively
    joined by time/program or subtracted from these rows.
    """
    def marked(value: Any) -> bool:
        return value is True or (type(value) is int and value == 1) or value == "true"

    if marked(row.get("is_probe")) or row.get("probe_kind") == "program_liveness":
        return "probe"
    if marked(row.get("is_test")):
        return "test"
    return None


def _map_runtime_click(row: dict[str, Any]) -> dict[str, Any]:
    slug = str(row.get("page_slug") or "").strip("/")
    kind = _noncommercial_kind(row)
    return {
        "cs_click_id": row.get("click_token"),
        "path": "/" + slug + "/" if slug else "/",
        "slot_id": row.get("link_position"),
        "program_id": row.get("program_id"),
        "network": row.get("network"),
        # This table stores our outbound token, NOT a network acceptance ID.
        "network_click_id": None,
        "occurred_at": row.get("created_at"),
        "analytics_consent": row.get("analytics_consent"),
        "provenance": "affiliate_click_map",
        "noncommercial_kind": kind,
        "probe_classification": "explicit_probe" if kind == "probe" else "unknown",
        "test_classification": "explicit_test" if kind == "test" else "unknown",
    }


def _collect_d1(data: dict[str, Any], sources: dict[str, Any]) -> None:
    # Clear stale cached populations before a fresh read; server durability does
    # not prove a browser sent an event (especially on consent-denied rows).
    for key in ("d1_clicks", "browser_clicks", "d1_noncommercial_clicks", "revenue_events",
                "noncommercial_revenue_events"):
        data[key] = []
    try:
        client = _d1_client()
        tables = {row["name"] for row in client.query(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
            "('affiliate_click_map','analytics_revenue_ledger_v1')"
        )}
        if "affiliate_click_map" not in tables:
            raise RuntimeError("affiliate_click_map_missing")
        columns = {row["name"] for row in client.query("PRAGMA table_info(affiliate_click_map)")}
        # Optional future provenance columns are projected only when present;
        # no migration or historical relabel is performed by the collector.
        optional = [name for name in ("is_probe", "probe_kind", "is_test") if name in columns]
        projection = "click_token,page_slug,link_position,program_id,network,created_at,analytics_consent"
        if optional:
            projection += "," + ",".join(optional)
        raw_clicks = client.query(f"SELECT {projection} FROM affiliate_click_map ORDER BY created_at,click_token")
        all_click_rows = [_map_runtime_click(row) for row in raw_clicks]
        click_rows = [row for row in all_click_rows if row["noncommercial_kind"] is None]
        excluded_clicks = [row for row in all_click_rows if row["noncommercial_kind"] is not None]
        data["d1_clicks"] = click_rows
        data["d1_noncommercial_clicks"] = excluded_clicks
        click_watermark = max((str(row.get("occurred_at") or "") for row in all_click_rows), default="") or None
        sources["d1_clicks"] = {
            **_source("healthy" if all_click_rows else "measured_zero", len(all_click_rows),
                      watermark=click_watermark or utc_now(), schema=EXPECTED_SCHEMAS["d1_clicks"]),
            "event_watermark": click_watermark,
            "collection_window": {
                "scope": "all_historical_rows", "table": "affiliate_click_map",
                "actual_rows_basis": "raw_runtime_rows_including_noncommercial",
                "economics_click_rows": len(click_rows),
                "explicit_probe_rows": sum(row["noncommercial_kind"] == "probe" for row in excluded_clicks),
                "explicit_test_rows": sum(row["noncommercial_kind"] == "test" for row in excluded_clicks),
                "unclassified_rows": len(click_rows),
                "historical_unmarked_probes": "unknown_not_inferred",
                "browser_emission_evidence": "not_provided_by_d1",
            },
        }
    except Exception as exc:  # noqa: BLE001
        sources["d1_clicks"] = _source("error", None, reason=str(exc)[:300],
                                       schema=EXPECTED_SCHEMAS["d1_clicks"])
        sources["network_revenue"] = _source("unavailable", None, reason="click_source_read_failed",
                                             schema=EXPECTED_SCHEMAS["network_revenue"])
        return
    try:
        if "analytics_revenue_ledger_v1" not in tables:
            sources["network_revenue"] = _source(
                "unavailable", None, reason="analytics_revenue_ledger_v1_missing",
                schema=EXPECTED_SCHEMAS["network_revenue"])
            data["revenue_events"] = []
            return
        revenue = client.query(
            "SELECT revenue_event_id,network,program_id,network_click_id,cs_click_id,event_type,"
            "event_status,conversion_id,currency,order_amount_minor,commission_minor,occurred_at,"
            "imported_at,attributes_json FROM analytics_revenue_ledger_v1 "
            "WHERE network<>'__receiver_selftest__' "
            "ORDER BY occurred_at"
        )
        click_by_cs = {str(row.get("cs_click_id")): row for row in all_click_rows if row.get("cs_click_id")}
        economic_revenue = []
        for row in revenue:
            click = click_by_cs.get(str(row.get("cs_click_id") or ""))
            # Do not borrow ownership/probe status across conflicting rail/program.
            if click and (row.get("program_id") != click.get("program_id")
                          or (click.get("network") and row.get("network") != click.get("network"))):
                click = None
            raw_attributes = row.pop("attributes_json", "{}")
            try:
                attributes = json.loads(raw_attributes)
            except (TypeError, ValueError):
                attributes = None
            kind = _noncommercial_kind(attributes) if isinstance(attributes, dict) else None
            kind = kind or (click.get("noncommercial_kind") if click else None)
            row["noncommercial_kind"] = kind
            row["test_classification"] = "explicit_test" if kind == "test" else "unknown"
            row["probe_classification"] = "explicit_probe" if kind == "probe" else "unknown"
            row["path"] = click.get("path") if click else None
            row["slot_id"] = click.get("slot_id") if click else None
            if kind:
                data["noncommercial_revenue_events"].append(row)
            else:
                economic_revenue.append(row)
        data["revenue_events"] = economic_revenue
        # A completed import with no rows is a measured zero only when an explicit
        # network-import watermark was recorded. Empty table alone is unavailable.
        watermarks = client.query(
            "SELECT watermark,state,last_successful_readback FROM analytics_source_watermark_v1 "
            "WHERE source='network_revenue' AND host_scope=''"
        )
        if not watermarks:
            sources["network_revenue"] = _source(
                "unavailable", None, reason="no_network_import_watermark",
                schema=EXPECTED_SCHEMAS["network_revenue"])
        elif (not watermarks[0].get("watermark")
              or watermarks[0].get("state") not in {"healthy", "measured_zero"}):
            sources["network_revenue"] = _source(
                "unavailable", None, reason="network_import_watermark_not_complete",
                schema=EXPECTED_SCHEMAS["network_revenue"])
        else:
            state = "healthy" if economic_revenue else "measured_zero"
            sources["network_revenue"] = {
                **_source(state, len(revenue), watermark=watermarks[0].get("watermark"),
                          schema=EXPECTED_SCHEMAS["network_revenue"]),
                "collection_window": {
                    "actual_rows_basis": "ledger_rows_excluding_receiver_selftest",
                    "economics_rows": len(economic_revenue),
                    "explicit_noncommercial_rows": len(data["noncommercial_revenue_events"]),
                    "unmarked_test_or_probe_classification": "unknown_not_real",
                },
            }
    except Exception as exc:  # noqa: BLE001
        data["revenue_events"] = []
        sources["network_revenue"] = _source("unavailable", None, reason=str(exc)[:300],
                                             schema=EXPECTED_SCHEMAS["network_revenue"])


def _collect_dataforseo(sources: dict[str, Any]) -> None:
    directory = BUSINESS / "reports/glp-clinic-scout/seo-machine/topic-data-score"
    candidates = sorted(directory.glob("topic-data-score-*.json"), key=lambda path: path.stat().st_mtime)
    if not candidates:
        sources["dataforseo"] = _source("unavailable", None, reason="topic_data_score_artifact_absent",
                                        schema=EXPECTED_SCHEMAS["dataforseo"])
        return
    path = candidates[-1]
    payload = read_json(path)
    if payload.get("schema") != EXPECTED_SCHEMAS["dataforseo"]:
        sources["dataforseo"] = {
            **_source("error", None, reason="dataforseo_artifact_schema_drift",
                      schema=EXPECTED_SCHEMAS["dataforseo"]),
            "observed_schema": payload.get("schema"),
        }
        return
    rows = int((payload.get("signals") or {}).get("dataforseo_terms_priced") or 0)
    state = "healthy" if rows else "measured_zero"
    sources["dataforseo"] = _source(state, rows, watermark=payload.get("finished_utc"),
                                    schema=EXPECTED_SCHEMAS["dataforseo"])


def _load_cache(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"sources": {}}
    payload = read_json(path)
    if payload.get("schema") != "glp-analytics-source-cache-v1":
        raise RuntimeError("analytics_source_cache_schema_invalid")
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("sources"), dict):
        raise RuntimeError("analytics_source_cache_data_invalid")
    return data


def _write_cache(path: Path, data: dict[str, Any], phase: str) -> None:
    payload = {
        "schema": "glp-analytics-source-cache-v1",
        "updated_at_utc": utc_now(),
        "last_phase": phase,
        "data": data,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _mark_stale_cached_sources(data: dict[str, Any], refreshed: set[str]) -> None:
    now = dt.datetime.now(dt.timezone.utc)
    max_age_hours = {
        "gsc": 30, "bing": 30, "ga4": 2, "posthog": 2, "d1_clicks": 2,
        "network_revenue": 30, "dataforseo": 24 * 8,
    }
    for name, source in (data.get("sources") or {}).items():
        if name in refreshed or source.get("state") not in {"healthy", "measured_zero"}:
            continue
        readback = source.get("last_successful_readback")
        if not readback:
            source.update({"state": "stale", "reason": "last_successful_readback_missing",
                           "metric_value": None})
            continue
        observed = dt.datetime.fromisoformat(str(readback).replace("Z", "+00:00"))
        age = (now - observed.astimezone(dt.timezone.utc)).total_seconds() / 3600.0
        threshold = float(max_age_hours.get(name, 24))
        if age > threshold:
            source.update({
                "state": "stale", "reason": f"cached_source_stale:{age:.2f}h>{threshold:.2f}h",
                "metric_value": None,
            })


# Bound to this collector's property 541440529. runReport metadata readback
# 2026-09-09 confirmed America/Los_Angeles (K4 SOURCE-API-READBACK.json).
# dateHourMinute is property-local, unlike PostHog's ISO UTC timestamps.
# A property/timezone migration must update this binding; never relabel it UTC.
GA4_REPORT_TIME_ZONE = "America/Los_Angeles"


def _provider_time(value: str) -> dt.datetime:
    text = str(value).strip()
    if re.fullmatch(r"\d{12}", text):
        return dt.datetime.strptime(text, "%Y%m%d%H%M").replace(
            tzinfo=ZoneInfo(GA4_REPORT_TIME_ZONE)
        ).astimezone(dt.timezone.utc)
    parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


#: 2026-08-28 execution 1973 (FAIL_SOURCE_DARK): GA4 went "dark" at 8.43h>2h
#: while PostHog proved production traffic in that exact window — the GA4 Data
#: API routinely lags HOURS behind collection, so a freshness bar tighter than
#: the sink's own reporting latency declares a working sink dead every quiet
#: evening. Same disease the probe's persistence gate fixed, second organ. A
#: sink cannot be judged dark before its reporting lag has elapsed: the lag
#: floor below is the minimum effective dark threshold per laggy sink. Past the
#: floor, darkness binds exactly as before. (Mirrors GA4_MISSING_HORIZON_HOURS
#: in analytics_brain.py.)
# Google documents processing up to 24–48h (not an SLA):
# https://support.google.com/analytics/answer/11198161?hl=en
SINK_LAG_FLOOR_HOURS = {"ga4": 48.0}
#: A successful provider read with no traffic is a measured zero, not evidence
#: that the sink is dead. A silent-sink alarm is therefore warranted only when
#: the paired sink observed newer production traffic that this sink should have
#: corroborated. Execution 2137 exposed both parts of the defect: GA4's last
#: reported event was 15.24h old, while PostHog had observed production traffic
#: only 8.23h earlier. The pair remains falsifiable: if that newer PostHog event
#: is still absent from GA4 after the 12h reporting allowance, GA4 goes dark.
#: Provider/API failures remain `error` and stale successful readbacks remain
#: `stale`; neither can be healed by this correlation rule.
CORROBORATING_TRAFFIC_SOURCE = {"ga4": "posthog", "posthog": "ga4"}


POSTHOG_APPROVED_SOURCE_SHA256 = "0a0a7729cb628fc7afb694080bd07f82df28975409030ca7f19850f4dd2fb22a"
POSTHOG_APPROVED_AUTHORITY = "reports/ops/glp43-wrangler-shape-repair-20260927/prepared-final381/release/public/assets/analytics.js:31-50"
POSTHOG_APPROVED_PROMOTION_UTC = "2026-09-27T23:40:59.897740Z"
POSTHOG_APPROVED_NEUTRAL_PATHS = (
    "/", "/providers/", "/compare/", "/privacy/", "/privacy/cookies/", "/terms/", "/about/",
)
GA4_APPROVED_NEUTRAL_PATHS = frozenset(("/", "/about/", "/privacy/", "/privacy/cookies/", "/terms/"))


def _apply_dark_thresholds(data: dict[str, Any], control: dict[str, Any],
                           now: dt.datetime | None = None) -> None:
    activated = _provider_time(str(control["activated_at_utc"]))
    current = now or dt.datetime.now(dt.timezone.utc)
    configured = control.get("source_dark_threshold_hours") or {}
    posthog = (data.get("sources") or {}).get("posthog") or {}
    expectation = (control.get("measurement_expectations") or {}).get("posthog") or {}
    # Transport/schema failures remain failures. A source contract grants only
    # field eligibility; zero host rows is not an ingestion receipt. Keep click
    # identity and every health/unknown route unmeasured under the new scope.
    verified = (expectation.get("authority") == POSTHOG_APPROVED_AUTHORITY
                and posthog.get("complete") is True
                and posthog.get("state") in {"healthy", "measured_zero"}
                and posthog.get("observed_schema") == EXPECTED_SCHEMAS["posthog"])
    for key in ("host_identity_available", "neutral_route_paths",
                "neutral_route_window_clean_after_utc", "measurement_authority"):
        posthog.pop(key, None)  # Cached eligibility belongs to the previous contract.
    if (verified and expectation.get("host_page_identity") == "neutral_routes_only"
            and expectation.get("click_identity") == "unmeasured_by_design"
            and expectation.get("source_sha256") == POSTHOG_APPROVED_SOURCE_SHA256
            and expectation.get("production_promotion_at_utc") == POSTHOG_APPROVED_PROMOTION_UTC
            and expectation.get("neutral_route_paths") == list(POSTHOG_APPROVED_NEUTRAL_PATHS)):
        posthog.update({"measurement_state": "unmeasured_by_design",
                        "measurement_scope": "click_and_non_neutral_route_identity",
                        "host_identity_available": True,
                        "neutral_route_paths": list(POSTHOG_APPROVED_NEUTRAL_PATHS),
                        "neutral_route_window_clean_after_utc": POSTHOG_APPROVED_PROMOTION_UTC,
                        "measurement_authority": POSTHOG_APPROVED_AUTHORITY,
                        "reason": "neutral_route_host_eligible_click_and_health_path_absent"})
    else:
        posthog.update(measurement_state="unmeasured_by_design",
                       measurement_scope="host_page_click_identity")
        if posthog.get("state") in {"healthy", "measured_zero"}:
            posthog["reason"] = "posthog_host_page_identity_contract_unverified"
    for name, default_threshold in {"ga4": 2, "posthog": 2, "d1_clicks": 24}.items():
        source = (data.get("sources") or {}).get(name)
        if (not source or source.get("state") not in {"healthy", "measured_zero"}
                or (source.get("measurement_state") == "unmeasured_by_design"
                    and source.get("measurement_scope") != "click_and_non_neutral_route_identity")):
            continue
        watermark = source.get("event_watermark")
        last_event = _provider_time(str(watermark)) if watermark else activated
        corroborating_name = CORROBORATING_TRAFFIC_SOURCE.get(name)
        if not corroborating_name:
            # A successful D1 read with zero click rows proves availability. With
            # no independent click sink, inactivity alone cannot distinguish a
            # quiet site from silent ingestion failure.
            continue
        corroborating = (data.get("sources") or {}).get(corroborating_name) or {}
        corroborating_watermark = corroborating.get("event_watermark")
        if (corroborating.get("state") not in {"healthy", "measured_zero"}
                or not corroborating_watermark):
            continue
        corroborating_event = _provider_time(str(corroborating_watermark))
        if name == "posthog" and source.get("host_identity_available") is True:
            # Pre-promotion GA4 truth cannot indict the newly approved neutral
            # PostHog export. Require a dated pageview on a GA4-approved route;
            # the host-wide watermark alone may belong to an excluded path.
            eligible = []
            for row in data.get("ga4_rows") or []:
                stamp = row.get("provider_time") or row.get("timestamp") or row.get("observed_at")
                if (row.get("host") != PRODUCTION_HOST
                        or row.get("path") not in GA4_APPROVED_NEUTRAL_PATHS
                        or row.get("event_name") != "page_view"
                        or int(row.get("event_count") or 0) <= 0 or not stamp):
                    continue
                try:
                    seen = _provider_time(str(stamp))
                except (TypeError, ValueError):
                    continue
                if seen > _provider_time(POSTHOG_APPROVED_PROMOTION_UTC):
                    eligible.append(seen)
            if not eligible:
                continue
            corroborating_event = min(max(eligible), corroborating_event)
        if corroborating_event <= last_event:
            # Both providers agree that no newer traffic exists. Their fresh
            # successful readbacks are healthy measured silence, not darkness.
            continue
        last_event = corroborating_event
        age = (current - last_event).total_seconds() / 3600.0
        threshold = max(float(configured.get(name, default_threshold)),
                        SINK_LAG_FLOOR_HOURS.get(name, 0.0))
        if name == "ga4" and age <= threshold:
            source.update({"freshness_state": "lagging", "reporting_latency_hours": 48,
                           "reason": "documented_ga4_processing_latency_24_48h"})
        if age > threshold:
            # 2026-08-27 launch grace (#106 designed-idle doctrine): a source
            # whose last production event predates ACTIVATION is awaiting its
            # first real visitor — inside the declared grace window that is a
            # designed state, not a dead sink. Once a post-activation event has
            # ever landed (or the grace expires with none), the dark alarm
            # binds exactly as before.
            grace_hours = float(control.get("launch_dark_grace_hours", 72))
            since_activation = (current - activated).total_seconds() / 3600.0
            # 2026-08-27 second hole, found live at execution 1786: ONE real
            # visitor at 09:44Z ended "awaiting_first_traffic" and re-armed the
            # 2h threshold — which near-zero launch traffic can then never
            # satisfy, so the first organic pageview CAUSED a dead-sink alarm.
            # A silence gap is only meaningful once the source has demonstrated
            # flow: inside the grace window the threshold binds only after the
            # source has produced at least minimum_flow post-activation events.
            minimum_flow = int(control.get("launch_dark_minimum_flow_events", 20))
            rows_key = {"ga4": "ga4_rows", "posthog": "posthog_rows", "d1_clicks": "d1_clicks"}.get(name)
            post_activation_rows = 0
            for row in (data.get(rows_key) or []):
                stamp = row.get("provider_time") or row.get("timestamp") or row.get("observed_at")
                try:
                    if stamp and _provider_time(str(stamp)) > activated:
                        post_activation_rows += 1
                except Exception:
                    continue
            if since_activation <= grace_hours and post_activation_rows < minimum_flow:
                label = "awaiting_first_traffic" if post_activation_rows == 0 else "sub_minimum_traffic_grace"
                source.update({
                    "designed_idle": label,
                    "reason": f"{label}:post_activation_events_{post_activation_rows}<{minimum_flow}:activated_{since_activation:.2f}h_ago:grace_{grace_hours:.0f}h",
                })
            else:
                source.update({
                    "state": "dark", "reason": f"no_production_event:{age:.2f}h>{threshold:.2f}h",
                    "metric_value": None,
                })


# Provider reporting cadence is NOT collector liveness. These are typical
# reporting expectations (calendar days), not API availability SLAs.
SEARCH_REPORT_EXPECTATIONS = {
    "gsc": {"days": 3, "documentation": "https://developers.google.com/webmaster-tools/v1/how-tos/all-your-data"},
    "bing": {"days": 7, "documentation": "https://learn.microsoft.com/en-us/dotnet/api/microsoft.bing.webmaster.api.interfaces.iwebmasterapi.getpagestats?view=bing-webmaster-dotnet"},
}


def _apply_search_freshness(data: dict[str, Any], now: dt.datetime | None = None) -> None:
    current = now or dt.datetime.now(dt.timezone.utc)
    for name, expectation in SEARCH_REPORT_EXPECTATIONS.items():
        source = (data.get("sources") or {}).get(name)
        if not source:
            continue
        source.update(collector_max_age_hours=30, reporting_latency_days=expectation["days"],
                      reporting_documentation=expectation["documentation"])
        if source.get("state") not in {"healthy", "measured_zero"}:
            source["collector_state"] = "dark"
            source["freshness_state"] = "dark"
            continue  # Never heal an existing transport/schema/dark gate.
        try:
            age = (current - _provider_time(source["last_successful_readback"])).total_seconds() / 3600
        except (KeyError, TypeError, ValueError):
            age = None
        source["collector_age_hours"] = age
        if age is None or age < 0 or age > 30:
            source.update(state="dark", collector_state="dark", freshness_state="dark",
                          metric_value=None, reason="collector_readback_stale_or_invalid")
            continue
        source["collector_state"] = "fresh"
        # Legacy cache watermarks were request end date (GSC) / now (Bing).
        # Without an explicit provider-date witness they are not reporting truth.
        watermark = source.get("watermark") if source.get("watermark_kind") == "provider_report_date" else None
        try:
            days = (current.date() - _provider_time(watermark).date()).days if watermark else None
        except (TypeError, ValueError):
            days = None
        source["reporting_age_days"] = days
        source["reporting_expectation_exceeded"] = (days > expectation["days"]) if days is not None else None
        source["freshness_state"] = "unknown" if days is None or days < 0 else ("lagging" if days else "current")
        source["freshness_reason"] = ("reporting_watermark_unknown" if days is None else
            "reporting_latency_beyond_typical_expectation" if days > expectation["days"] else "within_documented_reporting_cadence")


def _search_refresh_due(source: dict[str, Any], now: dt.datetime | None = None) -> bool:
    """Daily source reads share this collector; hourly repairs an overdue cache.

    Reporting latency never extends collector freshness. This does not run the
    legacy daily compatibility writer or create another scheduling owner.
    """
    current = now or dt.datetime.now(dt.timezone.utc)
    try:
        age = (current - _provider_time(source["last_successful_readback"])).total_seconds() / 3600
    except (KeyError, TypeError, ValueError):
        return True
    return age < 0 or age >= 24

def _bound_affiliate_release(control: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    """Bind governed program authority to the manifest actually deployed at /go."""
    policy = control["affiliate_obligations"]
    url = f"https://{PRODUCTION_HOST}/go/_manifest"
    if policy["deployed_manifest"] != url:
        raise RuntimeError("affiliate_manifest_host_mismatch")
    pointer = read_json(BUSINESS / policy["release_pointer"])
    if pointer.get("status") != "PASS":
        raise RuntimeError("affiliate_release_pointer_unaccepted")
    if not pointer.get("manifest"):
        raise RuntimeError("affiliate_release_manifest_missing")
    for key in ("final_receipt", "manifest"):
        seal = pointer.get(key)
        if not seal:
            raise RuntimeError(f"affiliate_release_{key}_missing")
        try:
            actual = hashlib.sha256(Path(seal["path"]).read_bytes()).hexdigest()
            matches = actual == seal["sha256"]
        except (KeyError, TypeError, ValueError, OSError):
            matches = False
        if not matches:
            raise RuntimeError(f"affiliate_release_{key}_drift")
    release = Path(pointer["release_root"]).resolve(strict=True)
    manifest = read_json(Path(pointer["manifest"]["path"]))
    sealed_rows = {row["path"]: row["sha256"] for row in manifest["release_rows"]}
    sealed_bytes = {}
    for name in ("functions/go/program-manifest.js",
                 "data/program-registry.json", "data/go-runtime-promotion-authority.json",
                 "data/approved-program-registry.json"):
        expected = sealed_rows.get(name)
        try:
            body = (release / name).read_bytes()
        except OSError as exc:
            raise RuntimeError(f"affiliate_release_authority_drift:{name}") from exc
        if not expected or hashlib.sha256(body).hexdigest() != expected:
            raise RuntimeError(f"affiliate_release_authority_drift:{name}")
        sealed_bytes[name] = body

    # This generated module is data: verify its exact wrapper and parse only
    # the JSON argument. Never import/evaluate JavaScript or use staging data
    # as a fallback for the accepted release's sealed runtime authority.
    module = sealed_bytes["functions/go/program-manifest.js"]
    prefix = (b"// Generated by scripts/build-staging.mjs. Do not edit by hand.\n"
              b"export const GO_RUNTIME_MANIFEST = Object.freeze(")
    if not module.startswith(prefix) or not module.endswith(b"});\n"):
        raise RuntimeError("affiliate_release_runtime_manifest_wrapper_invalid")

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_json_key")
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError(f"non_json_constant:{value}")

    try:
        local = json.loads(module[len(prefix):-3].decode("utf-8"),
                           object_pairs_hook=unique_object, parse_constant=reject_constant)
    except (UnicodeError, ValueError) as exc:
        raise RuntimeError("affiliate_release_runtime_manifest_invalid") from exc
    if (not isinstance(local, dict)
            or local.get("schema") != "affiliate-worker-staging-v3-registry"
            or local.get("program_registry_schema") != "site-program-registry-v1"
            or local.get("site_profile") != "glp-clinic-scout"
            or not isinstance(local.get("manifest_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", local["manifest_sha256"])
            or not isinstance(local.get("programs"), dict)
            or any(not program_id for program_id in local["programs"])):
        raise RuntimeError("affiliate_release_runtime_manifest_invalid")
    status, body = _http_text(url)
    try:
        live = json.loads(body, object_pairs_hook=unique_object, parse_constant=reject_constant)
    except ValueError as exc:
        raise RuntimeError("affiliate_deployed_release_manifest_mismatch") from exc
    if (status != 200 or not isinstance(live, dict)
            or live.get("schema") != local["schema"]
            or live.get("manifest_sha256") != local["manifest_sha256"]
            or not isinstance(live.get("program_ids"), list)
            or any(not isinstance(program_id, str) or not program_id for program_id in live["program_ids"])
            or len(live["program_ids"]) != len(set(live["program_ids"]))
            or set(live["program_ids"]) != set(local["programs"])):
        raise RuntimeError("affiliate_deployed_release_manifest_mismatch")
    return release, local["programs"]


def _current_network_holds(control: dict[str, Any]) -> set[str]:
    hold = control["affiliate_obligations"].get("current_network_holds")
    if hold is None:
        return set()
    networks = []
    try:
        networks = hold["networks"]
        body = (BUSINESS / hold["authority_path"]).read_bytes()
        valid = (isinstance(networks, list) and bool(networks)
                 and all(isinstance(value, str) and value == value.lower() for value in networks)
                 and hashlib.sha256(body).hexdigest() == hold["authority_sha256"])
    except (KeyError, TypeError, OSError):
        valid = False
    if not valid:
        raise RuntimeError("affiliate_current_hold_invalid")
    return set(networks)


def _assert_direct_decision(promotion: dict[str, Any]) -> None:
    decision = promotion.get("operator_approved_live_decision") or {}
    try:
        body = Path(decision["path"]).read_bytes()
        valid = hashlib.sha256(body).hexdigest() == decision["sha256"]
    except (KeyError, TypeError, OSError):
        valid = False
    if not valid:
        raise RuntimeError("affiliate_direct_decision_invalid")


def _approved_direct_target(runtime: dict[str, Any], promotion: dict[str, Any]) -> str | None:
    """The bound site contract permits this issued direct URL, never its /go candidate."""
    if not (runtime.get("entry_state") == "transparent_direct"
            and runtime.get("status") == "candidate_preview_only"
            and runtime.get("link_mode") == "instrumented_redirect_candidate"
            and runtime.get("candidate_gate") == "operator_approved_glp_live"
            and promotion.get("reason") == "operator_approved_glp_live"
            and promotion.get("promotion_authorized") is True
            and promotion.get("status") == "VERIFIED"):
        return None
    _assert_direct_decision(promotion)
    tracking = runtime.get("tracking") or {}
    href = tracking.get("base_url")
    if (not isinstance(href, str) or urlsplit(href).scheme != "https"
            or urlsplit(href).hostname not in (tracking.get("allowed_tracking_hosts") or [])
            or tracking.get("generated_link_readback_verified") is not True
            or runtime.get("network") != promotion.get("network")):
        raise RuntimeError("affiliate_direct_target_invalid")
    return href


def _relationship_denied(program: dict[str, Any]) -> bool:
    """Consume denial-only lifecycle evidence from the already sealed registry.

    Raw commercial state and the historical promotion map intentionally survive
    an ended relationship; the publisher removes it from the effective runtime.
    Absence alone is never a denial, nor can this branch grant a live link.
    """
    snapshot = program.get("affiliate_relationship_snapshot") or {}
    if not isinstance(snapshot, dict):
        raise RuntimeError(f"affiliate_relationship_denial_invalid:{program['program_id']}")
    if snapshot.get("lifecycle") not in {"ended", "withdrawn"}:
        return False
    if (snapshot.get("promotion_denied") is not True
            or snapshot.get("reported_information") is not True
            or not snapshot.get("network") or not snapshot.get("network_program_id")
            or not snapshot.get("relationship_status")):
        raise RuntimeError(f"affiliate_relationship_denial_invalid:{program['program_id']}")
    try:
        _provider_time(snapshot["source_checked_at"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"affiliate_relationship_denial_invalid:{program['program_id']}") from exc
    return True


def _approved_api_direct_target(release: Path, control: dict[str, Any], pid: str,
                                runtime: dict[str, Any], promotion: dict[str, Any]) -> str:
    """Read the sealed sole-writer API grant, never upgrade its /go candidate.

    This recognizes the maintained review-tracking-rebind producer's direct-only
    class. Current relationship/operator/network holds are evaluated by the caller
    first. Only provider-review and explicitly declared organic comparison
    surfaces are authorized by this reader; no health-information surface.
    """
    if promotion.get("reason") != "approved_api_link_sole_writer_ready":
        return ""
    if (runtime.get("entry_state") != "transparent_direct"
            or runtime.get("live_link_allowed") is not False
            or runtime.get("promotion_authorized") is not False
            or runtime.get("candidate_gate") != promotion["reason"]
            or promotion.get("status") != "VERIFIED"
            or promotion.get("promotion_authorized") is not True
            or promotion.get("constructor_verified") is not True
            or promotion.get("direct_only") is not True
            or str(promotion.get("network", "")).casefold() != "maxbounty"
            or str(runtime.get("network", "")).casefold() != "maxbounty"):
        raise RuntimeError("affiliate_api_direct_authority_invalid:" + pid)
    href = str(promotion.get("issued_link") or "")
    url = urlsplit(href)
    tracking = runtime.get("tracking") or {}
    if (url.scheme != "https" or not url.hostname or url.username or url.password
            or url.fragment or href != tracking.get("base_url")
            or tracking.get("generated_link_readback_verified") is not True
            or url.hostname not in tracking.get("allowed_tracking_hosts", [])
            or promotion.get("tracking_link_sha256") != hashlib.sha256(href.encode()).hexdigest()
            or promotion.get("source_receipt") != f"data/review-tracking-receipts.json#{pid}"):
        raise RuntimeError("affiliate_api_direct_target_invalid:" + pid)
    pointer = read_json(BUSINESS / control["affiliate_obligations"]["release_pointer"])
    seals = {row["path"]: row["sha256"] for row in read_json(Path(pointer["manifest"]["path"]))["release_rows"]}
    name = "data/review-tracking-receipts.json"
    path = release / name
    if not path.is_file() or seals.get(name) != hashlib.sha256(path.read_bytes()).hexdigest():
        raise RuntimeError("affiliate_api_direct_receipt_unsealed:" + pid)
    doc = read_json(path)
    receipt = doc.get("programs", {}).get(pid, {})
    campaign = receipt.get("campaign") or {}
    api = receipt.get("tracking_api_receipt") or {}
    response = api.get("response") or {}
    if (doc.get("schema") != "glp-review-approved-tracking-v1"
            or doc.get("tracking_clicks") != 0
            or campaign.get("details", {}).get("affiliate_campaign_status") != "Approved"
            or api.get("schema") != "glp.maxbounty.authorized_api_tracking_link.v1"
            or str(api.get("campaign_id")) != str(campaign.get("campaign_id"))
            or api.get("publisher_id") != "803003" or api.get("status") != 200
            or api.get("tracking_clicks") != 0 or response.get("success") is not True
            or response.get("tracking-link") != href):
        raise RuntimeError("affiliate_api_direct_receipt_invalid:" + pid)
    return href


def _bound_fastpath_permissions(release: Path, control: dict[str, Any],
                                programs: dict[str, Any], authority: dict[str, Any],
                                runtime: dict[str, Any], current_holds: set[str]) -> dict[str, Any]:
    """Bind active direct fastpaths separately; never invent /go membership.

    Measured's accepted direct CTA intentionally has no public redirect entry.
    Its own provider/review pages still require the exact issued direct URL.
    Read only sealed publication inputs; unknown/inactive terms stay link-dark.
    """
    candidates = {pid: program for pid, program in programs.items()
                  if pid != 'fella-health' and not _relationship_denied(program)
                  and pid not in runtime and program.get('commercial_state') == 'live'
                  and (authority.get(pid) or {}).get('promotion_authorized') is True
                  and (authority.get(pid) or {}).get('status') == 'VERIFIED'
                  and (program.get('profiles', {}).get('glp-clinic-scout') or {}).get('wired') is True
                  and (program.get('profiles', {}).get('glp-clinic-scout') or {}).get('vertical_fit') is True
                  and (program.get('profiles', {}).get('glp-clinic-scout', {}).get('surfaces') or {}).get('review') is True}
    if not candidates:
        return {}
    pointer = read_json(BUSINESS / control['affiliate_obligations']['release_pointer'])
    if Path(pointer['release_root']).resolve(strict=True) != release:
        raise RuntimeError('affiliate_fastpath_release_changed')
    seals = {row['path']: row['sha256'] for row in read_json(Path(pointer['manifest']['path']))['release_rows']}
    docs = []
    for name, schema in (
        ('data/provider-page-contracts.json', 'glp-provider-fastpath-contracts-v1'),
        ('data/provider-fastpath-contract.json', 'glp-provider-fastpath-integration-v1'),
        ('data/affiliate-wire-in-approvals.json', 'glp-affiliate-wire-in-approvals-v1'),
    ):
        path = release / name
        if (path.is_symlink() or not path.is_file() or not seals.get(name)
                or hashlib.sha256(path.read_bytes()).hexdigest() != seals[name]):
            raise RuntimeError('affiliate_fastpath_authority_drift:' + name)
        doc = read_json(path)
        if doc.get('schema') != schema:
            raise RuntimeError('affiliate_fastpath_authority_invalid:' + name)
        docs.append(doc)
    contracts, fastpaths, approvals = docs
    terms = (approvals.get('current_terms_reassessment') or {}).get('programs') or []
    permissions = {}
    for pid, program in candidates.items():
        active = [row for row in fastpaths.get('providers', []) if row.get('program_id') == pid]
        matching = [row for row in contracts.get('providers', []) if row.get('program_id') == pid]
        if (len(active) != 1 or len(matching) != 1
                or active[0].get('affiliate_cta_state') != 'ACTIVE_PROVIDER_ISSUED_DIRECT'
                or active[0].get('route_state') != 'LIVE_EDITORIAL_AFFILIATE_ACTIVE'):
            continue
        contract = matching[0]
        row, network = contract.get('row_contract') or {}, contract.get('network_contract') or {}
        promotion = authority[pid]
        term = [entry for entry in terms if entry.get('existing_release_alias') == pid
                and str(entry.get('provider_program_id')) == str(network.get('immutable_offer_id'))]
        href = network.get('tracking_url')
        if (len(term) != 1 or term[0].get('status') != 'operator_accepted_terms'
                or term[0].get('verdict') != 'operator_accepted_terms'
                or term[0].get('live_link_disposition') != 'preserve_existing_live'
                or term[0].get('future_change_terms_gate_open') is not True
                or not term[0].get('existing_live_surfaces')
                or contract.get('review_path') != program.get('review_path')
                or row.get('program_id') != pid or row.get('promotion_authorized') is not True
                or row.get('live_link_allowed') is not True or row.get('affiliate_link_mode') != 'transparent_direct'
                or network.get('activation_blocker') is not None
                or network.get('relationship_state') != 'approved_or_active'
                or network.get('transparent_direct_supported_by_current_renderer') is not True
                or not isinstance(href, str) or urlsplit(href).scheme != 'https'
                or not urlsplit(href).hostname or row.get('affiliate_href') != href
                or str(network.get('network') or '').lower() != str(promotion.get('network') or '').lower()
                or row.get('affiliate_network') != network.get('network')):
            continue
        if term[0].get('rescission_note'):
            risk = term[0].get('risk_acceptance') or {}
            decision = risk.get('operator_decision_receipt') or {}
            if (risk.get('risk_accepted') is not True
                    or term[0].get('decision_receipt_sha256') != decision.get('sha256')
                    or hashlib.sha256(Path(decision['path']).read_bytes()).hexdigest() != decision.get('sha256')):
                raise RuntimeError('affiliate_fastpath_rescission_invalid:' + pid)
        _assert_direct_decision(promotion)
        held = str(network['network']).lower() in current_holds
        permissions[pid] = {'obligation': 'forbidden' if held else 'required',
                            'allowed': not held, 'direct_target': None if held else href,
                            'go_allowed': False, 'network': str(network['network']).lower(),
                            'direct_paths': [f'/providers/{pid}/', program['review_path']]}
    return permissions


def _approved_directory_target(target: dict[str, Any], path: str, authority: dict[str, Any],
                               approved: dict[str, Any], current_holds: set[str]) -> bool:
    """Accepted directory-only direct rails are not /go registry members."""
    promotion = authority.get(str(target.get("program_id") or "")) or {}
    network = str(promotion.get("network") or "").lower()
    href = target.get("href")
    if not (path == "/providers/" and target.get("link_mode") == "transparent-direct"
            and isinstance(href, str) and urlsplit(href).scheme == "https"
            and promotion.get("promotion_authorized") is True and promotion.get("status") == "VERIFIED"
            and promotion.get("reason") == "operator_approved_glp_live"
            and str(target.get("network") or "").lower() == network and network not in current_holds):
        return False
    _assert_direct_decision(promotion)
    if promotion.get("directory_only") is True:
        return hashlib.sha256(href.encode()).hexdigest() == promotion.get("issued_link_sha256")
    for row in approved["programs"]:
        tracking = row.get("tracking") or {}
        if (row.get("registry_id") == target.get("program_id")
                and str(row.get("network") or "").lower() == network
                and row.get("routing_class") == "glp1_weightloss_coverage_candidate"
                and tracking.get("candidate_registry_wired") is True
                and tracking.get("public_go_surface") is False
                and tracking.get("provider_issued_url") == href
                and tracking.get("tracking_host") == urlsplit(href).hostname):
            return True
    return False


def _listing_contract(html: str) -> dict[str, Any]:
    """Read the native duration listing's actual cohorts and controls, not an empty flag."""
    if "data-listing-archetype" not in html:
        return {}
    hooks = ("data-filter-result", "data-provider-filters", "data-phase3-provider-grid",
             "data-filter-empty", "data-unpublished-provider-section", "data-provider-card-payloads",
             "data-phase3-dose-selection")

    class Listing(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.stack = []
            self.parts = {key: [] for key in hooks}
            self.active = []
            self.cards = 0
            self.route = []
            self.scripts = []

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            blocked = any(self.stack) or "hidden" in attrs or "inert" in attrs or attrs.get("aria-hidden") == "true"
            if tag == "main" and attrs.get("data-listing-archetype") == "A":
                self.route.append([attrs.get("data-phase3-route"), blocked])
            if "data-phase3-provider" in attrs or "data-unpublished-price-card" in attrs:
                self.cards += 1
            if tag == "script" and urlsplit(attrs.get("src") or "").path == "/assets/site.js":
                self.scripts.append([list(item) for item in sorted(attrs.items())])
            token = ["start", tag, [list(item) for item in sorted(attrs.items())], blocked]
            for _, tokens, _ in self.active:
                tokens.append(token)
            for key in hooks:
                if key in attrs:
                    tokens = [token]
                    self.parts[key].append(tokens)
                    self.active.append((key, tokens, len(self.stack)))
            if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
                self.stack.append(blocked)

        def handle_endtag(self, tag):
            if self.stack:
                self.stack.pop()
            for _, tokens, _ in self.active:
                tokens.append(["end", tag])
            self.active = [part for part in self.active if part[2] < len(self.stack)]

        def handle_data(self, text):
            text = " ".join(text.split())
            if text:
                for _, tokens, _ in self.active:
                    tokens.append(["text", text])

    parsed = Listing()
    parsed.feed(html)
    parts = parsed.parts
    counts = parts["data-filter-result"]
    text = " ".join(token[1] for tokens in counts for token in tokens if token[0] == "text")
    match = re.fullmatch(r"(\d+) comparable price results", text) if len(counts) == 1 else None
    numeric = int(match[1]) if match else None
    empty_tokens = parts["data-filter-empty"]
    empty_text = " ".join(token[1] for tokens in empty_tokens for token in tokens if token[0] == "text")
    reset = sum(token[0] == "start" and token[1] == "button"
                and dict(token[2]).get("type") == "button" and "data-filter-reset" in dict(token[2])
                for tokens in empty_tokens for token in tokens)
    blocked = any(tokens[0][3] for key in hooks[:4] for tokens in parts[key])
    empty = (len(parsed.route) == 1 and not parsed.route[0][1] and numeric == 0
             and all(len(parts[key]) == 1 for key in hooks[:4]) and not blocked
             and parsed.cards == 0 and not parts[hooks[4]] and not parts[hooks[5]] and reset == 1
             and empty_text == "No source-backed record matches those filters. An empty result is not a medical or availability conclusion. Active filters: none. Reset filters"
             and len(parsed.scripts) == 1)
    return {"route": parsed.route, "numeric_count": numeric,
            "unpublished_count": len(parts[hooks[4]]), "card_count": parsed.cards,
            "parts": parts, "runtime_scripts": parsed.scripts, "empty": empty}


def _listing_obligation(expected: dict[str, Any] | None, observed: dict[str, Any] | None) -> str:
    if (not expected or observed != expected or type(expected.get("numeric_count")) is not int
            or expected["numeric_count"] < 0 or len(expected.get("route") or []) != 1):
        return "unknown"
    # Empty is a prohibition on commercial anchors, NOT a shell/privacy exemption.
    return "forbidden" if expected.get("empty") is True else "required"


def _bound_listing_contracts(release: Path, control: dict[str, Any],
                             covered: set[str]) -> dict[str, dict[str, Any]]:
    """Exercise the existing exported producer on accepted, sealed inputs only."""
    policy = control["affiliate_obligations"].get("duration_listing_contract")
    if policy is None:
        return {}
    decision = (BUSINESS / policy["authority_path"]).read_bytes()
    if hashlib.sha256(decision).hexdigest() != policy["authority_sha256"]:
        raise RuntimeError("affiliate_listing_authority_drift")
    pointer = read_json(BUSINESS / control["affiliate_obligations"]["release_pointer"])
    if Path(pointer["release_root"]).resolve(strict=True) != release:
        raise RuntimeError("affiliate_listing_release_changed")
    rows = read_json(Path(pointer["manifest"]["path"]))["release_rows"]
    seals = {row["path"]: row["sha256"] for row in rows}
    # Retain the source preflight, but it is not the module-consumption boundary.
    # Node's synchronous load hook below authenticates the actual evaluated bytes.
    for source in (release / "src").rglob("*"):
        if source.is_file():
            name = source.relative_to(release).as_posix()
            if (source.is_symlink() or not seals.get(name)
                    or hashlib.sha256(source.read_bytes()).hexdigest() != seals[name]):
                raise RuntimeError(f"affiliate_listing_producer_drift:{name}")
    script = r'''
import fs from 'node:fs';
import cp from 'node:child_process';
import {syncBuiltinESMExports, registerHooks, isBuiltin} from 'node:module';
import {createHash} from 'node:crypto';
import {pathToFileURL, fileURLToPath} from 'node:url';
import path from 'node:path';
const input=JSON.parse(fs.readFileSync(0,'utf8'));
const root=process.argv[1];
const read=fs.readFileSync, exists=fs.existsSync, failures=[];
const fail=name=>{failures.push(name);throw new Error('affiliate_listing_input_invalid:'+name);};
const deny=()=>{throw new Error('affiliate_listing_effect_denied');};
fs.readFileSync=(file,...args)=>{
  const resolved=path.resolve(file instanceof URL?fileURLToPath(file):file);
  const name=path.relative(root,resolved);
  if(!input.seals[name]) return fail('unbound:'+name);
  let bytes;
  try { bytes=read(resolved); } catch { return fail('missing:'+name); }
  if(createHash('sha256').update(bytes).digest('hex')!==input.seals[name]) return fail('drift:'+name);
  if(name.endsWith('.json')) { try { JSON.parse(bytes); } catch { return fail('json:'+name); } }
  return args[0]?bytes.toString(typeof args[0]==='string'?args[0]:args[0].encoding):bytes;
};
fs.existsSync=file=>{
  const resolved=path.resolve(file instanceof URL?fileURLToPath(file):file), name=path.relative(root,resolved);
  if(input.seals[name]&&!exists(resolved)) return fail('missing:'+name);
  return exists(resolved);
};
for(const key of ['writeFileSync','appendFileSync','renameSync','unlinkSync','rmSync','mkdirSync','copyFileSync','createWriteStream']) fs[key]=deny;
for(const key of ['exec','execSync','execFile','execFileSync','spawn','spawnSync','fork']) cp[key]=deny;
globalThis.fetch=deny;
syncBuiltinESMExports();
// Node >=22.15 registerHooks runs in-thread, so caught import failures share
// the data-read failure latch. Check nextLoad's source, not a second path read:
// this is the exact buffer Node will evaluate, including dependency imports.
const moduleName=url=>{
  if(url.startsWith('node:')&&isBuiltin(url)) return null;
  if(!url.startsWith('file:')) return fail('module-url:'+url);
  const name=path.relative(root,fileURLToPath(url));
  if(path.isAbsolute(name)||name==='..'||name.startsWith('..'+path.sep)||!input.seals[name]) return fail('module-unbound:'+name);
  return name;
};
registerHooks({
  resolve(specifier,context,nextResolve){
    let result;
    try { result=nextResolve(specifier,context); } catch { return fail('module-resolve:'+specifier); }
    moduleName(result.url);
    return result;
  },
  load(url,context,nextLoad){
    const name=moduleName(url);
    let result;
    try { result=nextLoad(url,context); } catch { return fail('module-load:'+url); }
    if(name!==null){
      if(!['module','json'].includes(result.format)||!(typeof result.source==='string'||ArrayBuffer.isView(result.source)||result.source instanceof ArrayBuffer)) return fail('module-source:'+name);
      const bytes=result.source instanceof ArrayBuffer?new Uint8Array(result.source):result.source;
      if(createHash('sha256').update(bytes).digest('hex')!==input.seals[name]) return fail('module-drift:'+name);
      if(result.format==='json') { try { JSON.parse(Buffer.from(bytes).toString('utf8')); } catch { return fail('module-json:'+name); } }
    }
    return result;
  }
});
const json=name=>JSON.parse(fs.readFileSync(path.join(root,name),'utf8'));
const load=name=>import(pathToFileURL(path.join(root,'src',name)));
const phase=await load('phase3-page-graph.mjs');
const {publicProviderNames}=await load('public-provider-names.mjs');
const graph=json('data/phase3-page-graph.json'), pricingMatrix=publicProviderNames(json('data/provider-pricing-matrix.json'));
// Native provenance validation: unknown/missing pricing cannot become a zero cohort.
(await load('cost-calculator.mjs')).attachPricingMatrix(Object.keys(pricingMatrix.providers||{}).map(program_id=>({program_id})),pricingMatrix);
const specs=json('data/money-page-specs.json'), affiliateMap=(await load('affiliate-link-map.mjs')).loadAffiliateLinkMap('compare');
if(!Array.isArray(specs.pages)||specs.pages.some(page=>!Array.isArray(page.rows)||page.rows.some(row=>row.program_id&&!Object.hasOwn(affiliateMap,row.program_id)))) throw new Error('affiliate_listing_link_map_unknown');
const context={graph,pricingMatrix,
  registry:phase.phase3Registry(graph,publicProviderNames(json('data/program-registry.json'))),
  coupons:(await load('coupon-offers.mjs')).activeCouponMap(json('data/coupon-offers.json')),
  doseCapture:phase.loadPass2DoseCapture(),
  logoManifest:{...json('assets/provider-logos/manifest.json'),atlas:json('assets/provider-logos/pass2-lp-atlas.json')},
  articles:[],goAuthority:(await load('go-runtime-selection.mjs')).loadGoPromotionAuthority()};
const result={};
for(const page of graph.bulk_pages.filter(p=>input.covered.includes(p.route))) {
  if(!Number.isFinite(page.duration_months)||page.duration_months<=0) throw new Error('affiliate_listing_duration_invalid');
  result[page.route]={native:phase.renderConfiguredListing('A',page,context),
    published:fs.readFileSync(path.join(root,'public',page.route.slice(1),'index.html'),'utf8')};
}
if(failures.length) throw new Error('affiliate_listing_swallowed_input_failure:'+failures.join(','));
console.log(JSON.stringify(result));
'''
    result = subprocess.run(["node", "--input-type=module", "-e", script, str(release)],
                            input=json.dumps({"seals": seals, "covered": sorted(covered)}),
                            text=True, capture_output=True, timeout=45, check=False)
    result.check_returncode()  # Retain refusal; the genuine process result is observable first.
    contracts = {}
    for route, html in json.loads(result.stdout).items():
        # The shell finalizer installs site.js; bind its actual sealed loader and
        # runtime bytes, while cohorts/filter/reset markup comes from the producer.
        published = _listing_contract(html["published"])
        scripts = published["runtime_scripts"]
        if len(scripts) != 1:
            raise RuntimeError(f"affiliate_listing_runtime_missing:{route}")
        src = dict(scripts[0])["src"]
        name = "public" + urlsplit(src).path
        if (urlsplit(src).netloc or not seals.get(name)
                or hashlib.sha256((release / name).read_bytes()).hexdigest() != seals[name]):
            raise RuntimeError(f"affiliate_listing_runtime_drift:{route}")
        loader = '<script ' + ' '.join(f'{key}="{value}"' if value is not None else key
                                     for key, value in scripts[0]) + '></script>'
        contracts[route] = _listing_contract(html["native"] + loader)
    return contracts


def derive_affiliate_obligations(control: dict[str, Any],
                                 observed_routes: set[str] | None = None,
                                 static_routes: list[dict[str, Any]] | None = None) -> dict[str, str]:
    """Keep covered routes and bind duties to effective sealed commercial authority."""
    release, runtime_programs = _bound_affiliate_release(control)
    public_ids = set(runtime_programs)
    registry = read_json(release / "data/program-registry.json")
    authority = read_json(release / "data/go-runtime-promotion-authority.json")
    programs = {row["program_id"]: row for row in registry["programs"]}
    reviews = {row["review_path"]: row["program_id"] for row in registry["programs"]
               if row.get("review_path")}
    current_holds = _current_network_holds(control)
    fastpath_permissions = _bound_fastpath_permissions(
        release, control, programs, authority.get("programs") or {}, runtime_programs, current_holds)
    covered = set(control["affiliate_routes"])
    observed = set(observed_routes or ())
    for program_id, program in programs.items():
        promotion = (authority.get("programs") or {}).get(program_id)
        review = program.get("review_path")
        if promotion is None:
            for route in (f"/providers/{program_id}/", review):
                if route in observed:
                    covered.add(route)
            continue
        allowed = promotion.get("promotion_authorized")
        if _relationship_denied(program):
            allowed = False
        published_provider = program_id in public_ids or program_id in fastpath_permissions
        published_review = bool(review and review.strip("/").split("/")[-1] in public_ids)
        if allowed is False or (allowed is True and published_provider):
            covered.add(f"/providers/{program_id}/")
        if review and (allowed is False or (allowed is True and (published_provider or published_review))):
            covered.add(review)
    approved = read_json(release / "data/approved-program-registry.json")
    if (approved.get("schema") != "approved-program-successor-registry-v1"
            or not isinstance(approved.get("programs"), list)):
        raise RuntimeError("affiliate_directory_authority_invalid")
    permissions: dict[str, dict[str, Any]] = dict(fastpath_permissions)
    static = {row["path"]: row for row in static_routes or []}
    listing_policy = control["affiliate_obligations"].get("duration_listing_contract") or {}
    listing_routes = set(listing_policy.get("routes") or []) & covered
    try:
        listings = _bound_listing_contracts(release, control, listing_routes) if listing_routes else {}
    except Exception as exc:  # noqa: BLE001 - binding failure is unknown, never zero
        listings = {}
        for route in listing_routes & set(static):
            static[route]["listing_contract_error"] = f"{type(exc).__name__}:{str(exc)[:200]}"
    obligations: dict[str, str] = {}
    for route in sorted(covered):
        parts = route.strip("/").split("/")
        program_id = None
        if len(parts) == 2 and parts[0] == "providers":
            program_id = parts[1]
        elif len(parts) == 2 and parts[0] == "reviews":
            program_id = reviews.get(route)
            if program_id is None:
                raise RuntimeError(f"affiliate_review_identity_missing:{route}")
        if program_id is None:
            obligations[route] = (_listing_obligation(listings.get(route),
                                                      static.get(route, {}).get("listing_contract"))
                                  if route in listing_routes else "required")
            continue
        if program_id in fastpath_permissions:
            obligations[route] = fastpath_permissions[program_id]["obligation"]
            continue
        program = programs.get(program_id)
        promotion = (authority.get("programs") or {}).get(program_id)
        if program is None:
            raise RuntimeError(f"affiliate_program_authority_missing:{route}")
        if promotion is not None and not isinstance(promotion, dict):
            raise RuntimeError(f"affiliate_program_authority_invalid:{program_id}")
        if program.get("commercial_state") == "held":
            # Some deliberately excluded programs have no promotion-map row.
            # Their authenticated registry-bound denial is not unknown authority.
            meta = (((program.get("profiles") or {}).get("glp-clinic-scout") or {})
                    .get("exceptions") or {}).get("go") or {}
            receipt_path = release / meta.get("authority_path", "")
            receipt_bytes = receipt_path.read_bytes() if receipt_path.is_file() else b""
            receipt = json.loads(receipt_bytes) if receipt_bytes else {}
            if ((promotion is not None and promotion.get("promotion_authorized") is not False)
                    or meta.get("enabled") is not True
                    or hashlib.sha256(receipt_bytes).hexdigest() != meta.get("authority_sha256")
                    or receipt.get("schema") != "glp-receipted-research-hold-v1"
                    or receipt.get("program_id") != program_id
                    or receipt.get("disposition") != "HOLD"
                    or receipt.get("promotion_authorized") is not False
                    or receipt.get("live_link_allowed") is not False):
                raise RuntimeError(f"affiliate_operator_hold_invalid:{program_id}")
            if promotion is None:
                promotion = {"promotion_authorized": False, "status": "WITHHELD"}
        if promotion is None:
            obligations[route] = "unknown"
            continue
        allowed = promotion.get("promotion_authorized")
        status = promotion.get("status")
        if (allowed is True and status != "VERIFIED") or (allowed is False and status != "WITHHELD"):
            raise RuntimeError(f"affiliate_program_authority_invalid:{program_id}")
        if type(allowed) is not bool:
            raise RuntimeError(f"affiliate_program_authority_unknown:{program_id}")
        if _relationship_denied(program):
            allowed = False
        # The sealed generated runtime incorporates selection/current holds after
        # the historical promotion map. Membership alone is NOT a live grant.
        runtime = runtime_programs.get(program_id)
        if runtime is None and program.get("review_path"):
            runtime = runtime_programs.get(program["review_path"].strip("/").split("/")[-1])
        direct_target = None
        go_allowed = False
        if runtime is not None:
            if (not isinstance(runtime, dict)
                    or type(runtime.get("promotion_authorized")) is not bool
                    or type(runtime.get("live_link_allowed")) is not bool
                    or runtime["promotion_authorized"] != runtime["live_link_allowed"]
                    or (allowed is False and runtime["promotion_authorized"] is True)):
                raise RuntimeError(f"affiliate_runtime_authority_invalid:{program_id}")
            go_allowed = allowed and runtime["promotion_authorized"]
            # Runtime flags govern the instrumented redirect. The explicitly
            # receipted site-direct branch has a separate exact issued target.
            if allowed and not go_allowed:
                direct_target = _approved_direct_target(runtime, promotion)
                if direct_target is None:
                    direct_target = _approved_api_direct_target(
                        release, control, program_id, runtime, promotion) or None
            elif go_allowed:
                direct_target = (runtime.get("tracking") or {}).get("base_url")
            allowed = go_allowed or direct_target is not None
        elif allowed:
            raise RuntimeError(f"affiliate_runtime_authority_invalid:{program_id}")
        network = (runtime or {}).get("network") or promotion.get("network")
        if str(network or "").lower() in current_holds:
            allowed = False
        permission = {"allowed": allowed, "go_allowed": go_allowed and allowed,
                      "go_route": (runtime or {}).get("route"), "direct_target": direct_target,
                      "network": str(network or "").lower()}
        if promotion.get("reason") == "approved_api_link_sole_writer_ready" and direct_target:
            permission["direct_paths"] = [f"/providers/{program_id}/", program.get("review_path")]
            roles = (runtime or {}).get("allowed_page_roles", [])
            # JSON arrays of exact strings only: scalar substring and mapping-key
            # membership are not declared role grants. Keep legitimate direct paths.
            if type(roles) is list and all(type(role) is str for role in roles):
                permission["direct_paths"].extend(
                    f"/{role}/" for role in ("semaglutide", "tirzepatide")
                    if role in roles)
        permissions[program_id] = permission
        if program.get("review_path"):
            permissions[program["review_path"].strip("/").split("/")[-1]] = permission
        obligations[route] = "required" if allowed else "forbidden"
    for row in static_routes or []:
        targets = row.get("sponsored_anchor_targets")
        row["affiliate_permission_check_complete"] = (
            isinstance(targets, list) and len(targets) == row.get("sponsored_anchor_count", 0))
        if targets is None and row.get("sponsored_anchor_count", 0) == 0:
            row["affiliate_permission_check_complete"] = True
        bad = 0
        for target in targets or []:
            permission = permissions.get(target.get("program_id")) or {}
            parsed = urlsplit(target.get("href") or "")
            valid_go = (permission.get("go_allowed") is True
                        and parsed.path == permission.get("go_route")
                        and parsed.hostname in (None, PRODUCTION_HOST)
                        and parsed.scheme in ("", "https"))
            valid_direct = (permission.get("allowed") is True
                            and ("direct_paths" not in permission or row.get("resolved_path", row["path"]) in permission["direct_paths"])
                            and target.get("href") == permission.get("direct_target")
                            and target.get("link_mode") == "transparent-direct"
                            and str(target.get("network") or "").lower() == permission.get("network"))
            valid_directory = (not permission and _approved_directory_target(
                target, row.get("resolved_path", row["path"]), authority.get("programs") or {}, approved, current_holds))
            bad += not (valid_go or valid_direct or valid_directory)
        row["unauthorized_affiliate_anchor_count"] = bad
    return obligations


def collect_live(control: dict[str, Any], phase: str, cache_path: Path | None = None) -> dict[str, Any]:
    generated = utc_now()
    cache = cache_path or (DEFAULT_OUTPUT / "canonical-source-cache.json")
    data = _load_cache(cache)
    data["generated_at_utc"] = generated
    data.setdefault("sources", {})
    refreshed: set[str] = set()

    if phase in {"deploy", "route-census"}:
        routes, sitemap_sources = sitemap_routes()
        data["sitemap_routes"] = routes
        data["sitemap_complete"] = True
        data["sitemap_sources"] = sitemap_sources
        data["static_routes"] = inspect_routes(routes, control)

    observed = set(data.get("sitemap_routes") or []) | {
        row["path"] for row in data.get("static_routes") or []}
    data["affiliate_route_obligations"] = derive_affiliate_obligations(
        control, observed_routes=observed, static_routes=data.get("static_routes"))

    rails = _rail_module()
    high_frequency = phase in {"deploy", "hourly", "daily"}
    if phase == "daily" or (high_frequency and _search_refresh_due(data["sources"].get("gsc") or {})):
        try:
            google = rails.GoogleRail()
        except Exception as exc:  # noqa: BLE001
            data["sources"]["gsc"] = _source("error", None, reason=str(exc)[:300],
                                                    schema=EXPECTED_SCHEMAS["gsc"])
        else:
            _collect_gsc(data, data["sources"], google)
        refreshed.add("gsc")
    if phase == "daily" or (high_frequency and _search_refresh_due(data["sources"].get("bing") or {})):
        _collect_bing(data, data["sources"], rails)
        refreshed.add("bing")
    if high_frequency:
        try:
            google = rails.GoogleRail()
        except Exception as exc:  # noqa: BLE001
            data["sources"]["ga4"] = _source("error", None, host_scope=PRODUCTION_HOST,
                                                    reason=str(exc)[:300], schema=EXPECTED_SCHEMAS["ga4"])
        else:
            _collect_ga4(data, data["sources"], google)
        refreshed.add("ga4")
        _collect_posthog(data, data["sources"], rails)
        refreshed.add("posthog")
        _collect_d1(data, data["sources"])
        refreshed.update({"d1_clicks", "network_revenue"})
    elif phase == "monthly":
        _collect_d1(data, data["sources"])
        refreshed.update({"d1_clicks", "network_revenue"})
    if phase in {"daily", "weekly"}:
        _collect_dataforseo(data["sources"])
        refreshed.add("dataforseo")

    _apply_dark_thresholds(data, control)
    _apply_search_freshness(data)
    _mark_stale_cached_sources(data, refreshed)
    data["collection_cadence"] = {
        "phase": phase,
        "sources_refreshed": sorted(refreshed),
        "route_census_refreshed": phase in {"deploy", "route-census"},
        "gsc_end_date_lag_days": 3 if "gsc" in refreshed else None,
    }
    _write_cache(cache, data, phase)
    return data


def control_status(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"activated": False, "reason": "control_file_missing"}
    payload = read_json(path)
    if payload.get("schema") != "glp-analytics-brain-control-v1":
        raise RuntimeError("analytics_brain_control_schema_invalid")
    if payload.get("activated") is True:
        if not payload.get("activated_at_utc"):
            raise RuntimeError("analytics_brain_activation_timestamp_required")
        _provider_time(str(payload["activated_at_utc"]))
    return payload

def family_deploy_readback() -> dict[str, Any]:
    """Registered deploy leg; no identity means no provider calls or fabricated PASS."""
    path = BUSINESS / "scripts/glp_analytics_every_family_verifier.py"
    spec = importlib.util.spec_from_file_location("glp44_registered_family_readback", path)
    if spec is None or spec.loader is None:
        return {"verdict": "BLOCKED_VERIFIER_NOT_INSTALLED", "provider_calls": 0}
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        trigger = read_json(DEPLOY_TRIGGER) if DEPLOY_TRIGGER.is_file() else None
        return module.registered_deploy_readback(trigger)
    except Exception as exc:  # noqa: BLE001 - collector's existing output remains inspectable
        return {"verdict": "BLOCKED_FAMILY_READBACK_ERROR", "provider_calls": 0,
                "error_type": type(exc).__name__}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--database", type=Path, default=DEFAULT_DB)
    parser.add_argument("--control", type=Path, default=CONTROL)
    parser.add_argument("--phase", choices=["deploy", "hourly", "daily", "route-census", "weekly", "monthly"],
                        default="hourly")
    parser.add_argument("--decision-class", choices=["search", "profitability", "cro"], default="search")
    parser.add_argument("--minimum-revenue-sample", type=int, default=20)
    args = parser.parse_args(argv)

    control = control_status(args.control)
    if args.fixture:
        data = read_json(args.fixture.resolve(strict=True))
    elif control.get("activated") is not True:
        print(json.dumps({
            "schema": "glp-analytics-brain-run-v1",
            "status": "PRELAUNCH_INACTIVE",
            "output_count": 0,
            "provider_calls": 0,
            "reason": control.get("reason") or "launch_activation_required",
            "candidate_tree_reads": 0,
            "candidate_tree_writes": 0,
        }, sort_keys=True))
        return 0
    else:
        data = collect_live(control, args.phase, args.output_dir / "canonical-source-cache.json")

    # The prior run's matrix feeds the probe's persistence gate: a cross-sink
    # asymmetry only FAILS once it has outlived the slower sink's reporting
    # latency, and the episode onset is carried across runs. Absence or
    # corruption of the prior file degrades to "first sighting", never a crash.
    prior_matrix = None
    prior_path = args.output_dir / "route-instrumentation-matrix.json"
    try:
        prior_matrix = json.loads(prior_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - first run or corrupt prior
        prior_matrix = None
    outputs = build_outputs(data, decision_class=args.decision_class,
                            minimum_revenue_sample=args.minimum_revenue_sample,
                            prior_matrix=prior_matrix)
    write_outputs(args.output_dir, outputs)
    args.database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(args.database)
    try:
        apply_fact_model(connection, MIGRATION)
        record_source_facts(connection, outputs)
    finally:
        connection.close()
    remote_fact_statements = 0
    if args.fixture is None:
        remote_fact_statements = record_d1_fact_model(outputs)
    mode = outputs["analytics-source-health.json"]["mode"]
    result = {
        "schema": "glp-analytics-brain-run-v1",
        "status": mode,
        "output_count": len(outputs),
        "outputs": [str(args.output_dir / name) for name in outputs],
        "source_count": len(outputs["analytics-source-health.json"]["sources"]),
        "route_count": outputs["route-instrumentation-matrix.json"]["sitemap_route_count"],
        "reconciled_clicks": outputs["affiliate-reconciliation.json"]["counts"]["browser"],
        "fact_model_sinks": {
            "local": "written",
            "d1": "written" if args.fixture is None else "fixture_not_eligible",
            "d1_statement_count": remote_fact_statements,
        },
        "candidate_tree_reads": 0,
        "candidate_tree_writes": 0,
    }
    if args.phase == "deploy" and args.fixture is None:
        result["family_release_readback"] = family_deploy_readback()
    print(json.dumps(result, sort_keys=True))
    if mode.startswith("FAIL_"):
        return 2
    if mode == "HOLD_REVENUE_OPTIMIZATION":
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
