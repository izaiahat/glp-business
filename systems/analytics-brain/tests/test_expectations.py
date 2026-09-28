import datetime as dt
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import collector
import analytics_brain as brain


def test_published_neutral_scope_config_and_transport_fail_closed():
    control = json.loads((Path(__file__).resolve().parents[3] / 'ops/glp/analytics-brain-control.json').read_text())
    expected = control['measurement_expectations']['posthog']
    assert expected['host_page_identity'] == 'neutral_routes_only'
    assert expected['neutral_route_paths'] == [
        '/', '/providers/', '/compare/', '/privacy/', '/privacy/cookies/', '/terms/', '/about/'
    ]
    assert expected['click_identity'] == 'unmeasured_by_design'
    assert expected['source_sha256'] == '0a0a7729cb628fc7afb694080bd07f82df28975409030ca7f19850f4dd2fb22a'
    source = collector._source('measured_zero', 0, host_scope=brain.PRODUCTION_HOST,
                               schema='posthog-hogql-v1')
    source['complete'] = True
    data = {'sources': {'posthog': source}}
    collector._apply_dark_thresholds(data, control, now=dt.datetime(2026, 9, 28, tzinfo=dt.timezone.utc))
    assert source['measurement_state'] == 'unmeasured_by_design'  # click/health paths only
    assert source['measurement_scope'] == 'click_and_non_neutral_route_identity'
    assert source['host_identity_available'] is True
    assert source['neutral_route_paths'] == expected['neutral_route_paths']
    assert source['neutral_route_window_clean_after_utc'] == '2026-09-30T23:40:59.897740Z'
    assert source['state'] == 'measured_zero' and source.get('event_watermark') is None
    readback = brain.source_health(data)
    posthog_health = next(row for row in readback['sources'] if row['source'] == 'posthog')
    assert posthog_health['host_identity_available'] is True
    assert posthog_health['actual_rows'] == 0  # eligibility is not ingestion
    clicks = _base_data(clicks=1)
    clicks['sources']['posthog'] = source
    clicks['posthog_events'] = []
    sink = brain.affiliate_reconciliation(clicks)['mature_sink_deltas']['posthog']
    assert sink['missing'] is None and sink['reason'] == 'click_identity_unmeasured_by_design'
    for broken in ('error', 'stale'):
        failed = collector._source(broken, None, host_scope=brain.PRODUCTION_HOST, schema='posthog-hogql-v1')
        failed['complete'] = True
        packet = {'sources': {'posthog': failed}}
        collector._apply_dark_thresholds(packet, control)
        assert failed['state'] == broken and 'host_identity_available' not in failed
    bad_schema = collector._source('healthy', 1, host_scope=brain.PRODUCTION_HOST, schema='wrong')
    bad_schema['complete'] = True
    collector._apply_dark_thresholds({'sources': {'posthog': bad_schema}}, control)
    assert 'host_identity_available' not in bad_schema


def test_neutral_probe_available_not_ingested_health_absent():
    source = collector._source('measured_zero', 0, host_scope=brain.PRODUCTION_HOST,
                               schema='posthog-hogql-v1')
    source.update(complete=True, measurement_state='unmeasured_by_design',
                  measurement_scope='click_and_non_neutral_route_identity',
                  host_identity_available=True, neutral_route_paths=['/', '/providers/'],
                  neutral_route_window_clean_after_utc='2026-09-30T23:40:59.897740Z')
    static = {'http_status': 200, 'ga4_static_present': True,
              'gtm_static_present': True, 'posthog_static_present': True,
              'csp_present': True, 'csp_gtm_allowed': True, 'csp_posthog_allowed': True,
              'consent_default_denied_present': True, 'representative_probe_required': True}
    data = {'sources': {'posthog': source}, 'sitemap_routes': ['/providers/', '/reviews/health/'],
            'static_routes': [dict(static, path=path) for path in ('/providers/', '/reviews/health/')],
            'ga4_rows': [{'path': path, 'host': brain.PRODUCTION_HOST,
                          'event_name': 'page_view', 'event_count': 30}
                         for path in ('/providers/', '/reviews/health/')],
            'posthog_rows': [], 'generated_at_utc': '2026-09-27T23:40:59Z'}
    prior = brain.route_matrix(data)
    before = {row['path']: row for row in prior['routes']}
    assert before['/providers/']['probe_measurement_state'] == 'eligible_window_mixed'
    assert before['/providers/']['probe_evidence_sufficient'] is False
    assert before['/reviews/health/']['probe_measurement_state'] == 'unmeasured_by_design'
    data['generated_at_utc'] = '2026-10-01T23:40:59Z'
    result = brain.route_matrix(data, prior_matrix=prior)
    rows = {row['path']: row for row in result['routes']}
    assert rows['/providers/']['probe_measurement_state'] == 'eligible_unobserved'
    assert rows['/providers/']['probe_evidence_sufficient'] is True
    assert rows['/providers/']['probe_pass'] is True  # first clean-window observation
    data['generated_at_utc'] = '2026-10-05T23:40:59Z'
    mature = brain.route_matrix(data, prior_matrix=result)
    assert next(row for row in mature['routes'] if row['path'] == '/providers/')['probe_pass'] is False
    assert rows['/reviews/health/']['probe_measurement_state'] == 'unmeasured_by_design'
    assert rows['/reviews/health/']['probe_evidence_sufficient'] is False
    policy = {'mode': 'PASS_SEARCH_ONLY', 'revenue_blind': True}
    facts = brain.page_facts(data, policy)['pages']
    by_path = {row['path']: row for row in facts}
    assert by_path['/providers/']['engagement']['posthog_engaged_sessions']['value'] is None
    assert by_path['/providers/']['engagement']['posthog_engaged_sessions']['unavailable_reason'] == 'engaged_sessions_not_proven_by_neutral_pageview'
    assert by_path['/reviews/health/']['engagement']['posthog_engaged_sessions']['unavailable_reason'] == 'unmeasured_by_design'
    # A PostHog pageview is not a click-ID join or an engaged-session metric.
    data['posthog_rows'] = [{'host': brain.PRODUCTION_HOST, 'path': '/providers/', 'views': 2}]
    observed = brain.route_matrix(data)
    assert next(row for row in observed['routes'] if row['path'] == '/providers/')['probe_measurement_state'] == 'observed_neutral_route'
    assert brain.page_facts(data, policy)['pages'][0]['engagement']['posthog_engaged_sessions']['value'] is None


def test_ga4_documented_latency():
    data={'sources':{'ga4':{'state':'healthy','event_watermark':'2026-09-06T00:00:00Z'},'posthog':{'state':'healthy','event_watermark':'2026-09-07T00:00:00Z'}}}
    control={'activated_at_utc':'2026-08-01T00:00:00Z'}
    collector._apply_dark_thresholds(data,control,now=dt.datetime(2026,9,8,12,tzinfo=dt.timezone.utc))
    assert data['sources']['ga4']['state']=='healthy'
    assert data['sources']['ga4']['freshness_state']=='lagging'
    collector._apply_dark_thresholds(data,control,now=dt.datetime(2026,9,9,1,tzinfo=dt.timezone.utc))
    assert data['sources']['ga4']['state']=='dark'


def test_privacy_measurement_is_not_source_darkness():
    control={'activated_at_utc':'2026-08-01T00:00:00Z',
             'measurement_expectations':{'posthog':{'host_page_identity':'unmeasured_by_design','authority':'K4c-D'}}}
    data={'sources':{'posthog':collector._source('healthy',3,host_scope=brain.PRODUCTION_HOST,watermark='2026-09-01T00:00:00Z',schema='posthog-hogql-v1'),
                     'ga4':{'state':'healthy','event_watermark':'2026-09-08T00:00:00Z'}}}
    data['sources']['posthog']['complete']=True
    collector._apply_dark_thresholds(data,control,now=dt.datetime(2026,9,9,tzinfo=dt.timezone.utc))
    source=data['sources']['posthog']
    assert source['state']=='healthy'  # authenticated transport, NOT measured identity
    assert source['measurement_state']=='unmeasured_by_design'
    assert brain._validate_source('posthog',source)['measurement_state']=='unmeasured_by_design'
    source['state']='error'
    source['reason']='HTTP429'
    collector._apply_dark_thresholds(data,control)
    assert source['state']=='error'
    assert source['reason']=='HTTP429'


def _base_data(*, clicks=0):
    """Portable synthetic fixture; no installed workspace or account reads."""
    def source(name, *, rows=1):
        return {'state': 'healthy', 'actual_rows': rows,
                'watermark': '2026-09-09T00:00:00Z', 'host_scope': brain.PRODUCTION_HOST
                if name in {'ga4', 'posthog'} else None,
                'host_filter': {'dimension': 'hostName', 'exact': brain.PRODUCTION_HOST}
                if name in {'ga4', 'posthog'} else None,
                'expected_schema': f'{name}-v1', 'observed_schema': f'{name}-v1'}
    sources = {name: source(name, rows=clicks if name == 'd1_clicks' else 1)
               for name in ('gsc', 'bing', 'ga4', 'posthog', 'd1_clicks', 'dataforseo')}
    sources['network_revenue'] = {'state': 'unavailable', 'reason': 'not_imported'}
    static = {'path': '/best/example/', 'http_status': 200, 'ga4_static_present': True,
              'gtm_static_present': True, 'posthog_static_present': True,
              'csp_present': True, 'csp_gtm_allowed': True, 'csp_posthog_allowed': True,
              'consent_default_denied_present': True, 'affiliate_route_expected': True,
              'sponsored_anchor_count': 1, 'client_marker_count': 1,
              'first_party_redirect_count': 1, 'sponsor_slot_ids': ['hero']}
    events = [{'cs_click_id': f'cs-{i:03d}', 'path': '/best/example/',
               'slot_id': 'hero', 'program_id': 'program-1',
               'occurred_at': '2026-09-01T00:00:00Z'} for i in range(clicks)]
    return {'generated_at_utc': '2026-09-09T00:00:00Z', 'sources': sources,
            'sitemap_complete': True, 'sitemap_routes': ['/best/example/'],
            'static_routes': [static], 'browser_clicks': events,
            'ga4_events': [dict(row, host=brain.PRODUCTION_HOST) for row in events],
            'posthog_events': [dict(row, host=brain.PRODUCTION_HOST) for row in events],
            'd1_clicks': list(events), 'revenue_events': [],
            'gsc_rows': [{'path': '/best/example/', 'impressions': 100, 'clicks': 5}],
            'bing_rows': [{'path': '/best/example/', 'impressions': 20, 'clicks': 1}],
            'ga4_rows': [{'path': '/best/example/', 'sessions': 50}],
            'posthog_rows': [{'path': '/best/example/', 'views': 25}]}


def test_unmeasured_identity_does_not_create_zero_or_decision_hold():
    data=_base_data(clicks=0)
    data['sources']['posthog'].update(measurement_state='unmeasured_by_design',measurement_authority='K4c-D',measurement_scope='host_page_click_identity')
    result=brain.build_outputs(data)
    assert result['analytics-source-health.json']['mode']=='PASS_SEARCH_ONLY'
    assert result['decision-receipt.json']['status']=='PASS_NO_DECISIONS_REQUESTED'
    facts=brain.page_facts(data,{'mode':'PASS_SEARCH_ONLY','revenue_blind':True})
    for page in facts['pages']:
        assert page['engagement']['posthog_engaged_sessions']['value'] is None
        assert page['engagement']['posthog_engaged_sessions']['unavailable_reason']=='unmeasured_by_design'


def test_identity_free_probe_is_unevaluable_not_failed():
    data=_base_data(clicks=0)
    data['static_routes'][0]['representative_probe_required']=True
    data['ga4_rows']=[{'path':'/best/example/','host':brain.PRODUCTION_HOST,'event_name':'page_view','event_count':30}]
    data['posthog_rows']=[]
    data['generated_at_utc']='2026-09-01T00:00:00Z'
    prior=brain.route_matrix(data)
    data['generated_at_utc']='2026-09-09T00:00:00Z'
    data['sources']['posthog'].update(measurement_state='unmeasured_by_design',measurement_authority='K4c-D')
    result=brain.route_matrix(data,prior_matrix=prior)
    row=next(x for x in result['routes'] if x['path']=='/best/example/')
    assert row['probe_measurement_state']=='unmeasured_by_design'
    assert row['probe_pass'] is True
    data['sources']['posthog']['state']='error'
    assert brain.build_outputs(data)['analytics-source-health.json']['mode']=='FAIL_SOURCE_DARK'


def test_identity_free_click_sink_is_unevaluable():
    data=_base_data(clicks=20)
    data['generated_at_utc']='2026-09-09T00:00:00Z'
    data['posthog_events']=[]
    data['sources']['posthog'].update(measurement_state='unmeasured_by_design',measurement_authority='K4c-D')
    result=brain.build_outputs(data)
    assert result['analytics-source-health.json']['mode']=='PASS_SEARCH_ONLY'
    sink=result['affiliate-reconciliation.json']['mature_sink_deltas']['posthog']
    assert sink['missing'] is None
    assert sink['measurement_state']=='unmeasured_by_design'
    data['ga4_events']=[]
    assert brain.build_outputs(data)['analytics-source-health.json']['mode']=='FAIL_SINK_DIVERGENCE'
