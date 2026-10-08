"""Offline GLP336 regressions. Synthetic work/transport is not business acceptance."""
import hashlib
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from scripts import glp_cogny_queue_drain as cogny
from scripts import glp_host_ingress as ingress
from ops.glp import cogny_client


class Client:
    def __init__(self, pending=None):
        self.calls = []
        self.pending = pending or []
        self.completed = False
        self.lost_ack = False

    def call(self, name, args=None, **kwargs):
        self.calls.append(name)
        if name == 'get_queue_status':
            return {'stats': {'pending': len(self.pending), 'in_progress': 0,
                              'completed': int(self.completed)},
                    'tasks': self.pending + ([{'id': 'task-336', 'status': 'completed'}]
                                             if self.completed else [])}
        if name == 'get_next_task':
            return {'hasTask': True, 'task': self.pending.pop(0)}
        if name == 'list_findings':
            return {'findings': []}
        if name == 'complete_task':
            self.completed = True
            return {'cogny_error': 'synthetic lost ACK'} if self.lost_ack else {'success': True}
        return {'ok': True}


def task(skill='seo-audit'):
    return {'id': 'task-336', 'status': 'pending', 'skill_id': skill,
            'task_type': 'analysis', 'skill_content': 'Read-only synthetic audit'}


@pytest.fixture
def local(tmp_path, monkeypatch):
    monkeypatch.setattr(cogny, 'ROOT', tmp_path / 'artifacts')
    monkeypatch.setattr(cogny, 'BUSINESS', tmp_path)
    monkeypatch.setattr(cogny, 'STATE_PATH', tmp_path / 'queue.json')
    monkeypatch.setattr(cogny, 'ENABLED', True)
    return tmp_path


def bound(local):
    directory = local / 'artifacts' / 'custody-336'
    directory.mkdir(parents=True)
    report = directory / 'report.md'
    report.write_text('Synthetic report only')
    receipt = directory / 'agent.json'
    receipt.write_text(json.dumps({'task_id': 'task-336', 'custody_id': 'custody-336',
                                  'ok': True, 'result_summary': 'Synthetic result',
                                  'report_path': str(report), 'findings': []}))
    rec = {'task_id': 'task-336', 'job_id': 'job-336', 'status': 'in_flight',
           'custody_id': 'custody-336', 'agent_receipt': str(receipt),
           'report_dir': str(directory)}
    cogny.save_state({'dispatches': [rec]})
    return rec, receipt, report


def done(job_id):
    return {'job_id': job_id, 'status': 'done', 'rc': 0, 'action': 'hermes_chat'}


def test_host_tick_sends_authenticated_bound_intent_and_verifies_terminal_ack(local, monkeypatch):
    token = local / 'synthetic-token'
    token.write_text('offline-fixture-not-a-credential')
    db = local / 'intents.sqlite3'
    server = ThreadingHTTPServer(('127.0.0.1', 0), ingress.handler(db, token))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # Only transport remaps the hard-coded production loopback endpoint.
    real_open = urllib.request.build_opener().open
    requests = []
    def transport(req, **kwargs):
        assert req.full_url.startswith('http://127.0.0.1:8790/')
        requests.append((req.get_method(), req.full_url, json.loads(req.data) if req.data else None))
        req.full_url = req.full_url.replace(':8790/', ':' + str(server.server_port) + '/')
        return real_open(req, **kwargs)
    monkeypatch.setattr(cogny, 'BRIDGE_TOKEN_FILE', token)
    class Transport:
        def open(self, req, **kwargs):
            return transport(req, **kwargs)
    def opener(*handlers):
        assert any(isinstance(h, urllib.request.ProxyHandler) and h.proxies == {} for h in handlers)
        assert any(isinstance(h, urllib.request.HTTPRedirectHandler) for h in handlers)
        return Transport()
    monkeypatch.setattr(cogny.urllib.request, 'build_opener', opener)
    client = Client([task()])
    monkeypatch.setattr(cogny, 'bridge_hermes_chat', lambda *args: {'job_id': 'job-336', 'status': 'running'})
    monkeypatch.setattr(cogny_client, 'connect', lambda **kwargs: client)
    monkeypatch.setattr(cogny, 'bridge_job_status', lambda j: {'job_id': j, 'status': 'running'})
    try:
        out = cogny.mode_tick(client)
        assert out['callback'].get('status') == 'acknowledged', (out, requests)
        state = cogny.load_state()
        rec = state['dispatches'][0]
        intent_id = rec['callback_intent_id']
        assert requests[0][2] == {'intent_id': intent_id, 'kind': 'cogny_completion',
                                  'payload': {'task_id': 'task-336', 'job_id': 'job-336'}}
        with ingress.connect(db) as c:
            ingress.drain(c)
            assert c.execute('SELECT status FROM intents').fetchone()[0] == 'running'
        report = Path(rec['report_dir']) / 'report.md'
        report.write_text('Synthetic report')
        Path(rec['agent_receipt']).write_text(json.dumps({
            'task_id': rec['task_id'], 'custody_id': rec['custody_id'], 'ok': True,
            'result_summary': 'Synthetic result', 'report_path': str(report), 'findings': []}))
        monkeypatch.setattr(cogny, 'bridge_job_status', done)
        with ingress.connect(db) as c:
            ingress.drain(c)
        out = cogny.mode_tick(client)
        assert out['callback']['status'] == 'completed'
        assert cogny.load_state()['dispatches'][0]['callback_terminal_ack']['intent_id'] == intent_id
        cogny.mode_tick(client)
        assert client.calls.count('get_next_task') == 1
        assert client.calls.count('complete_task') == 1
        # Exercise auth/raw-envelope refusals against the real handler, without secrets.
        base = 'http://127.0.0.1:' + str(server.server_port)
        with pytest.raises(urllib.error.HTTPError) as err:
            real_open(urllib.request.Request(base + '/intents', data=b'{}'), timeout=5)
        assert err.value.code == 401
        with pytest.raises(urllib.error.HTTPError) as err:
            real_open(urllib.request.Request(base + '/intents', data=b'{"job_id":"job-336"}',
                headers={'Authorization': 'Bearer offline-fixture-not-a-credential'}), timeout=5)
        assert err.value.code == 409
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_only_matched_early_held_callback_converges_from_durable_completion(local, monkeypatch):
    rec, _, _ = bound(local)
    client = Client()
    monkeypatch.setattr(cogny_client, 'connect', lambda **kwargs: client)
    with ingress.connect(local / 'intents.sqlite3') as c:
        ingress.accept(c, {'intent_id': 'cogny-early-336', 'kind': 'cogny_completion',
                           'payload': {'task_id': 'task-336', 'job_id': 'job-336'}})
        ingress.update(c, 'cogny-early-336', status='held', receipt=json.dumps({'reason': 'job_not_terminal'}))
        ingress.accept(c, {'intent_id': 'release-held-336', 'kind': 'affiliati_release', 'payload': {}})
        ingress.update(c, 'release-held-336', status='held', receipt='{}')
        ingress.drain(c, call=lambda *args: pytest.fail('held release replay'))
        assert c.execute("SELECT status FROM intents WHERE intent_id='cogny-early-336'").fetchone()[0] == 'held'
        cogny.mode_resume(client, 'job-336', 'task-336', job_status=done)
        ingress.drain(c, call=lambda *args: pytest.fail('held release replay'))
        row = c.execute("SELECT * FROM intents WHERE intent_id='cogny-early-336'").fetchone()
        assert row['status'] == 'completed' and row['job_id'] == 'job-336'
        assert c.execute("SELECT status FROM intents WHERE intent_id='release-held-336'").fetchone()[0] == 'held'
        assert client.calls.count('complete_task') == 1


def test_disabled_lane_does_not_heartbeat_or_claim(local, monkeypatch):
    monkeypatch.setattr(cogny, 'ENABLED', False)
    client = Client([task()])
    out = cogny.mode_dispatch(client, '', 600, cogny.MODEL)
    assert out['reason'] == 'disabled' and not client.calls


def test_unqualified_pending_scope_is_refused_before_claim(local):
    client = Client([task('google-ads-audit')])
    out = cogny.mode_dispatch(client, '', 600, cogny.MODEL)
    assert out['reason'] == 'pending_scope_unqualified'
    assert 'get_next_task' not in client.calls and 'heartbeat' not in client.calls
    assert not cogny.load_state().get('claim_uncertain')


def test_unknown_census_is_not_zero(local):
    client = Client()
    original = client.call
    client.call = lambda name, *args, **kwargs: {'cogny_error': 'synthetic outage'} if name == 'get_queue_status' else original(name, *args, **kwargs)
    out = cogny.mode_status(client)
    assert out['ok'] is False and out['pending'] is None and out['output_count'] is None


def test_error_status_with_rc_zero_cannot_complete(local):
    bound(local)
    client = Client()
    out = cogny.mode_resume(client, 'job-336', 'task-336',
                            job_status=lambda j: {**done(j), 'status': 'error'})
    assert not out['ok'] and 'complete_task' not in client.calls


def test_foreign_report_path_cannot_be_completed(local):
    rec, receipt, _ = bound(local)
    foreign = local / 'foreign.md'
    foreign.write_text('Unowned bytes')
    doc = json.loads(receipt.read_text())
    doc['report_path'] = str(foreign)
    receipt.write_text(json.dumps(doc))
    client = Client()
    out = cogny.mode_resume(client, 'job-336', 'task-336', job_status=done)
    assert out['reason'] == 'result_custody_unqualified' and 'complete_task' not in client.calls


def test_lost_completion_ack_needs_exact_result_readback_not_status_or_replay(local):
    bound(local)
    client = Client()
    client.lost_ack = True
    out = cogny.mode_resume(client, 'job-336', 'task-336', job_status=done)
    assert out['reason'] == 'completion_ack_unknown_no_replay'
    out = cogny.mode_resume(client, 'job-336', 'task-336', job_status=done)
    assert not out['ok'] and out['reason'] == 'completion_ack_unknown_no_replay'
    saved = cogny.load_state()['dispatches'][0]
    original = client.call
    def readback(name, *args, **kwargs):
        value = original(name, *args, **kwargs)
        if name == 'get_queue_status':
            value['tasks'][0]['result_summary'] = saved['submitted_result_summary']
        return value
    client.call = readback
    out = cogny.mode_resume(client, 'job-336', 'task-336', job_status=done)
    assert out['ok'] and client.calls.count('complete_task') == 1
    assert saved['result_custody']['report_sha256'] == hashlib.sha256(b'Synthetic report only').hexdigest()
