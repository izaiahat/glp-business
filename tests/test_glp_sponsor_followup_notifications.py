"""S2: offline follow-up/reply acceptance, with all effects confined to fixtures."""
import datetime as dt
import json
import sys
from email.message import EmailMessage
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'scripts')]
import glp_sponsor_campaign as campaign
from inbox_control_plane import control_plane as cp, sponsor_replies as sr


def test_followups_use_original_day4_day10_even_if_first_followup_delayed():
    recipient = {'email':'partners@example.invalid', 'host':'example.invalid'}
    initial = dt.datetime.fromisoformat('2026-10-05T09:25:00-07:00')
    row = {'sent':[{'sent_at':initial.isoformat()}]}
    ledger = {recipient['email']:row}
    # Weekday batch is at 09:00, not the previous transport's variable minute.
    assert campaign.due(recipient, ledger, dt.datetime.fromisoformat('2026-10-09T09:00:00-07:00')) == 1
    row['sent'].append({'sent_at':'2026-10-12T09:00:00-07:00'})
    assert campaign.due(recipient, ledger, dt.datetime.fromisoformat('2026-10-15T09:00:00-07:00')) == 2
    # Never compress two overdue follow-ups into the same local day.
    assert campaign.due(recipient, ledger, dt.datetime.fromisoformat('2026-10-12T10:00:00-07:00')) is None
    # Day counts follow the Pacific calendar across a DST transition.
    row['sent'] = [{'sent_at':'2026-10-30T09:25:00-07:00'}]
    assert campaign.due(recipient, ledger, dt.datetime.fromisoformat('2026-11-03T06:00:00-08:00')) == 1


@pytest.fixture
def reply_fixture(tmp_path, monkeypatch):
    for name in ('PRIVATE_ROOT','RAW_ROOT','ATTACHMENT_ROOT','BACKUP_ROOT','REPORT_ROOT','DAILY_AUDIT_DIR','STATE_ROOT'):
        monkeypatch.setattr(cp, name, tmp_path/name.lower())
    enrolled = {'partners@example.invalid':{'host':'example.invalid','organization':'Synthetic Clinic',
        'sent':[{'sent_at':'2026-10-05T09:00:00-07:00','thread_id':'fixture-thread'}]}}
    enrollment = tmp_path/'sponsor-ledger.json'
    enrollment.write_text(json.dumps(enrolled))
    monkeypatch.setattr(sr, 'LEDGER_PATH', enrollment)
    monkeypatch.setattr(campaign, 'INBOX_DB', tmp_path/'email-monitor-fixture.sqlite3')
    monkeypatch.setattr(cp, 'run_fable_email_drafter', lambda *a,**k:pytest.fail('NO DRAFT IN SPONSOR FIXTURE'))
    box = {'mailbox_id':'glp_ops_gmail','venture':'glp_affiliate','company':'GLP','canonical_address':'ops@glpclinicscout.com',
        'aliases':['partners@glpclinicscout.com'],'provider':'gmail','adapter':'gmail','auth_reference':'fixture',
        'owner':'Izaiah','lifecycle':'active','poll_seconds':300,'freshness_sla_seconds':900,'inbound_surface':'email','routing_policy':'fixture'}
    store = cp.Ledger(campaign.INBOX_DB, email_flow_db_path=tmp_path/'flow-fixture.sqlite3')
    store.sync_registry({'mailboxes':[box]})
    posts = []
    def discord_fixture(path, method='GET', body=None):
        if method == 'POST':
            posts.append({'path':path, 'content':body['content']})
            return {'id':str(len(posts))}
        if path.endswith('?limit=100'):
            return []
        item = posts[int(path.rsplit('/',1)[-1])-1]
        return {'id':path.rsplit('/',1)[-1], 'channel_id':path.split('/')[2], 'content':item['content']}
    monkeypatch.setattr(cp, 'discord_request', discord_fixture)
    def ingest(body, uid='fixture-message'):
        msg = EmailMessage()
        msg['From']='partners@example.invalid'; msg['To']=campaign.SENDER
        msg['Date']='Mon, 12 Oct 2026 16:00:00 +0000'; msg['Subject']='Re: Sponsor pilot'
        msg['Message-ID']=f'<{uid}@example.invalid>'; msg.set_content(body)
        return store.ingest(box, uid, msg.as_bytes(), provider_thread_id='fixture-thread')
    yield store, enrolled, posts, ingest
    store.close()


def test_synthetic_reply_stops_contact_and_provider_sequence(reply_fixture):
    store, enrolled, posts, ingest = reply_fixture
    ingest('Can you explain the placement scope?')
    result = campaign.consume_reply_stops(enrolled)
    assert result['applied'] == 1
    assert enrolled['partners@example.invalid']['stopped'] == 'question'
    at = dt.datetime.fromisoformat('2026-10-15T09:00:00-07:00')
    assert campaign.due({'email':'partners@example.invalid','host':'example.invalid'}, enrolled, at) is None
    assert campaign.due({'email':'other@example.invalid','host':'example.invalid'}, enrolled, at) is None
    assert campaign.consume_reply_stops(enrolled)['applied'] == 0
    assert not store.conn.execute('SELECT 1 FROM local_drafts').fetchone()


@pytest.mark.parametrize('body,channel',[
    ('Can you explain the placement scope?',sr.OPS),
    ('We agree to proceed.',sr.DECISIONS),
    ('STOP',sr.OPS),
    ('Not interested.',sr.OPS),
    ('I am out of office until 2026-10-20.',sr.OPS),
    ('Thanks for your message.',sr.OPS),
    ('Mail delivery failed: undeliverable.',sr.OPS),
])
def test_every_reply_notifies_sender_company_thread_action_once(reply_fixture,body,channel):
    store, enrolled, posts, ingest = reply_fixture
    ingest(body)
    row = store.conn.execute("SELECT * FROM notification_outbox WHERE idempotency_key LIKE 'sponsor-reply:%'").fetchone()
    assert row['destination_channel_id'] == channel
    for expected in ('Sender: partners@example.invalid','Company: Synthetic Clinic','https://mail.google.com/',
                     'fixture-thread','Suggested next action:','All automatic follow-ups stopped.'):
        assert expected in row['payload']
    assert cp.dispatch_outbox(store,live=True)['delivered'] >= 1
    ingest(body)  # Same message replay must not duplicate either effect.
    assert cp.dispatch_outbox(store,live=True)['delivered'] == 0
    ingest(body,'fixture-second-reply')  # Every DISTINCT reply is notified, even after stopping.
    assert cp.dispatch_outbox(store,live=True)['delivered'] >= 1
    # Bounces also retain the canonical monitor's generic delivery alert.
    sponsor_posts = [item for item in posts if item['content'].startswith('Sponsor reply:')]
    assert len(sponsor_posts) == 2
    assert all(item['path'] == f'/channels/{channel}/messages' for item in sponsor_posts)
    assert all('ops%40glpclinicscout.com' in item['content'] for item in sponsor_posts)


@pytest.mark.parametrize('at',[
    '2026-10-11T09:00:00-07:00', '2026-10-10T09:00:00-07:00',
    '2026-10-12T05:59:59-07:00', '2026-10-12T17:00:00-07:00',
    '2026-11-02T05:59:59-08:00', '2026-11-02T17:00:00-08:00',
])
def test_transport_never_sends_outside_weekday_window(monkeypatch,at):
    monkeypatch.setattr(campaign,'now',lambda:dt.datetime.fromisoformat(at))
    with pytest.raises(RuntimeError,match='outside_approved_weekday_06_17_PT_send_window'):
        # Object has no transport: refusal must precede any alias/auth/send call.
        campaign.send_message(object(),{}, {}, [])
