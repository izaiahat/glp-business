"""No-network continuation identity and immutable lineage regression."""
import datetime as dt
import importlib.util
import sys
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'systems/seo-blog-machine/scripts'))
import r2_update_carrier as r2

def test_continuation_preserves_operation_and_refuses_wrong_day_or_changed_lineage(tmp_path):
    day=dt.date(2026,9,3)
    candidate={'route':'/guides/original/','article_id':'original','priority':'P4','refresh_tier':'R2','target_query':'original query','gsc_queries':[{'query':'original query'}]}
    topic=r2.topic_id(candidate,day)
    operation=f'r2-refresh:{topic}:{candidate["route"]}'
    weekly=r2.write_sealed(tmp_path/'weekly.json',{'schema':'glp-seo-existing-article-update-selection-v1','weekly_plan':{'selected':[candidate]}},'receipt_sha256')
    authority={'schema':'glp-seo-r2-update-authority-v1','status':'AUTHORIZED_R2_REFRESH','operation':'r2_refresh','topic_id':topic,'article_id':'original','target_route':candidate['route'],'target_query':'original query','priority':'P4','as_of':day.isoformat(),'selected_r2':candidate,'selected_r2_sha256':r2.object_hash(candidate),'inputs':{'weekly_gsc_selection':r2.spec(weekly)}}
    auth=r2.write_sealed(tmp_path/'authority.json',authority,'authority_sha256')
    selection=r2.write_sealed(tmp_path/'selection.json',{'operation':'r2_refresh','selected':[{'topic_id':topic,'article_id':'original','target_route':candidate['route'],'target_query':'original query','priority':'P4'}],'inputs':{'update_authority':r2.spec(auth), 'strategy_queue':{'path':'/retained/queue','sha256':'a'*64},'strategy_map':{'path':'/retained/map','sha256':'b'*64}}},'receipt_sha256')
    row={key:authority[key] for key in ('topic_id','article_id','target_route','target_query','priority','operation')}
    row.update(authority=r2.spec(auth),selection=r2.spec(selection),slot=1,authority_role='primary')
    plan=r2.write_sealed(tmp_path/'daily-plan.json',{'schema':'glp-seo-r2-daily-authority-plan-v1','as_of':day.isoformat(),'update_day_index':3,'quota':1,'authorities':[row],'substitute_authorities':[]},'plan_sha256')
    result=r2.load_continuation_candidate(plan,operation,as_of=day,update_day_index=3)
    assert result['candidate']==candidate
    assert result['weekly_selection']==weekly
    assert result['operation_id']==operation
    for kwargs in ({'operation':operation+'wrong','as_of':day,'update_day_index':3}, {'operation':operation,'as_of':dt.date(2026,9,10),'update_day_index':3}, {'operation':operation,'as_of':day,'update_day_index':2}):
        with pytest.raises(RuntimeError): r2.load_continuation_candidate(plan,**kwargs)
    weekly.chmod(0o644); weekly.write_bytes(weekly.read_bytes()+b' ')
    with pytest.raises(RuntimeError): r2.load_continuation_candidate(plan,operation,as_of=day,update_day_index=3)
