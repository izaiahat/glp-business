"""Offline renewal contract tests; fixture responses are not live Cloud proof."""
import copy
import datetime as dt
import json
import os
from pathlib import Path
import sys
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import codex_cloud_vpn_renewal as r


def fixture_env():
    return {'name':'hermes-evidence-account1','account':'izaiahtilton1@gmail.com','environment_id':'fixture-env','key_id':'fixture-old','tags':[r.TAG]}


def test_vpn_renewal_14day_threshold_and_no_remint(tmp_path):
    now=dt.datetime(2026,10,11,tzinfo=dt.timezone.utc)
    assert not r.due((now+dt.timedelta(days=14)).isoformat(),now)
    assert r.due((now+dt.timedelta(days=14,seconds=-1)).isoformat(),now)
    class Fake:
        def __init__(self): self.mints=0
        def metadata(self,key):
            created=now if key=='fixture-new' else now-dt.timedelta(days=84)
            return {'id':key,'created':created.isoformat(),'expires':(created+dt.timedelta(days=90)).isoformat(),'capabilities':{'devices':{'create':dict(reusable=True,ephemeral=True,preauthorized=True,tags=[r.TAG])}}}
        def mint(self,description):
            self.mints+=1
            return {'key':'fixture-not-a-real-key','id':'fixture-new'}
    api=Fake();state=tmp_path/'state.json';secure=tmp_path/'secure';config={'environments':[fixture_env()]}
    out=r.mint_due(config,state,api,now,secure)
    assert out['environments'][0]['lifetime_days']==90
    secret=secure/'pending-hermes-evidence-account1.json'
    assert secret.stat().st_mode&0o777==0o600
    assert 'fixture-not-a-real-key' not in state.read_text()
    r.mint_due(config,state,api,now,secure)
    assert api.mints==1
    value=json.loads(state.read_text());value['environments'][fixture_env()['name']]['phase']='MINT_INTENT';r.atomic(state,value)
    with pytest.raises(RuntimeError,match='ambiguous_mint'):
        r.mint_due(config,state,api,now,secure)
    assert api.mints==1


def test_vpn_private_reader_refuses_symlink_and_hardlink(tmp_path):
    original=tmp_path/'original';r.atomic(original,{'synthetic':'fixture'},private=True)
    assert r.private_read(original)=={'synthetic':'fixture'}
    link=tmp_path/'link';link.symlink_to(original)
    with pytest.raises(OSError): r.private_read(link)
    hard=tmp_path/'hard';os.link(original,hard)
    with pytest.raises(RuntimeError,match='private_file'): r.private_read(hard)


def test_vpn_cloud_proof_requires_new_task_nonce_allowed_and_denied():
    env=fixture_env()
    value={'schema':'codex-cloud-vpn-proof-v1','nonce':'fixture-nonce','environment_id':env['environment_id'],'new_cloud_task':True,'task_id':'11111111-1111-4111-8111-111111111111','task_url':'https://chatgpt.com/local/11111111-1111-4111-8111-111111111111','allowed':{'command_exit':0,'http':200,'schema':'codex-evidence-v1','generated_at':'2026-10-11T00:00:00Z'},'denied':{'port':8443,'command_exit':0,'http':503}}
    assert r.parse_proof(json.dumps(value),env,'fixture-nonce')['allowed']['http']==200
    for change in ({'nonce':'wrong'},{'new_cloud_task':False},{'new_cloud_task':'true'},{'task_id':'not-a-task'},{'task_url':'http://localhost/x'},{'allowed':dict(value['allowed'],http=403)},{'allowed':dict(value['allowed'],command_exit=False)},{'denied':{'port':8443,'http':503}},{'denied':dict(value['denied'],http=200)}):
        with pytest.raises(ValueError): r.parse_proof(json.dumps({**value,**change}),env,'fixture-nonce')


def test_vpn_native_runtime_id_and_null_url_require_creation_selector():
    env=fixture_env()
    value={'schema':'codex-cloud-vpn-proof-v1','nonce':'fixture-nonce','environment_id':'ccarenv_b64_fixture','new_cloud_task':True,'task_id':'11111111-1111-4111-8111-111111111111','task_url':None,'allowed':{'command_exit':0,'http':200,'schema':'codex-evidence-v1','generated_at':'2026-10-11T00:00:00Z'},'denied':{'port':8443,'command_exit':0,'http':503}}
    custody={'task_id':value['task_id'],'environment_config_id':env['environment_id'],'runtime_environment_id':value['environment_id']}
    assert r.parse_proof(json.dumps(value),env,'fixture-nonce',custody)['task_url'] is None
    assert r.parse_proof(json.dumps({**value,'native_creation':custody}),env,'fixture-nonce')['task_url'] is None
    for change in ({'task_id':'22222222-2222-4222-8222-222222222222'}, {'environment_config_id':'foreign-env'}, {'runtime_environment_id':'foreign-runtime'}):
        with pytest.raises(ValueError):
            r.parse_proof(json.dumps(value),env,'fixture-nonce',{**custody,**change})
    with pytest.raises(ValueError):
        r.parse_proof(json.dumps(value),env,'fixture-nonce')


def test_vpn_pending_proof_never_revokes_old_key(tmp_path,monkeypatch):
    env=fixture_env();path=tmp_path/'state.json'
    row={**env,'phase':'PROOF_PENDING','native_ticket':'fixture-ticket','key_id':'candidate-fixture','old_key_id':'old-fixture'}
    r.atomic(path,{'schema':1,'environments':{env['name']:row}})
    class FakeAPI:
        def __init__(self):self.revoked=[]
        def revoke(self,key):self.revoked.append(key)
    api=FakeAPI()
    monkeypatch.setattr(r,'ROOT',tmp_path);monkeypatch.setattr(r,'STATE',path)
    monkeypatch.setattr(r,'mint_due',lambda *a,**k:{})
    monkeypatch.setattr(r,'proof',lambda *a:None)
    result=r.run({'environments':[env]},path,api)
    assert result['environments'][0]['status']=='PROOF_PENDING'
    assert api.revoked==[]
