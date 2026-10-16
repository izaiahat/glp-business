import importlib.util
import json
from pathlib import Path
import pytest
import subprocess
import datetime as dt

ROOT = Path(__file__).resolve().parents[1]
def load():
    spec = importlib.util.spec_from_file_location('strategy_inputs', ROOT/'ops/glp/weekly_strategy_inputs.py')
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

def test_estate_is_real_bound_evidence_not_placeholder(tmp_path):
    m = load(); current = tmp_path/'reports/glp-clinic-scout/analytics/current'; current.mkdir(parents=True)
    (current/'analytics-source-health.json').write_text(json.dumps({'schema':'analytics-source-health-v1','generated_at_utc':m.now(),'mode':'PASS_SEARCH_ONLY','sources':[{'source':'network_revenue','state':'unavailable','actual_rows':None}]}))
    (current/'affiliate-reconciliation.json').write_text(json.dumps({'schema':'affiliate-reconciliation-v1','generated_at_utc':m.now(),'counts':{'d1':501,'unmatched_d1_to_network':501},'network':{'measurement_state':'unavailable'}}))
    doc=m.estate_snapshot(tmp_path, jobs=[])
    assert doc['result']['ok'] is True and doc['result']['complete'] is True
    assert doc['result']['components'][0]['live_effect']['mode']=='PASS_SEARCH_ONLY'
    assert all({'owner','natural_run','manual_run','live_effect','required_artifact'}<=set(c) for c in doc['result']['components'])
    assert len(doc['bindings'])==2
    assert doc['result']['components'][1]['live_effect']['unmatched_d1_to_network']==501

def test_missing_or_stale_estate_is_unknown(tmp_path):
    m=load(); assert m.estate_snapshot(tmp_path,jobs=[])['result']['ok'] is False
    current=tmp_path/'reports/glp-clinic-scout/analytics/current';current.mkdir(parents=True)
    for name in ('analytics-source-health.json','affiliate-reconciliation.json'):
        (current/name).write_text(json.dumps({'generated_at_utc':'2020-01-01T00:00:00Z'}))
    assert m.estate_snapshot(tmp_path,jobs=[])['result']['complete'] is False

def test_gsc_actions_are_non_executable_scoped_and_deduplicated():
    m=load()
    rows=[{'keys':['compare options','https://glpclinicscout.com/compare/'],'position':12,'clicks':0,'impressions':100},
          {'keys':['compare options','https://glpclinicscout.com/compare/'],'position':12,'clicks':0,'impressions':100},
          {'keys':['bad','https://elsewhere.example/'],'position':12,'impressions':100},
          {'keys':['track','https://glpclinicscout.com/go/test'],'position':12,'impressions':100},
          {'keys':['held','https://glpclinicscout.com/reviews/fella-health/'],'position':12,'impressions':100}]
    doc=m.topic_recommendations({'result':{'ok':True,'complete':True,'rows':rows}},held_routes=[])
    assert len(doc['recommendations'])==1
    row=doc['recommendations'][0]
    assert row['action']=='improve_existing_page' and row['query']=='compare options'
    assert doc['operational_queue_writes']==0 and doc['publication_authorized'] is False
    assert m.topic_recommendations({'result':{'ok':False,'rows':rows}},held_routes=[])['status']=='UNKNOWN_GSC_INCOMPLETE'


def test_registered_gate_runs_report_and_does_not_recollect_same_week(tmp_path, monkeypatch, capsys):
    spec=importlib.util.spec_from_file_location('strategy_gate',ROOT/'ops/hermes-cron-scripts/weekly_strategy_review_gate.py')
    assert spec and spec.loader
    gate=importlib.util.module_from_spec(spec); spec.loader.exec_module(gate)
    monkeypatch.syspath_prepend(str(ROOT/'systems/seo-blog-machine/scripts'))
    monkeypatch.setattr(gate,'B',tmp_path); monkeypatch.setattr(gate,'MARKER',tmp_path/'marker.json')
    count=[]
    def execute(command,**kwargs):
        count.append(command)
        output=tmp_path/'report';output.mkdir(parents=True)
        (output/'REPORT.json').write_text(json.dumps({'schema':'glp-weekly-strategy-report-v1','generated_at_utc':dt.datetime.now(dt.timezone.utc).isoformat(),'status':'COMPLETE_WITH_EXPLICIT_HOLDS','unknowns':{},'action_count':2,'module_statuses':{'estate_health':'PASS_ANALYSIS_ONLY'}}))
        result={'status':'COMPLETE_WITH_EXPLICIT_HOLDS','report':str(output/'REPORT.json'),'review':str(output/'REVIEW.md'),'topic_recommendations':str(output/'TOPIC-RECOMMENDATIONS.json'),'summary':{'action_count':2},'host_completion':{'task_id':'offline-host-task'}}
        return subprocess.CompletedProcess(command,0,json.dumps(result))
    monkeypatch.setattr(gate.subprocess,'run',execute)
    monkeypatch.setattr('ops.glp.weekly_analytics_owner.consume',lambda result,consumer:{'task_id':result['host_completion']['task_id'],'consumer':consumer})
    monkeypatch.setattr('sys.argv',['gate'])
    assert gate.main()==0
    first=json.loads(capsys.readouterr().out)
    assert first['wakeAgent'] is True and first['context']['report_summary']['action_count']==2
    assert gate.main()==0
    second=json.loads(capsys.readouterr().out)
    assert second['context']==first['context'] and len(count)==1
    assert '--host-owned' in count[0] and '--update-strategy' not in count[0]
    assert second['context']['host_analytics_consumption']['consumer']=='020bcdd7f0e1'
    assert second['context']['focus']=='GLP Clinic Scout only; affiliate revenue first; all ads remain paused'

