"""Host queue custody regressions: temporary storage and injected transports only."""
import importlib.util
import json
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]

def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / (name + '.py'))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_ingress_ack_restart_dedupe_and_conflict(tmp_path):
    m = load('glp_host_ingress')
    path = tmp_path / 'state.sqlite3'
    body = {'intent_id': 'test-intent-001', 'kind': 'self_test', 'payload': {}}
    with m.connect(path) as c:
        assert m.accept(c, body)['status'] == 'acknowledged'
    with m.connect(path) as c:
        assert m.accept(c, body)['status'] == 'acknowledged'
        with pytest.raises(ValueError, match='conflict'):
            m.accept(c, {**body, 'kind': 'affiliati_release'})
        assert m.drain(c, lambda *a: pytest.fail('probe must not deploy'))['unresolved'] == 0
        assert m.accept(c, body)['status'] == 'completed'
        assert c.execute('select count(*) from intents').fetchone()[0] == 1


def test_lost_release_ack_is_not_replayed(tmp_path):
    m = load('glp_host_ingress')
    with m.connect(tmp_path / 'state.sqlite3') as c:
        m.accept(c, {'intent_id': 'release-intent-001', 'kind': 'affiliati_release', 'payload': {}})
        def lose(*a):
            raise TimeoutError()
        with pytest.raises(TimeoutError):
            m.drain(c, lose)
        assert m.drain(c, lambda *a: pytest.fail('unknown POST replay'))['unresolved'] == 1
        assert c.execute('select status from intents').fetchone()[0] == 'post_uncertain'


def test_cogny_corrupt_state_is_not_empty(tmp_path):
    m = load('glp_cogny_queue_drain')
    p = tmp_path / 'state.json'
    p.write_text('{broken')
    with pytest.raises(ValueError):
        m.load_state(p)


def test_cogny_no_foreign_resume_and_no_completion_replay(tmp_path):
    m = load('glp_cogny_queue_drain')
    p = tmp_path / 'state.json'
    m.save_state({'dispatches': [{'task_id': 'task1', 'job_id': 'job1', 'status': 'completed',
                                 'completion_ack': {'success': True}}]}, p)
    class Client:
        def call(self, *a, **kw):
            pytest.fail('completed or foreign task must not call remote')
    assert not m.mode_resume(Client(), 'foreign', 'foreign', state_path=p)['ok']
    assert m.mode_resume(Client(), 'job1', 'task1', state_path=p)['ok']


def test_cogny_early_resume_cannot_complete_task(tmp_path):
    m = load('glp_cogny_queue_drain')
    p = tmp_path / 'state.json'
    m.save_state({'dispatches': [{'task_id': 'task1', 'job_id': 'job1', 'status': 'in_flight'}]}, p)
    class Client:
        def call(self, *a, **kw):
            pytest.fail('running task must not complete')
    result = m.mode_resume(Client(), 'job1', 'task1', lambda _: {'job_id': 'job1', 'status': 'running'}, p)
    assert not result['ok']
    assert result['reason'] == 'job_not_terminal'


def test_cogny_claim_complete_ack_once(tmp_path, monkeypatch):
    m = load('glp_cogny_queue_drain')
    monkeypatch.setattr(m, 'ROOT', tmp_path)
    path = tmp_path / 'state.json'
    class Client:
        def __init__(self): self.claims = self.completes = 0
        def call(self, name, args=None, **kw):
            if name == 'get_queue_status':
                return {'stats': {'pending': 0 if self.claims else 1},
                        'tasks': [{'id': 'task1', 'status': 'completed' if self.completes else 'pending'}]}
            if name == 'get_next_task':
                self.claims += 1
                return {'hasTask': True, 'task': {'id': 'task1', 'skill_id': 'seo-audit', 'task_type': 'analysis'}}
            if name == 'list_findings': return {'findings': []}
            if name == 'complete_task':
                self.completes += 1
                assert m.load_state(path)['dispatches'][0]['status'] == 'completion_uncertain'
                return {'success': True}
            return {'ok': True}
    client = Client()
    def poster(*a):
        assert m.load_state(path)['dispatches'][0]['status'] == 'dispatch_uncertain'
        return {'job_id': 'job1', 'status': 'running'}
    result = m.mode_dispatch(client, '', 60, m.MODEL, poster=poster, state_path=path)
    Path(result['agent_receipt']).write_text(json.dumps({'task_id': 'task1', 'ok': True,
        'result_summary': 'Fixture evidence', 'report_path': str(tmp_path / 'report'), 'findings': []}))
    terminal = lambda _: {'job_id': 'job1', 'status': 'done', 'rc': 0}
    assert m.mode_resume(client, 'job1', 'task1', terminal, path)['ok']
    assert m.mode_resume(client, 'job1', 'task1', terminal, path)['ok']
    assert (client.claims, client.completes) == (1, 1)


def test_http_auth_before_durable_ack(tmp_path):
    import threading
    import urllib.request
    import urllib.error
    m = load('glp_host_ingress')
    token = tmp_path / 'test-token'
    token.write_text('fixture-only-no-production-secret')
    server = m.ThreadingHTTPServer(('127.0.0.1', 0), m.handler(tmp_path / 'db', token))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = 'http://127.0.0.1:' + str(server.server_port)
    body = json.dumps({'intent_id': 'http-probe-1', 'kind': 'self_test', 'payload': {}}).encode()
    try:
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(urllib.request.Request(base + '/intents', data=body))
        assert err.value.code == 401
        assert not (tmp_path / 'db').exists()
        for _ in range(2):
            req = urllib.request.Request(base + '/intents', data=body, headers={'Authorization': 'Bearer ' + token.read_text()})
            with urllib.request.urlopen(req) as response: assert response.status == 202
        c = m.connect(tmp_path / 'db')
        assert c.execute('select count(*) from intents').fetchone()[0] == 1
        c.close()
    finally:
        server.shutdown(); server.server_close(); thread.join()
