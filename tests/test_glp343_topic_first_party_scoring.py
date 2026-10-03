"""GLP-343: topic/refresh scoring from the first-party receipts already collected.

Offline fixtures only: a small analytics source cache (GSC query/page rows on the
exact sc-domain property, Bing Webmaster query/page stats, GA4 page sessions),
one weekly GSC sitewide aggregate (exact property, finalized day window), and the
maintained strategy/topic queues. No rail, provider or network boundary exists.
Fixture clocks are relative to now so the real selector loader sees a fresh sidecar.
"""
from __future__ import annotations

import ast
import datetime as dt
import hashlib
import json
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "systems/analytics-brain"))  # decision_policy for the learning emitter's imports
sys.path.insert(0, str(ROOT / "systems/seo-blog-machine/scripts"))

# Load the worktree's real policy before the older helper prepends its live
# host path. Offline checks must not read sealed/recovering installed sources.
import decision_policy  # noqa: E402, F401
import glp_topic_data_score as scorer  # noqa: E402
import learning_loop_actions as actions  # noqa: E402

NOW = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
READBACK = (NOW - dt.timedelta(hours=4)).isoformat().replace("+00:00", "Z")
REPORT_DATE = NOW.date() - dt.timedelta(days=4)
WINDOW_START, WINDOW_END = REPORT_DATE - dt.timedelta(days=9), REPORT_DATE - dt.timedelta(days=3)
CACHE_WINDOW = {"start": (NOW.date() - dt.timedelta(days=30)).isoformat(),
                "end": (NOW.date() - dt.timedelta(days=3)).isoformat()}
SELECTOR = ROOT / "systems/seo-blog-machine/scripts/select_opus_content_queue.py"
COST_ROUTE = "/guides/glp-1-cost-by-drug-provider/"
INSURANCE_ROUTE = "/guides/glp-1-insurance-support-vs-coverage/"


def put(path: Path, value: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def source(state: str, rows: int | None, schema: str, **extra: Any) -> dict[str, Any]:
    healthy = state in ("healthy", "measured_zero")
    return {"state": state, "actual_rows": rows, "expected_schema": schema,
            "observed_schema": schema if healthy else None,
            "last_successful_readback": READBACK if healthy else None,
            "complete": True, "metric_value": None, "reason": None, **extra}


def source_cache(**overrides: Any) -> dict[str, Any]:
    sources = {
        "gsc": source("healthy", 5, "gsc-searchanalytics-v3", property="sc-domain:glpclinicscout.com",
                      watermark=CACHE_WINDOW["end"], watermark_kind="provider_report_date",
                      collection_window=dict(CACHE_WINDOW)),
        "bing": source("healthy", 3, "bing-webmaster-v1", site="https://glpclinicscout.com",
                       watermark=CACHE_WINDOW["end"], watermark_kind="provider_report_date"),
        "ga4": source("healthy", 3, "ga4-data-v1beta", host_scope="glpclinicscout.com", watermark="202609301200"),
    }
    data = {
        "sources": sources,
        "gsc_rows": [
            {"path": COST_ROUTE, "query": "GLP-1 cost", "clicks": 12, "impressions": 900, "ctr": 0.013, "position": 14.0},
            {"path": COST_ROUTE, "query": "glp1 cost by drug", "clicks": 1, "impressions": 100, "ctr": 0.01, "position": 22.0},
            {"path": INSURANCE_ROUTE, "query": "glp-1 insurance coverage", "clicks": 2, "impressions": 40, "ctr": 0.05, "position": 9.0},
            {"path": INSURANCE_ROUTE, "query": None, "clicks": 0, "impressions": 15, "ctr": 0.0, "position": 30.0},
            {"path": "/best/unrelated/", "query": "unrelated term", "clicks": 0, "impressions": 3, "ctr": 0.0, "position": 55.0},
        ],
        "bing_query_rows": [
            {"query": "glp-1 cost", "impressions": 50, "clicks": 1, "avg_position": 12.0, "avg_click_position": 11.0},
            {"query": "tirzepatide cost", "impressions": None, "clicks": None, "avg_position": None, "avg_click_position": None},
            {"query": "cheapest glp-1", "impressions": 20, "clicks": 0, "avg_position": 18.0, "avg_click_position": None},
        ],
        "bing_rows": [
            {"path": COST_ROUTE, "impressions": 60, "clicks": 1, "position": 13.0},
            {"path": "/reviews/bing-only-route/", "impressions": 7, "clicks": 0, "position": 20.0},
        ],
        "ga4_rows": [
            {"host": "glpclinicscout.com", "path": "/about/", "event_name": "page_view",
             "provider_time": "202609301200", "event_count": 30, "sessions": 20, "engaged_sessions": 15},
            {"host": "glpclinicscout.com", "path": "/about/", "event_name": "scroll",
             "provider_time": "202609301200", "event_count": 99, "sessions": 99, "engaged_sessions": 99},
            {"host": "partners.glpclinicscout.com", "path": "/guides/ga4-only-route/", "event_name": "page_view",
             "provider_time": "202609301200", "event_count": 9, "sessions": 9, "engaged_sessions": 9},
            {"host": "glpclinicscout.com", "path": "/privacy/", "event_name": "page_view",
             "provider_time": "202609301200", "event_count": 4, "sessions": 4, "engaged_sessions": 1},
        ],
    }
    data.update(overrides)
    return {"schema": "glp-analytics-source-cache-v1", "updated_at_utc": READBACK, "last_phase": "hourly", "data": data}


def sitewide_aggregate(*, consumer_missing_day: bool = False) -> dict[str, Any]:
    days = [(WINDOW_START + dt.timedelta(days=n)).isoformat() for n in range(7)]
    scopes = {}
    for label, prop, total in (("consumer_https_apex", "https://glpclinicscout.com/", 1400),
                               ("domain_including_partners", "sc-domain:glpclinicscout.com", 2100)):
        missing = [days[-1]] if consumer_missing_day and label == "consumer_https_apex" else []
        metrics = {"clicks": 20, "impressions": total, "ctr": 20 / total, "position": 17.5}
        unknown = dict.fromkeys(metrics)
        scopes[label] = {
            "context": {"property": prop, "start_date": days[0], "end_date": days[-1], "window_days": 7,
                        "search_type": "web", "data_state": "final", "dimensions": [],
                        "aggregation_type": "byProperty", "timezone": "America/Los_Angeles",
                        "denominator": "fixture"},
            "request_started_at_utc": READBACK, "captured_at_utc": READBACK,
            "state": "UNKNOWN_DATE_COVERAGE" if missing else "SOURCE_REPORTED",
            "totals": dict(unknown) if missing else metrics,
            "date_availability": [{"date": day, "state": "UNKNOWN" if day in missing else "SOURCE_REPORTED",
                                   "metrics": dict(unknown) if day in missing else metrics} for day in days],
            "missing_dates": missing,
        }
    return {"schema": "glp.weekly-gsc-sitewide-aggregate.v1", "scopes": scopes, "note": "fixture"}


def strategy_queue(map_path: Path) -> dict[str, Any]:
    rows = [
        (1, "cost-pricing", "pillar", "GLP-1 cost", COST_ROUTE, "refresh"),
        (2, "insurance-eligibility", "pillar", "glp-1 insurance coverage", INSURANCE_ROUTE, "refresh"),
        (3, "cost-pricing", "spoke", "cheapest glp-1", "/guides/cheapest-glp-1/", "new"),
        (4, "oral", "pillar", "oral semaglutide pill", "/guides/oral-semaglutide-pill/", "new"),
        (5, "cost-pricing", "spoke", "glp 1 cost", "/guides/duplicate-key-row/", "new"),
    ]
    return {
        "schema": "glp-seo-cluster-rotation-queue-v1", "status": "READY_FOR_DAILY_CANDIDATE_WAVE",
        "map_path": str(map_path),
        "queue": [{"queue_position": position, "cluster_id": cluster, "depth_stage": stage,
                   "target_query": query, "target_route": route, "current_route_status": status,
                   "requires_pillar_predecessor": False, "measured_volume": None, "keyword_difficulty": None}
                  for position, cluster, stage, query, route, status in rows],
    }


def topic_queue() -> dict[str, Any]:
    def row(topic_id: str, query: str, route: str) -> dict[str, Any]:
        return {"topic_id": topic_id, "target_query": query, "evidence_state": "READY_FOR_RESEARCH",
                "rank_score": 0.5,
                "competitor_gap": {"operator_ready_for_research": True, "operator_directive_id": "op-1",
                                   "target_route": route, "class_rank": 2}}
    return {"schema": "seo-blog-machine-topic-queue-v1",
            "queue": [row("t-cheapest", "Cheapest GLP-1", "/guides/cheapest-glp-1/"),
                      row("t-oral", "oral semaglutide pill", "/guides/oral-semaglutide-pill/"),
                      row("t-new", "wegovy vs zepbound", "/compare/wegovy-vs-zepbound/")]}


@pytest.fixture
def lane(tmp_path: Path) -> dict[str, Any]:
    cache = put(tmp_path / "analytics/current/canonical-source-cache.json", source_cache())
    report_root = tmp_path / "weekly-ranking-report"
    put(report_root / f"{REPORT_DATE.isoformat()}-gsc-sitewide-aggregate.json", sitewide_aggregate())
    older = REPORT_DATE - dt.timedelta(days=7)
    put(report_root / f"{older.isoformat()}-gsc-sitewide-aggregate.json", {"schema": "stale-older-week"})
    map_path = put(tmp_path / "inputs/cluster-map.json", {
        "schema": "glp-seo-commercial-cluster-map-v1", "status": "PASS_MAP_READY_QUEUE_ROTATION",
        "clusters": [{"id": c} for c in ("cost-pricing", "insurance-eligibility", "oral")]})
    strategy = put(tmp_path / "inputs/strategy-queue.json", strategy_queue(map_path))
    topics = put(tmp_path / "topic-queues/latest.json", topic_queue())
    return {"root": tmp_path, "cache": cache, "report_root": report_root, "strategy": strategy,
            "topics": topics, "output": tmp_path / "topic-data-score"}


def run_scorer(lane: dict[str, Any], capsys, **overrides: Any) -> dict[str, Any]:
    import urllib.request
    original = urllib.request.urlopen
    urllib.request.urlopen = lambda *a, **k: pytest.fail("network forbidden")
    try:
        argv = ["--source-cache", str(overrides.get("cache", lane["cache"])),
                "--weekly-report-root", str(overrides.get("report_root", lane["report_root"])),
                "--strategy-queue", str(lane["strategy"]), "--topic-queue", str(lane["topics"]),
                "--output-dir", str(overrides.get("output", lane["output"])), "--now", NOW.isoformat()]
        assert scorer.main(argv) == 0
    finally:
        urllib.request.urlopen = original
    return json.loads(capsys.readouterr().out)


def test_topics_and_refreshes_scored_from_exact_property_finalized_window_receipts(lane, capsys):
    result = run_scorer(lane, capsys)
    assert result["status"] == "PASS_FIRST_PARTY_DEMAND_SCORE"
    assert result["effects"] == {"network_calls": 0, "provider_calls": 0, "sends": 0, "deploys": 0, "model_scraping": False}
    assert result["sources"] == {"gsc": "SOURCE_REPORTED", "bing": "SOURCE_REPORTED", "ga4": "SOURCE_REPORTED"}
    assert result["degraded"] == []
    # Five distinct keys: one strategy row and two topic rows collapsed onto existing keys.
    assert (result["candidates"], result["duplicates_removed"], result["multipliers_written"]) == (5, 3, 3)
    assert result["top_pick"] == "GLP-1 cost"
    assert result["finalized_window"] == {"start": WINDOW_START.isoformat(), "end": WINDOW_END.isoformat(),
                                          "days": 7, "data_state": "final", "timezone": "America/Los_Angeles"}

    sidecar = json.loads(Path(result["sidecar"]["path"]).read_bytes())
    assert hashlib.sha256(Path(result["sidecar"]["path"]).read_bytes()).hexdigest() == result["sidecar"]["sha256"]
    assert sidecar["schema"] == "glp-topic-demand-multiplier-v1" and sidecar["max_age_hours"] == 96
    assert sidecar["written_utc"] == NOW.isoformat()
    assert sidecar["sources"]["gsc"] == {"state": "SOURCE_REPORTED", "property": "sc-domain:glpclinicscout.com",
                                         "window": CACHE_WINDOW, "watermark": CACHE_WINDOW["end"]}
    assert sidecar["sources"]["bing"]["site"] == "https://glpclinicscout.com"
    assert sidecar["sources"]["ga4"]["host"] == "glpclinicscout.com"
    assert sidecar["finalized_window"]["report_date"] == REPORT_DATE.isoformat()
    assert sidecar["finalized_window"]["scopes"]["domain_including_partners"]["totals"]["impressions"] == 2100

    rows = {row["key"]: row for row in sidecar["multipliers"]}
    assert [row["rank"] for row in sidecar["multipliers"]] == [1, 2, 3, None, None]
    multipliers = [row["multiplier"] for row in sidecar["multipliers"] if row["multiplier"] is not None]
    assert multipliers == sorted(multipliers, reverse=True)
    assert all(0.1 <= value <= 5.0 for value in multipliers)
    # Exact GSC query (900 impressions at position 14 -> striking bonus) + hub-gap overlap + Bing.
    cost = rows["glp1 cost"]
    assert cost["state"] == "SOURCE_REPORTED" and cost["rank"] == 1 and cost["origin"] == "strategy_queue"
    assert cost["weighted_impressions"] == 1080 and cost["target_route"] == COST_ROUTE
    assert cost["multiplier"] == round(1 + __import__("math").log10(1081) + 0.5
                                       + sidecar["scoring_context"]["applied_bonus"], 4)
    # An absent query is not a measured zero, even in a complete API pull:
    # GSC/Bing do not guarantee every suppressed/unreported query is returned.
    assert (rows["oral semaglutide pill"]["multiplier"], rows["oral semaglutide pill"]["state"],
            rows["oral semaglutide pill"]["weighted_impressions"]) == (None, "UNKNOWN_METRICS_NULL", None)
    assert rows["wegovy vs zepbound"]["origin"] == "topic_queue"

    receipt = json.loads(Path(result["receipt"]["path"]).read_bytes())
    assert receipt["receipt_sha256"] == scorer.payload_hash(receipt) == result["receipt_sha256"]
    assert not stat.S_IMODE(Path(result["receipt"]["path"]).stat().st_mode) & 0o222
    assert receipt["inputs"]["gsc_sitewide_aggregate"]["path"].endswith(f"{REPORT_DATE.isoformat()}-gsc-sitewide-aggregate.json")
    assert [row["key"] for row in receipt["inputs"]["duplicates_removed"]] == [
        "glp1 cost", "cheapest glp1", "oral semaglutide pill"]
    cost_detail = next(row for row in receipt["topics"] if row["key"] == "glp1 cost")
    assert cost_detail["gsc"]["exact_impressions"] == 900 and cost_detail["gsc"]["exact_position"] == 14.0
    assert cost_detail["striking_bonus"] == 0.5
    assert cost_detail["gsc"]["related_queries"][0] == {"query": "glp1 cost by drug", "impressions": 100, "clicks": 1,
                                                        "position": 22.0, "overlap": 0.5, "weight": 100.0}
    assert cost_detail["bing"]["exact_impressions"] == 50
    # A Bing row whose metrics the provider left null is counted as unknown, never as zero.
    assert "tirzepatide cost" not in {row["query"] for row in cost_detail["bing"]["related_queries"]}

    refreshes = {row["route"]: row for row in receipt["refreshes"]}
    assert set(refreshes) == {COST_ROUTE, INSURANCE_ROUTE, "/best/unrelated/", "/reviews/bing-only-route/"}
    lead = refreshes[COST_ROUTE]
    assert lead["rank"] == 1 and lead["search_impressions"] == 1060
    assert lead["engagement_fraction"] == round(16 / 24, 4)
    assert lead["queue_keyword"] == "GLP-1 cost" and lead["queue_position"] == 1
    assert lead["gsc"] == {"state": "SOURCE_REPORTED", "impressions": 1000, "clicks": 13, "position": 14.8, "query_count": 2}
    assert lead["bing"] == {"state": "SOURCE_REPORTED", "impressions": 60, "clicks": 1, "position": 13.0}
    assert lead["ga4"] == {"state": "UNKNOWN_ROUTE_UNMEASURED_BY_DESIGN", "page_views": None,
                           "sessions": None, "engaged_sessions": None}
    neutral = receipt["scoring_context"]["ga4"]
    assert neutral["paths"] == ["/about/", "/privacy/"] and neutral["sessions"] == 24
    assert neutral["engagement_fraction"] == round(16 / 24, 4)
    assert [row["route"] for row in sidecar["refreshes"]] == [row["route"] for row in receipt["refreshes"]]

    # Re-running at the same instant is idempotent: identical immutable receipt, same sidecar bytes.
    again = run_scorer(lane, capsys)
    assert again["receipt"] == result["receipt"] and again["sidecar"] == result["sidecar"]


def test_unknown_metrics_stay_null_and_degraded_sources_are_named(lane, capsys):
    cache = source_cache()
    cache["data"]["sources"]["gsc"].update(state="error", observed_schema=None, last_successful_readback=None,
                                           reason="gsc_read_failed")
    cache["data"]["sources"]["bing"]["site"] = "https://other.example"
    cache["data"]["ga4_rows"] = []
    cache["data"]["sources"]["ga4"].update(state="measured_zero", actual_rows=0)
    degraded_cache = put(lane["root"] / "degraded-cache.json", cache)
    report_root = lane["root"] / "degraded-reports"
    put(report_root / f"{REPORT_DATE.isoformat()}-gsc-sitewide-aggregate.json",
        sitewide_aggregate(consumer_missing_day=True))
    result = run_scorer(lane, capsys, cache=degraded_cache, report_root=report_root, output=lane["root"] / "degraded-out")
    assert result["status"] == "PASS_FIRST_PARTY_DEMAND_SCORE"  # GA4 still reported
    assert result["sources"] == {"gsc": "UNKNOWN_SOURCE_ERROR", "bing": "UNKNOWN_SITE_MISMATCH", "ga4": "SOURCE_REPORTED"}
    assert result["degraded"] == ["gsc:UNKNOWN_SOURCE_ERROR", "bing:UNKNOWN_SITE_MISMATCH"]
    assert result["multipliers_written"] == 0 and result["top_pick"] is None
    sidecar = json.loads(Path(result["sidecar"]["path"]).read_bytes())
    assert len(sidecar["multipliers"]) == 5
    assert all(row["multiplier"] is None and row["rank"] is None and row["weighted_impressions"] is None
               and row["state"] == "UNKNOWN_NO_FIRST_PARTY_SOURCE" for row in sidecar["multipliers"])
    # The finalized window still binds the exact property; a scope with a missing day is UNKNOWN, not 0.
    scopes = sidecar["finalized_window"]["scopes"]
    assert sidecar["finalized_window"]["state"] == "SOURCE_REPORTED"
    assert scopes["consumer_https_apex"]["state"] == "UNKNOWN_DATE_COVERAGE"
    assert scopes["consumer_https_apex"]["totals"] == {"clicks": None, "impressions": None, "ctr": None, "position": None}
    assert scopes["consumer_https_apex"]["missing_dates"] == [WINDOW_END.isoformat()]
    assert scopes["domain_including_partners"]["totals"]["impressions"] == 2100
    # Refreshes cannot invent GA4 demand for uncollected health paths.
    receipt = json.loads(Path(result["receipt"]["path"]).read_bytes())
    assert [row["route"] for row in receipt["refreshes"]] == [COST_ROUTE, INSURANCE_ROUTE]
    for row in receipt["refreshes"]:
        assert row["gsc"] is None and row["bing"] is None and row["search_impressions"] is None
        assert row["ga4"]["state"] == "UNKNOWN_ROUTE_UNMEASURED_BY_DESIGN" and row["refresh_score"] is None
        assert row["state"] == "UNKNOWN_NO_FIRST_PARTY_SOURCE"

    # No source cache at all: nothing is scored and nothing is written as zero.
    absent = run_scorer(lane, capsys, cache=lane["root"] / "missing.json", report_root=lane["root"] / "no-reports",
                        output=lane["root"] / "absent-out")
    # Same instant, different inputs, same output directory: the dated receipt refuses to be rewritten.
    with pytest.raises(RuntimeError, match="topic_score_receipt_immutable_conflict"):
        run_scorer(lane, capsys, cache=lane["root"] / "missing.json", report_root=lane["root"] / "no-reports",
                   output=lane["root"] / "degraded-out")
    assert absent["status"] == "DEGRADED_NO_FIRST_PARTY_SOURCE"
    assert absent["sources"] == {name: "UNKNOWN_SOURCE_CACHE_ABSENT" for name in ("gsc", "bing", "ga4")}
    assert "gsc_sitewide_aggregate:UNKNOWN_ABSENT" in absent["degraded"]
    assert absent["multipliers_written"] == 0 and absent["refreshes_scored"] == 0 and absent["finalized_window"] is None
    absent_receipt = json.loads(Path(absent["receipt"]["path"]).read_bytes())
    assert [row["route"] for row in absent_receipt["refreshes"]] == [COST_ROUTE, INSURANCE_ROUTE]
    assert all(row["refresh_score"] is None and row["state"] == "UNKNOWN_NO_FIRST_PARTY_SOURCE"
               and row["gsc"] is None and row["bing"] is None and row["ga4"] is None
               for row in absent_receipt["refreshes"])
    assert all(row["multiplier"] is None for row in absent_receipt["topics"])


def test_stale_or_mismatched_receipts_are_unknown_not_data(lane):
    cache = source_cache()["data"]
    cache["sources"]["gsc"]["property"] = "https://glpclinicscout.com/"
    cache["sources"]["bing"]["last_successful_readback"] = (NOW - dt.timedelta(days=30)).isoformat()
    cache["sources"]["ga4"]["observed_schema"] = "ga4-data-v2"
    states = {name: scorer.source_state(cache["sources"], name, now=NOW) for name in ("gsc", "bing", "ga4")}
    assert states["gsc"]["state"] == "UNKNOWN_PROPERTY_MISMATCH"
    assert states["bing"]["state"] == "UNKNOWN_STALE"
    assert states["ga4"]["state"] == "UNKNOWN_SOURCE_SCHEMA_DRIFT"
    old_date = (NOW.date() - dt.timedelta(days=40)).isoformat()
    old = put(lane["root"] / f"old/{old_date}-gsc-sitewide-aggregate.json", sitewide_aggregate())
    assert scorer.load_sitewide_aggregate(old, now=NOW)["state"] == "UNKNOWN_STALE"
    bad = sitewide_aggregate()
    bad["scopes"]["domain_including_partners"]["context"]["data_state"] = "all"
    bad_path = put(lane["root"] / f"bad/{REPORT_DATE.isoformat()}-gsc-sitewide-aggregate.json", bad)
    assert scorer.load_sitewide_aggregate(bad_path, now=NOW)["state"] == "UNKNOWN_CONTEXT:domain_including_partners"
    # A key whose only provider rows carry null metrics is unknown for that source, not measured zero.
    queries, _pages = scorer.bing_tables(source_cache()["data"]["bing_query_rows"], [])
    assert queries["tirzepatide cost"] == {"impressions": None, "clicks": None, "position": None, "rows": 0,
                                           "unknown_metric_rows": 1, "unknown": True}
    assert scorer.demand_for("tirzepatide cost", queries)["state"] == "UNKNOWN_METRICS_NULL"
    assert scorer.demand_for("tirzepatide cost", queries)["weighted_impressions"] is None


def load_selector_namespace() -> dict[str, Any]:
    """Execute the real selector's pure bodies without its host-only imports."""
    tree = ast.parse(SELECTOR.read_text())
    tree.body = [node for node in tree.body if not (
        isinstance(node, ast.ImportFrom) and node.module in (
            "current_content_base", "machine_config", "selection_receipt", "decision_policy")
        or isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
        and ast.unparse(node.value.func) == "sys.path.insert"
        or isinstance(node, ast.If) and "__name__" in ast.unparse(node.test))]
    namespace: dict[str, Any] = {"__name__": "selector_fixture"}
    exec(compile(tree, str(SELECTOR), "exec"), namespace)
    return namespace


def test_sidecar_is_a_valid_downstream_selector_input(lane, capsys):
    result = run_scorer(lane, capsys)
    selector = load_selector_namespace()
    table, meta = selector["load_demand_multipliers"](Path(result["sidecar"]["path"]))
    assert meta["state"] == "applied" and meta["count"] == 3 and meta["degraded"] == []
    assert selector["demand_multiplier"]("GLP 1 Cost", table) == table["glp1 cost"]
    assert table["glp1 cost"] > table["cheapest glp1"] > selector["demand_multiplier"]("oral semaglutide pill", table) == 1.0
    assert "oral semaglutide pill" not in table  # neutral fallback is not a measured metric
    # The downstream research selection orders the strategy-safe set by this measured demand
    # when no candidate carries a GSC striking-distance weight (the no-rankings-yet case).
    strategy = json.loads(lane["strategy"].read_text())["queue"]
    topics = [row for row in topic_queue()["queue"] if row["topic_id"] != "t-new"]
    selected = selector["select_strategy_safe"](topics, strategy, {"cost-pricing", "insurance-eligibility", "oral"},
                                                set(), set(), set(), {}, demand_table=table)
    assert [row["topic_id"] for row in selected["eligible"]] == ["t-cheapest", "t-oral"]
    assert selected["selected"][0]["topic_id"] == "t-cheapest"
    assert selected["selected"][0]["demand_multiplier"] == table["cheapest glp1"]
    assert selected["selection_mode"] == "deterministic_cluster_queue_fallback"
    # A stale sidecar fails open in the same consumer: ordering falls back, nothing breaks.
    stale = json.loads(Path(result["sidecar"]["path"]).read_bytes())
    stale["written_utc"] = (NOW - dt.timedelta(days=30)).isoformat()
    stale_table, stale_meta = selector["load_demand_multipliers"](put(lane["root"] / "stale-sidecar.json", stale))
    assert stale_table == {} and stale_meta["state"] == "stale"


def test_weekly_learning_p5_spokes_are_prioritized_and_deduplicated_fail_open(lane, capsys):
    result = run_scorer(lane, capsys)
    sidecar_path = Path(result["sidecar"]["path"])
    scores, meta = actions.load_first_party_refresh_scores(sidecar_path, now=NOW)
    assert meta["state"] == "applied" and meta["scored_routes"] == 4 and meta["degraded"] == []
    assert scores[COST_ROUTE]["rank"] == 1
    spokes = [
        {"route": "/reviews/bing-only-route/", "priority": "P5", "position": 45.0, "action": "net_new_spoke"},
        {"route": "/guides/unscored-route/", "priority": "P5", "position": 41.0, "action": "net_new_spoke"},
        {"route": COST_ROUTE, "priority": "P5", "position": 60.0, "action": "net_new_spoke"},
        {"route": "/reviews/bing-only-route/", "priority": "P5", "position": 45.0, "action": "net_new_spoke"},
    ]
    ordered, priority = actions.prioritize_p5_spokes(spokes, scores)
    assert [row["route"] for row in ordered] == [COST_ROUTE, "/reviews/bing-only-route/", "/guides/unscored-route/"]
    assert priority == {"work_orders": 3, "scored": 2, "unscored": 1,
                        "duplicate_routes_removed": ["/reviews/bing-only-route/"]}
    assert ordered[0]["first_party_demand"]["state"] == "SOURCE_REPORTED"
    assert ordered[0]["first_party_demand"]["refresh_score"] == scores[COST_ROUTE]["refresh_score"]
    assert ordered[-1]["first_party_demand"] == {"state": "UNKNOWN_NOT_SCORED", "refresh_score": None, "rank": None}
    # Stale, absent and malformed sidecars all fail open to the existing position order.
    stale = json.loads(sidecar_path.read_bytes())
    stale["written_utc"] = (NOW - dt.timedelta(days=30)).isoformat()
    for path in (put(lane["root"] / "stale.json", stale), lane["root"] / "absent.json",
                 put(lane["root"] / "wrong.json", {"schema": "other"})):
        table, meta = actions.load_first_party_refresh_scores(path, now=NOW)
        assert table == {} and meta["state"] in ("stale", "absent", "schema_mismatch")
        fallback, _ = actions.prioritize_p5_spokes(spokes, table)
        assert [row["route"] for row in fallback] == ["/guides/unscored-route/", "/reviews/bing-only-route/", COST_ROUTE]
