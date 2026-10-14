import importlib.util
from pathlib import Path
import pytest
from tests.test_glp336_host_weekly import (
    test_weekly_completion_and_consumer_ack_are_once_only,
    test_empty_analysis_never_completes,
    test_crash_after_analysis_resumes_without_collection,
    test_tampered_analysis_refuses_instead_of_recollecting,
    test_wrong_week_and_failed_budget_cannot_complete,
    test_free_analysis_completes_with_explicit_metered_hold,
    test_report_changed_between_validation_and_binding_must_refuse,
    test_completion_and_ack_require_validated_report_hash,
    test_rebound_invalid_report_cannot_verify_or_ack,
    test_report_drift_at_consumer_lock_refuses_ack,
)

ROOT=Path(__file__).resolve().parents[1]
def load():
    spec=importlib.util.spec_from_file_location('strategy_sync',ROOT/'ops/glp/weekly_strategy_sync.py')
    assert spec is not None and spec.loader is not None
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module

def test_strategy_updates_group_existing_pages_not_duplicate_articles():
    m=load();topics={'week_start':'2026-10-05','recommendations':[
        {'route':'/reviews/example/','query':'example reviews','priority':1,'impressions':20,'clicks':2,'average_position':12},
        {'route':'/reviews/example/','query':'example costs','priority':2,'impressions':10,'clicks':0,'average_position':19}]}
    modules={'modules':{'keyword_gap':{'status':'UNKNOWN_INPUT','required_artifact':'spend unknown'}}}
    doc=m.build_updates(topics,modules,{'gsc-28':{'path':'x','sha256':'a'}},source_observations={})
    assert len(doc['page2_targets'])==2 and len(doc['refresh_list'])==1
    assert doc['refresh_list'][0]['queries']==['example reviews','example costs']
    assert doc['refresh_list'][0]['action']=='refresh_existing_page'
    assert doc['keyword_gaps'] is None and doc['keyword_gap_status']=='UNKNOWN_INPUT'
    assert doc['publication_authorized'] is False
    import datetime as dt
    captured=dt.datetime.fromisoformat(doc['generated_at_utc'])
    expiry=dt.datetime.fromisoformat(doc['expires_at_utc'])
    assert expiry-captured==dt.timedelta(days=8)
    assert doc['own_site_analytics_gates_content'] is False
    assert all(t['evidence_class']=='LOW_VOLUME_CONTEXT' for t in doc['page2_targets'])

class Client:
    def __init__(self):self.nodes={};self.calls=[]
    def call(self,tool,args):
        self.calls.append(tool)
        if tool=='write_context_node':self.nodes[args['path']]=dict(args);return {'path':args['path']}
        return self.nodes[args['path']]

def test_cogny_sync_readback_and_current_input_are_bound(tmp_path):
    m=load();client=Client();updates={'schema':'glp-weekly-strategy-updates-v1','week_start':'2026-10-05','topic_priorities':[]}
    receipt=m.sync_updates(client,updates,tmp_path/'run')
    assert client.calls==['write_context_node','read_context_node']
    assert receipt['status']=='COGNY_EXACT_CONTENT_READBACK'
    pointer=m.publish_current(tmp_path/'current.json',updates,receipt)
    assert m.read_current(tmp_path/'current.json')['updates']==updates
    Path(pointer['updates']['path']).write_text('{}')
    with pytest.raises(ValueError,match='strategy_input_changed'):m.read_current(tmp_path/'current.json')

def test_failed_cogny_readback_never_publishes(tmp_path):
    m=load()
    class Bad(Client):
        def call(self,tool,args):
            if tool=='read_context_node':return {'content':'wrong'}
            return super().call(tool,args)
    with pytest.raises(ValueError,match='cogny_strategy_readback_mismatch'):
        m.sync_updates(Bad(),{'schema':'glp-weekly-strategy-updates-v1','week_start':'2026-10-05'},tmp_path/'run')
    assert not (tmp_path/'current.json').exists()
