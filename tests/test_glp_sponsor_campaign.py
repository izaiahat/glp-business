import datetime as dt
import json
import sys
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
