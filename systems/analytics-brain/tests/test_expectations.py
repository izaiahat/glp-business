import datetime as dt
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import collector
import analytics_brain as brain


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


def test_unmeasured_identity_does_not_create_zero_or_decision_hold():
    from test_analytics_brain import base_data
    data=base_data(revenue=False,clicks=0)
    data['sources']['posthog'].update(measurement_state='unmeasured_by_design',measurement_authority='K4c-D',measurement_scope='host_page_click_identity')
    result=brain.build_outputs(data)
    assert result['analytics-source-health.json']['mode']=='PASS_SEARCH_ONLY'
    assert result['decision-receipt.json']['status']=='PASS_NO_DECISIONS_REQUESTED'
    facts=brain.page_facts(data,{'mode':'PASS_SEARCH_ONLY','revenue_blind':True})
    for page in facts['pages']:
        assert page['engagement']['posthog_engaged_sessions']['value'] is None
        assert page['engagement']['posthog_engaged_sessions']['unavailable_reason']=='unmeasured_by_design'


def test_identity_free_probe_is_unevaluable_not_failed():
    from test_analytics_brain import base_data
    data=base_data(revenue=False,clicks=0)
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
    from test_analytics_brain import base_data
    data=base_data(revenue=False,clicks=20)
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
