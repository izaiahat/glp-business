"""Unattended sponsor regressions; synthetic offline fixtures only."""
import datetime as dt
import importlib.util
import sys
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'scripts'),str(ROOT)]
import glp_sponsor_campaign as c
import glp_sponsor_current_receiving as r
from inbox_control_plane import control_plane as cp

def helper(name):
    spec=importlib.util.spec_from_file_location('s3_'+name,ROOT/'tests'/('test_glp_sponsor_'+name+'.py'))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module
h=helper('campaign')

def decisions(tmp_path,monkeypatch):
    monkeypatch.setattr(c,'ROOT',tmp_path)
    text=('6. GLP-328: GO at 25/day\n'
          '15. Outbound sponsor/outreach email sends only Mon-Fri 06:00-17:00 America/Los_Angeles.\n'
          '5. Sponsor outreach: up to 25 first-touch/day, weekdays 06:00–17:00 PT; automatic follow-ups day 4 and day 10 in the same window, stopped on reply. Operator must be notified of every reply via the existing inbox monitor so replies are handled.\n')
    for name in (r.DECISION,r.AUTOMATION_DECISION):
        path=tmp_path/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(text)

def test_recorded_authority_no_tui_and_missing_scope_refuses(tmp_path,monkeypatch):
    decisions(tmp_path,monkeypatch)
    monkeypatch.setattr(r,'live_coordinator',lambda *a:pytest.fail('NO TUI DEPENDENCY'))
    authority=r.recorded_operator_authority(c)
    assert len(authority['decisions'])==2 and authority['new_provider_daily_cap']==25
    for name in (r.DECISION,r.AUTOMATION_DECISION):
        path=tmp_path/name;original=path.read_text();path.write_text('No approval')
        with pytest.raises(RuntimeError,match='recorded_operator_decision_missing'):r.recorded_operator_authority(c)
        path.write_text(original)

def test_preflight_final_transport_no_claim_or_send(tmp_path,monkeypatch):
    config,client,rows=h.offline_campaign(tmp_path,monkeypatch)
    result=c.run(config,transport_preflight=True)
    assert result['would_send']==25 and result['sent']==0 and not client.deliveries
    assert not (c.STATE/'ledger.json').exists()
    assert result['denominators']['final_admission_checked'] is True

def test_preflight_requires_approval(tmp_path,monkeypatch):
    config,client,rows=h.offline_campaign(tmp_path,monkeypatch)
    monkeypatch.setattr(c,'approval_ready',lambda *a:False)
    with pytest.raises(RuntimeError,match='transport_preflight_approval_required'):c.run(config,transport_preflight=True)
    assert not client.deliveries

@pytest.mark.parametrize('when',['2026-10-11T16:00:00Z','2026-10-12T12:59:59Z','2026-10-13T00:00:00Z'])
def test_preflight_weekday_window(tmp_path,monkeypatch,when):
    config,client,rows=h.offline_campaign(tmp_path,monkeypatch)
    with pytest.raises(RuntimeError,match='outside_approved_weekday_06_17_PT_send_window'):
        c.run(config,transport_preflight=True,preflight_at=dt.datetime.fromisoformat(when))
    assert not client.deliveries

def test_daily_25_cap_includes_uncertain_claims(tmp_path,monkeypatch):
    config,client,rows=h.offline_campaign(tmp_path,monkeypatch);config['new_provider_daily_cap']=25
    c.save(c.STATE/'ledger.json',{rows[0]['email']:{'host':rows[0]['host'],'claimed':{'stage':0,'at':h.SEND_SLOT.isoformat()}}})
    before=(c.STATE/'ledger.json').read_bytes();result=c.run(config,transport_preflight=True)
    assert result['would_send']==24 and (c.STATE/'ledger.json').read_bytes()==before and not client.deliveries

def test_reply_stop_keeps_provider_stopped(tmp_path,monkeypatch):
    config,client,rows=h.offline_campaign(tmp_path,monkeypatch);rows[:]=rows[:1]
    c.save(c.STATE/'ledger.json',{rows[0]['email']:{'host':rows[0]['host'],'stopped':'human_reply'}})
    assert c.run(config,transport_preflight=True)['would_send']==0 and not client.deliveries

def test_suppression_rechecked_before_transport(tmp_path,monkeypatch):
    config,client,rows=h.offline_campaign(tmp_path,monkeypatch);rows[:]=rows[:1]
    def admit(*args):rows.clear();return True,''
    monkeypatch.setattr(c,'mail_admit',admit)
    assert c.run(config,transport_preflight=True)['would_send']==0 and not client.deliveries

def test_future_time_never_overrides_live_send_clock(tmp_path,monkeypatch):
    config,client,rows=h.offline_campaign(tmp_path,monkeypatch)
    with pytest.raises(ValueError,match='preflight_at_requires_transport_preflight'):c.run(config,preflight_at=h.SEND_SLOT)
    assert not client.deliveries

def test_draft_discovery_explicit_not_duplicate(monkeypatch):
    class Request:
        def __init__(self,value):self.value=value
        def execute(self):return self.value
    class Gmail:
        def users(self):return self
        def messages(self):return self
        def getProfile(self,**kw):return Request({'emailAddress':'ops@glpclinicscout.com'})
        def list(self,**kw):return Request({'messages':[{'id':'nuimage-unsent-draft'}]})
        def get(self,**kw):return Request({'labelIds':['DRAFT'],'threadId':'nuimage-thread','raw':''})
    class Ledger:
        def event_exists(self,*a):return False
        def health(self,*a,**kw):pass
        def ingest(self,*a,**kw):pytest.fail('DRAFT IS NOT INBOUND')
    monkeypatch.setattr(cp,'gmail_service_for',lambda *a:Gmail())
    result=cp.poll_gmail_api(Ledger(),{'mailbox_id':'glp_ops_gmail','canonical_address':'ops@glpclinicscout.com'},'run',False,25)
    assert result['discovered']==result['draft_only_skipped']==result['non_inbound_skipped']==1
    assert result['persisted']==result['duplicates']==0

def test_recorded_renewal_real_packet_review_and_receipt_no_tui(tmp_path,monkeypatch):
    module=helper('current_receiving')
    module.test_real_packet_binding_closed_members_and_guard_refusals(tmp_path,monkeypatch,'none',recorded_operator=True)


def test_coverage_known_skips_not_unknown_gap(tmp_path,monkeypatch):
    import sqlite3
    boundary=helper('boundary_repairs');monkeypatch.setattr(c,'INBOX_DB',tmp_path/'inbox.sqlite')
    boundary.inbox(c.INBOX_DB)
    with sqlite3.connect(c.INBOX_DB) as db:
        db.execute('ALTER TABLE inbox_runs ADD COLUMN non_inbound_count INTEGER NOT NULL DEFAULT 0')
        db.execute('UPDATE inbox_runs SET discovered_count=3,non_inbound_count=1')
    assert c.consume_reply_stops({},at=h.SEND_SLOT)['status']=='read_complete'
    with sqlite3.connect(c.INBOX_DB) as db:db.execute('UPDATE inbox_runs SET discovered_count=4')
    assert c.consume_reply_stops({},at=h.SEND_SLOT)['status']=='unknown'
