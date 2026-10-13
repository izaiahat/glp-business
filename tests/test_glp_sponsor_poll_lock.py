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
