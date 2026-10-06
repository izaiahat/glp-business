import copy
import datetime as dt
import json
import sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import collector
import analytics_brain as brain


def test_posthog_collector_queries_only_fixed_scope_aggregates():
    queries = []
    class PostHog:
        def page_conversion(self, **kwargs):
            raise AssertionError('legacy arbitrary URL-derived route read forbidden')
        def hogql(self, query):
            queries.append(query)
            assert all(word not in query for word in
                       ('$current_url', 'cs_click_id', 'distinct_id', 'person', 'quiz'))
            if 'AS host_bucket' in query:
                return {'ok': True, 'rows': [
                    ['production_host', 'neutral_route', 3, '2026-10-02T01:00:00Z'],
                    ['production_host', 'route_unknown', 17, '2026-10-02T02:00:00Z'],
                    ['off_host_or_unknown', 'neutral_route', 100, '2026-10-02T03:00:00Z']]}
            assert "properties.$host = 'glpclinicscout.com'" in query
            assert 'properties.$pathname IN' in query
            return {'ok': True, 'rows': [['/', 2, 1]]}
    data = {'posthog_rows': [{'path': '/old/'}], 'posthog_events': [{'cs_click_id': 'old'}]}
    sources = {}
    collector._collect_posthog(data, sources, type('Rails', (), {'PostHog': PostHog}))
    assert sources['posthog']['state'] == 'healthy', sources['posthog']
    assert len(queries) == 2
    assert data['posthog_rows'] == [dict(path='/', host=brain.PRODUCTION_HOST,
                                        views=2, affiliate_clicks=1)]
    assert data['posthog_events'] == []
    assert sources['posthog']['event_watermark'] == '2026-10-02T01:00:00Z'
    assert sources['posthog']['host_distribution'] == {brain.PRODUCTION_HOST: 3}
    assert sources['posthog']['non_allowlisted_event_count'] == 0  # excluded buckets are not selected-row pollution
    scope = sources['posthog']['collection_window']['scope_counts']
    assert scope['production_host/route_unknown']['event_count'] == 17
    assert scope['off_host_or_unknown/neutral_route']['event_count'] == 100
    # The natural machine receipt consumes the fixed buckets, not raw hosts.
    health = brain.source_health({'sources': sources})
    posthog_health = next(row for row in health['sources'] if row['source'] == 'posthog')
    assert posthog_health['collection_window']['scope_counts'] == scope


def test_ga4_collector_requests_only_reviewed_neutral_aggregate_dimensions():
    requests = []
    class Google:
        def ga4_run_report(self, **kwargs):
            requests.append(kwargs)
            assert 'customEvent:cs_click_id' not in kwargs['dimensions']
            if kwargs['dimensions'] == ['streamId', 'hostName']:
                return {'ok': True, 'complete': True, 'dimension_names': kwargs['dimensions'],
                        'metric_names': kwargs['metrics'], 'rows': []}
            assert kwargs['dimensions'] == ['dateHourMinute', 'streamId', 'hostName', 'pagePath', 'eventName']
            expected = {'andGroup': {'expressions': [
                {'filter': {'fieldName': 'streamId', 'stringFilter': {
                    'matchType': 'EXACT', 'value': '15060837778', 'caseSensitive': True}}},
                {'filter': {'fieldName': 'pagePath', 'inListFilter': {
                    'values': sorted(collector.GA4_APPROVED_NEUTRAL_PATHS), 'caseSensitive': True}}}]}}
            assert kwargs['dimension_filter'] == expected
            return {'ok': True, 'complete': True, 'host_filtered': True,
                    'dimension_names': kwargs['dimensions'], 'metric_names': kwargs['metrics'],
                    'host_filter': {'exact': brain.PRODUCTION_HOST}, 'pages': 1,
                    'rows': [{'dimensions': ['202610020100', '15060837778', brain.PRODUCTION_HOST, '/', 'page_view'],
                              'metrics': ['2', '1', '1']}]}
    data, sources = {'ga4_events': [{'cs_click_id': 'old'}]}, {}
    collector._collect_ga4(data, sources, Google())
    assert sources['ga4']['state'] == 'healthy', sources['ga4']
    assert len(requests) == 2
    assert data['ga4_events'] == []
    assert data['ga4_rows'][0]['path'] == '/'


def test_ga4_consumer_stream_binding_preserves_property_diagnostics_and_refuses_bad_data():
    consumer, partners = '15060837778', '15224515823'
    partner_host = 'glp-clinic-scout-partners.pages.dev'
    class Google:
        def __init__(self, audit_rows=None, fact_rows=None, audit_extra=None):
            self.requests = []
            self.audit_rows = audit_rows if audit_rows is not None else [
                {'dimensions': [partners, partner_host], 'metrics': ['3']}]
            self.fact_rows = fact_rows
            self.audit_extra = audit_extra or {}
        def ga4_run_report(self, **kwargs):
            self.requests.append(kwargs)
            dims = kwargs['dimensions']
            if 'pagePath' not in dims:
                # Preserve the proven predecessor property-wide false failure.
                rows = self.audit_rows if 'streamId' in dims else [
                    {'dimensions': [partner_host], 'metrics': ['3']}]
                return dict({'ok': True, 'complete': True, 'dimension_names': dims,
                             'metric_names': ['eventCount'], 'rows': rows}, **self.audit_extra)
            row = {'dateHourMinute': '202610060001', 'streamId': consumer,
                   'hostName': brain.PRODUCTION_HOST, 'pagePath': '/', 'eventName': 'page_view'}
            return {'ok': True, 'complete': True, 'host_filtered': True,
                    'dimension_names': dims, 'metric_names': kwargs['metrics'],
                    'host_filter': {'exact': brain.PRODUCTION_HOST}, 'pages': 1,
                    'rows': self.fact_rows if self.fact_rows is not None else [
                        {'dimensions': [row[d] for d in dims], 'metrics': ['2', '1', '1']}]}
    def collect(google):
        data = {'ga4_rows': [{'path': '/old/'}], 'ga4_events': [{'cs_click_id': 'old'}]}
        sources = {}
        collector._collect_ga4(data, sources, google)
        return data, sources['ga4']
    google = Google()
    data, source = collect(google)
    assert source['non_allowlisted_event_count'] == 0, source
    facts, audit = google.requests
    assert facts['dimensions'] == ['dateHourMinute', 'streamId', 'hostName', 'pagePath', 'eventName']
    assert facts['property_id'] == audit['property_id'] == '541440529'
    assert facts['dimension_filter'] == {'andGroup': {'expressions': [
        {'filter': {'fieldName': 'streamId', 'stringFilter': {
            'matchType': 'EXACT', 'value': consumer, 'caseSensitive': True}}},
        {'filter': {'fieldName': 'pagePath', 'inListFilter': {
            'values': sorted(collector.GA4_APPROVED_NEUTRAL_PATHS), 'caseSensitive': True}}}]}}
    assert audit['dimensions'] == ['streamId', 'hostName'] and audit['host_filter'] is None
    assert 'dimension_filter' not in audit
    assert source['host_distribution'] == {}
    scope = source['collection_window']
    assert scope['consumer_stream_id'] == consumer
    assert scope['host_audit'] == {
        'scope': 'whole_property_partitioned_by_stream',
        'property_host_distribution': {partner_host: 3},
        'stream_host_distribution': {partners: {partner_host: 3}},
        'other_stream_host_distribution': {partners: {partner_host: 3}},
        'property_event_count': 3, 'consumer_stream_event_count': 0, 'other_stream_event_count': 3}
    assert next(row for row in brain.source_health({'sources': {'ga4': source}})['sources']
                if row['source'] == 'ga4')['collection_window'] == scope
    assert data['ga4_events'] == [] and data['ga4_rows'][0]['path'] == '/'
    mixed = Google(audit_rows=[{'dimensions': [consumer, partner_host], 'metrics': ['4']},
                              {'dimensions': [partners, partner_host], 'metrics': ['3']}])
    _, polluted = collect(mixed)
    assert polluted['non_allowlisted_event_count'] == 4
    assert polluted['collection_window']['host_audit']['property_event_count'] == 7
    packet = _base_data()
    packet['sources']['ga4'] = polluted
    assert brain.build_outputs(packet)['analytics-source-health.json']['mode'] == 'FAIL_HOST_POLLUTION'
    # Every incomplete/malformed stream, row and count stays unknown/error, not zero.
    for rows, extra in [([], {'complete': False}), ([], {'ok': False}),
                        ([], {'rows': None}), ([], {'dimension_names': ['hostName']}),
                        ([{}], {}), ([{'dimensions': [partner_host], 'metrics': ['3']}], {}),
                        ([{'dimensions': ['', partner_host], 'metrics': ['3']}], {}),
                        ([{'dimensions': ['(not set)', partner_host], 'metrics': ['3']}], {}),
                        ([{'dimensions': [partners, None], 'metrics': ['3']}], {}),
                        ([{'dimensions': [partners, partner_host], 'metrics': []}], {}),
                        ([{'dimensions': [partners, partner_host], 'metrics': ['-1']}], {}),
                        ([{'dimensions': [partners, partner_host], 'metrics': ['1.5']}], {})]:
        bad_data, bad_source = collect(Google(audit_rows=rows, audit_extra=extra))
        assert bad_source['state'] == 'error', (rows, extra, bad_source)
        assert bad_data['ga4_rows'] == bad_data['ga4_events'] == []
        assert bad_source['actual_rows'] is None
    for stream, host, path in [(partners, brain.PRODUCTION_HOST, '/'),
                               ('', brain.PRODUCTION_HOST, '/'),
                               (consumer, partner_host, '/'),
                               (consumer, brain.PRODUCTION_HOST, '/reviews/health/')]:
        bad_data, bad_source = collect(Google(fact_rows=[{
            'dimensions': ['202610060001', stream, host, path, 'page_view'], 'metrics': ['2', '1', '1']}]))
        assert bad_source['state'] == 'error'
        assert bad_data['ga4_rows'] == bad_data['ga4_events'] == []
    empty_data, empty_source = collect(Google(audit_rows=[], fact_rows=[]))
    assert empty_source['state'] == 'measured_zero' and empty_source['actual_rows'] == 0
    assert empty_source['event_watermark'] is None and empty_data['ga4_rows'] == []


def test_posthog_off_host_or_unknown_route_totals_do_not_prove_production():
    class PostHog:
        def hogql(self, query):
            if 'AS host_bucket' in query:
                return {'ok': True, 'rows': [
                    ['production_host', 'route_unknown', 500, '2026-10-02T02:00:00Z'],
                    ['off_host_or_unknown', 'neutral_route', 1000, '2026-10-02T03:00:00Z']]}
            return {'ok': True, 'rows': []}
    data, sources = {}, {}
    collector._collect_posthog(data, sources, type('Rails', (), {'PostHog': PostHog}))
    source = sources['posthog']
    assert source['state'] == 'measured_zero' and source['actual_rows'] == 0
    assert source['event_watermark'] is None and source['host_distribution'] == {}
    assert not data['posthog_rows'] and not data['posthog_events']
    assert source['collection_window']['scope_counts']['production_host/route_unknown']['event_count'] == 500


def test_scoped_collectors_fail_closed_without_cached_identity_or_route_rows():
    for response in ({'ok': False, 'error': 'HTTP429'}, {'ok': True, 'rows': [['/not-allowed/', 1, 0]]}):
        class PostHog:
            def hogql(self, query):
                return response
        data = {'posthog_rows': [{'path': '/old/'}], 'posthog_events': [{'cs_click_id': 'old'}]}
        sources = {}
        collector._collect_posthog(data, sources, type('Rails', (), {'PostHog': PostHog}))
        assert sources['posthog']['state'] == 'error'
        assert data['posthog_rows'] == data['posthog_events'] == []
    class Google:
        def ga4_run_report(self, **kwargs):
            return {'ok': True, 'complete': True, 'host_filtered': True,
                    'dimension_names': kwargs['dimensions'], 'metric_names': kwargs['metrics'],
                    'rows': [{'dimensions': ['202610020100', '15060837778', brain.PRODUCTION_HOST, '/not-allowed/', 'page_view'],
                              'metrics': ['2', '1', '1']}]}
    data, sources = {'ga4_rows': [{'path': '/old/'}], 'ga4_events': [{'cs_click_id': 'old'}]}, {}
    collector._collect_ga4(data, sources, Google())
    assert sources['ga4']['state'] == 'error'
    assert data['ga4_rows'] == data['ga4_events'] == []


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
    assert source['neutral_route_window_clean_after_utc'] == '2026-09-27T23:40:59.897740Z'
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
                  neutral_route_window_clean_after_utc='2026-09-27T23:40:59.897740Z',
                  route_query_window={'start_utc': '2026-09-27T00:00:00Z',
                                      'end_utc': '2026-09-27T23:00:00Z',
                                      'basis': 'capture_before_query_minus_3_days'},
                  last_successful_readback='2026-10-01T01:00:00Z')
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
    source['route_query_window'].update(start_utc='2026-09-28T00:00:00Z',
                                        end_utc='2026-10-01T00:00:00Z')
    result = brain.route_matrix(data, prior_matrix=prior)
    rows = {row['path']: row for row in result['routes']}
    assert rows['/providers/']['probe_measurement_state'] == 'eligible_unobserved'
    assert rows['/providers/']['ga4_comparison_state'] == 'not_applicable'
    assert rows['/providers/']['probe_evidence_sufficient'] is False
    assert rows['/providers/']['probe_pass'] is True
    data['generated_at_utc'] = '2026-10-05T23:40:59Z'
    mature = brain.route_matrix(data, prior_matrix=result)
    assert next(row for row in mature['routes'] if row['path'] == '/providers/')['probe_pass'] is True
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


# All four defects are provider-free; only F1 runs the real cache/caller path.
def _expectation_control():
    return json.loads((Path(__file__).resolve().parents[3] / 'ops/glp/analytics-brain-control.json').read_text())


def _probe_fixture(path='/', at='2026-10-01T00:00:00Z', views=25):
    def source(name):
        return dict(state='measured_zero', actual_rows=0, complete=True,
                    expected_schema=collector.EXPECTED_SCHEMAS[name],
                    observed_schema=collector.EXPECTED_SCHEMAS[name],
                    host_scope=brain.PRODUCTION_HOST,
                    host_filter={'exact': brain.PRODUCTION_HOST},
                    last_successful_readback=at, watermark=at, event_watermark=None)
    return {'generated_at_utc': at, 'sources': {name: source(name) for name in ('ga4', 'posthog')},
            'sitemap_routes': [path], 'sitemap_complete': True,
            'static_routes': [{'path': path, 'http_status': 200, 'representative_probe_required': True}],
            'posthog_rows': [{'path': path, 'host': brain.PRODUCTION_HOST, 'views': views}] if views else [],
            'ga4_rows': [], 'posthog_events': [], 'ga4_events': []}


def _probe_row(data, prior=None):
    return brain.route_matrix(data, prior_matrix=prior)['routes'][0]


def test_cached_posthog_query_window_is_not_report_generation_time(tmp_path):
    control = _expectation_control()
    cache_path = tmp_path / 'cache.json'
    initial = _probe_fixture(at='2026-09-30T23:00:00Z')
    class Clock(dt.datetime):
        current = dt.datetime(2026, 9, 30, 23, 0, tzinfo=dt.timezone.utc)
        @classmethod
        def now(cls, tz=None):
            return cls.current.astimezone(tz) if tz else cls.current.replace(tzinfo=None)
    class PostHog:
        def hogql(self, query):
            if 'AS host_bucket' in query:
                return {'ok': True, 'rows': [['production_host', 'neutral_route', 25, '2026-09-30T22:00:00Z']]}
            assert 'properties.$pathname IN' in query
            return {'ok': True, 'rows': [['/', 25, 0]]}
    Rails = type('Rails', (), {'PostHog': PostHog})
    with patch.object(collector.dt, 'datetime', Clock):
        collector._collect_posthog(initial, initial['sources'], Rails)
        collector._apply_dark_thresholds(initial, control, Clock.current)
        collector._write_cache(cache_path, initial, 'hourly')
        assert _probe_row(initial)['probe_evidence_sufficient'] is False
        Clock.current = dt.datetime(2026, 9, 30, 23, 50, tzinfo=dt.timezone.utc)
        with (patch.object(collector, '_rail_module', return_value=object()),
              patch.object(collector, 'derive_affiliate_obligations', return_value={}),
              patch.object(collector, '_collect_dataforseo')):
            reused = collector.collect_live(control, 'weekly', cache_path)
    source = reused['sources']['posthog']
    assert reused['collection_cadence']['sources_refreshed'] == ['dataforseo']
    assert source['last_successful_readback'] == initial['sources']['posthog']['last_successful_readback']
    assert source['route_query_window'] == initial['sources']['posthog']['route_query_window']
    health = brain.source_health(reused)
    assert next(s for s in health['sources'] if s['source'] == 'posthog')['route_query_window'] == source['route_query_window']
    assert _probe_row(reused)['probe_measurement_state'] == 'eligible_window_mixed'
    assert _probe_row(reused)['probe_evidence_sufficient'] is False
    assert collector._load_cache(cache_path)['sources']['posthog']['route_query_window'] == source['route_query_window']
    # A separately dated fresh read with a post-promotion query start is usable.
    Clock.current = dt.datetime(2026, 10, 1, 1, 0, tzinfo=dt.timezone.utc)
    with patch.object(collector.dt, 'datetime', Clock):
        collector._collect_posthog(reused, reused['sources'], Rails)
        reused['generated_at_utc'] = '2026-10-01T01:00:00Z'
        collector._apply_dark_thresholds(reused, control, Clock.current)
    source = reused['sources']['posthog']
    assert source['route_query_window']['start_utc'] == '2026-09-28T01:00:00Z'
    assert _probe_row(reused)['probe_measurement_state'] == 'observed_neutral_route'
    del source['route_query_window']  # old receipts must not be backfilled from the report clock
    assert _probe_row(reused)['probe_evidence_sufficient'] is False


def test_wrong_approved_binding_restricts_identity_and_clears_cached_eligibility():
    control = _expectation_control()
    data = _probe_fixture('/health-unknown/')
    collector._apply_dark_thresholds(data, control)
    source = data['sources']['posthog']
    assert source['host_identity_available'] is True
    for field, value in [('source_sha256', '0' * 64), ('authority', 'unsupported'),
                         ('host_page_identity', 'unknown'), ('production_promotion_at_utc', None),
                         ('neutral_route_paths', None), ('click_identity', 'unknown')]:
        broken = copy.deepcopy(control)
        broken['measurement_expectations']['posthog'][field] = value
        collector._apply_dark_thresholds(data, broken)
        assert source['state'] == 'measured_zero'
        assert source['measurement_state'] == 'unmeasured_by_design'
        assert source.get('host_identity_available') is not True
        assert 'neutral_route_paths' not in source
        assert 'neutral_route_window_clean_after_utc' not in source
        assert 'measurement_authority' not in source
        assert _probe_row(data)['probe_evidence_sufficient'] is False
    missing = copy.deepcopy(control)
    missing.pop('measurement_expectations')
    collector._apply_dark_thresholds(data, missing)
    assert source['measurement_state'] == 'unmeasured_by_design'
    assert _probe_row(data)['probe_evidence_sufficient'] is False
    for state in ('error', 'stale'):
        source.update(state=state, reason='retained_provider_failure')
        collector._apply_dark_thresholds(data, broken)
        assert source['state'] == state and source['reason'] == 'retained_provider_failure'


def test_posthog_only_routes_not_subject_to_ga4_absence():
    control = _expectation_control()
    data = _probe_fixture('/providers/')
    collector._apply_dark_thresholds(data, control)
    data['sources']['posthog']['route_query_window'] = {
        'start_utc': '2026-09-28T00:00:00Z', 'end_utc': '2026-10-01T00:00:00Z',
        'basis': 'capture_before_query_minus_3_days'}
    first = brain.route_matrix(data)
    data['generated_at_utc'] = '2026-10-03T01:00:00Z'
    row = _probe_row(data, first)
    assert row['probe_measurement_state'] == 'observed_neutral_route'
    assert row['probe_evidence_sufficient'] is False
    assert row['probe_lagging_sink'] is None and row['probe_pass'] is True
    assert row['ga4_comparison_state'] == 'not_applicable'
    data['sitemap_routes'] = ['/compare/']
    data['static_routes'][0]['path'] = '/compare/'
    data['posthog_rows'][0]['path'] = '/compare/'
    compare = _probe_row(data)
    assert compare['probe_measurement_state'] == 'observed_neutral_route'
    assert compare['ga4_comparison_state'] == 'not_applicable'
    assert compare['probe_pass'] is True
    # The common route retains the original sample and 48h persistence test.
    data['sitemap_routes'] = ['/']
    data['static_routes'][0]['path'] = '/'
    data['posthog_rows'][0]['path'] = '/'
    common = brain.route_matrix(data)
    data['generated_at_utc'] = '2026-10-05T02:00:00Z'
    assert _probe_row(data, common)['probe_pass'] is False


def test_posthog_host_darkness_needs_postpromotion_eligible_corroboration():
    control = _expectation_control()
    data = _probe_fixture(at='2026-09-28T00:30:00Z', views=0)
    data['sources']['ga4'].update(state='healthy', event_watermark='2026-09-27T12:00:00Z')
    collector._apply_dark_thresholds(data, control,
                                     dt.datetime(2026, 9, 28, 0, 30, tzinfo=dt.timezone.utc))
    assert data['sources']['posthog']['state'] == 'measured_zero'
    data['ga4_rows'] = [{'host': brain.PRODUCTION_HOST, 'path': '/health-unknown/',
                         'event_name': 'page_view', 'event_count': 25,
                         'provider_time': '2026-09-28T00:00:00Z'}]
    data['sources']['ga4']['event_watermark'] = '2026-09-28T00:00:00Z'
    collector._apply_dark_thresholds(data, control,
                                     dt.datetime(2026, 9, 28, 3, 1, tzinfo=dt.timezone.utc))
    assert data['sources']['posthog']['state'] == 'measured_zero'
    # A genuine post-promotion eligible pageview still re-arms the original 2h gate.
    data['ga4_rows'] = [{'host': brain.PRODUCTION_HOST, 'path': '/', 'event_name': 'page_view',
                         'event_count': 25, 'provider_time': '2026-09-28T00:00:00Z'}]
    data['sources']['ga4']['event_watermark'] = '2026-09-28T00:00:00Z'
    collector._apply_dark_thresholds(data, control,
                                     dt.datetime(2026, 9, 28, 3, 1, tzinfo=dt.timezone.utc))
    assert data['sources']['posthog']['state'] == 'dark'
    assert '2.00h' in data['sources']['posthog']['reason']
