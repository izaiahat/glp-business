"""Synthetic overlapping canonical poll: real flock, no network or sends."""
import datetime as dt
import fcntl
import importlib.util
import sys
import threading
from pathlib import Path
import pytest
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'scripts'), str(ROOT)]
import glp_sponsor_campaign as c
import glp_sponsor_nuimage_reply as n
spec = importlib.util.spec_from_file_location('s4_campaign_fixture', ROOT/'tests/test_glp_sponsor_campaign.py')
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)


def test_overlapping_poll_finishes_before_campaign_eligibility_read(tmp_path, monkeypatch):
    config, client, rows = h.offline_campaign(tmp_path, monkeypatch)
    lock_path = tmp_path/'poll.lock'
    monkeypatch.setattr(c, 'INBOX_POLL_LOCK', lock_path, raising=False)
    pending, released = threading.Event(), threading.Event()
    original_read = c.read_values
    def eligibility():
        assert released.is_set(), 'eligibility read raced the active canonical poll'
        with lock_path.open('a') as other:
            with pytest.raises(BlockingIOError):fcntl.flock(other,fcntl.LOCK_EX|fcntl.LOCK_NB)
        return original_read()
    monkeypatch.setattr(c, 'read_values', eligibility)
    with lock_path.open('a') as poll:
        fcntl.flock(poll, fcntl.LOCK_EX | fcntl.LOCK_NB)
        def complete_poll():
            assert pending.wait(2)
            threading.Event().wait(0.3)
            # Release models completion/committed poll receipt, not cancellation.
            released.set()
            fcntl.flock(poll, fcntl.LOCK_UN)
        worker = threading.Thread(target=complete_poll)
        worker.start()
        pending.set()
        try:
            result = c.run(config, transport_preflight=True)
        finally:
            worker.join(3)
    assert not worker.is_alive()
    assert result['would_send'] == 25 and result['sent'] == 0
    assert not client.deliveries and not (c.STATE/'ledger.json').exists()


def test_poll_timeout_alerts_once_before_any_eligibility_read(tmp_path, monkeypatch):
    config, client, rows = h.offline_campaign(tmp_path, monkeypatch)
    path = tmp_path/'poll.lock'
    monkeypatch.setattr(c, 'INBOX_POLL_LOCK', path)
    monkeypatch.setattr(c, 'INBOX_POLL_WAIT_SECONDS', 0.05)
    notices = []
    monkeypatch.setattr(c, 'notify_inbox_poll_timeout', lambda job: notices.append(job), raising=False)
    monkeypatch.setattr(c, 'read_values', lambda: pytest.fail('timeout reached eligibility'))
    with path.open('a') as poll:
        fcntl.flock(poll, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match='canonical_inbox_poll_lock_timeout'):
            c.run(config)
        assert notices == ['Sponsor outreach']
        with pytest.raises(RuntimeError, match='canonical_inbox_poll_lock_timeout'):
            c.run(config, transport_preflight=True)
    assert notices == ['Sponsor outreach']  # no-send cannot send an alert
    assert not client.deliveries and not (c.STATE/'ledger.json').exists()


def test_boundary_releases_after_eligibility_exception(tmp_path, monkeypatch):
    config, client, rows = h.offline_campaign(tmp_path, monkeypatch)
    def fail():
        raise RuntimeError('synthetic_eligibility_refusal')
    monkeypatch.setattr(c, 'read_values', fail)
    with pytest.raises(RuntimeError, match='synthetic_eligibility_refusal'):
        c.run(config, transport_preflight=True)
    with c.INBOX_POLL_LOCK.open('a') as other:
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert not client.deliveries


def test_ops_alert_transport_is_minimized_and_never_retried(tmp_path, monkeypatch, capsys):
    import json
    import urllib.request
    secret = tmp_path/'.hermes/secrets/n8n/ops-alerts-webhook.txt'
    secret.parent.mkdir(parents=True)
    secret.write_text('https://ops-alerts.invalid/nonsecret-test')
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    requests = []
    class Reply:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self):
            return json.dumps(dict(id='synthetic-alert', channel_id='1487300132054765629')).encode()
    def post(request, **kwargs):
        requests.append(request)
        assert kwargs == {'timeout': 30}
        return Reply()
    monkeypatch.setattr(urllib.request, 'urlopen', post)
    c.notify_inbox_poll_timeout('Sponsor outreach')
    assert len(requests) == 1
    payload = json.loads(requests[0].data)
    assert payload['allowed_mentions'] == {'parse': []}
    assert '240s' in payload['content'] and 'Outbound sends **0**' in payload['content']
    assert requests[0].method == 'POST' and 'wait=true' in requests[0].full_url
    def uncertain(*args, **kwargs):
        requests.append(args[0])
        raise RuntimeError('URL_BEARING_EXCEPTION_MUST_NOT_PRINT')
    monkeypatch.setattr(urllib.request, 'urlopen', uncertain)
    c.notify_inbox_poll_timeout('Nu Image queued reply')
    assert len(requests) == 2
    assert capsys.readouterr().err == 'sponsor_inbox_lock_alert_unconfirmed:RuntimeError\n'


def freshness_campaign(tmp_path, monkeypatch):
    """Real cursor/SLA and SQLite boundary; fixture transport advances time."""
    import sqlite3
    config, client, rows = h.offline_campaign(tmp_path, monkeypatch)
    config['require_canonical_reply_coverage'] = True
    client.fail = None
    monkeypatch.setattr(c, 'INBOX_DB', tmp_path/'canonical.sqlite')
    clock = [h.SEND_SLOT]
    monkeypatch.setattr(c, 'now', lambda: clock[0])
    stamp = (clock[0]-dt.timedelta(seconds=240)).isoformat()
    with sqlite3.connect(c.INBOX_DB) as db:
        db.executescript('''
CREATE TABLE mailbox_actions(action_id TEXT,event_id TEXT,action TEXT,status TEXT,provider_receipt_json TEXT,created_at TEXT);
CREATE TABLE mailboxes(mailbox_id TEXT,lifecycle TEXT,freshness_sla_seconds INTEGER);
CREATE TABLE provider_cursors(mailbox_id TEXT,last_auth_success_at TEXT,last_read_success_at TEXT,last_complete_poll_at TEXT,unhealthy_reason TEXT,poll_receipt_id TEXT);
CREATE TABLE inbox_runs(run_id TEXT,finished_at TEXT,exit_status TEXT,discovered_count INTEGER,persisted_count INTEGER,duplicate_count INTEGER);
''')
        for box in ('glp_ops_gmail', 'glp_izaiah_gmail'):
            db.execute('INSERT INTO mailboxes VALUES(?,?,?)', (box,'active',720))
            db.execute('INSERT INTO provider_cursors VALUES(?,?,?,?,?,?)', (box,stamp,stamp,stamp,None,'synthetic-poll'))
        db.execute('INSERT INTO inbox_runs VALUES(?,?,?,?,?,?)', ('synthetic-poll',stamp,'pass',2,0,2))
    def auth(mode='read'):
        if mode == 'send':clock[0] += dt.timedelta(seconds=20)
        return client
    monkeypatch.setattr(c, 'service', auth)
    return config, client, rows, clock


def complete_fixture_poll(clock, stops=()):
    import sqlite3
    import json
    # Existing shared flock retained; no SQLite send transaction may be held.
    with c.INBOX_POLL_LOCK.open('a') as contender:
        with pytest.raises(BlockingIOError):fcntl.flock(contender,fcntl.LOCK_EX|fcntl.LOCK_NB)
    clock[0] += dt.timedelta(seconds=2)
    stamp = clock[0].isoformat()
    with sqlite3.connect(c.INBOX_DB, timeout=0) as db:
        db.execute('UPDATE provider_cursors SET last_auth_success_at=?,last_read_success_at=?,last_complete_poll_at=?', (stamp,stamp,stamp))
        db.execute('UPDATE inbox_runs SET finished_at=?', (stamp,))
        for stop in stops:
            db.execute('INSERT INTO mailbox_actions VALUES(?,?,?,?,?,?)', ('stop','stop','sponsor_reply_stop','verified',json.dumps(stop),stamp))


def test_long_batch_refreshes_real_720_second_cursor_before_claim(tmp_path, monkeypatch):
    config, client, rows, clock = freshness_campaign(tmp_path, monkeypatch)
    polls = []
    def refresh():
        polls.append(clock[0])
        complete_fixture_poll(clock)
    monkeypatch.setattr(c, 'refresh_canonical_poll', refresh, raising=False)
    result = c.run(config)
    assert result['sent'] == 25 and len(client.deliveries) == 25
    assert len(polls) == 1
    ledger = c.load(c.STATE/'ledger.json')
    assert not any(row.get('claimed') for row in ledger.values())
    assert sum(len(row.get('sent', [])) for row in ledger.values()) == 25
    assert c.run(config)['sent'] == 0  # no replay or cap refill


def test_failed_refresh_preserves_partial_receipts_without_new_claim(tmp_path, monkeypatch):
    config, client, rows, clock = freshness_campaign(tmp_path, monkeypatch)
    attempts = []
    def failed_poll():
        attempts.append(clock[0])
        raise RuntimeError('canonical_sponsor_poll_refresh_failed')
    monkeypatch.setattr(c, 'refresh_canonical_poll', failed_poll)
    with pytest.raises(RuntimeError, match='canonical_sponsor_poll_refresh_failed'):c.run(config)
    ledger = c.load(c.STATE/'ledger.json')
    assert len(client.deliveries) == 24 and len(attempts) == 1
    assert sum(len(r.get('sent', [])) for r in ledger.values()) == 24
    assert not any(r.get('claimed') for r in ledger.values())
    with c.INBOX_POLL_LOCK.open('a') as contender:fcntl.flock(contender,fcntl.LOCK_EX|fcntl.LOCK_NB)


def test_new_stop_committed_by_refresh_wins_before_claim(tmp_path, monkeypatch):
    config, client, rows, clock = freshness_campaign(tmp_path, monkeypatch)
    rows[:] = rows[:25]
    stop = {'kind':'opt_out','event_id':'stop','provider_hosts':[rows[24]['host']],
            'enrolled_addresses':[rows[24]['email']]}
    monkeypatch.setattr(c, 'refresh_canonical_poll', lambda: complete_fixture_poll(clock, [stop]))
    result = c.run(config)
    assert result['sent'] == 24 and rows[24]['email'] not in client.deliveries
    entry = c.load(c.STATE/'ledger.json')[rows[24]['email']]
    assert entry['stopped'] == 'opt_out' and 'claimed' not in entry


def test_stale_initial_poll_is_renewed_without_relaxing_sla(tmp_path, monkeypatch):
    config, client, rows, clock = freshness_campaign(tmp_path, monkeypatch)
    rows[:] = rows[:1]
    clock[0] += dt.timedelta(seconds=500)
    assert c.consume_reply_stops({})['status'] == 'unknown'
    monkeypatch.setattr(c, 'refresh_canonical_poll', lambda: complete_fixture_poll(clock))
    assert c.run(config)['sent'] == 1


@pytest.mark.parametrize('preflight', [False, True])
def test_no_send_modes_never_refresh_canonical_cursor(tmp_path, monkeypatch, preflight):
    config, client, rows, clock = freshness_campaign(tmp_path, monkeypatch)
    clock[0] += dt.timedelta(seconds=500)
    monkeypatch.setattr(c, 'refresh_canonical_poll', lambda: pytest.fail('NO POLL WRITE IN FORECAST'))
    if preflight:
        with pytest.raises(RuntimeError, match='canonical_sponsor_reply_coverage_unknown'):
            c.run(config, transport_preflight=True)
    else:
        assert c.run(config, dry_run=True)['sent'] == 0
    assert client.deliveries == [] and not (c.STATE/'ledger.json').exists()


def test_successful_poll_receipt_without_fresh_cursor_cannot_send(tmp_path, monkeypatch):
    config, client, rows, clock = freshness_campaign(tmp_path, monkeypatch)
    clock[0] += dt.timedelta(seconds=500)
    attempts = []
    monkeypatch.setattr(c, 'refresh_canonical_poll', lambda: attempts.append('poll'))
    with pytest.raises(RuntimeError, match='canonical_sponsor_reply_coverage_unknown'):c.run(config)
    assert attempts == ['poll'] and client.deliveries == []


@pytest.mark.parametrize('outcome', ['pass','failed','timeout','malformed','mutation','draft','send','secret'])
def test_canonical_refresh_uses_only_installed_poll_path_and_sanitizes_errors(tmp_path, monkeypatch, outcome):
    import json
    import subprocess
    monkeypatch.setattr(c, 'ROOT', tmp_path)
    calls = []
    def runner(argv, **kwargs):
        calls.append(argv)
        assert argv == [sys.executable, str(c.ROOT/'inbox_control_plane/control_plane.py'),'--poll','--limit','250']
        assert kwargs == dict(capture_output=True,text=True,timeout=240)
        if outcome == 'timeout':raise subprocess.TimeoutExpired('PRIVATE_SENTINEL',240)
        receipt = dict(status='pass',run_id='synthetic-poll',outbound_send_count=0,
                       provider_draft_count=0,provider_mutation_count=0,secret_printed=False)
        if outcome == 'mutation':receipt['provider_mutation_count']=1
        if outcome == 'draft':receipt['provider_draft_count']=1
        if outcome == 'send':receipt['outbound_send_count']=1
        if outcome == 'secret':receipt['secret_printed']=True
        return subprocess.CompletedProcess(argv,2 if outcome=='failed' else 0,
            'PRIVATE_SENTINEL' if outcome=='malformed' else json.dumps(receipt),'PRIVATE_SENTINEL')
    monkeypatch.setattr(subprocess,'run',runner)
    if outcome == 'pass':c.refresh_canonical_poll()
    else:
        with pytest.raises(RuntimeError,match='^canonical_sponsor_poll_refresh_failed$'):c.refresh_canonical_poll()
    assert len(calls) == 1


def test_scheduled_failure_reports_actual_day_sends_not_zero(tmp_path, monkeypatch, capsys):
    config, client, rows, clock = freshness_campaign(tmp_path, monkeypatch)
    c.save(c.STATE/'ledger.json', {rows[0]['email']:{'sent':[{'sent_at':clock[0].isoformat(),'message_id':'synthetic-sent'}]},
                                  rows[1]['email']:{'claimed':{'at':clock[0].isoformat(),'stage':0}}})
    monkeypatch.setattr(c,'campaign_config',lambda:config)
    monkeypatch.setattr(sys,'argv',['sponsor','--scheduled'])
    def failed(*args, **kwargs):raise RuntimeError('PRIVATE_SENTINEL')
    monkeypatch.setattr(c,'run',failed)
    with pytest.raises(SystemExit) as error:c.main()
    assert error.value.code == 1
    output = capsys.readouterr()
    assert 'PT-day confirmed sends: 1' in output.err and 'outstanding claims: 1' in output.err
    assert 'PRIVATE_SENTINEL' not in output.err
    receipt = c.load(c.STATE/'latest-failure.json')
    assert receipt['day_sends'][0]['message_id'] == 'synthetic-sent'
    assert receipt['replay_allowed'] is False


def test_poll_crossing_window_cannot_claim_or_send(tmp_path, monkeypatch):
    config, client, rows, clock = freshness_campaign(tmp_path, monkeypatch)
    clock[0] = h.SEND_SLOT.replace(hour=23, minute=59, second=50)  # 16:59:50 PT
    def refresh():
        clock[0] += dt.timedelta(seconds=20)
        complete_fixture_poll(clock)
    monkeypatch.setattr(c,'refresh_canonical_poll',refresh)
    result = c.run(config)
    assert result['sent'] == 0
    assert {row['reason'] for row in result['send_boundary_exclusions']} == {'outside_approved_weekday_06_17_PT_send_window'}
    assert client.deliveries == [] and not (c.STATE/'ledger.json').exists()


def test_future_cursor_does_not_trigger_refresh_or_send(tmp_path, monkeypatch):
    config, client, rows, clock = freshness_campaign(tmp_path, monkeypatch)
    import sqlite3
    with sqlite3.connect(c.INBOX_DB) as db:
        db.execute('UPDATE provider_cursors SET last_auth_success_at=?',((clock[0]+dt.timedelta(seconds=1)).isoformat(),))
    monkeypatch.setattr(c,'refresh_canonical_poll',lambda:pytest.fail('FUTURE CURSOR IS NOT STALE PROOF'))
    with pytest.raises(RuntimeError,match='canonical_sponsor_reply_coverage_unknown'):c.run(config)
    assert client.deliveries == []


@pytest.mark.parametrize('stop', [False, True])
def test_interested_effect_renews_expired_proof_and_rechecks_stop(tmp_path, monkeypatch, stop):
    config, client, rows, clock = freshness_campaign(tmp_path, monkeypatch)
    row = rows[0]
    ledger = {row['email']: {'host':row['host'], 'organization':'Fixture',
        'stopped':'interested', 'stop_event_id':'original',
        'sent':[{'sent_at':h.SEND_SLOT.isoformat(), 'thread_id':'old', 'rfc_message_id':'<old>'}]}}
    c.save(c.ROOT/'ops/sponsor-machine/copy.json', {'interested_next_step': {'subject':'Fixture', 'body':'Hello {organization}'}})
    monkeypatch.setattr(c, 'invoice_provider_hosts', lambda *a: {row['host']})
    clock[0] += dt.timedelta(seconds=500)
    stops = [{'kind':'opt_out','event_id':'new','provider_hosts':[row['host']], 'enrolled_addresses':[row['email']]}] if stop else []
    polls = []
    def refresh():
        polls.append(True)
        complete_fixture_poll(clock, stops)
    monkeypatch.setattr(c, 'refresh_canonical_poll', refresh)
    with c.inbox_poll_boundary():
        count = c.send_interested_next_steps(ledger, config, {row['host']}, 1)
    assert polls == [True]
    assert count == (0 if stop else 1)
    assert len(client.deliveries) == count
    assert not ledger[row['email']].get('next_step_claim')


def test_invoice_effect_renews_expired_proof_before_write(tmp_path, monkeypatch):
    import glp_sponsor_paypal as paypal
    config, client, rows, clock = freshness_campaign(tmp_path, monkeypatch)
    row = rows[0]
    c.save(c.STATE/'orders.json', {'fixture': {'order_id':'fixture', 'provider_host':row['host'], 'invoice_action':'draft'}})
    config.update(paypal_read_only_polling=True, paypal_invoice_drafts_enabled=True)
    clock[0] += dt.timedelta(seconds=500)
    polls, writes = [], []
    def refresh():
        polls.append(True)
        complete_fixture_poll(clock)
    class Payment:
        def merchant(self, at): pass
        def invoice_write(self, *a):
            writes.append(True)
            raise RuntimeError('synthetic uncertain effect')
    monkeypatch.setattr(c, 'refresh_canonical_poll', refresh)
    monkeypatch.setattr(c, 'invoice_provider_hosts', lambda *a: {row['host']})
    monkeypatch.setattr(paypal, 'PayPalClient', Payment)
    monkeypatch.setattr(paypal, 'authorize_order', lambda *a: 'synthetic-approval')
    monkeypatch.setattr(paypal, 'poll_order', lambda *a: paypal.unknown('fixture'))
    with c.inbox_poll_boundary():
        result = c.refresh_paypal_orders(config, clock[0], finance_hosts={row['host']}, ledger={})
    assert polls == [True] and writes == [True]
    assert result['financial_write_attempts'] == 1
    assert c.load(c.STATE/'orders.json')['fixture']['invoice_claim']
    with c.inbox_poll_boundary():
        c.refresh_paypal_orders(config, clock[0], finance_hosts={row['host']}, ledger={})
    assert writes == [True]  # uncertain effect remains unreplayable


def test_failed_job_missing_ledger_reports_unknown_not_zero(tmp_path, monkeypatch, capsys):
    config, client, rows, clock = freshness_campaign(tmp_path, monkeypatch)
    monkeypatch.setattr(c,'campaign_config',lambda:config)
    monkeypatch.setattr(sys,'argv',['sponsor','--scheduled'])
    def failed(*args, **kwargs):raise RuntimeError('PRIVATE_SENTINEL')
    monkeypatch.setattr(c,'run',failed)
    with pytest.raises(SystemExit):c.main()
    assert 'PT-day confirmed sends: unknown' in capsys.readouterr().err
    assert c.load(c.STATE/'latest-failure.json')['day_sends'] is None
