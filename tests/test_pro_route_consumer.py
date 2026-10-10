"""Native verified prep delivery: no workflow mutations and no blind retries."""
import hashlib,json,sys
from pathlib import Path
from contextlib import nullcontext
import pytest
from test_pro_route_backlog import load

class Client:
 role='fleet'
 def __init__(self):self.comments=[];self.writes=[];self.ambiguous=False
 def operation(self,name,variables):
  if name=='identity':return {'viewer':{'id':'fleet-author'}}
  assert name=='issue_get'
  return {'issue':{'id':'id-47','identifier':'GLP-47','team':{'key':'GLP'},'state':{'name':'In Progress'}}}
 def pages(self,q,v,path):return self.comments

def fixture(tmp_path,monkeypatch):
 m=load(tmp_path,monkeypatch);job='route-glp-47-prep';m.save({'job':job,'issue':'GLP-47','state':'complete','account':'acct2'})
 d=m.relay.JOBS/job/'outbox';d.mkdir(parents=True);(d/'ADVISORY.md').write_text('# Grounded prep\nExact files, risks and host proof commands.')
 files={'ADVISORY.md':m.hash_file(d/'ADVISORY.md')};(d/'OUTPUT-MANIFEST.json').write_text(json.dumps({'files':files}));(d.parent/'RESPONSE.md').write_text('native final marker')
 st={'job':job,'status':'done','source_reply_sha256':m.hash_file(d.parent/'RESPONSE.md'),'fetch_verification':{'state':'VERIFIED','manifest_sha256':m.hash_file(d/'OUTPUT-MANIFEST.json')}}
 m.relay._atomic_json(d.parent/'JOB.json',st)
 ctl=m.directory()/'CONSUMER.json';m.relay._atomic_json(ctl,{'schema':'pro-prep-consumer-v1','enabled':True,'jobs':[job]})
 return m,job,d

def test_verified_delivery_restart_and_comment_only(tmp_path,monkeypatch):
 m,job,d=fixture(tmp_path,monkeypatch);c=Client()
 def write(client,kind,inp):
  assert kind=='comment' and set(inp)=={'issueId','body'}
  client.writes.append(inp);r={'id':'comment-1','body':inp['body'],'issue':{'id':'id-47','identifier':'GLP-47'}};client.comments.append(r);return r
 first=m.consume_outboxes(client=c,writer=write);assert first[0]['state']=='delivered';assert len(c.writes)==1
 dest=tmp_path/'reports/ops/fleet-inbox/GLP-47/pro'/job
 assert (dest/'ADVISORY.md').read_bytes()==(d/'ADVISORY.md').read_bytes()
 assert m.consume_outboxes(client=c,writer=write)==[];assert len(c.writes)==1

def test_drift_refuses_before_comment(tmp_path,monkeypatch):
 m,job,d=fixture(tmp_path,monkeypatch);(d/'ADVISORY.md').write_text('changed');c=Client()
 assert m.consume_outboxes(client=c,writer=lambda *a: pytest.fail('unexpected write'))[0]['state']=='blocked';assert not c.writes

def test_ambiguous_post_reconciles_without_resend(tmp_path,monkeypatch):
 m,job,d=fixture(tmp_path,monkeypatch);c=Client()
 def ambiguous(client,kind,inp):
  client.writes.append(inp);client.comments.append({'id':'landed','body':inp['body'],'user':{'id':'fleet-author'},'issue':{'id':'id-47','identifier':'GLP-47'}});raise OSError('response lost')
 assert m.consume_outboxes(client=c,writer=ambiguous)[0]['state']=='uncertain'
 assert m.consume_outboxes(client=c,writer=ambiguous)[0]['state']=='delivered';assert len(c.writes)==1

def test_unknown_post_no_blind_retry(tmp_path,monkeypatch):
 m,job,d=fixture(tmp_path,monkeypatch);c=Client()
 def ambiguous(client,kind,inp):client.writes.append(inp);raise OSError('lost')
 assert m.consume_outboxes(client=c,writer=ambiguous)[0]['state']=='uncertain'
 assert m.consume_outboxes(client=c,writer=ambiguous)[0]['state']=='uncertain';assert len(c.writes)==1


def test_wrong_author_cannot_reconcile_uncertain_post(tmp_path,monkeypatch):
 m,job,d=fixture(tmp_path,monkeypatch);c=Client()
 def lost(client,kind,inp):
  client.writes.append(inp)
  client.comments.append({'id':'spoof','body':inp['body'],'user':{'id':'other'},'issue':{'id':'id-47'}})
  raise OSError('lost')
 assert m.consume_outboxes(client=c,writer=lost)[0]['state']=='uncertain'
 assert m.consume_outboxes(client=c,writer=lost)[0]['state']=='uncertain'
 assert len(c.writes)==1


def test_destination_symlink_refuses(tmp_path,monkeypatch):
 m,job,d=fixture(tmp_path,monkeypatch);c=Client()
 reports=tmp_path/'reports/ops';reports.mkdir(parents=True)
 other=tmp_path/'other';other.mkdir()
 (reports/'fleet-inbox').symlink_to(other,target_is_directory=True)
 assert m.consume_outboxes(client=c,writer=lambda *a:pytest.fail('unexpected write'))[0]['state']=='blocked'
 assert not list(other.iterdir())


def test_comment_uses_authenticated_bytes_despite_source_change(tmp_path, monkeypatch):
 m,job,d=fixture(tmp_path,monkeypatch);c=Client()
 original=(d/'ADVISORY.md').read_text()
 operation=c.operation
 def change_after_copy(name,variables):
  if name=='issue_get':
   (d/'ADVISORY.md').write_text('UNVERIFIED replacement after copy')
  return operation(name,variables)
 c.operation=change_after_copy
 def write(client,kind,inp):
  client.writes.append(inp)
  return {'id':'comment-1','body':inp['body'],'issue':{'id':'id-47'}}
 result=m.consume_outboxes(client=c,writer=write)
 assert result[0]['state']=='delivered'
 assert c.writes[0]['body'].endswith(original)
 assert 'UNVERIFIED replacement' not in c.writes[0]['body']


def test_dispatch_automatically_consumes(tmp_path,monkeypatch):
 m=load(tmp_path,monkeypatch);calls=[]
 monkeypatch.setattr(m,'dispatch',lambda **k:calls.append('dispatch') or [])
 monkeypatch.setattr(m,'consume_outboxes',lambda:calls.append('consume') or [])
 assert m.main(['dispatch'])==0
 assert calls==['dispatch','consume']
