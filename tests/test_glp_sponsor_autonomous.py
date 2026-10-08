"""Offline contract tests for the once-reviewed weekday sponsor campaign."""
import datetime as dt
import json
import sys
from pathlib import Path
import pytest
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'scripts'), str(ROOT/'ops')]
from inbox_control_plane import sponsor_replies as replies
import glp_sponsor_campaign as campaign


def test_weekday_capacity_and_single_config_keys():
    def unique(pairs):
        result = {}
        for key, value in pairs:
            assert key not in result, 'duplicate config key: '+key
            result[key] = value
        return result
    config = json.loads((ROOT/'ops/sponsor-machine/campaign.json').read_text(), object_pairs_hook=unique)
    assert config['send_days'] == list(range(5))
    assert config['schedule'] == '0 9 * * 1-5'
    assert config['new_provider_daily_cap'] == 25
    assert config['follow_up_daily_cap'] == 50
    assert config['send_enabled'] is False


@pytest.mark.parametrize('body,kind', [
    ('We are interested. Can you send the next steps?', 'interested'),
    ('Not interested, thanks.', 'not_interested'),
    ('What reporting is included?', 'question'),
    ('Please unsubscribe us.', 'opt_out'),
    ('We agree to proceed.', 'agreement_candidate'),
    ('This is not approved; do not proceed.', 'human_reply_or_review'),
])
def test_reply_triage(body,kind):
    assert replies.classify(body,'Re: pilot',{},'2026-10-07T16:00:00+00:00')['kind'] == kind


def test_interested_next_step_is_once_and_never_resumes_sequence(tmp_path, monkeypatch):
    from contextlib import nullcontext
    monkeypatch.setattr(campaign,'STATE',tmp_path)
    monkeypatch.setattr(campaign,'ROOT',ROOT)
    monkeypatch.setattr(campaign,'service',lambda mode: object())
    monkeypatch.setattr(campaign,'canonical_send_boundary',lambda *a: nullcontext())
    monkeypatch.setattr(campaign,'read_values',lambda: [])
    monkeypatch.setattr(campaign,'read_local',lambda: {})
    monkeypatch.setattr(campaign,'invoice_provider_hosts',lambda *a: {'example.invalid'})
    calls=[]
    def send(client,preview,previous,attachments):
        calls.append(preview)
        assert previous['next_step_claim']['event_id']=='reply-1'
        assert not attachments
        return {'message_id':'fixture-only','sent_at':campaign.stamp()}
    monkeypatch.setattr(campaign,'send_message',send)
    ledger={'partners@example.invalid': {'host':'example.invalid','organization':'Fixture','stopped':'interested',
        'stop_event_id':'reply-1','sent':[{'thread_id':'fixture-thread'}]}}
    config={'packet_sha256':'fixture','postal_address':'Fixture only'}
    assert campaign.send_interested_next_steps(ledger,config,{'example.invalid'},1)==1
    assert campaign.send_interested_next_steps(ledger,config,{'example.invalid'},1)==0
    assert len(calls)==1 and '$500' in calls[0]['body']
    assert ledger['partners@example.invalid']['stopped']=='interested'


def test_final_followup_cannot_bunch_after_delayed_second_touch():
    start=dt.datetime(2026,10,1,tzinfo=dt.timezone.utc)
    recipient={'email':'partners@example.invalid'}
    ledger={recipient['email']:{'sent':[{'sent_at':start.isoformat()}, {'sent_at':(start+dt.timedelta(days=9)).isoformat()}]}}
    assert campaign.due(recipient,ledger,start+dt.timedelta(days=10)) is None
    assert campaign.due(recipient,ledger,start+dt.timedelta(days=15))==2


def test_optout_cannot_be_erased_by_later_interest(tmp_path, monkeypatch):
    import sqlite3
    from test_glp_sponsor_boundary_repairs import inbox
    path=tmp_path/'inbox.sqlite'; inbox(path)
    monkeypatch.setattr(campaign,'INBOX_DB',path)
    ledger={'partner@example.com':{'host':'example.com','stopped':'opt_out','stop_event_id':'prior'}}
    data={'kind':'interested','event_id':'later','provider_hosts':['example.com'],'enrolled_addresses':['partner@example.com']}
    with sqlite3.connect(path) as db:
        db.execute('INSERT INTO mailbox_actions VALUES(?,?,?,?,?,?)',('later','later','sponsor_reply_stop','verified',json.dumps(data),'2026-10-07T16:00:00+00:00'))
    campaign.consume_reply_stops(ledger)
    assert ledger['partner@example.com']['stopped']=='opt_out'


def test_runtime_activation_does_not_mutate_sealed_campaign(tmp_path,monkeypatch):
    monkeypatch.setattr(campaign,'ROOT',tmp_path/'source')
    monkeypatch.setattr(campaign,'STATE',tmp_path/'state')
    path=campaign.ROOT/'ops/sponsor-machine/campaign.json'
    campaign.save(path,{'send_enabled':False,'packet_sha256':None,'new_provider_daily_cap':25})
    original=path.read_bytes()
    assert campaign.campaign_config()['send_enabled'] is False
    campaign.save(campaign.STATE/'activation.json',{'send_enabled':True,'packet_sha256':'a'*64})
    assert campaign.campaign_config()['send_enabled'] is True
    assert path.read_bytes()==original
    campaign.save(campaign.STATE/'activation.json',{'send_enabled':True,'packet_sha256':'a'*64,'new_provider_daily_cap':999})
    with pytest.raises(ValueError,match='activation'):
        campaign.campaign_config()
