import datetime as dt
import json
import sys
import base64
from email.parser import BytesParser
from email.policy import default
from types import ModuleType
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import glp_sponsor_campaign as m
import glp328_outreach_policy as policy

@pytest.fixture(autouse=True)
def isolated_inbox(tmp_path,monkeypatch):
 monkeypatch.setattr(m,'INBOX_DB',tmp_path/'no-live-inbox.sqlite')
 monkeypatch.setattr(m,'ROOT',tmp_path/'no-live-source')

NOW=dt.datetime(2026,10,3,tzinfo=dt.timezone.utc)

def inputs():
 row=['']*27
 row[2]='Example GLP-1 Provider (US)';row[3]='GLP-1';row[6]='https://example.com';row[7]='US GLP-1 provider';row[10]='approved_active_not_wired';row[25]='glp1-weightloss'
 sheet=[list(policy.EXPECTED_HEADERS),row]
 import hashlib
 html='<p>Commercial partnerships: <a href="mailto:partners@example.com">partners@example.com</a></p>';sha=hashlib.sha256(html.encode()).hexdigest()
 evidence={'address':'partners@example.com','how':'mailto','found_on':'https://example.com/partners','page_sha256':sha,'captured_at_utc':NOW.isoformat()}
 discovery={'organizations':{'example.com':{'discovery':{'contact_email':'partners@example.com','contact_tier':'partnerships','contact_evidence':evidence,'_markup':[{'url':evidence['found_on'],'final_url':evidence['found_on'],'html':html,'sha256':sha}]}}}}
 local={'suppression':{'complete':True,'entries':[]},'conversations':[]}
 return sheet,discovery,local

def test_failure_diagnostic_preserves_code_origin_and_no_replay(tmp_path,monkeypatch,capsys):
 monkeypatch.setattr(m,'STATE',tmp_path)
 monkeypatch.setattr(m,'campaign_config',lambda:{})
 monkeypatch.setattr(sys,'argv',['sponsor','--scheduled'])
 calls=[]
 def injected_failure(*args,**kwargs):
  calls.append('run')
  try:raise ValueError('private upstream body')
  except ValueError as cause:raise RuntimeError('canonical_sponsor_reply_coverage_unknown') from cause
 monkeypatch.setattr(m,'run',injected_failure)
 monkeypatch.setattr(m,'send_message',lambda *a,**kw:pytest.fail('NO SEND'))
 for _ in range(2):
  with pytest.raises(SystemExit) as error:m.main()
  assert error.value.code==1
  failure=m.load(tmp_path/'latest-failure.json')
  assert failure['error_code']=='canonical_sponsor_reply_coverage_unknown'
  assert failure['message']=='canonical_sponsor_reply_coverage_unknown'
  assert failure['origin']['filename']==Path(__file__).name
  assert failure['origin']['function']=='injected_failure'
  assert isinstance(failure['origin']['line'],int) and failure['origin']['line']>0
  assert failure['exception_chain']==['RuntimeError','ValueError']
  assert failure['replay_allowed'] is False
  assert failure['day_sends'] is None and failure['outstanding_claims'] is None
 output=capsys.readouterr()
 assert 'canonical_sponsor_reply_coverage_unknown' in output.err and 'RuntimeError' in output.err
 assert 'private upstream body' not in output.err
 assert calls==['run','run'] and not (tmp_path/'ledger.json').exists()


@pytest.mark.parametrize('mode',['--scheduled','--transport-preflight','--dry-run'])
def test_failure_redacts_hostile_text_in_every_entry_mode(tmp_path,monkeypatch,capsys,mode):
 monkeypatch.setattr(m,'STATE',tmp_path)
 monkeypatch.setattr(m,'campaign_config',lambda:{})
 monkeypatch.setattr(sys,'argv',['sponsor',mode])
 hostile='canonical_sponsor_reply_coverage_unknown\nAuthorization: Bearer token_SUPERSECRET contact@private.invalid '+('HTTP_BODY'*1000)
 def failed(*args,**kwargs):raise RuntimeError(hostile)
 monkeypatch.setattr(m,'run',failed)
 with pytest.raises(SystemExit) as error:m.main()
 assert error.value.code==1
 failure=m.load(tmp_path/'latest-failure.json');output=capsys.readouterr()
 assert failure['error_code']=='sponsor_unexpected_failure'
 assert failure['message']=='[redacted unexpected exception]'
 assert failure['origin']['function']=='failed'
 assert len(failure['message'])<=160
 for secret in ('token_SUPERSECRET','contact@private.invalid','HTTP_BODY','Authorization'):
  assert secret not in json.dumps(failure)+output.err+output.out
 assert 'sponsor_unexpected_failure' in output.err
 assert failure['replay_allowed'] is False and failure['day_sends'] is None


@pytest.mark.parametrize('reason',['approved_asset_changed','current_packet_installed_binding_changed','suppression_ledger_incomplete'])
def test_allowlisted_detail_prefix_redacts_entire_suffix(reason):
 receipt=m.safe_failure_diagnostic(RuntimeError(reason+':Bearer token_SECRET contact@private.invalid\nHTTP BODY'))
 assert receipt['error_code']==reason
 assert receipt['message']==reason+':[redacted details]'
 assert 'token_SECRET' not in json.dumps(receipt) and 'private.invalid' not in json.dumps(receipt)


def test_config_failure_uses_same_safe_diagnostic(tmp_path,monkeypatch,capsys):
 monkeypatch.setattr(m,'STATE',tmp_path)
 monkeypatch.setattr(sys,'argv',['sponsor','--transport-preflight'])
 def broken_config():raise ValueError('sponsor_activation_invalid')
 monkeypatch.setattr(m,'campaign_config',broken_config)
 monkeypatch.setattr(m,'run',lambda *a,**kw:pytest.fail('CONFIG MUST FAIL FIRST'))
 with pytest.raises(SystemExit) as error:m.main()
 assert error.value.code==1
 assert m.load(tmp_path/'latest-failure.json')['origin']['function']=='broken_config'
 assert 'sponsor_activation_invalid' in capsys.readouterr().err


@pytest.mark.parametrize('broken',['helper','serialization','storage'])
def test_diagnostics_failure_still_exits_one(tmp_path,monkeypatch,capsys,broken):
 monkeypatch.setattr(m,'STATE',tmp_path)
 monkeypatch.setattr(m,'campaign_config',lambda:{})
 monkeypatch.setattr(sys,'argv',['sponsor','--scheduled'])
 def failed(*args,**kwargs):raise RuntimeError('canonical_sponsor_reply_coverage_unknown')
 monkeypatch.setattr(m,'run',failed)
 if broken=='helper':monkeypatch.setattr(m,'safe_failure_diagnostic',failed)
 else:
  def broken_save(*args):
   if broken=='serialization':raise TypeError('private serialization body')
   raise OSError('private storage body')
  monkeypatch.setattr(m,'save',broken_save)
 with pytest.raises(SystemExit) as error:m.main()
 assert error.value.code==1
 output=capsys.readouterr()
 assert 'Sponsor outreach FAILED' in output.err and 'private' not in output.err
 assert not (tmp_path/'ledger.json').exists()


def test_new_provider_capacity_is_separate_from_followups(tmp_path,monkeypatch):
 config,client,rows=offline_campaign(tmp_path,monkeypatch);client.fail=None
 config.update(new_provider_daily_cap=25,follow_up_daily_cap=25)
 m.save(m.STATE/'ledger.json',{rows[0]['email']:{'host':rows[0]['host'],'sent':[{'sent_at':(SEND_SLOT-dt.timedelta(days=4)).isoformat(),'thread_id':'old','rfc_message_id':'<old>'}]}})
 receipt=m.run(config)
 assert receipt['sent']==26
 assert receipt['new_provider_count']==25
 assert receipt['follow_up_count']==1
 assert m.run(config)['sent']==0 # Repeated run cannot refill the new-provider allowance.


def test_stopped_contacts_do_not_consume_new_provider_allowance(tmp_path,monkeypatch):
 config,client,rows=offline_campaign(tmp_path,monkeypatch);client.fail=None
 config.update(new_provider_daily_cap=25,follow_up_daily_cap=25)
 stopped={r['email'] for r in rows[:2]}
 monkeypatch.setattr(m,'mail_admit',lambda r,p,c:(r['email'] not in stopped,'fixture_prior_reply'))
 result=m.run(config)
 assert result['sent']==25
 assert result['new_provider_count']==25
 assert len(client.deliveries)==25


@pytest.mark.parametrize('late_change',['expiry','latest_negative'])
def test_final_verifier_reads_boundary_time_and_latest_decision(tmp_path,monkeypatch,late_change):
 import glp_sponsor_readiness as ready
 config,client,rows=offline_campaign(tmp_path,monkeypatch);client.fail=None;rows[:]=rows[:1]
 config['require_bouncer']=True
 path=m.ROOT/'reports/bouncer-shared-credit-ledger-2026-06-25.jsonl'
 evidence={'email_hash':ready.email_hash(rows[0]['email']),'event':'verify_email','http_status':200,'provider_status':'deliverable','send_eligible':True,'blockers':[],'ts':(SEND_SLOT-dt.timedelta(days=7)+dt.timedelta(seconds=1)).isoformat()}
 path.write_text(json.dumps(evidence)+'\n')
 clock=[SEND_SLOT];monkeypatch.setattr(m,'now',lambda:clock[0])
 def blocking_mail(*a):
  if late_change=='expiry':clock[0]=SEND_SLOT+dt.timedelta(seconds=2)
  else:path.write_text(json.dumps(evidence)+'\n'+json.dumps({**evidence,'send_eligible':False,'provider_status':'undeliverable','blockers':['negative'],'ts':SEND_SLOT.isoformat()})+'\n')
  return True,''
 monkeypatch.setattr(m,'mail_admit',blocking_mail)
 result=m.run(config)
 assert result['sent']==0 and not client.deliveries
 assert not any(row.get('claimed') for row in m.load(m.STATE/'ledger.json').values()) if (m.STATE/'ledger.json').exists() else True


def test_bouncer_cache_unknown_and_latest_negative_fail_closed(tmp_path):
 import glp_sponsor_readiness as ready
 path=tmp_path/'bouncer.jsonl';email='partners@example.com'
 assert ready.bouncer_readiness(email,path,NOW)['status']=='unknown'
 row={'email_hash':ready.email_hash(email),'event':'verify_email','http_status':200,'provider_status':'deliverable','send_eligible':True,'blockers':[],'ts':NOW.isoformat()}
 path.write_text(json.dumps(row)+'\n')
 assert ready.bouncer_readiness(email,path,NOW)['status']=='verified'
 path.write_text(json.dumps(row)+'\n'+json.dumps(dict(row,send_eligible=False,provider_status='unknown'))+'\n')
 assert ready.bouncer_readiness(email,path,NOW)['status']=='blocked'
 assert ready.bouncer_readiness(email,path,NOW+dt.timedelta(days=8))['status']=='expired'


def test_apollo_search_is_read_only_and_unrevealed_email_unknown():
 import glp_sponsor_readiness as ready
 calls=[]
 def transport(method,path,payload):
  calls.append((method,path,payload));return {'people':[{'id':'p1','title':'CEO','organization':{'primary_domain':'example.com'}}]}
 result=ready.apollo_search('example.com',transport)
 assert result['contacts'][0]['verification_status']=='unknown'
 assert result['contacts'][0].get('email') is None
 assert calls[0][1]=='/api/v1/mixed_people/api_search'
 assert result['enrichment_debits']==0


def test_existing_stock_without_public_proof_is_held_and_persisted(tmp_path):
 import copy
 sheet,discovery,local=inputs()
 org=discovery['organizations']['example.com']
 public=copy.deepcopy(org['discovery']['contact_evidence'])
 apollo={'email':'unpublished@example.com','source':'apollo','person_id':'p1'}
 org['professional_contacts']=[apollo]
 before=copy.deepcopy(org['discovery'])
 assert m.reconcile_public_stock(discovery,NOW)==1
 assert apollo['stock_status']=='HELD_NOT_PUBLIC'
 assert org['discovery']['contact_evidence']==public
 assert org['discovery']['_markup']==before['_markup']
 p=tmp_path/'discovery.json';m.save(p,discovery)
 assert m.load(p)['organizations']['example.com']['professional_contacts'][0]['stock_status']=='HELD_NOT_PUBLIC'
 # A valid published contact remains available; an enriched email never wins.
 result=m.recipients(sheet,discovery,local)
 assert [x['email'] for x in result['recipients']]==[public['address']]
 org['discovery']['_markup']=[]
 assert m.reconcile_public_stock(discovery,NOW)==2
 assert org['discovery']['contact_evidence']['stock_status']=='HELD_NOT_PUBLIC'
 assert not m.recipients(sheet,discovery,local)['recipients']


def test_professional_contact_and_relationship_require_exact_current_evidence():
 import glp_sponsor_readiness as ready
 sheet,discovery,local=inputs();discovery['organizations']['example.com']['discovery']={}
 professional={'email':'izaiah@example.com','country':'US','title':'Head of Partnerships','organization_domain':'example.com','source':'apollo','person_id':'p1','captured_at_utc':NOW.isoformat(),'evidence_sha256':'b'*64}
 discovery['organizations']['example.com']['professional_contacts']=[professional]
 result=m.recipients(sheet,discovery,local)
 assert not result['recipients']
 assert result['exclusions'][-1]['status']=='HELD_NOT_PUBLIC'
 professional['title']='Patient Support';assert not m.recipients(sheet,discovery,local)['recipients']
 evidence={'provider_host':'example.com','network':'Impact','program_id':'p1','status':'approved','captured_at_utc':NOW.isoformat(),'evidence_sha256':'c'*64}
 assert ready.relationship('example.com',evidence,NOW)['variant']=='cold' # caller-created digest is not authenticated network custody
 assert ready.relationship('elsewhere.com',evidence,NOW)['variant']=='cold'
 assert ready.relationship('example.com',evidence,NOW+dt.timedelta(days=31))['variant']=='cold'


def test_second_contact_cannot_reenroll_provider_and_missing_verification_cannot_send(tmp_path,monkeypatch):
 old={'email':'partners@ww.com','host':'ww.com'}
 ledger={old['email']:{'host':'ww.com','sent':[{'sent_at':NOW.isoformat()}]}}
 assert m.due({'email':'marketing@weightwatchers.com','host':'weightwatchers.com'},ledger,NOW+dt.timedelta(days=15)) is None
 config,client,rows=offline_campaign(tmp_path,monkeypatch);client.fail=None
 config.update(require_bouncer=True)
 result=m.run(config)
 assert result['sent']==0 and not client.deliveries
 assert result['verification_readiness']['unknown']==27


def test_paypal_readiness_requires_authenticated_matching_completed_capture():
 import glp_sponsor_readiness as ready
 order=dict(order_reference='GLP-001',currency='USD',amount='500.00',payee='ops@glpclinicscout.com',capture_id='CAPTURE1',merchant_id='MERCHANT1',reviewed_order=True)
 assert ready.payment_readiness(order)['status']=='unknown'
 capture=dict(id='CAPTURE1',status='COMPLETED',amount=dict(value='500.00',currency_code='USD'),invoice_id='GLP-001',payee=dict(email_address='ops@glpclinicscout.com',merchant_id='MERCHANT1'))
 transport=lambda cid:dict(http_status=200,authenticated=True,environment='live',capture=capture)
 assert ready.payment_readiness(order,transport)['payment_verified'] is False # synthetic authenticated flag is not provider allocation evidence
 capture['amount']['value']='250.00'
 assert ready.payment_readiness(order,transport)['payment_verified'] is False
 capture['amount']['value']='500.00';capture['status']='REFUNDED'
 assert ready.payment_readiness(order,transport)['payment_verified'] is False
 capture['status']='COMPLETED'
 assert not ready.payment_readiness(order,lambda cid:dict(authenticated=False,capture=capture))['payment_verified']


def test_disabled_does_not_read_approval(tmp_path,monkeypatch):
 monkeypatch.setattr(m,'STATE',tmp_path)
 assert m.approval_ready({'send_enabled':False}) is False
 with pytest.raises(FileNotFoundError):m.approval_ready({'send_enabled':True})

def test_registry_suppression_hold_and_business_purpose():
 sheet,discovery,local=inputs()
 assert len(m.recipients(sheet,discovery,local)['recipients'])==1
 local['suppression']['entries']=[{'domain':'example.com'}]
 assert not m.recipients(sheet,discovery,local)['recipients']
 local['suppression']['entries']=[];sheet[1][10]='hold'
 assert not m.recipients(sheet,discovery,local)['recipients']
 sheet[1][10]='approved_active_not_wired';discovery['organizations']['example.com']['discovery']['contact_email']='support@example.com'
 discovery['organizations']['example.com']['discovery']['contact_evidence']['address']='support@example.com'
 assert not m.recipients(sheet,discovery,local)['recipients']

def test_followup_schedule_stop_and_uncertain_claim():
 r={'email':'partners@example.com'};ledger={}
 assert m.due(r,ledger,NOW)==0
 ledger[r['email']]={'sent':[{'sent_at':NOW.isoformat()}]}
 assert m.due(r,ledger,NOW+dt.timedelta(days=3)) is None
 assert m.due(r,ledger,NOW+dt.timedelta(days=4))==1
 ledger[r['email']]['sent'].append({'sent_at':(NOW+dt.timedelta(days=4)).isoformat()})
 assert m.due(r,ledger,NOW+dt.timedelta(days=9)) is None
 assert m.due(r,ledger,NOW+dt.timedelta(days=10))==2
 ledger[r['email']]['stopped']='reply'
 assert m.due(r,ledger,NOW+dt.timedelta(days=10)) is None
 ledger[r['email']].pop('stopped');ledger[r['email']]['claimed']={'stage':2}
 assert m.due(r,ledger,NOW+dt.timedelta(days=10)) is None

def test_dryrun_exercises_entry_without_transport(tmp_path,monkeypatch):
 sheet,discovery,local=inputs();monkeypatch.setattr(m,'STATE',tmp_path)
 monkeypatch.setattr(m,'INBOX_POLL_LOCK',tmp_path/'poll.lock')
 monkeypatch.setattr(m,'read_values',lambda:sheet);monkeypatch.setattr(m,'read_local',lambda:local)
 monkeypatch.setattr(m,'load',lambda p: discovery if 'discovery' in str(p) else {})
 monkeypatch.setattr(m,'render',lambda r,s,c:dict(to=r['email'],stage=s))
 monkeypatch.setattr(m,'service',lambda *a:pytest.fail('NO AUTH OR SEND IN DRYRUN'))
 result=m.run({'send_enabled':True,'batch_cap':25},dry_run=True)
 assert result['sent']==0 and result['send_enabled'] is False and result['first_batch_count']==1
 assert not (tmp_path/'ledger.json').exists()

def test_organization_identity_alias_and_fella():
 assert m.same('weightwatchers.com','ww.com')
 sheet,discovery,local=inputs();sheet[1][2]='Fella Health';assert not m.recipients(sheet,discovery,local)['recipients']

SEND_SLOT=dt.datetime(2026,10,6,16,tzinfo=dt.timezone.utc) # Tuesday 09:00 PT

def offline_campaign(tmp_path,monkeypatch):
 """Exercise run/send_message; replace only external inputs and mail transport."""
 monkeypatch.setattr(m,'INBOX_POLL_LOCK',tmp_path/'canonical-poll.lock')
 root=tmp_path/'source';state=tmp_path/'state'
 assets=root/'reports/ops/sponsor-machine-20261003/assets';assets.mkdir(parents=True)
 for name in ('rate-card.pdf','placement-template.pdf','promo-page-preview.pdf'):
  (assets/name).write_bytes(b'offline attachment fixture')
 copy=root/'ops/sponsor-machine/copy.json';copy.parent.mkdir(parents=True)
 copy.write_text(json.dumps({'sequence':[{'subject':'Offline sponsorship','body':'Hello {organization}'}]*3}))
 discovery=root/'reports/ops/sponsor-machine-20261003/contact-discovery.private.json'
 discovery.write_text('{}')
 monkeypatch.setattr(m,'ROOT',root);monkeypatch.setattr(m,'STATE',state)
 monkeypatch.setattr(m,'now',lambda:SEND_SLOT)
 monkeypatch.setattr(m,'approval_ready',lambda c:True)
 monkeypatch.setattr(m,'read_values',lambda:[])
 monkeypatch.setattr(m,'read_local',lambda:{'suppression':{'complete':True}})
 recipients=[{'email':f'partners@provider{i:02}.invalid','host':f'provider{i:02}.invalid',
              'organization':f'Provider {i:02}',
              'contact_evidence':{'found_on':f'https://provider{i:02}.invalid/partners'}} for i in range(27)]
 monkeypatch.setattr(m,'recipients',lambda *a:{'recipients':recipients,'registry_rows':27,'candidate_rows':27,'exclusions':[]})
 monkeypatch.setattr(m,'mail_admit',lambda *a:(True,''))
 monkeypatch.setattr(m,'fresh_public_contact',lambda r,at:r['contact_evidence'])
 contacts=ModuleType('glp_provider_contact_discovery')
 contacts.discover=lambda name,*a,**k:{'contact_email':recipients[int(name.split()[-1])]['email'],'contact_tier':'partnerships'}
 monkeypatch.setitem(sys.modules,contacts.__name__,contacts)
 partner=ModuleType('glp_application_email_apply');partner.require_alias=lambda c:None
 monkeypatch.setitem(sys.modules,partner.__name__,partner)

 class OfflineMail:
  def __init__(self):self.deliveries=[];self.raw={};self.fail='readback';self.service=self
  def users(self):return self
  def messages(self):return self
  def send(self,**kwargs):self.pending=kwargs['body']['raw'];return self
  def execute(self):return self.send_raw(self.pending)
  def send_raw(self,raw):
   message=BytesParser(policy=default).parsebytes(base64.urlsafe_b64decode(raw))
   self.deliveries.append(str(message['To']))
   message_id=f'offline-{len(self.deliveries)}';message['Message-ID']=f'<{message_id}@example.invalid>'
   self.raw[message_id]=base64.urlsafe_b64encode(message.as_bytes()).decode()
   if self.fail=='uncertain':raise RuntimeError('offline_effect_result_unknown')
   return {'id':message_id,'threadId':message_id}
  def get_message(self,message_id,**kwargs):
   if self.fail=='readback':raise RuntimeError('offline_readback_failed')
   return {'raw':self.raw[message_id]}
 client=OfflineMail();monkeypatch.setattr(m,'service',lambda *a:client)
 return {'send_enabled':True,'batch_cap':25,'packet_sha256':'a'*64},client,recipients

def test_diagnostics_preserve_partial_effect_claim_without_duplicate_send(tmp_path,monkeypatch,capsys):
 config,client,rows=offline_campaign(tmp_path,monkeypatch)
 config['batch_cap']=2
 m.save(m.STATE/'ledger.json',{rows[0]['email']:{'sent':[{'sent_at':SEND_SLOT.isoformat(),'message_id':'confirmed'}]}})
 monkeypatch.setattr(m,'campaign_config',lambda:config)
 monkeypatch.setattr(sys,'argv',['sponsor','--scheduled'])
 with pytest.raises(SystemExit) as error:m.main()
 assert error.value.code==1
 receipt=m.load(m.STATE/'latest-failure.json')
 assert receipt['day_sends'][0]['message_id']=='confirmed'
 assert len(receipt['outstanding_claims'])==1 and receipt['replay_allowed'] is False
 assert receipt['origin']['function']=='get_message'
 m.main() # Existing claim consumes remaining capacity, not permission to replay.
 assert client.deliveries==[rows[1]['email']]
 assert 'PT-day confirmed sends: 1; outstanding claims: 1' in capsys.readouterr().err


def test_diagnostic_never_stringifies_http_error_and_bounds_chain():
 class HostileHTTPError(RuntimeError):
  def __str__(self):raise AssertionError('must never stringify HTTP body')
 root=HostileHTTPError({'Authorization':'private'},'private@invalid.test')
 current=root
 for _ in range(12):
  current.__cause__=ValueError('private body');current=current.__cause__
 current.__cause__=root
 receipt=m.safe_failure_diagnostic(root)
 assert receipt['message']=='[redacted unexpected exception]'
 assert len(receipt['exception_chain'])==8
 assert 'private' not in json.dumps(receipt)


@pytest.mark.parametrize('failure',['readback','uncertain'])
def test_ambiguous_effects_consume_daily_cap_across_runs(tmp_path,monkeypatch,failure):
 config,client,recipients=offline_campaign(tmp_path,monkeypatch);client.fail=failure
 for _ in range(26):
  try:m.run(config)
  except RuntimeError as error:assert str(error) in ('offline_readback_failed','offline_effect_result_unknown')
 assert len(client.deliveries)==25
 assert len(set(client.deliveries))==25 # Claims never replay the same recipient.
 ledger=m.load(m.STATE/'ledger.json')
 assert sum(bool(entry.get('claimed')) for entry in ledger.values())==25
 assert not any(entry.get('sent') for entry in ledger.values())
 assert m.run(config)['sent']==0

@pytest.mark.parametrize('stage',[0,1,2])
def test_claim_and_confirmed_receipts_share_daily_cap(tmp_path,monkeypatch,stage):
 config,client,recipients=offline_campaign(tmp_path,monkeypatch)
 ledger={f'old{i}@past.invalid':{'sent':[{'sent_at':SEND_SLOT.isoformat()}]} for i in range(24)}
 if stage:
  ledger[recipients[0]['email']]={'sent':[{'sent_at':(SEND_SLOT-dt.timedelta(days=10)).isoformat(),
                                         'thread_id':'old-thread','rfc_message_id':'<old@example.invalid>'}]*stage}
 m.save(m.STATE/'ledger.json',ledger)
 with pytest.raises(RuntimeError,match='offline_readback_failed'):m.run(config)
 client.fail=None
 assert m.run(config)['sent']==0
 assert len(client.deliveries)==1
 # An earlier PT day's reservation remains unreplayable but frees today's cap.
 monkeypatch.setattr(m,'now',lambda:SEND_SLOT+dt.timedelta(days=1))
 assert m.run(config)['sent']==25
 assert client.deliveries.count(recipients[0]['email'])==1
 assert len(set(client.deliveries))==26

@pytest.mark.parametrize('claim_at,expected',[
 (dt.datetime(2026,10,6,0,30,tzinfo=dt.timezone.utc),25), # Monday PT, Tuesday UTC
 (dt.datetime(2026,10,6,8,30,tzinfo=dt.timezone.utc),24), # Tuesday PT
])
def test_claim_capacity_uses_pt_date(tmp_path,monkeypatch,claim_at,expected):
 config,client,recipients=offline_campaign(tmp_path,monkeypatch);client.fail=None
 claimed={'stage':0,'at':claim_at.isoformat(),'packet_sha256':config['packet_sha256']}
 m.save(m.STATE/'ledger.json',{'partners@held.invalid':{'claimed':claimed}})
 assert m.run(config)['sent']==expected
 assert len(client.deliveries)==expected
 assert m.load(m.STATE/'ledger.json')['partners@held.invalid']['claimed']==claimed

@pytest.mark.parametrize('claimed',[{'stage':0},{'stage':0,'at':'invalid'}])
def test_invalid_claim_date_refuses_before_transport(tmp_path,monkeypatch,claimed):
 config,client,recipients=offline_campaign(tmp_path,monkeypatch)
 m.save(m.STATE/'ledger.json',{'partners@held.invalid':{'claimed':claimed}})
 with pytest.raises((KeyError,ValueError)):m.run(config)
 assert client.deliveries==[]
 assert m.load(m.STATE/'ledger.json')['partners@held.invalid']['claimed']==claimed
