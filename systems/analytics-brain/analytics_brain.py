#!/usr/bin/env python3
"""GLP Clinic Scout analytics fact model, guard, reconciliation, and decisions.

The module is deliberately provider-free. ``collector.py`` converts live or
fixture inputs into this canonical shape; consumers import the policy functions
here. Missing revenue is always ``None``. A numeric zero is accepted only when a
source explicitly says the metric was measured.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

PRODUCTION_HOST = "glpclinicscout.com"
GA4_PROPERTY_ID = "541440529"
GTM_CONTAINER_ID = "GTM-PWR49VVK"
POSTHOG_PROJECT_ID = "508172"
D1_DATABASE_NAME = "glp-clinic-scout-prod"
D1_DATABASE_ID = "bcb91d2d-c3f1-4c55-93e3-070587e86d60"
GSC_PROPERTY = "sc-domain:glpclinicscout.com"
BING_SITE = "https://glpclinicscout.com"

MODES = (
    "PASS_SEARCH_ONLY",
    "PASS_REVENUE_INFORMED",
    "HOLD_REVENUE_OPTIMIZATION",
    "FAIL_SOURCE_DARK",
    "FAIL_ROUTE_COVERAGE",
    "FAIL_HOST_POLLUTION",
    "FAIL_SCHEMA_DRIFT",
    "FAIL_SINK_DIVERGENCE",
    "FAIL_RECONCILIATION_LAG",
)
FAIL_MODES = frozenset(mode for mode in MODES if mode.startswith("FAIL_"))
SEARCH_SOURCES = frozenset({"gsc", "bing", "ga4", "posthog", "d1_clicks", "dataforseo"})
REVENUE_SOURCES = frozenset({"network_revenue"})
SOURCE_STATES = frozenset({"healthy", "measured_zero", "unavailable", "stale", "dark", "error"})
MEASURED_STATES = frozenset({"measured", "measured_zero"})
UNAVAILABLE_STATES = frozenset({"unavailable", "not_eligible", "not_due", "error", "stale"})
OUTPUT_NAMES = (
    "analytics-source-health.json",
    "route-instrumentation-matrix.json",
    "affiliate-reconciliation.json",
    "page-decision-facts.json",
    "decision-receipt.json",
)
SINK_DIVERGENCE_MAX_RATIO = 0.05
SINK_PROCESSING_LAG_HOURS = 1.0


class AnalyticsContractError(RuntimeError):
    """Raised when an input could create a plausible-looking false fact."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def parse_utc(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def canonical_route(value: Any) -> str:
    route = str(value or "").strip()
    if not route:
        raise AnalyticsContractError("route_missing")
    if "://" in route:
        from urllib.parse import urlsplit
        parsed = urlsplit(route)
        if parsed.hostname != PRODUCTION_HOST:
            raise AnalyticsContractError(f"route_foreign_host:{parsed.hostname}")
        route = parsed.path
    route = "/" + route.strip("/")
    return "/" if route == "/" else route + "/"


def measured_or_null(value: Any, state: str, metric: str) -> int | float | None:
    """Enforce the core invariant: unavailable is null, never numeric zero."""
    state = str(state or "")
    if state in UNAVAILABLE_STATES:
        if value is not None:
            raise AnalyticsContractError(f"unavailable_metric_must_be_null:{metric}:{state}:{value!r}")
        return None
    if state not in MEASURED_STATES:
        raise AnalyticsContractError(f"metric_state_invalid:{metric}:{state}")
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AnalyticsContractError(f"measured_metric_must_be_numeric:{metric}:{value!r}")
    if not math.isfinite(float(value)):
        raise AnalyticsContractError(f"metric_not_finite:{metric}")
    if state == "measured_zero" and float(value) != 0.0:
        raise AnalyticsContractError(f"measured_zero_nonzero_value:{metric}:{value!r}")
    return value


def metric(value: Any, state: str, source: str, *, reason: str | None = None) -> dict[str, Any]:
    result = {
        "value": measured_or_null(value, state, f"{source}.value"),
        "measurement_state": state,
        "source": source,
    }
    if reason:
        result["unavailable_reason"] = reason
    return result


def _validate_source(name: str, row: dict[str, Any]) -> dict[str, Any]:
    state = str(row.get("state") or "unavailable")
    if state not in SOURCE_STATES:
        raise AnalyticsContractError(f"source_state_invalid:{name}:{state}")
    actual = row.get("actual_rows")
    expected = row.get("expected_rows")
    if actual is not None and (isinstance(actual, bool) or not isinstance(actual, int) or actual < 0):
        raise AnalyticsContractError(f"source_actual_rows_invalid:{name}")
    if expected is not None and (isinstance(expected, bool) or not isinstance(expected, int) or expected < 0):
        raise AnalyticsContractError(f"source_expected_rows_invalid:{name}")
    if state in {"unavailable", "stale", "dark", "error"} and row.get("metric_value") is not None:
        raise AnalyticsContractError(f"source_unavailable_metric_not_null:{name}")
    host_scope = row.get("host_scope")
    if name in {"ga4", "posthog"}:
        if host_scope != PRODUCTION_HOST:
            raise AnalyticsContractError(f"exact_host_scope_missing:{name}:{host_scope}")
        host_filter = row.get("host_filter") or {}
        if host_filter.get("exact") != PRODUCTION_HOST:
            raise AnalyticsContractError(f"exact_host_filter_missing:{name}")
    result = {
        "source": name,
        "state": state,
        "watermark": row.get("watermark"),
        "actual_rows": actual,
        "expected_rows": expected,
        "host_scope": host_scope,
        "host_filter": row.get("host_filter"),
        "expected_schema": row.get("expected_schema"),
        "observed_schema": row.get("observed_schema"),
        "last_successful_readback": row.get("last_successful_readback"),
        "reason": row.get("reason"),
    }
    for field in (
        "complete", "pages_fetched", "provider_host_filter", "host_distribution", "event_watermark",
        "non_allowlisted_event_count", "collection_window", "property", "project", "site",
        "page_rows", "query_rows", "measurement_state", "measurement_scope",
        "host_identity_available", "neutral_route_paths", "neutral_route_window_clean_after_utc",
        "route_query_window",
        "measurement_authority", "freshness_state", "reporting_latency_hours",
        "collector_state", "collector_age_hours", "collector_max_age_hours",
        "reporting_latency_days", "reporting_documentation", "reporting_age_days",
        "reporting_expectation_exceeded", "freshness_reason", "watermark_kind",
        "reporting_watermarks", "last_attempt_at", "prior_last_successful_readback",
    ):
        if field in row:
            result[field] = row[field]
    return result


def source_health(data: dict[str, Any]) -> dict[str, Any]:
    raw = data.get("sources") or {}
    names = sorted(SEARCH_SOURCES | REVENUE_SOURCES | set(raw))
    sources = []
    for name in names:
        row = dict(raw.get(name) or {})
        if not row:
            row = {
                "state": "unavailable", "actual_rows": None, "expected_rows": None,
                "watermark": None, "host_scope": PRODUCTION_HOST if name in {"ga4", "posthog"} else None,
                "host_filter": ({"dimension": "hostName", "exact": PRODUCTION_HOST}
                                if name in {"ga4", "posthog"} else None),
                "metric_value": None, "reason": "source_not_collected",
            }
        sources.append(_validate_source(name, row))
    payload = {
        "schema": "analytics-source-health-v1",
        "generated_at_utc": data.get("generated_at_utc") or utc_now(),
        "site": PRODUCTION_HOST,
        "resource_ids": {
            "ga4_property": GA4_PROPERTY_ID,
            "gtm_container": GTM_CONTAINER_ID,
            "posthog_project": POSTHOG_PROJECT_ID,
            "d1_database": D1_DATABASE_NAME,
            "d1_database_id": D1_DATABASE_ID,
            "gsc_property": GSC_PROPERTY,
            "bing_site": BING_SITE,
            "deleted_debug_stream_restored": False,
        },
        "exact_host_scope": PRODUCTION_HOST,
        "sources": sources,
    }
    payload["source_digest"] = digest(payload)
    return payload


def path_in_sitemap_contract(row: dict[str, Any]) -> bool:
    """Instrumentation contract scope (2026-08-27): sitemap routes always;
    off-sitemap crawl discoveries only when they claim affiliate surface."""
    return bool(row.get("in_sitemap") or row.get("affiliate_route_expected")
                or row.get("affiliate_route_forbidden") or row.get("affiliate_route_unknown"))


#: 2026-08-28, FIFTH iteration of the representative probe. Execution 2216
#: failed /providers/ because ONE PostHog pageview stayed absent from GA4 for
#: 13 hours. The fourth iteration compared any GA4 event to PostHog pageviews,
#: collapsed both to booleans, and used a 12h horizon even though Google says
#: complete GA4 processing can take 24-48h. That made one visitor conclusive.
#:
#: Compare pageviews to pageviews, require the same 20-event flow floor used by
#: launch source-health policy before an asymmetry episode starts, and allow
#: GA4's documented complete-processing window. Static instrumentation remains
#: independently fail-closed. Dynamic falsifiability also remains: a 20+
#: pageview mismatch that persists past the sink horizon fails hard.
ROUTE_PROBE_MINIMUM_PAGEVIEWS = 20
GA4_MISSING_HORIZON_HOURS = 48.0
POSTHOG_MISSING_HORIZON_HOURS = 2.0  #: PostHog is near-real-time; 2h absence is real


def _pageview_counts(rows: Iterable[dict[str, Any]], *, source: str) -> dict[str, int]:
    """Return exact-host pageviews by canonical route for one traffic sink."""
    counts: Counter[str] = Counter()
    for row in rows:
        if not isinstance(row, dict) or row.get("host") != PRODUCTION_HOST or not row.get("path"):
            continue
        if source == "ga4":
            if row.get("event_name") != "page_view":
                continue
            value = int(row.get("event_count") or 0)
        elif source == "posthog":
            value = int(row.get("views") or 0)
        else:
            raise AnalyticsContractError(f"traffic_source_invalid:{source}")
        if value > 0:
            counts[canonical_route(row.get("path"))] += value
    return dict(counts)


def _prior_probe_state(prior_matrix: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not isinstance(prior_matrix, dict):
        return {}
    state: dict[str, dict[str, Any]] = {}
    for row in prior_matrix.get("routes") or []:
        if isinstance(row, dict) and row.get("path"):
            state[row["path"]] = row
    return state


def _neutral_posthog_route_available(source: dict[str, Any], path: str) -> bool:
    """Published field scope, not a claim that any event was ingested."""
    return bool(source.get("measurement_state") == "unmeasured_by_design"
                and source.get("measurement_scope") == "click_and_non_neutral_route_identity"
                and source.get("state") in {"healthy", "measured_zero"}
                and source.get("complete") is True
                and source.get("host_identity_available") is True
                and source.get("host_filter", {}).get("exact") == PRODUCTION_HOST
                and path in (source.get("neutral_route_paths") or []))


GA4_NEUTRAL_ROUTE_PATHS = frozenset(("/", "/about/", "/privacy/", "/privacy/cookies/", "/terms/"))


def _neutral_posthog_probe_window_clean(source: dict[str, Any]) -> bool:
    """Only a source-captured query window can establish post-promotion truth."""
    cutoff = source.get("neutral_route_window_clean_after_utc")
    window = source.get("route_query_window") or {}
    if not cutoff or window.get("basis") != "capture_before_query_minus_3_days":
        return False
    try:
        start = parse_utc(window["start_utc"])
        end = parse_utc(window["end_utc"])
        readback = parse_utc(source["last_successful_readback"])
        return parse_utc(cutoff) <= start <= end <= readback
    except (KeyError, TypeError, ValueError):
        return False


def route_matrix(data: dict[str, Any],
                 prior_matrix: dict[str, Any] | None = None) -> dict[str, Any]:
    prior_state = _prior_probe_state(prior_matrix)
    prior_generated = (prior_matrix or {}).get("generated_at_utc")
    # The SAME timestamp the payload stamps below — episode ages are measured
    # against the run's own clock so fixture runs stay deterministic.
    now_utc = data.get("generated_at_utc") or utc_now()
    sitemap = sorted({canonical_route(route) for route in data.get("sitemap_routes") or []})
    static_by_route = {
        canonical_route(row.get("path")): row for row in data.get("static_routes") or []
        if isinstance(row, dict) and row.get("path")
    }
    ga4_paths = {
        canonical_route(row.get("path")) for row in data.get("ga4_events") or []
        if row.get("path") and row.get("host") == PRODUCTION_HOST
    }
    posthog_paths = {
        canonical_route(row.get("path")) for row in data.get("posthog_events") or []
        if row.get("path") and row.get("host") == PRODUCTION_HOST
    }
    # Use the same unit on both sides. GA4 rows include every event name while
    # PostHog's aggregate contains pageviews; boolean path presence previously
    # let a custom GA4 event masquerade as a pageview and discarded sample size.
    ga4_pageviews = _pageview_counts(data.get("ga4_rows") or [], source="ga4")
    posthog_pageviews = _pageview_counts(data.get("posthog_rows") or [], source="posthog")
    observed_200 = sorted(path for path, row in static_by_route.items() if row.get("http_status") == 200)
    # 2026-08-27: the crawl deliberately discovers MORE than the sitemap —
    # noindex utility pages (/privacy/, /terms/, network-verification
    # endpoints) are 200 but excluded from the sitemap by design. Coverage
    # means every sitemap route is live, not that the crawl found nothing else.
    obligations = data.get("affiliate_route_obligations")
    missing_obligated_routes = sorted(set(obligations or {}) - (set(sitemap) | set(static_by_route)))
    route_inventory_equal = set(sitemap) <= set(observed_200) and not missing_obligated_routes
    routes: list[dict[str, Any]] = []
    for path in sorted(set(sitemap) | set(static_by_route)):
        static = static_by_route.get(path) or {}
        if obligations is None:
            obligation = "required" if static.get("affiliate_route_expected") is True else "none"
        else:
            obligation = obligations.get(path, "none")
            if (obligation not in {"required", "forbidden", "unknown", "none"}
                    or (static.get("affiliate_route_expected") is True and obligation == "none")):
                raise AnalyticsContractError(f"affiliate_route_obligation_invalid:{path}")
        anchors = int(static.get("sponsored_anchor_count") or 0)
        slot_ids = sorted({str(item) for item in static.get("sponsor_slot_ids") or [] if str(item)})
        client_markers = int(static.get("client_marker_count") or 0)
        redirects = int(static.get("first_party_redirect_count") or 0)
        row = {
            "path": path,
            "in_sitemap": path in sitemap,
            "http_status": static.get("http_status"),
            "ga4_static_present": static.get("ga4_static_present") is True,
            "ga4_direct_loader_present": static.get("ga4_direct_loader_present") is True,
            "gtm_static_present": static.get("gtm_static_present") is True,
            "posthog_static_present": static.get("posthog_static_present") is True,
            "clarity_static_present": static.get("clarity_static_present") is True,
            "csp_present": static.get("csp_present") is True,
            "csp_gtm_allowed": static.get("csp_gtm_allowed") is True,
            "csp_posthog_allowed": static.get("csp_posthog_allowed") is True,
            "consent_default_denied_present": static.get("consent_default_denied_present") is True,
            "sponsored_anchor_count": anchors,
            "client_marker_count": client_markers,
            "first_party_redirect_count": redirects,
            "sponsor_slot_ids": slot_ids,
            "client_marker_coverage": client_markers == anchors,
            "first_party_redirect_coverage": redirects == anchors,
            "affiliate_route_expected": obligation == "required",
            "affiliate_route_forbidden": obligation == "forbidden",
            "affiliate_route_unknown": obligation == "unknown",
            "affiliate_route_obligation": obligation,
            "ga4_event_seen": path in ga4_paths,
            "posthog_event_seen": path in posthog_paths,
            "representative_probe_required": static.get("representative_probe_required") is True,
        }
        # 2026-08-27: crawl-discovered routes outside the sitemap are noindex
        # utility pages (some intentionally script-dark, e.g. the OAuth app
        # path ships CSP script-src 'none'); they are not held to the shell
        # instrumentation contract unless they carry affiliate expectations.
        row["static_contract_exempt"] = static.get("static_contract_exempt") is True
        # A DECLARED exemption (control.static_exempt_routes — the byte-pinned
        # legacy article) beats the contract; it is recorded on the row, never
        # silently passed.
        if not path_in_sitemap_contract(row) or (row["static_contract_exempt"] and obligation not in {"forbidden", "unknown"}):
            row["static_instrumentation_pass"] = True
            row["probe_pass"] = True
            routes.append(row)
            continue
        row["static_instrumentation_pass"] = bool(
            row["http_status"] == 200
            and row["in_sitemap"]
            and row["ga4_static_present"]
            and row["gtm_static_present"]
            and not row["ga4_direct_loader_present"]
            and row["posthog_static_present"]
            and row["csp_present"]
            and row["csp_gtm_allowed"]
            and row["csp_posthog_allowed"]
            and row["consent_default_denied_present"]
            and row["client_marker_coverage"]
            and row["first_party_redirect_coverage"]
            and obligation != "unknown"
            and (obligation != "required" or anchors > 0)
            and (obligation != "forbidden" or anchors == 0)
        )
        # 2026-08-27 day-zero semantics (#106 designed-idle doctrine): a probe
        # can only FAIL when its source is demonstrably flowing but this key
        # route is dark. Before the first real visitor both sinks hold zero
        # events for the whole host and every probe would false-alarm forever.
        # 2026-08-27, SECOND correction to my own day-zero fix. The first one
        # excused probes only while a sink held ZERO events for the whole host.
        # Real traffic arrived the same afternoon, the excuse lapsed, and the
        # gate then demanded a click-class event on EVERY representative probe
        # route — including /how-we-make-money/ and /insights/, pages that may
        # legitimately never produce one. A probe proves that events FLOW from a
        # route, which is unknowable until that route is actually visited;
        # "nobody read this page today" is not an instrumentation defect. So a
        # probe route only FAILS once it has demonstrably carried traffic on a
        # sink and still shows no event there. Static instrumentation presence
        # is already enforced independently by static_instrumentation_pass, so
        # nothing goes unchecked — this narrows the probe to what it can prove.
        # THIRD pass on this, and the previous two were both wrong in the same
        # direction. ga4_event_seen/posthog_event_seen are built from the CLICK
        # event set (affiliate_click/sponsor_click), so requiring them on a probe
        # route demanded a monetized click on pages like /how-we-make-money/ that
        # may never produce one, and demanded a click from every reader who only
        # browsed. "Did a human click an affiliate link here today" is not a
        # measure of whether instrumentation works.
        #
        # A probe can only prove something non-circular by comparing the two
        # sinks against each other: we cannot know about traffic neither sink
        # recorded, but if ONE sink recorded traffic on a route and the OTHER
        # recorded none, that asymmetry is a real, actionable collection gap.
        # A route with no traffic in either sink is simply unproven, not broken.
        row["ga4_pageviews"] = int(ga4_pageviews.get(path, 0))
        row["posthog_pageviews"] = int(posthog_pageviews.get(path, 0))
        row["ga4_traffic_seen"] = row["ga4_pageviews"] > 0
        row["posthog_traffic_seen"] = row["posthog_pageviews"] > 0
        row["probe_minimum_pageviews"] = ROUTE_PROBE_MINIMUM_PAGEVIEWS
        # One asymmetric browser cannot prove broken route instrumentation:
        # consent, blocking, and sink processing legitimately differ. Start the
        # persistence clock only once the observed sink has enough pageviews to
        # make a route-level absence actionable.
        asym_now = row["ga4_traffic_seen"] != row["posthog_traffic_seen"]
        posthog_source = (data.get("sources") or {}).get("posthog") or {}
        neutral_eligible = _neutral_posthog_route_available(posthog_source, path)
        neutral_window_clean = (neutral_eligible and _neutral_posthog_probe_window_clean(posthog_source))
        identity_unmeasured = (posthog_source.get("measurement_state") == "unmeasured_by_design"
                               and not neutral_window_clean)
        ga4_comparable = path in GA4_NEUTRAL_ROUTE_PATHS
        row["ga4_comparison_state"] = "applicable" if ga4_comparable else "not_applicable"
        row["probe_measurement_state"] = (
            "eligible_window_mixed" if neutral_eligible and not neutral_window_clean else
            "unmeasured_by_design" if identity_unmeasured else
            "observed_neutral_route" if neutral_window_clean and row["posthog_traffic_seen"] else
            "eligible_unobserved" if neutral_window_clean else "measured"
        )
        row["probe_evidence_sufficient"] = bool(
            ga4_comparable and not identity_unmeasured and asym_now
            and max(row["ga4_pageviews"], row["posthog_pageviews"])
            >= ROUTE_PROBE_MINIMUM_PAGEVIEWS
        )
        row["probe_lagging_sink"] = (
            "ga4" if ga4_comparable and asym_now and row["posthog_traffic_seen"]
            else "posthog" if ga4_comparable and asym_now else None
        )
        row["probe_horizon_hours"] = (
            GA4_MISSING_HORIZON_HOURS if row["probe_lagging_sink"] == "ga4"
            else POSTHOG_MISSING_HORIZON_HOURS if row["probe_lagging_sink"] == "posthog"
            else None
        )
        row["probe_asymmetry_since"] = None
        row["probe_asymmetry_age_hours"] = None
        if row["probe_evidence_sufficient"]:
            prior = prior_state.get(path) or {}
            same_direction = (
                prior.get("ga4_traffic_seen") == row["ga4_traffic_seen"]
                and prior.get("posthog_traffic_seen") == row["posthog_traffic_seen"]
                and prior.get("probe_evidence_sufficient") is True
            )
            if same_direction and prior.get("probe_asymmetry_since"):
                row["probe_asymmetry_since"] = prior["probe_asymmetry_since"]
            elif same_direction and prior_generated:
                # Prior run saw the same asymmetry but predates the episode
                # field — its own timestamp is the best evidence of onset.
                row["probe_asymmetry_since"] = prior_generated
            else:
                row["probe_asymmetry_since"] = now_utc
            horizon = float(row["probe_horizon_hours"])
            try:
                age_hours = (parse_utc(now_utc) - parse_utc(row["probe_asymmetry_since"])
                             ).total_seconds() / 3600.0
            except Exception:  # noqa: BLE001 - an unparseable stamp must fail closed
                age_hours = horizon + 1.0
            row["probe_asymmetry_age_hours"] = round(age_hours, 6)
            persistent = age_hours >= horizon
        else:
            persistent = False
        row["probe_pass"] = bool(
            not row["representative_probe_required"]
            or not row["probe_evidence_sufficient"]
            or not persistent
        )
        routes.append(row)
    payload = {
        "schema": "route-instrumentation-matrix-v1",
        "generated_at_utc": now_utc,
        "site": PRODUCTION_HOST,
        "sitemap_route_count": len(sitemap),
        "expected_route_count": len(sitemap),
        "observed_200_route_count": len(observed_200),
        "instrumented_route_count": sum(row["static_instrumentation_pass"] for row in routes),
        "uninstrumented_route_count": sum(not row["static_instrumentation_pass"] for row in routes),
        "route_inventory_equal": route_inventory_equal,
        "missing_obligated_routes": missing_obligated_routes,
        "route_cap": None,
        "sitemap_complete": data.get("sitemap_complete") is True,
        "routes": routes,
        "summary": {
            "static_pass": sum(row["static_instrumentation_pass"] for row in routes),
            "static_fail": sum(not row["static_instrumentation_pass"] for row in routes),
            "probe_required": sum(row["representative_probe_required"] for row in routes),
            "probe_fail": sum(not row["probe_pass"] for row in routes),
            "sponsored_anchors": sum(row["sponsored_anchor_count"] for row in routes),
            "client_markers": sum(row["client_marker_count"] for row in routes),
            "first_party_redirects": sum(row["first_party_redirect_count"] for row in routes),
            "gtm_routes": sum(row["gtm_static_present"] for row in routes),
            "posthog_routes": sum(row["posthog_static_present"] for row in routes),
            "clarity_routes": sum(row["clarity_static_present"] for row in routes),
        },
    }
    payload["source_digest"] = digest(payload)
    return payload


def _identity(rows: Iterable[dict[str, Any]], field: str = "cs_click_id") -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        identity = str(row.get(field) or "")
        if identity:
            out[identity] = row
    return out


REVERSED_REVENUE_STATUSES = frozenset({"reversed", "rejected", "declined", "cancelled", "canceled", "void"})
APPROVED_REVENUE_STATUSES = frozenset({"approved", "payable", "paid"})
PAID_REVENUE_STATUSES = frozenset({"paid", "settled"})


def revenue_summary(events: list[dict[str, Any]], *, measured: bool) -> dict[str, Any]:
    """Summarize latest conversion state while preserving unknown money as null."""
    if not measured:
        return {
            "network_accepted_clicks": None,
            "accepted_conversions": None,
            "approved_commission_minor": None,
            "paid_commission_minor": None,
        }
    latest: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(events):
        identity = str(
            row.get("conversion_id") or row.get("revenue_event_id")
            or row.get("network_click_id") or row.get("cs_click_id") or f"event-{index}"
        )
        key = (str(row.get("occurred_at") or ""), str(row.get("imported_at") or ""), index)
        prior = latest.get(identity)
        prior_key = prior.get("_latest_key") if prior else None
        if prior_key is None or key > prior_key:
            latest[identity] = {**row, "_latest_key": key}
    active = [row for row in latest.values()
              if str(row.get("event_status") or "").strip().lower() not in REVERSED_REVENUE_STATUSES
              and row.get("event_type") != "reversal"]
    accepted_click_ids = {
        str(row.get("network_click_id") or row.get("cs_click_id") or identity)
        for identity, row in latest.items()
        if row in active and (row.get("network_click_id") or row.get("event_type") == "accepted_click")
    }
    conversion_rows = [row for row in active
                       if row.get("event_type") in {"accepted_conversion", "commission", "commission_status", "payment"}]
    approved_rows = [row for row in conversion_rows
                     if str(row.get("event_status") or "").strip().lower() in APPROVED_REVENUE_STATUSES]
    paid_rows = [row for row in conversion_rows
                 if str(row.get("event_status") or "").strip().lower() in PAID_REVENUE_STATUSES
                 or row.get("event_type") == "payment"]

    def money(rows: list[dict[str, Any]]) -> int | None:
        measured_values = [int(row["commission_minor"]) for row in rows
                           if row.get("commission_minor") is not None]
        if measured_values:
            return sum(measured_values)
        return None if rows else 0

    return {
        "network_accepted_clicks": len(accepted_click_ids),
        "accepted_conversions": len(conversion_rows),
        "approved_commission_minor": money(approved_rows),
        "paid_commission_minor": money(paid_rows),
    }


def affiliate_reconciliation(data: dict[str, Any]) -> dict[str, Any]:
    browser = _identity(data.get("browser_clicks") or [])
    ga4 = _identity([row for row in data.get("ga4_events") or [] if row.get("host") == PRODUCTION_HOST])
    posthog = _identity([row for row in data.get("posthog_events") or [] if row.get("host") == PRODUCTION_HOST])
    d1 = _identity(data.get("d1_clicks") or [])
    revenue_events = list(data.get("revenue_events") or [])
    revenue_by_click: dict[str, list[dict[str, Any]]] = defaultdict(list)
    network_id_by_click: dict[str, set[str]] = defaultdict(set)
    for row in revenue_events:
        click_id = str(row.get("cs_click_id") or "")
        if not click_id:
            continue
        revenue_by_click[click_id].append(row)
        if row.get("network_click_id"):
            network_id_by_click[click_id].add(str(row["network_click_id"]))
    revenue_state = str((data.get("sources") or {}).get("network_revenue", {}).get("state") or "unavailable")
    revenue_measured = revenue_state in {"healthy", "measured_zero"}
    keys = sorted(set(browser) | set(ga4) | set(posthog) | set(d1) | set(revenue_by_click))
    rows: list[dict[str, Any]] = []
    now = parse_utc(data.get("generated_at_utc") or utc_now())
    for click_id in keys:
        origin = browser.get(click_id) or d1.get(click_id) or ga4.get(click_id) or posthog.get(click_id) or {}
        occurred = origin.get("occurred_at") or origin.get("created_at")
        age_hours = None
        if occurred:
            age_hours = round(max(0.0, (now - parse_utc(str(occurred))).total_seconds() / 3600.0), 3)
        events = revenue_by_click.get(click_id) or []
        outcome = revenue_summary(events, measured=revenue_measured)
        rows.append({
            "cs_click_id": click_id,
            "page_path": canonical_route(origin.get("path") or origin.get("page_path") or origin.get("page_slug") or "/"),
            "slot_id": origin.get("slot_id") or origin.get("link_position"),
            "program_id": origin.get("program_id"),
            "browser_sent": click_id in browser,
            "ga4_received": click_id in ga4,
            "posthog_received": click_id in posthog,
            "d1_appended": click_id in d1,
            "network_click_ids": sorted(network_id_by_click.get(click_id) or []),
            "revenue_event_count": len(events) if revenue_measured else None,
            "accepted_conversion_count": outcome["accepted_conversions"],
            "approved_commission_minor": outcome["approved_commission_minor"],
            "paid_commission_minor": outcome["paid_commission_minor"],
            "age_hours": age_hours,
        })
    browser_count = len(browser)
    def delta(sink: dict[str, Any]) -> dict[str, Any]:
        matched = len(set(browser) & set(sink))
        return {"sent": browser_count, "received": len(sink), "matched": matched,
                "delta": browser_count - matched,
                "match_rate": round(matched / browser_count, 6) if browser_count else None}
    mature_browser = {
        row["cs_click_id"] for row in rows
        if row["browser_sent"] and row.get("age_hours") is not None
        and float(row["age_hours"]) >= SINK_PROCESSING_LAG_HOURS
    }
    mature_deltas = {}
    for name, sink in {"ga4": ga4, "posthog": posthog, "d1": d1}.items():
        if (name == "posthog" and ((data.get("sources") or {}).get("posthog") or {}).get("measurement_state") == "unmeasured_by_design"):
            mature_deltas[name] = {
                "eligible_sent": None, "missing": None, "missing_ratio": None,
                "maximum_ratio": SINK_DIVERGENCE_MAX_RATIO,
                "processing_lag_hours": SINK_PROCESSING_LAG_HOURS,
                "divergence_exceeded": False, "measurement_state": "unmeasured_by_design",
                "reason": ("click_identity_unmeasured_by_design"
                           if ((data.get("sources") or {}).get("posthog") or {}).get("measurement_scope")
                           == "click_and_non_neutral_route_identity"
                           else "privacy_export_allowlist_omits_host_page_identity"),
            }
            continue
        missing = len(mature_browser - set(sink))
        ratio = missing / len(mature_browser) if mature_browser else None
        mature_deltas[name] = {
            "eligible_sent": len(mature_browser),
            "missing": missing,
            "missing_ratio": round(ratio, 6) if ratio is not None else None,
            "maximum_ratio": SINK_DIVERGENCE_MAX_RATIO,
            "processing_lag_hours": SINK_PROCESSING_LAG_HOURS,
            "divergence_exceeded": ratio is not None and ratio > SINK_DIVERGENCE_MAX_RATIO,
        }
    network_matched = sum(bool(row["network_click_ids"]) for row in rows if row["browser_sent"])
    revenue_metrics = revenue_summary(revenue_events, measured=revenue_measured)
    browser_missing_d1 = sorted(set(browser) - set(d1))
    d1_missing_network = sorted(identity for identity, row in d1.items()
                                if not row.get("network_click_id"))
    unmatched_revenue = sorted(
        str(event.get("revenue_event_id") or event.get("conversion_id") or "unidentified")
        for event in revenue_events
        if not (
            (event.get("cs_click_id") and str(event["cs_click_id"]) in d1)
            or (event.get("network_click_id") and any(
                str(click.get("network_click_id") or "") == str(event["network_click_id"])
                for click in d1.values()
            ))
        )
    )
    late_outcomes = sorted(
        row["cs_click_id"] for row in rows
        if row.get("age_hours") is not None and (
            (row["d1_appended"] and not row["network_click_ids"] and float(row["age_hours"]) > 48)
            or (row["network_click_ids"] and row["revenue_event_count"] in {0, None}
                and float(row["age_hours"]) > 72)
        )
    )
    payload = {
        "schema": "affiliate-reconciliation-v1",
        "generated_at_utc": data.get("generated_at_utc") or utc_now(),
        "site": PRODUCTION_HOST,
        "exact_host_scope": PRODUCTION_HOST,
        "counts": {
            "browser": browser_count,
            "ga4": len(ga4),
            "posthog": len(posthog),
            "d1": len(d1),
            "network_id_matched": network_matched,
            "network_accepted_clicks": revenue_metrics["network_accepted_clicks"],
            "conversions": revenue_metrics["accepted_conversions"],
            "revenue_events": len(revenue_events) if revenue_measured else None,
            "approved_commission_minor": revenue_metrics["approved_commission_minor"],
            "paid_commission_minor": revenue_metrics["paid_commission_minor"],
            "unmatched_browser_to_d1": len(browser_missing_d1),
            "unmatched_d1_to_network": len(d1_missing_network),
            "unmatched_network_outcomes": len(unmatched_revenue) if revenue_measured else None,
            "late_outcomes": len(late_outcomes),
        },
        "sink_deltas": {"ga4": delta(ga4), "posthog": delta(posthog), "d1": delta(d1)},
        "mature_sink_deltas": mature_deltas,
        "duplicate_cs_click_ids": {
            name: sorted(identity for identity, count in Counter(
                str(row.get("cs_click_id") or "") for row in values if row.get("cs_click_id")
            ).items() if count > 1)
            for name, values in {
                "browser": data.get("browser_clicks") or [],
                "ga4": data.get("ga4_events") or [],
                "posthog": data.get("posthog_events") or [],
                "d1": data.get("d1_clicks") or [],
            }.items()
        },
        "network": {
            "measurement_state": "measured" if revenue_measured else "unavailable",
            "sample_clicks": browser_count,
            "network_id_matched": network_matched,
            "revenue_events": len(revenue_events) if revenue_measured else None,
            **revenue_metrics,
            "unavailable_reason": None if revenue_measured else
                                  ((data.get("sources") or {}).get("network_revenue", {}).get("reason") or "network_revenue_unavailable"),
        },
        "unmatched_queues": {
            "browser_missing_d1": browser_missing_d1,
            "d1_missing_network_click_id": d1_missing_network,
            "network_outcomes_missing_d1_join": unmatched_revenue if revenue_measured else None,
            "late_network_outcomes": late_outcomes,
        },
        "rows": rows,
    }
    payload["source_digest"] = digest(payload)
    return payload


def _hard_failure(health: dict[str, Any], matrix: dict[str, Any], reconciliation: dict[str, Any],
                  data: dict[str, Any]) -> tuple[str | None, list[str]]:
    reasons: list[str] = []
    for source in health["sources"]:
        expected = source.get("expected_schema")
        observed = source.get("observed_schema")
        if expected and observed and expected != observed:
            reasons.append(f"{source['source']}:expected={expected}:observed={observed}")
    if reasons:
        return "FAIL_SCHEMA_DRIFT", reasons

    duplicates = [f"{source}:{identity}" for source, identities in
                  reconciliation.get("duplicate_cs_click_ids", {}).items() for identity in identities]
    if duplicates:
        return "FAIL_SCHEMA_DRIFT", duplicates

    if (not matrix.get("sitemap_complete") or matrix["sitemap_route_count"] == 0
            or matrix.get("route_inventory_equal") is not True):
        return "FAIL_ROUTE_COVERAGE", matrix.get("missing_obligated_routes") or ["sitemap_incomplete_or_empty"]
    route_failures = [row["path"] for row in matrix["routes"]
                      if not row["static_instrumentation_pass"] or not row["probe_pass"]]
    if route_failures:
        return "FAIL_ROUTE_COVERAGE", route_failures

    foreign = sorted({str(row.get("host")) for key in ("ga4_events", "posthog_events")
                      for row in data.get(key) or []
                      if row.get("host") and row.get("host") != PRODUCTION_HOST})
    polluted_sources = [row["source"] for row in health["sources"]
                        if int(row.get("non_allowlisted_event_count") or 0) > 0]
    foreign.extend(f"{source}:non_allowlisted_events" for source in polluted_sources)
    if foreign:
        return "FAIL_HOST_POLLUTION", sorted(set(foreign))

    dark = []
    for source in health["sources"]:
        if source["source"] not in SEARCH_SOURCES:
            continue
        if source["state"] in {"unavailable", "dark", "error", "stale"}:
            dark.append(f"{source['source']}:{source['state']}")
        if source["state"] in {"healthy", "measured_zero"} and not source.get("watermark"):
            dark.append(f"{source['source']}:watermark_missing")
        if (source.get("expected_rows") or 0) > 0 and source.get("actual_rows") == 0:
            dark.append(f"{source['source']}:known_positive_zero_rows")
    if dark:
        return "FAIL_SOURCE_DARK", dark

    browser_count = reconciliation["counts"]["browser"]
    if browser_count:
        divergence = [name for name, row in reconciliation["mature_sink_deltas"].items()
                      if row["divergence_exceeded"]]
        if divergence:
            return "FAIL_SINK_DIVERGENCE", divergence

    # 2026-09-09: the network join can only be "late" when the network side is
    # actually measured. While the network import is unavailable (watermark not
    # complete, machine not installed) a D1 click with no network id is
    # UNEVALUABLE, not lagging -- reporting it as FAIL every hour blocked search
    # decisions for a reason unrelated to search data and drowned #ops-alerts.
    # Revenue decisions stay fail-closed regardless: evaluate_mode already holds
    # them whenever network_revenue is not measured. Unevaluable rows are
    # surfaced through reconciliation["network_join_unevaluable"] so nothing is
    # hidden; they become a reason on the resulting mode.
    network_state = str((reconciliation.get("network") or {}).get("measurement_state") or "unavailable")
    network_measured = network_state in MEASURED_STATES or network_state == "healthy"
    lag = []
    unevaluable = []
    for row in reconciliation["rows"]:
        age = row.get("age_hours")
        if age is None:
            continue
        if row["browser_sent"] and not row["d1_appended"] and age > 1:
            lag.append(f"{row['cs_click_id']}:d1>{age}h")
        if row["d1_appended"] and not row["network_click_ids"] and age > 48:
            if network_measured:
                lag.append(f"{row['cs_click_id']}:network>{age}h")
            else:
                unevaluable.append(f"{row['cs_click_id']}:network_unmeasured:{network_state}")
        if row["network_click_ids"] and row["revenue_event_count"] in {0, None} and age > 72:
            lag.append(f"{row['cs_click_id']}:dashboard>{age}h")
    reconciliation["network_join_unevaluable"] = unevaluable
    if lag:
        return "FAIL_RECONCILIATION_LAG", lag
    return None, []


def evaluate_mode(health: dict[str, Any], matrix: dict[str, Any], reconciliation: dict[str, Any],
                  data: dict[str, Any], *, decision_class: str = "search",
                  minimum_revenue_sample: int = 20) -> dict[str, Any]:
    if decision_class not in {"search", "profitability", "cro"}:
        raise AnalyticsContractError(f"decision_class_invalid:{decision_class}")
    failure, reasons = _hard_failure(health, matrix, reconciliation, data)
    if failure:
        return {"mode": failure, "allowed": False, "reasons": reasons,
                "revenue_blind": True, "minimum_revenue_sample": minimum_revenue_sample}
    by_source = {row["source"]: row for row in health["sources"]}
    revenue_source = by_source.get("network_revenue") or {"state": "unavailable"}
    browser = int(reconciliation["counts"]["browser"])
    matched = int(reconciliation["counts"]["network_id_matched"])
    unmatched_outcomes = reconciliation["counts"].get("unmatched_network_outcomes")
    commission_complete = (
        reconciliation["counts"].get("approved_commission_minor") is not None
        and reconciliation["counts"].get("paid_commission_minor") is not None
    )
    revenue_measured = revenue_source["state"] in {"healthy", "measured_zero"}
    observed_revenue_sample = browser if revenue_measured else None
    revenue_complete = (
        revenue_measured
        and observed_revenue_sample >= minimum_revenue_sample
        and matched == browser
        and unmatched_outcomes == 0
        and commission_complete
    )
    if revenue_complete:
        return {"mode": "PASS_REVENUE_INFORMED", "allowed": True, "reasons": [],
                "revenue_blind": False, "minimum_revenue_sample": minimum_revenue_sample,
                "observed_revenue_sample": browser}
    reasons = []
    if not revenue_measured:
        reasons.append(f"network_revenue:{revenue_source['state']}")
    elif observed_revenue_sample < minimum_revenue_sample:
        reasons.append(f"sample_below_threshold:{browser}<{minimum_revenue_sample}")
    unevaluable = reconciliation.get("network_join_unevaluable") or []
    if unevaluable:
        reasons.append(f"network_join_unevaluable:{len(unevaluable)}")
    if matched != browser:
        reasons.append(f"network_click_join_incomplete:{matched}/{browser}")
    if unmatched_outcomes not in {0, None}:
        reasons.append(f"network_outcomes_unmatched:{unmatched_outcomes}")
    if revenue_measured and not commission_complete:
        reasons.append("commission_measurement_incomplete")
    if decision_class in {"profitability", "cro"}:
        return {"mode": "HOLD_REVENUE_OPTIMIZATION", "allowed": False, "reasons": reasons,
                "revenue_blind": True, "minimum_revenue_sample": minimum_revenue_sample,
                "observed_revenue_sample": observed_revenue_sample}
    return {"mode": "PASS_SEARCH_ONLY", "allowed": True, "reasons": reasons,
            "revenue_blind": True, "minimum_revenue_sample": minimum_revenue_sample,
            "observed_revenue_sample": observed_revenue_sample}


def page_facts(data: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    routes = sorted({canonical_route(route) for route in data.get("sitemap_routes") or []})
    sources = data.get("sources") or {}
    by_path: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    mappings = {
        "gsc_rows": "gsc", "bing_rows": "bing", "ga4_rows": "ga4",
        "posthog_rows": "posthog", "d1_clicks": "d1_clicks", "revenue_events": "network_revenue",
    }
    for key, source in mappings.items():
        for row in data.get(key) or []:
            route_value = row.get("path") or row.get("page_path") or row.get("page_slug")
            if route_value:
                by_path[canonical_route(route_value)][source].append(row)

    revenue_measured = str(sources.get("network_revenue", {}).get("state")) in {"healthy", "measured_zero"}
    pages: list[dict[str, Any]] = []
    for route in routes:
        grouped = by_path.get(route) or {}
        gsc = grouped.get("gsc") or []
        bing = grouped.get("bing") or []
        ga4 = grouped.get("ga4") or []
        posthog = grouped.get("posthog") or []
        clicks = grouped.get("d1_clicks") or []
        revenue = grouped.get("network_revenue") or []
        impressions = sum(int(row.get("impressions") or 0) for row in gsc)
        gsc_clicks = sum(int(row.get("clicks") or 0) for row in gsc)
        sessions = sum(int(row.get("sessions") or row.get("views") or 0) for row in ga4)
        engaged = sum(int(row.get("engaged_sessions") or row.get("views") or 0) for row in posthog)
        outcomes = revenue_summary(revenue, measured=revenue_measured)
        approved_commission = outcomes["approved_commission_minor"]
        epc: float | None = None
        revenue_per_session: float | None = None
        if approved_commission is not None and clicks:
            epc = round(approved_commission / len(clicks), 4)
        if approved_commission is not None and sessions:
            revenue_per_session = round(approved_commission / sessions, 4)

        def revenue_metric(value: int | float | None, name: str, *, denominator: int | None = None):
            if not revenue_measured:
                return metric(None, "unavailable", "network_revenue",
                              reason="network_revenue_unavailable")
            if value is None:
                reason = "denominator_not_measured" if denominator == 0 else "commission_amount_not_eligible"
                return metric(None, "not_eligible", "network_revenue", reason=reason)
            return metric(value, "measured_zero" if float(value) == 0.0 else "measured",
                          "network_revenue")

        unavailable_signals = []
        if not revenue_measured:
            unavailable_signals.extend([
                "network_accepted_clicks", "accepted_conversions", "approved_commission_minor",
                "paid_commission_minor", "epc_minor", "revenue_per_session_minor",
            ])
        else:
            if epc is None:
                unavailable_signals.append("epc_minor")
            if revenue_per_session is None:
                unavailable_signals.append("revenue_per_session_minor")
        facts = {
            "path": route,
            "search": {
                "gsc_impressions": metric(impressions, "measured" if impressions else "measured_zero", "gsc"),
                "gsc_clicks": metric(gsc_clicks, "measured" if gsc_clicks else "measured_zero", "gsc"),
                "bing_impressions": metric(sum(int(row.get("impressions") or 0) for row in bing),
                                             "measured" if bing else "measured_zero", "bing"),
            },
            "engagement": {
                "ga4_sessions": metric(sessions, "measured" if sessions else "measured_zero", "ga4"),
                "posthog_engaged_sessions": (
                    metric(None, "unavailable", "posthog",
                           reason=("engaged_sessions_not_proven_by_neutral_pageview"
                                   if _neutral_posthog_route_available(sources.get("posthog") or {}, route)
                                   else "unmeasured_by_design"))
                    if sources.get("posthog", {}).get("measurement_state") == "unmeasured_by_design"
                    else metric(engaged, "measured" if engaged else "measured_zero", "posthog")),
            },
            "revenue": {
                "d1_clicks": metric(len(clicks), "measured" if clicks else "measured_zero", "d1_clicks"),
                "network_accepted_clicks": revenue_metric(outcomes["network_accepted_clicks"],
                                                          "network_accepted_clicks"),
                "accepted_conversions": revenue_metric(outcomes["accepted_conversions"],
                                                       "accepted_conversions"),
                "commission_minor": revenue_metric(approved_commission, "commission_minor"),
                "approved_commission_minor": revenue_metric(approved_commission,
                                                             "approved_commission_minor"),
                "paid_commission_minor": revenue_metric(outcomes["paid_commission_minor"],
                                                         "paid_commission_minor"),
                "epc_minor": revenue_metric(epc, "epc_minor", denominator=len(clicks)),
                "revenue_per_click_minor": revenue_metric(epc, "revenue_per_click_minor",
                                                          denominator=len(clicks)),
                "revenue_per_session_minor": revenue_metric(
                    revenue_per_session, "revenue_per_session_minor", denominator=sessions,
                ),
            },
            "data_quality_mode": policy["mode"],
            "revenue_blind": bool(policy["revenue_blind"]),
            "data_quality_flags": {
                "exact_host_scope": PRODUCTION_HOST,
                "revenue_source_state": str(sources.get("network_revenue", {}).get("state") or "unavailable"),
                "revenue_eligible": revenue_measured and not unavailable_signals,
                "unavailable_signals": unavailable_signals,
            },
        }
        pages.append(facts)
    payload = {
        "schema": "page-decision-facts-v1",
        "generated_at_utc": data.get("generated_at_utc") or utc_now(),
        "site": PRODUCTION_HOST,
        "data_quality_mode": policy["mode"],
        "revenue_blind": bool(policy["revenue_blind"]),
        "pages": pages,
    }
    assert_null_revenue(payload)
    payload["source_digest"] = digest(payload)
    return payload


def assert_null_revenue(payload: dict[str, Any]) -> None:
    """Reject the exact missing-as-zero defect class at every page boundary."""
    for page in payload.get("pages") or []:
        for name, fact in (page.get("revenue") or {}).items():
            state = fact.get("measurement_state")
            value = fact.get("value")
            measured_or_null(value, state, f"{page.get('path')}.revenue.{name}")


def make_decision(*, decision_type: str, subject: str, action: str,
                  pre_signal_score: float, components: list[dict[str, Any]],
                  policy: dict[str, Any], minimum_revenue_sample: int | None = None) -> dict[str, Any]:
    if decision_type in {"provider_profitability", "placement_profitability"}:
        required = minimum_revenue_sample
        if required is None or required <= 0:
            raise AnalyticsContractError("profitability_sample_threshold_required")
        if policy["mode"] != "PASS_REVENUE_INFORMED" or not policy["allowed"]:
            raise AnalyticsContractError(f"profitability_decision_not_allowed:{policy['mode']}")
        raw_observed = policy.get("observed_revenue_sample")
        if isinstance(raw_observed, bool) or not isinstance(raw_observed, (int, float)):
            raise AnalyticsContractError("profitability_observed_sample_missing_or_invalid")
        observed = int(raw_observed)
        if observed < required:
            raise AnalyticsContractError(
                f"profitability_sample_below_threshold:{observed}<{required}"
            )
    elif not policy["allowed"]:
        raise AnalyticsContractError(f"decision_not_allowed:{policy['mode']}")
    included: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for raw in components:
        row = dict(raw)
        name = str(row.get("signal") or "")
        if not name:
            raise AnalyticsContractError("decision_component_signal_missing")
        available = row.get("available") is True
        delta = row.get("delta")
        if not available:
            if delta is not None:
                raise AnalyticsContractError(f"unavailable_component_delta_must_be_null:{name}")
            row["included"] = False
            excluded.append(row)
            continue
        if isinstance(delta, bool) or not isinstance(delta, (int, float)):
            raise AnalyticsContractError(f"available_component_delta_invalid:{name}")
        row["included"] = True
        included.append(row)
    final = round(float(pre_signal_score) + sum(float(row["delta"]) for row in included), 6)
    unsigned = {
        "decision_type": decision_type,
        "subject": subject,
        "action": action,
        "pre_signal_score": float(pre_signal_score),
        "components": included + excluded,
        "final_score": final,
        "data_quality_mode": policy["mode"],
        "revenue_blind": bool(policy["revenue_blind"]),
        "signals_moved": [{"signal": row["signal"], "delta": row["delta"]}
                          for row in included if float(row["delta"]) != 0.0],
        "unavailable_signals_excluded": [{"signal": row["signal"],
                                           "reason": row.get("reason") or "unavailable"}
                                          for row in excluded],
        "minimum_revenue_sample": minimum_revenue_sample,
        "observed_revenue_sample": policy.get("observed_revenue_sample"),
    }
    unsigned["decision_id"] = "decision-" + digest(unsigned)[:24]
    return unsigned


def decision_receipt(data: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    decisions: list[dict[str, Any]] = []
    for request in data.get("decision_requests") or []:
        decisions.append(make_decision(
            decision_type=str(request["decision_type"]),
            subject=str(request["subject"]),
            action=str(request["action"]),
            pre_signal_score=float(request.get("pre_signal_score") or 0.0),
            components=list(request.get("components") or []),
            policy=policy,
            minimum_revenue_sample=request.get("minimum_revenue_sample"),
        ))
    payload = {
        "schema": "decision-receipt-v1",
        "generated_at_utc": data.get("generated_at_utc") or utc_now(),
        "status": ("PASS_DECISIONS_RECORDED" if decisions else
                   "PASS_NO_DECISIONS_REQUESTED" if policy.get("allowed") and
                   ((data.get("sources") or {}).get("posthog") or {}).get("measurement_state") == "unmeasured_by_design"
                   else "HOLD_NO_DECISIONS_REQUESTED"),
        "data_quality_mode": policy["mode"],
        "revenue_blind": bool(policy["revenue_blind"]),
        "decisions": decisions,
    }
    payload["receipt_sha256"] = digest(payload)
    return payload


def build_outputs(data: dict[str, Any], *, decision_class: str = "search",
                  minimum_revenue_sample: int = 20,
                  prior_matrix: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    health = source_health(data)
    matrix = route_matrix(data, prior_matrix=prior_matrix)
    reconciliation = affiliate_reconciliation(data)
    policy = evaluate_mode(health, matrix, reconciliation, data,
                           decision_class=decision_class,
                           minimum_revenue_sample=minimum_revenue_sample)
    health["mode"] = policy["mode"]
    health["allowed"] = policy["allowed"]
    health["mode_reasons"] = policy["reasons"]
    health["minimum_revenue_sample"] = policy.get("minimum_revenue_sample")
    health["observed_revenue_sample"] = policy.get("observed_revenue_sample")
    outputs = {
        "analytics-source-health.json": health,
        "route-instrumentation-matrix.json": matrix,
        "affiliate-reconciliation.json": reconciliation,
        "page-decision-facts.json": page_facts(data, policy),
        "decision-receipt.json": decision_receipt(data, policy),
    }
    if tuple(outputs) != OUTPUT_NAMES:
        raise AnalyticsContractError("required_output_set_drift")
    return outputs


def write_outputs(output_dir: Path, outputs: dict[str, dict[str, Any]]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for name in OUTPUT_NAMES:
        payload = outputs[name]
        temporary = output_dir / f".{name}.tmp"
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(output_dir / name)


def apply_fact_model(connection: sqlite3.Connection, migration: Path) -> None:
    connection.executescript(migration.read_text(encoding="utf-8"))


def source_fact_rows(outputs: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Materialize source-specific counts without turning absence into zero."""
    health = outputs["analytics-source-health.json"]
    generated = str(health["generated_at_utc"])
    metric_date = generated[:10]
    rows: list[dict[str, Any]] = []
    for source in health["sources"]:
        for metric_name, value in (
            ("actual_row_count", source.get("actual_rows")),
            ("expected_row_count", source.get("expected_rows")),
        ):
            if metric_name == "expected_row_count" and value is None:
                continue
            if value is None:
                measurement_state = {
                    "error": "error", "stale": "stale",
                }.get(str(source.get("state")), "unavailable")
            elif int(value) == 0:
                measurement_state = "measured_zero"
            else:
                measurement_state = "measured"
            unsigned = {
                "source": source["source"],
                "host_scope": source.get("host_scope") or "",
                "metric_date": metric_date,
                "page_path": "",
                "metric_name": metric_name,
                "metric_value": value,
                "measurement_state": measurement_state,
                "source_watermark": source.get("watermark"),
                "source_receipt_sha256": health["source_digest"],
                "recorded_at": generated,
            }
            rows.append({"fact_id": digest(unsigned), **unsigned})
    return rows


SOURCE_FACT_INSERT_SQL = """INSERT OR IGNORE INTO analytics_source_fact_v1
   (fact_id,source,host_scope,metric_date,page_path,metric_name,metric_value,
    measurement_state,source_watermark,source_receipt_sha256,recorded_at)
   VALUES (?,?,?,?,?,?,?,?,?,?,?)"""
SOURCE_FACT_COLUMNS = (
    "fact_id", "source", "host_scope", "metric_date", "page_path", "metric_name",
    "metric_value", "measurement_state", "source_watermark", "source_receipt_sha256",
    "recorded_at",
)


SOURCE_WATERMARK_UPSERT_SQL = """INSERT INTO analytics_source_watermark_v1
   (source,host_scope,watermark,state,actual_rows,expected_rows,last_successful_readback,
    expected_schema,observed_schema,receipt_sha256,updated_at)
   VALUES (?,?,?,?,?,?,?,?,?,?,?)
   ON CONFLICT(source,host_scope) DO UPDATE SET
     watermark=excluded.watermark,state=excluded.state,actual_rows=excluded.actual_rows,
     expected_rows=excluded.expected_rows,last_successful_readback=excluded.last_successful_readback,
     expected_schema=excluded.expected_schema,observed_schema=excluded.observed_schema,
     receipt_sha256=excluded.receipt_sha256,updated_at=excluded.updated_at"""


def fact_model_statements(outputs: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Return parameterized statements shared by the local and D1 fact sinks."""
    health = outputs["analytics-source-health.json"]
    generated = health["generated_at_utc"]
    statements = [{
        "sql": SOURCE_WATERMARK_UPSERT_SQL,
        "params": [
            row["source"], row.get("host_scope") or "", row.get("watermark"), row["state"],
            row.get("actual_rows"), row.get("expected_rows"), row.get("last_successful_readback"),
            row.get("expected_schema"), row.get("observed_schema"), health["source_digest"], generated,
        ],
    } for row in health["sources"]]
    statements.extend({
        "sql": SOURCE_FACT_INSERT_SQL,
        "params": [row[column] for column in SOURCE_FACT_COLUMNS],
    } for row in source_fact_rows(outputs))
    for name, payload in outputs.items():
        fact_id = digest({"name": name, "digest": payload.get("source_digest") or payload.get("receipt_sha256")})
        statements.append({
            "sql": """INSERT OR IGNORE INTO analytics_output_ledger_v1
               (output_id,output_name,schema_name,status_or_mode,generated_at,payload_sha256)
               VALUES (?,?,?,?,?,?)""",
            "params": [fact_id, name, payload["schema"], payload.get("status") or payload.get("mode"),
                       payload.get("generated_at_utc"), digest(payload)],
        })
    return statements


def record_source_facts(connection: sqlite3.Connection, outputs: dict[str, dict[str, Any]]) -> None:
    for statement in fact_model_statements(outputs):
        connection.execute(statement["sql"], statement["params"])
    connection.commit()
