"""Offline regressions for the registered PH commercial query owners.

The transport doubles evaluate the exact query's TEST predicate over mixed
fixtures. No provider, job dispatch, event capture, or Discord send is used.
"""
import datetime as dt
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'systems/analytics-brain'))
import collector
import analytics_brain as brain

SPEC = importlib.util.spec_from_file_location('neutral_ads_monitor', ROOT / 'systems/analytics-brain/ads_monitor.py')
ads = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ads)
EXCLUSION = "ifNull(toString(properties.is_test), '') NOT IN ('true', '1')"
OLD = '2026-10-01T00:00:00Z'
NEW = '2026-10-04T00:00:00Z'


@pytest.mark.parametrize('ordinary', [False, True])
def test_all_collector_aggregates_and_bucket_clocks_exclude_test(ordinary):
    queries = []
    class PostHog:
        def hogql(self, query):
            queries.append(query)
            assert EXCLUSION in query, 'commercial count/max must filter producer event TEST'
            assert 'person' not in query and 'cohort' not in query
            if 'AS host_bucket' in query:
                return {'ok': True, 'rows': [
                    [host, route, 2, OLD]
                    for host in ('production_host', 'off_host_or_unknown')
                    for route in ('neutral_route', 'route_unknown', 'other_route')
                ] if ordinary else []}
            return {'ok': True, 'rows': [['/', 1, 1]] if ordinary else []}
    data, sources = {}, {}
    collector._collect_posthog(data, sources, SimpleNamespace(PostHog=PostHog))
    assert len(queries) == 2
    source = sources['posthog']
    assert source['state'] == ('healthy' if ordinary else 'measured_zero'), source
    assert source['event_watermark'] == (OLD if ordinary else None)
    assert source['host_distribution'] == ({brain.PRODUCTION_HOST: 2} if ordinary else {})
    buckets = source['collection_window']['scope_counts']
    assert len(buckets) == (6 if ordinary else 0)
    assert all(row['event_watermark'] == OLD and row['event_count'] == 2 for row in buckets.values())
    assert NEW not in str(source)
    assert data['posthog_events'] == []
    assert data['posthog_rows'] == ([{'path': '/', 'host': brain.PRODUCTION_HOST, 'views': 1, 'affiliate_clicks': 1}] if ordinary else [])


@pytest.mark.parametrize('ordinary', [False, True])
def test_native_canary_count_max_dark_clock_excludes_test_paid_query_unchanged(monkeypatch, ordinary):
    queries = []
    class PostHog:
        def hogql(self, query):
            queries.append(query)
            if "event = 'affiliate_click'" in query:
                assert EXCLUSION in query
                assert 'person' not in query and 'cohort' not in query
                return {'ok': True, 'rows': [[2, 1, OLD] if ordinary else [0, 0, None]]}
            # Neutral producer strips all three paid IDs: do not rewrite this disjoint query.
            assert query == (
                "SELECT countIf(properties.gclid IS NOT NULL AND properties.gclid != ''), "
                "countIf(properties.fbclid IS NOT NULL AND properties.fbclid != ''), "
                "countIf(properties.msclkid IS NOT NULL AND properties.msclkid != '') "
                "FROM events WHERE timestamp > now() - INTERVAL 7 DAY "
                "AND properties.$host = 'glpclinicscout.com'")
            return {'ok': True, 'rows': [[3, 4, 5]]}
    monkeypatch.setattr(ads, '_rails', lambda: SimpleNamespace(PostHog=PostHog))
    monkeypatch.setattr(ads, '_d1_client', lambda: SimpleNamespace(query=lambda _: []))
    monkeypatch.setattr(ads, 'utc_now', lambda: dt.datetime(2026, 10, 4, tzinfo=dt.timezone.utc))
    result = ads.click_join_readback({'canary_dark_hours': 26})['posthog']
    assert result['state'] == 'healthy', result
    assert len(queries) == 2
    assert result['affiliate_click_30d'] == (2 if ordinary else 0)
    assert result['with_cs_click_id_30d'] == (1 if ordinary else 0)
    assert result['last_affiliate_click'] == (OLD if ordinary else None)
    assert result['canary_dark_hours'] == (72.0 if ordinary else None)
    assert result['paid_click_ids_7d'] == {'gclid': 3, 'fbclid': 4, 'msclkid': 5}


def test_registered_ads_phase_imports_candidate_not_installed_host():
    spec = importlib.util.spec_from_file_location('neutral_cadence', ROOT / 'scripts/glp_analytics_brain_cadence.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    native = module._ads_module()
    assert Path(native.__file__).resolve() == ROOT / 'systems/analytics-brain/ads_monitor.py'
