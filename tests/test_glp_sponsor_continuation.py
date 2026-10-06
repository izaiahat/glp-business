"""Offline sponsor continuation regressions. All mail/payment transports are fixtures."""
import datetime as dt
import json
import sys
from email.message import EmailMessage
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'scripts')]
from inbox_control_plane import control_plane as cp
import glp_sponsor_campaign as campaign


def test_canonical_ingest_persists_reply_stop_and_idempotent_agreement_notice(tmp_path,monkeypatch):
 from inbox_control_plane import sponsor_replies as sr
 state=tmp_path/'sponsor';state.mkdir()
 (state/'ledger.json').write_text(json.dumps({'partners@example.com':{'host':'example.com','sent':[{'sent_at':'2026-10-01T00:00:00+00:00','thread_id':'sponsor-thread'}]}}))
 monkeypatch.setattr(sr,'LEDGER_PATH',state/'ledger.json')
 monkeypatch.setattr(cp,'RAW_ROOT',tmp_path/'raw')
 monkeypatch.setattr(cp,'ATTACHMENT_ROOT',tmp_path/'attachments')
 monkeypatch.setattr(cp,'run_fable_email_drafter',lambda *a,**k:pytest.fail('No draft for sponsor reply'))
 box={'mailbox_id':'glp_ops_gmail','venture':'glp_affiliate','company':'GLP','canonical_address':'ops@glpclinicscout.com','aliases':['partners@glpclinicscout.com'],'provider':'gmail','adapter':'gmail','auth_reference':'fixture','owner':'Izaiah','lifecycle':'active','poll_seconds':300,'freshness_sla_seconds':900,'inbound_surface':'email','routing_policy':'fixture'}
 store=cp.Ledger(tmp_path/'inbox.sqlite',email_flow_db_path=tmp_path/'flow.sqlite')
 try:
  store.sync_registry({'mailboxes':[box]})
  msg=EmailMessage();msg['From']='partners@example.com';msg['To']='partners@glpclinicscout.com';msg['Date']='Tue, 06 Oct 2026 16:00:00 +0000';msg['Subject']='Re: Sponsor pilot';msg['Message-ID']='<fixture@example.com>';msg.set_content('We agree to proceed with the sponsor pilot. Please confirm the order.')
  result=store.ingest(box,'fixture-message',msg.as_bytes(),provider_thread_id='sponsor-thread')
  assert store.conn.execute("SELECT count(*) FROM mailbox_actions WHERE action='sponsor_reply_stop'").fetchone()[0]==1
  assert store.conn.execute("SELECT count(*) FROM notification_outbox WHERE purpose='sponsor_agreement_candidate'").fetchone()[0]==1
  store.ingest(box,'fixture-message',msg.as_bytes(),provider_thread_id='sponsor-thread')
  assert store.conn.execute("SELECT count(*) FROM notification_outbox WHERE purpose='sponsor_agreement_candidate'").fetchone()[0]==1
  original=store.queue_notification
  def fail_notice(*args,**kwargs):raise RuntimeError('offline_notice_fault')
  monkeypatch.setattr(store,'queue_notification',fail_notice)
  msg.replace_header('Message-ID','<fixture-two@example.com>')
  with pytest.raises(RuntimeError,match='offline_notice_fault'):
   store.ingest(box,'fixture-message-two',msg.as_bytes(),provider_thread_id='sponsor-thread')
  assert store.conn.execute("SELECT count(*) FROM mailbox_actions WHERE action='sponsor_reply_stop'").fetchone()[0]==1
  monkeypatch.setattr(store,'queue_notification',original)
  monkeypatch.setattr(campaign,'INBOX_DB',tmp_path/'inbox.sqlite')
  ledger=json.loads((state/'ledger.json').read_text())
  assert campaign.consume_reply_stops(ledger)['applied']==1
  assert ledger['partners@example.com']['stopped']=='agreement_candidate'
  assert campaign.due({'email':'partners@example.com','host':'example.com'},ledger,dt.datetime.now(dt.timezone.utc)) is None
  posts=[]
  def discord_fixture(path,method='GET',payload=None):
   if method=='POST':posts.append(payload['content']);return {'id':'fixture-discord'}
   if path.endswith('?limit=100'):return []
   return {'channel_id':cp.DECISIONS_CHANNEL_ID,'content':posts[0],'id':'fixture-discord'}
  monkeypatch.setattr(cp,'discord_request',discord_fixture)
  assert cp.dispatch_outbox(store,live=True)['delivered']==1
  assert cp.dispatch_outbox(store,live=True)['delivered']==0
  assert len(posts)==1
  assert not store.conn.execute('SELECT 1 FROM local_drafts').fetchone()
 finally:store.close()


def test_ooo_is_deferred_not_agreement_or_auto_resume():
 from inbox_control_plane import sponsor_replies as sr
 result=sr.classify('I am out of office until 2026-10-12','Automatic reply',{'Auto-Submitted':'auto-replied'},'2026-10-06T16:00:00+00:00')
 assert result['kind']=='out_of_office'
 assert result['review_after']=='2026-10-12'
 assert result['auto_resume'] is False
 assert sr.classify('Do not proceed. Unsubscribe.','Re: pilot',{},'2026-10-06T16:00:00+00:00')['kind']=='opt_out'
