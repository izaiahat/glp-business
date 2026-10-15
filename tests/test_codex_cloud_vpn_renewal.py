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


def fixture_denial_custody(value, env):
    """Forged negative input; not authenticated custody or a Fable receipt."""
    import hashlib
    return {'task_id':value['task_id'],'runtime_environment_id':value['environment_id'],
            'environment_config_id':env['environment_id'],'nonce':value['nonce'],
            'method':'HEAD','target':'https://hermes-01.tail9e25df.ts.net:8443/',
            **value['denied'],'enforcement':'tailscale_acl',
            'accepted_by':'designated independent Fable reviewer',
            'receipt_sha256':hashlib.sha256(b'offline synthetic denial acceptance').hexdigest()}


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
    with pytest.raises(ValueError,match='head_denial_authenticated_witness_unavailable'):
        r.parse_proof(json.dumps(value),env,'fixture-nonce',denial_custody=fixture_denial_custody(value,env))
    for change,reason in (({'nonce':'wrong'},'cloud_proof_binding_mismatch'),({'new_cloud_task':False},'new_cloud_task_custody_missing'),({'new_cloud_task':'true'},'new_cloud_task_custody_missing'),({'task_id':'not-a-task'},'cloud_task_id_invalid'),({'task_url':'http://localhost/x'},'new_cloud_task_custody_missing'),({'allowed':dict(value['allowed'],http=403)},'allowed_cloud_get_not_proved'),({'allowed':dict(value['allowed'],command_exit=False)},'allowed_cloud_get_not_proved'),({'denied':{'port':8443,'http':503}},'proof_fields_not_allowlisted')):
        with pytest.raises(ValueError,match=reason):
            r.parse_proof(json.dumps({**value,**change}),env,'fixture-nonce',denial_custody=fixture_denial_custody(value,env))


def test_vpn_native_runtime_id_and_null_url_require_creation_selector():
    env=fixture_env()
    value={'schema':'codex-cloud-vpn-proof-v1','nonce':'fixture-nonce','environment_id':'ccarenv_b64_fixture','new_cloud_task':True,'task_id':'11111111-1111-4111-8111-111111111111','task_url':None,'allowed':{'command_exit':0,'http':200,'schema':'codex-evidence-v1','generated_at':'2026-10-11T00:00:00Z'},'denied':{'port':8443,'command_exit':0,'http':503}}
    custody={'task_id':value['task_id'],'environment_config_id':env['environment_id'],'runtime_environment_id':value['environment_id']}
    denial=fixture_denial_custody(value,env)
    with pytest.raises(ValueError,match='head_denial_authenticated_witness_unavailable'):
        r.parse_proof(json.dumps(value),env,'fixture-nonce',custody,denial)
    with pytest.raises(ValueError,match='head_denial_authenticated_witness_unavailable'):
        r.parse_proof(json.dumps({**value,'native_creation':custody}),env,'fixture-nonce',denial_custody=denial)
    for change in ({'task_id':'22222222-2222-4222-8222-222222222222'}, {'environment_config_id':'foreign-env'}, {'runtime_environment_id':'foreign-runtime'}):
        with pytest.raises(ValueError,match='native_creation_selector_mismatch'):
            r.parse_proof(json.dumps(value),env,'fixture-nonce',{**custody,**change})
    with pytest.raises(ValueError,match='cloud_proof_binding_mismatch'):
        r.parse_proof(json.dumps(value),env,'fixture-nonce')


@pytest.mark.parametrize('http', [403, 503])
def test_vpn_raw_head_status_is_not_policy_denial(http, tmp_path, monkeypatch):
    """A proxy/origin status must not unlock CLOUD_PROVED/key revocation."""
    env=fixture_env()
    value={'schema':'codex-cloud-vpn-proof-v1','nonce':'fixture-nonce','environment_id':env['environment_id'],'new_cloud_task':True,'task_id':'11111111-1111-4111-8111-111111111111','task_url':'https://chatgpt.com/local/11111111-1111-4111-8111-111111111111','allowed':{'command_exit':0,'http':200,'schema':'codex-evidence-v1','generated_at':'2026-10-11T00:00:00Z'},'denied':{'port':8443,'command_exit':0,'http':http}}
    with pytest.raises(ValueError, match='head_denial_attribution_required'):
        r.parse_proof(json.dumps(value),env,'fixture-nonce')
    # F1 regression: labels and a syntactically valid hash are not a witness.
    denial=fixture_denial_custody(value,env)
    with pytest.raises(ValueError, match='head_denial_authenticated_witness_unavailable'):
        r.parse_proof(json.dumps(value),env,'fixture-nonce',denial_custody=denial)
    # A worker may not award itself independently accepted denial custody.
    with pytest.raises(ValueError, match='proof_fields_not_allowlisted'):
        r.parse_proof(json.dumps({**value,'head_denial_custody':denial}),env,'fixture-nonce')
    # Exercise the actual proof consumer too: raw status cannot advance phase.
    import types
    monkeypatch.setitem(sys.modules,'dot_dispatch',types.SimpleNamespace())
    monkeypatch.setitem(sys.modules,'dot_lane',types.SimpleNamespace(terminal=lambda *a:{'outcome':'result','text':json.dumps(value)}))
    monkeypatch.setattr(r,'ROOT',tmp_path)
    monkeypatch.setattr(r,'STATE',tmp_path/'state.json')
    directory=tmp_path/'reports/ops/dot-tickets/fixture-ticket'
    directory.mkdir(parents=True)
    (directory/'TICKET.json').write_text(json.dumps({'dot':'rook','task_owner':'R1','request_id':'fixture-request','parent':'fable-plan-2-20261009','state':'complete','reply_id':'fixture-reply'}))
    row:dict={'phase':'PROOF_PENDING','native_ticket':'fixture-ticket','native_request_id':'fixture-request','proof_nonce':'fixture-nonce'}
    state={'environments':{env['name']:row}}
    with pytest.raises(ValueError, match='head_denial_attribution_required'):
        r.proof(env,row,state)
    assert row['phase']=='PROOF_PENDING' and not r.STATE.exists()
    row['head_denial_custody']=denial
    with pytest.raises(ValueError, match='head_denial_authenticated_witness_unavailable'):
        r.proof(env,row,state)
    assert row['phase']=='PROOF_PENDING' and not r.STATE.exists()
    # Forged persisted custody must not unlock metadata/revocation either.
    row.update(phase='CLOUD_PROVED',cloud_proof=value,key_id='fixture-new',old_key_id='fixture-old')
    r.atomic(r.STATE,state)
    effects=[]
    api=types.SimpleNamespace(metadata=lambda *a:effects.append('metadata'),revoke=lambda *a:effects.append('revoke'))
    monkeypatch.setattr(r,'mint_due',lambda *a,**k:{})
    monkeypatch.setattr(r,'alert_failure',lambda *a:None)
    result=r.run({'environments':[env]},r.STATE,api)
    assert result['environments'][0]['reason']=='head_denial_authenticated_witness_unavailable'
    assert result['status']=='FAILED' and effects==[]


def test_vpn_cli_defers_when_heavy_seat_is_owned_elsewhere(monkeypatch):
    import types
    calls=[]
    fake=types.SimpleNamespace(acquire=lambda *a:1,release=lambda *a:calls.append('release'))
    monkeypatch.setitem(sys.modules,'candidate_build_lock',fake)
    monkeypatch.setattr(r,'acquire_free',lambda *a:1)
    monkeypatch.delenv('CANDIDATE_BUILD_SEAT',raising=False)
    monkeypatch.setattr(r,'run_cli',lambda:calls.append('effect'))
    assert r.main()==0
    assert calls==[]


def test_vpn_cli_releases_its_own_heavy_seat(monkeypatch):
    import types
    calls=[]
    fake=types.SimpleNamespace(acquire=lambda *a:0,release=lambda *a:calls.append('release'))
    monkeypatch.setitem(sys.modules,'candidate_build_lock',fake)
    monkeypatch.setattr(r,'acquire_free',lambda *a:0)
    monkeypatch.delenv('CANDIDATE_BUILD_SEAT',raising=False)
    monkeypatch.setattr(r,'run_cli',lambda:0)
    assert r.main()==0
    assert calls==['release']


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
