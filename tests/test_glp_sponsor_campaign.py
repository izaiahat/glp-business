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

NOW=dt.datetime(2026,10,3,tzinfo=dt.timezone.utc)

def inputs():
 row=['']*27
 row[2]='Example GLP-1 Provider (US)';row[3]='GLP-1';row[6]='https://example.com';row[7]='US GLP-1 provider';row[10]='approved_active_not_wired';row[25]='glp1-weightloss'
 sheet=[list(policy.EXPECTED_HEADERS),row]
 evidence={'address':'partners@example.com','how':'mailto','found_on':'https://example.com/partners','page_sha256':'a'*64,'captured_at_utc':NOW.isoformat()}
 discovery={'organizations':{'example.com':{'discovery':{'contact_email':'partners@example.com','contact_tier':'partnerships','contact_evidence':evidence}}}}
 local={'suppression':{'complete':True,'entries':[]},'conversations':[]}
 return sheet,discovery,local

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
