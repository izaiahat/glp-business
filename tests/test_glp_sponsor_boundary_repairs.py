"""Offline independent-seam regressions; every transport and archive is synthetic."""
import datetime as dt
import importlib.util
import json
import sqlite3
import sys
import zipfile
from pathlib import Path
import pytest
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'scripts')]
spec = importlib.util.spec_from_file_location('sponsor_author_helpers', ROOT/'tests/test_glp_sponsor_campaign.py')
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)
m = helpers.m

@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
 import socket
 monkeypatch.setattr(socket, 'create_connection', lambda *a, **k: pytest.fail('NETWORK FORBIDDEN'))
 monkeypatch.setattr(m, 'ROOT', tmp_path/'source')
 monkeypatch.setattr(m, 'STATE', tmp_path/'state')
 monkeypatch.setattr(m, 'INBOX_DB', tmp_path/'inbox.sqlite')

def inbox(path, fresh=True):
 with sqlite3.connect(path) as db:
  db.execute('CREATE TABLE mailbox_actions(action_id TEXT,event_id TEXT,action TEXT,status TEXT,provider_receipt_json TEXT,created_at TEXT)')
  if fresh:
   db.execute('CREATE TABLE mailboxes(mailbox_id TEXT,lifecycle TEXT,freshness_sla_seconds INTEGER)')
   db.execute('CREATE TABLE provider_cursors(mailbox_id TEXT,cursor TEXT,last_auth_success_at TEXT,last_read_success_at TEXT,last_complete_poll_at TEXT,backlog_count INTEGER,unhealthy_reason TEXT,poll_receipt_id TEXT)')
   db.execute('CREATE TABLE inbox_runs(run_id TEXT PRIMARY KEY,finished_at TEXT,exit_status TEXT,discovered_count INTEGER,persisted_count INTEGER,duplicate_count INTEGER)')
   for box in ('glp_ops_gmail','glp_izaiah_gmail'):
    db.execute('INSERT INTO mailboxes VALUES(?,?,?)',(box,'active',900))
    stamp=helpers.SEND_SLOT.isoformat()
    db.execute('INSERT INTO provider_cursors VALUES(?,?,?,?,?,?,?,?)',(box,None,stamp,stamp,stamp,2,None,'fixture-'+box))
    db.execute('INSERT INTO inbox_runs VALUES(?,?,?,?,?,?)',('fixture-'+box,stamp,'pass',2,1,1))

def test_committed_optout_before_claim_prevents_followup(tmp_path, monkeypatch):
 config, client, rows = helpers.offline_campaign(tmp_path, monkeypatch)
 client.fail=None; rows[:]=rows[:1]
 config['require_canonical_reply_coverage']=True; inbox(m.INBOX_DB)
 m.save(m.STATE/'ledger.json',{rows[0]['email']:{'host':rows[0]['host'],'sent':[{'sent_at':(helpers.SEND_SLOT-dt.timedelta(days=4)).isoformat(),'thread_id':'old','rfc_message_id':'<old>'}]}})
 def admitted(*args):
  stop={'kind':'opt_out','event_id':'stop-1','provider_hosts':[rows[0]['host']],'enrolled_addresses':[rows[0]['email']]}
  with sqlite3.connect(m.INBOX_DB) as db:
   db.execute('INSERT INTO mailbox_actions VALUES(?,?,?,?,?,?)',('stop-1','stop-1','sponsor_reply_stop','verified',json.dumps(stop),'2026-10-06'))
  return True,''
 monkeypatch.setattr(m,'mail_admit',admitted)
 result=m.run(config)
 assert result['sent']==0 and client.deliveries==[]
 assert m.load(m.STATE/'ledger.json')[rows[0]['email']]['stopped']=='opt_out'

def test_canonical_send_boundary_blocks_concurrent_stop_commits(tmp_path):
 inbox(m.INBOX_DB)
 with m.canonical_send_boundary({},helpers.SEND_SLOT,True):
  with sqlite3.connect(m.INBOX_DB,timeout=0) as contender:
   with pytest.raises(sqlite3.OperationalError,match='locked'):
    contender.execute("INSERT INTO mailbox_actions VALUES('other','other','sponsor_reply_stop','verified','{}','now')")
 with sqlite3.connect(m.INBOX_DB,timeout=0) as contender:
  contender.execute("INSERT INTO mailbox_actions VALUES('other','other','sponsor_reply_stop','verified','{}','now')")


def test_empty_unpolled_actions_cannot_claim_complete_coverage(tmp_path,monkeypatch):
 config,client,rows=helpers.offline_campaign(tmp_path,monkeypatch)
 client.fail=None;rows[:]=rows[:1];config['require_canonical_reply_coverage']=True
 inbox(m.INBOX_DB,fresh=False)
 with pytest.raises(RuntimeError,match='canonical_sponsor_reply_coverage'):
  m.run(config)
 assert client.deliveries==[]

def test_loose_manifest_rehash_does_not_reapprove_changed_copy(tmp_path):
 required=['scripts/glp_sponsor_campaign.py','scripts/glp_sponsor_paypal.py','scripts/glp_sponsor_readiness.py','ops/sponsor-machine/campaign.json','ops/sponsor-machine/copy.json','inbox_control_plane/control_plane.py','inbox_control_plane/sponsor_replies.py']
 for rel in required:
  p=m.ROOT/rel;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('original fixture')
 manifest={rel:m.digest(m.ROOT/rel) for rel in required}
 m.save(m.ROOT/'ops/sponsor-machine/assets.json',manifest)
 packet=m.ROOT/'reports/ops/sponsor-machine-20261003/sponsor-review-packet.zip';packet.parent.mkdir(parents=True)
 with zipfile.ZipFile(packet,'w') as archive:
  for rel in required:archive.write(m.ROOT/rel,rel)
  archive.writestr('ops/sponsor-machine/assets.json',json.dumps(manifest))
 sha=m.digest(packet)
 config={'send_enabled':True,'packet_sha256':sha,'postal_address':'fixture address','require_full_source_binding':True}
 m.save(m.STATE/'approval.json',{'actor':'Izaiah Tilton','packet_sha256':sha,'decision':'APPROVE SPONSOR PACKET '+sha,'discord_message_id':'SYNTHETIC-NOT-APPROVAL'})
 assert m.approval_ready(config)
 changed=m.ROOT/'ops/sponsor-machine/copy.json';changed.write_text('unapproved fixture delta')
 manifest['ops/sponsor-machine/copy.json']=m.digest(changed)
 m.save(m.ROOT/'ops/sponsor-machine/assets.json',manifest)
 with pytest.raises(RuntimeError,match='approved_(packet|asset|manifest)'):
  m.approval_ready(config)

def test_self_authenticated_payment_and_relationship_never_count_as_evidence():
 import glp_sponsor_readiness as ready
 order={'reviewed_order':True,'order_reference':'R1','capture_id':'C1','merchant_id':'M1','currency':'USD','amount':'500.00','payee':'ops@glpclinicscout.com'}
 capture={'authenticated':True,'http_status':200,'environment':'live','capture':{'id':'C1','status':'COMPLETED','invoice_id':'R1','payee':{'merchant_id':'M1','email_address':'ops@glpclinicscout.com'},'amount':{'value':'500.00','currency_code':'USD'}}}
 assert ready.payment_readiness(order,lambda cid:capture)['payment_verified'] is False
 assert ready.relationship('example.invalid',{'provider_host':'example.invalid','status':'approved','network':'Invented','program_id':'P1','captured_at_utc':helpers.SEND_SLOT.isoformat(),'evidence_sha256':'a'*64},helpers.SEND_SLOT)['variant']=='cold'

def test_documented_professional_source_and_existing_bouncer_job_path(tmp_path,monkeypatch):
 import subprocess,hashlib
 config={'professional_enrichment_enabled':True,'bouncer_debits_enabled':True}
 discovery={'organizations':{}}
 plan={'recipients':[],'exclusions':[{'host':'example.com','reason':'no_verified_business_purpose_contact'}]}
 requests=[];verifications=[]
 def transport(method,path,payload):
  requests.append((method,path,payload))
  if path.endswith('/api_search'):return {'people':[{'id':'P1','title':'Head of Partnerships','has_email':True}]}
  assert path=='/api/v1/people/match' and payload['reveal_personal_emails'] is False and payload['reveal_phone_number'] is False
  return {'person':{'id':'P1','email':'jordan@example.com','title':'Head of Partnerships','country':'United States','organization':{'primary_domain':'example.com'}}}
 def runner(argv,**kwargs):
  assert argv[1]==str(m.ROOT/'scripts/bouncer_shared_account_guard.py')
  assert argv[2]=='verify-email' and argv[4]=='glp-scout'
  email=argv[6];verifications.append(email)
  shared=m.ROOT/'reports/bouncer-shared-credit-ledger-2026-06-25.jsonl';shared.parent.mkdir(parents=True,exist_ok=True)
  shared.write_text(json.dumps({'event':'verify_email','email_hash':hashlib.sha256(email.encode()).hexdigest(),'http_status':200,'provider_status':'deliverable','send_eligible':True,'blockers':[],'ts':helpers.SEND_SLOT.isoformat()})+'\n')
  return subprocess.CompletedProcess(argv,0,json.dumps({'send_eligible':True}),'')
 result=m.refresh_professional_sources(plan,discovery,helpers.SEND_SLOT,config,transport=transport,bouncer_runner=runner)
 assert result['enrichment_requests']==1 and verifications==['jordan@example.com']
 assert discovery['organizations']['example.com']['professional_contacts'][0]['email']=='jordan@example.com'
 again=m.refresh_professional_sources(plan,discovery,helpers.SEND_SLOT,config,transport=transport,bouncer_runner=runner)
 assert len(requests)==2 and len(verifications)==1
 assert again['enrichment_requests']==0
 disabled=m.refresh_professional_sources(plan,discovery,helpers.SEND_SLOT,{},transport=lambda *a:pytest.fail('No unapproved debit'))
 assert disabled['status']=='disabled_pending_approval'


@pytest.mark.parametrize('outcome',['uncertain_match','uncertain_verify','completed_verify'])
def test_run_discovery_ttl_preserves_paid_attempt_custody(tmp_path,monkeypatch,outcome):
 import subprocess
 import glp_sponsor_readiness as ready
 config,client,_=helpers.offline_campaign(tmp_path,monkeypatch)
 config.update(professional_enrichment_enabled=True,bouncer_debits_enabled=True,apollo_read_only_discovery=True)
 plan={'recipients':[],'exclusions':[{'host':'example.invalid','reason':'no_verified_business_purpose_contact'}],
       'registry_rows':1,'candidate_rows':1}
 monkeypatch.setattr(m,'recipients',lambda *a:plan)
 monkeypatch.setattr(m,'service',lambda *a:pytest.fail('NO LIVE INBOX OR SEND'))
 searches=[];matches=[];verifies=[]
 def transport(method,path,payload):
  assert method=='POST'
  if path.endswith('/api_search'):
   searches.append(m.now());return {'people':[{'id':'P1','title':'Head of Partnerships'}]}
  assert path=='/api/v1/people/match'
  assert all(payload[k] is False for k in ('reveal_personal_emails','reveal_phone_number','run_waterfall_email','run_waterfall_phone'))
  matches.append(payload['id'])
  if outcome=='uncertain_match':raise OSError('SYNTHETIC paid match result unknown')
  return {'person':{'id':'P1','email':'jordan@example.invalid','title':'Head of Partnerships',
                    'country':'United States','organization':{'primary_domain':'example.invalid'}}}
 def runner(argv,**kwargs):
  assert argv[1]==str(m.ROOT/'scripts/bouncer_shared_account_guard.py') and argv[2]=='verify-email'
  verifies.append(argv[6])
  if outcome=='uncertain_verify':raise subprocess.TimeoutExpired('SYNTHETIC verification',90)
  return subprocess.CompletedProcess(argv,3,'SYNTHETIC blocked verification','')
 monkeypatch.setattr(ready,'apollo_transport',transport)
 monkeypatch.setattr(subprocess,'run',runner)
 cache=m.STATE/'apollo-sourcing.private.json'
 assert m.run(config)['sent']==0
 before=m.load(cache)['example.invalid']
 custody_keys=('attempted_at','search_response','enrichment_claim','enrichment_response',
               'professional_contacts','verification_claim','verification_attempted_at')
 custody={key:before[key] for key in custody_keys if key in before}
 assert custody['attempted_at']==helpers.SEND_SLOT.isoformat() and matches==['P1']
 if outcome=='uncertain_match':assert custody['enrichment_claim']=={'person_id':'P1','at':helpers.SEND_SLOT.isoformat()}
 elif outcome=='uncertain_verify':assert custody['verification_claim']['at']==helpers.SEND_SLOT.isoformat()
 else:assert custody['verification_attempted_at']==helpers.SEND_SLOT.isoformat()
 expected_verifies=[] if outcome=='uncertain_match' else ['jordan@example.invalid']
 assert verifies==expected_verifies and len(searches)==2
 assert m.run(config)['sent']==0 and len(searches)==2 # Same-day hold is not TTL proof.
 for days in (8,16,31):
  later=(helpers.SEND_SLOT.astimezone(m.PT)+dt.timedelta(days=days)).astimezone(dt.timezone.utc)
  monkeypatch.setattr(m,'now',lambda:later)
  result=m.run(config)
  assert result['sent']==0 and result['sourcing_readiness']['requests']==1
  after=m.load(cache)['example.invalid']
  assert {key:after[key] for key in custody_keys if key in after}==custody
  assert after['captured_at_utc']==later.isoformat() # Discovery may refresh, paid custody may not.
  assert matches==['P1'] and verifies==expected_verifies
 assert len(searches)==5 and client.deliveries==[]


@pytest.mark.parametrize('sender,expected_stops',[('legal@example.invalid',0),('partners@example.invalid',1)])
def test_domain_only_privacy_mail_keeps_existing_escalation(tmp_path,monkeypatch,sender,expected_stops):
 from inbox_control_plane import control_plane as cp,sponsor_replies as sr
 from email.message import EmailMessage
 for name in ('PRIVATE_ROOT','BACKUP_ROOT','REPORT_ROOT','DAILY_AUDIT_DIR','STATE_ROOT','RAW_ROOT','ATTACHMENT_ROOT'):
  monkeypatch.setattr(cp,name,tmp_path/name.lower())
 enrollment=tmp_path/'enrollment.json';enrollment.write_text(json.dumps({'partners@example.invalid':{'host':'example.invalid','sent':[{'sent_at':helpers.SEND_SLOT.isoformat(),'thread_id':'old'}]}}))
 monkeypatch.setattr(sr,'LEDGER_PATH',enrollment)
 monkeypatch.setattr(cp,'run_fable_email_drafter',lambda *a,**k:pytest.fail('NO DRAFT'))
 box={'mailbox_id':'glp_ops_gmail','venture':'glp_affiliate','company':'GLP','canonical_address':'ops@glpclinicscout.com','aliases':['partners@glpclinicscout.com'],'provider':'gmail','adapter':'gmail','auth_reference':'fixture','owner':'Izaiah','lifecycle':'active','poll_seconds':300,'freshness_sla_seconds':900,'inbound_surface':'email','routing_policy':'fixture'}
 store=cp.Ledger(tmp_path/'canonical.sqlite',email_flow_db_path=tmp_path/'flow.sqlite')
 try:
  store.sync_registry({'mailboxes':[box]})
  msg=EmailMessage();msg['From']=sender;msg['To']='ops@glpclinicscout.com';msg['Date']='Tue, 06 Oct 2026 16:00:00 +0000';msg['Subject']='CCPA privacy request';msg['Message-ID']='<privacy@example.invalid>';msg.set_content('Formal privacy request: delete personal information. This is a legal notice, not a sponsorship reply.')
  store.ingest(box,'privacy-fixture',msg.as_bytes(),provider_thread_id='unrelated-thread')
  row=store.conn.execute('SELECT category,route FROM inbox_classifications').fetchone()
  assert row['category']=='legal_compliance' and row['route']!='silent'
  assert store.conn.execute("SELECT count(*) FROM mailbox_actions WHERE action='sponsor_reply_stop'").fetchone()[0]==expected_stops
 finally:store.close()
