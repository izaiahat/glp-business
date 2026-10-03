"""Offline regressions for the real demand receipt contracts (no live imports)."""
import datetime as dt
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("scorer_guards", ROOT / "scripts/glp_topic_data_score.py")
assert SPEC is not None and SPEC.loader is not None
scorer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scorer)
NOW = dt.datetime(2026, 10, 3, 12, tzinfo=dt.timezone.utc)


def test_absent_query_is_unknown_not_measured_zero():
    result = scorer.demand_for("unreported query", {})
    assert result["weighted_impressions"] is None
    assert result["exact_impressions"] is None
    assert result["exact_clicks"] is None
    assert result["state"].startswith("UNKNOWN_")


def test_partial_null_rows_do_not_become_complete_totals():
    queries, _ = scorer.gsc_tables([
        {"path": "/guides/test/", "query": "test", "clicks": 2, "impressions": 5, "position": 15},
        {"path": "/guides/test/", "query": "test", "clicks": None, "impressions": None, "position": None},
    ])
    assert scorer.demand_for("test", queries)["weighted_impressions"] is None


def test_missing_page_is_unknown_not_zero():
    row = scorer._page_metric({}, "/guides/never-collected/", True, ("impressions", "clicks", "position"))
    assert row["impressions"] is None and row["clicks"] is None
    assert row["state"].startswith("UNKNOWN_")


def test_ga4_neutral_scope_cannot_be_joined_to_health_routes():
    pages = scorer.ga4_pages([
        {"host": "glpclinicscout.com", "path": "/guides/health/", "event_name": "page_view",
         "event_count": 999, "sessions": 999, "engaged_sessions": 999},
        {"host": "partners.glpclinicscout.com", "path": "/about/", "event_name": "page_view",
         "event_count": 999, "sessions": 999, "engaged_sessions": 999},
        {"host": "glpclinicscout.com", "path": "/about/", "event_name": "page_view",
         "event_count": 4, "sessions": 4, "engaged_sessions": 2},
    ])
    assert set(pages) == {"/about/"}


def test_invalid_or_unfinalized_collection_window_is_unknown():
    source = {"state": "healthy", "observed_schema": "gsc-searchanalytics-v3", "complete": True,
              "property": scorer.GSC_PROPERTY, "last_successful_readback": NOW.isoformat(),
              "watermark": "2026-09-29", "watermark_kind": "provider_report_date",
              "collection_window": {"start": "2026-09-02", "end": "2026-10-03"}}
    assert scorer.source_state({"gsc": source}, "gsc", now=NOW)["state"].startswith("UNKNOWN_")
    source["collection_window"] = {"start": "not-a-day", "end": "2026-09-29"}
    assert scorer.source_state({"gsc": source}, "gsc", now=NOW)["state"].startswith("UNKNOWN_")


def test_real_sitewide_and_neutral_ga4_aggregates_change_scores(tmp_path):
    # Source windows stay distinct: no weekly site totals used as a query-row denominator.
    import sys
    sys.path.insert(0, str(ROOT / "tests"))
    from test_glp343_topic_first_party_scoring import source_cache, strategy_queue, sitewide_aggregate, NOW as fixture_now, REPORT_DATE
    cache = source_cache()
    cache["data"]["ga4_rows"] = [
        {"host": "glpclinicscout.com", "path": "/about/", "event_name": "page_view",
         "event_count": 10, "sessions": 10, "engaged_sessions": 8},
    ]
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(json.dumps(cache))
    strategy = tmp_path / "strategy.json"
    strategy.write_text(json.dumps(strategy_queue(tmp_path / "map.json")))
    reports = tmp_path / "reports"
    reports.mkdir()
    aggregate_path = reports / f"{REPORT_DATE.isoformat()}-gsc-sitewide-aggregate.json"
    aggregate_path.write_text(json.dumps(sitewide_aggregate()))
    first = scorer.run(source_cache=cache_path, weekly_report_root=reports, strategy_queue=strategy,
                       topic_queue=None, output_dir=tmp_path / "first", now=fixture_now)
    cache["data"]["ga4_rows"][0]["engaged_sessions"] = 0
    cache_path.write_text(json.dumps(cache))
    second = scorer.run(source_cache=cache_path, weekly_report_root=reports, strategy_queue=strategy,
                        topic_queue=None, output_dir=tmp_path / "second", now=fixture_now)
    a = json.loads(Path(first["receipt"]["path"]).read_text())
    b = json.loads(Path(second["receipt"]["path"]).read_text())
    assert a["topics"][0]["multiplier"] > b["topics"][0]["multiplier"]
    assert a["refreshes"][0]["refresh_score"] > b["refreshes"][0]["refresh_score"]
    aggregate_path.unlink()
    third = scorer.run(source_cache=cache_path, weekly_report_root=reports, strategy_queue=strategy,
                       topic_queue=None, output_dir=tmp_path / "third", now=fixture_now)
    c = json.loads(Path(third["receipt"]["path"]).read_text())
    assert b["topics"][0]["multiplier"] > c["topics"][0]["multiplier"]
    assert b["refreshes"][0]["refresh_score"] > c["refreshes"][0]["refresh_score"]
    assert a["scoring_context"]["ga4"]["scope"] == "approved_neutral_routes_only_no_health_path_join"


def test_refresh_work_orders_consume_scores_without_changing_tiers_or_authority():
    import sys
    sys.path.insert(0, str(ROOT / "tests"))
    from test_glp343_topic_first_party_scoring import actions
    rows = [
        {"route": "/guides/low/", "priority": "P1", "position": 12, "refresh_tier": "R2"},
        {"route": "/guides/high/", "priority": "P1", "position": 14, "refresh_tier": "R2"},
        {"route": "/guides/other/", "priority": "P2", "position": 24, "refresh_tier": "R2"},
        {"route": "/guides/reserve/", "priority": "P4", "position": 44,
         "operator_freshness_cadence_authority": True, "refresh_tier": "R2"},
    ]
    scores = {"/guides/high/": {"refresh_score": 4.0, "rank": 1},
              "/guides/low/": {"refresh_score": 2.0, "rank": 2},
              "/guides/reserve/": {"refresh_score": 5.0, "rank": 3}}
    ordered, meta = actions.prioritize_r2_work_orders(rows, scores)
    assert [row["route"] for row in ordered] == ["/guides/high/", "/guides/low/", "/guides/other/", "/guides/reserve/"]
    assert [row["priority"] for row in ordered] == [row["priority"] for row in rows]
    assert {row["route"] for row in ordered} == {row["route"] for row in rows}
    assert ordered[-1]["operator_freshness_cadence_authority"] is True
    assert meta["scored"] == 3
    assert actions.prioritize_r2_work_orders(rows, {})[0] == rows
