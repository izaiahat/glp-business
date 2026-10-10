import copy
import importlib.util
from pathlib import Path
import pytest


def test_missing_reddit_skill_repair_preserves_capabilities_and_foreign_jobs():
    path=Path(__file__).resolve().parents[1]/'scripts/glp_reddit_scout_skill_repair.py'
    spec=importlib.util.spec_from_file_location('reddit_skill_repair',path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    job={'id':module.JOB_ID,'skills':['web-research-automation'],'skill':'web-research-automation',
         'enabled_toolsets':['code_execution','firecrawl'],'model':'gpt-6.1-sol',
         'provider':'openai-codex','deliver':'discord:1487300132054765629','fire_claim':None}
    edits=[]
    def update(job_id, values):
        edits.append((job_id,values))
        return {**job,**values,'skill':values['skills'][0]}
    assert module.repair(job,update)['changed_fields']==['skill','skills']
    assert edits==[(module.JOB_ID,{'skills':['reddit-community-research']})]
    for delta in ({'id':'foreign'},{'fire_claim':{'by':'owner'}},
                  {'enabled_toolsets':['all']},{'skills':['another-active-skill']}):
        with pytest.raises(ValueError):
            module.repair({**job,**delta},update)
    assert len(edits)==1
