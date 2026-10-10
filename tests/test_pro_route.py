"""Route tests use isolated custody, never live Drive/browser/config."""
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))


def route():
    path = ROOT / 'scripts/pro_route.py'
    assert path.is_file(), 'Persistent Pro route is missing'
    spec = importlib.util.spec_from_file_location('pro_route', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_enqueue_is_content_idempotent_and_registered_packet(tmp_path, monkeypatch):
    m = route()
    monkeypatch.setattr(m, 'ROOT', tmp_path)
    monkeypatch.setattr(m.relay, 'BUSINESS', tmp_path)
    monkeypatch.setattr(m.relay, 'load_config', lambda: {'ledger': 'ledger.jsonl'})
    monkeypatch.setattr(m.relay, 'JOBS', tmp_path / 'jobs')
    monkeypatch.setattr(m.packet, 'BUSINESS', tmp_path)
    monkeypatch.setattr(m.packet, 'JOBS', tmp_path / 'jobs')
    prompt = tmp_path / 'input.md'; prompt.write_text('Read the supplied source. Advise only.')
    first = m.enqueue('GLP-73', prompt, 'auto', 'test-session')
    before = {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    second = m.enqueue('GLP-73', prompt, 'auto', 'different-session')
    assert first['job'] == second['job']
    assert before == {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    assert (tmp_path / 'jobs' / first['job'] / 'MANIFEST.json').is_file()
    assert json.loads((tmp_path / 'jobs' / first['job'] / 'MANIFEST.json').read_text())['producer'] == 'scripts/gpt6pro_packet.py'
    assert first['state'] == 'queued'


def test_dispatch_never_retries_ambiguous_send(tmp_path, monkeypatch):
    m = route()
    monkeypatch.setattr(m, 'ROOT', tmp_path)
    row = {'job': 'route-glp-73-test', 'state': 'queued', 'requested_account': 'auto', 'attempts': []}
    m.save(row)
    monkeypatch.setattr(m.relay, 'job_state', lambda job: {})
    monkeypatch.setattr(m, 'choose_account', lambda requested: 'acct2')
    calls = []
    def uncertain(row, action):
        calls.append(action)
        raise RuntimeError('transport interrupted')
    monkeypatch.setattr(m, 'effect', uncertain, raising=False)
    m.dispatch()
    m.dispatch()
    saved = json.loads(next((m.directory() / 'queue').glob('*.json')).read_text())
    assert calls == ['submit']
    assert saved['state'] == 'uncertain'


def test_effect_defers_when_heavy_is_held_without_acquire(tmp_path, monkeypatch):
    from types import SimpleNamespace
    m = route(); monkeypatch.setattr(m, 'ROOT', tmp_path)
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout='{"status":"HELD","seat":"other"}', stderr='')
    monkeypatch.setattr(m.subprocess, 'run', run)
    result = m.effect({'job': 'route-glp-73-test', 'account': 'acct1'}, 'poll')
    assert result['deferred']
    assert len(calls) == 1 and calls[0][-1] == 'status'


def test_local_verification_rejects_tampered_payload(tmp_path, monkeypatch):
    import hashlib
    m = route(); monkeypatch.setattr(m.relay, 'JOBS', tmp_path)
    d = tmp_path / 'job'; out = d / 'outbox'; out.mkdir(parents=True)
    sha = lambda b: hashlib.sha256(b).hexdigest()
    payload = b'useful genuine advisory\n'
    raw = json.dumps({'files': {'ADVISORY.md': sha(payload)}}).encode()
    (out / 'ADVISORY.md').write_bytes(payload)
    (out / 'OUTPUT-MANIFEST.json').write_bytes(raw)
    (d / 'RESPONSE.md').write_bytes(b'final')
    st = {'source_reply_sha256': sha(b'final'), 'fetch_verification': {'state': 'VERIFIED', 'manifest_sha256': sha(raw)}}
    monkeypatch.setattr(m.relay, 'job_state', lambda job: st)
    assert m.verified_files('job')['ADVISORY.md'] == sha(payload)
    (out / 'ADVISORY.md').write_bytes(b'changed')
    import pytest
    with pytest.raises(ValueError, match='OUTBOX_BYTES_CHANGED'):
        m.verified_files('job')


def test_cli_submit_status_fetch_use_one_persistent_identity(tmp_path, monkeypatch, capsys):
    m = route(); monkeypatch.setattr(m, 'ROOT', tmp_path)
    monkeypatch.setattr(m, 'enqueue', lambda *a: {'job': 'route-glp-73-test', 'state': 'queued'})
    calls = []
    monkeypatch.setattr(m, 'dispatch', lambda **kw: calls.append(kw) or [])
    assert m.main(['submit', '--issue', 'GLP-73', '--prompt-file', str(tmp_path / 'prompt')]) == 0
    assert calls == [{'only': 'route-glp-73-test'}]
    assert 'route-glp-73-test' in capsys.readouterr().out


def test_account_routing_capacity_and_unbound_hold(tmp_path, monkeypatch):
    from contextlib import nullcontext
    m = route(); monkeypatch.setattr(m, 'ROOT', tmp_path)
    cfg = {'accounts': [{'label': 'A', 'cdp_port': 1}, {'label': 'B', 'cdp_port': 2}, {'label': 'C', 'cdp_port': 3}], 'envelope': {'max_concurrent_chats_per_ip': 6, 'max_concurrent_chats_per_account': 3}}
    monkeypatch.setattr(m.relay, 'load_config', lambda: cfg)
    rows = [{'account': 'A'}] * 3
    monkeypatch.setattr(m.relay, 'active_registry', lambda: rows)
    monkeypatch.setattr(m.relay, 'budget_check', lambda c, a: (2, 200))
    monkeypatch.setattr(m.relay, 'sibling_bindings', lambda account, job: [{'status': 'failed', 'retry_blocked': True}] if account == 'B' else [])
    monkeypatch.setattr(m.relay, '_verified_terminal_unsent', lambda st: False)
    monkeypatch.setattr(m.socket, 'create_connection', lambda *a, **k: nullcontext())
    assert m.choose_account('auto') == 'acct3'
    assert m.choose_account('acct1') is None
    assert m.choose_account('acct2') is None
    rows.extend([{'account': 'C'}] * 3)
    assert m.choose_account('auto') is None


def test_assign_only_before_external_reservation(tmp_path, monkeypatch):
    import pytest
    m = route(); monkeypatch.setattr(m, 'ROOT', tmp_path)
    row = {'job':'job','state':'queued','requested_account':'acct2','attempts':[]}
    m.save(row)
    monkeypatch.setattr(m.relay, 'job_state', lambda job: {})
    monkeypatch.setattr(m.relay, 'load_config', lambda: {})
    monkeypatch.setattr(m.relay, 'ledger_rows', lambda cfg: [])
    m.assign('job','acct1')
    assert m.request_rows()[0]['requested_account']=='acct1'
    monkeypatch.setattr(m.relay, 'job_state', lambda job: {'status':'submitting'})
    with pytest.raises(ValueError, match='RESERVED'):
        m.assign('job','acct3')


def test_retry_requires_maintained_unsent_receipt(tmp_path, monkeypatch):
    import pytest
    m = route(); monkeypatch.setattr(m,'ROOT',tmp_path)
    monkeypatch.setattr(m.relay,'JOBS',tmp_path/'jobs')
    d=m.relay.JOBS/'job';d.mkdir(parents=True)
    p=d/'JOB.json';p.write_text('{"status":"failed","typed":true,"sent":"UNKNOWN"}')
    m.save({'job':'job','state':'uncertain','attempts':[]})
    with pytest.raises(ValueError, match='UNSENT_RECEIPT_REQUIRED'):
        m.requeue_unsent('job',m.hash_file(p))


def test_configured_account_hold_is_respected(tmp_path, monkeypatch):
    m=route();monkeypatch.setattr(m,'ROOT',tmp_path)
    control=m.directory()/'CONTROL.json';control.parent.mkdir(parents=True)
    control.write_text(json.dumps({'paused_accounts':{'acct1':'retained draft'}}))
    monkeypatch.setattr(m.relay,'load_config',lambda:{'accounts':[{'label':'A','cdp_port':1}], 'envelope':{'max_concurrent_chats_per_ip':6,'max_concurrent_chats_per_account':3}})
    monkeypatch.setattr(m.relay,'active_registry',lambda:[])
    monkeypatch.setattr(m.relay,'sibling_bindings',lambda *a:(_ for _ in ()).throw(AssertionError('held account probed')))
    assert m.choose_account('auto') is None


def test_terminal_retained_tabs_do_not_consume_live_tab_allowance(monkeypatch):
    import gpt6pro_relay as r
    monkeypatch.setattr(r, 'load_config', lambda: {'accounts':[{'label':'A','cdp_port':9340}], 'envelope':{'max_concurrent_chats_per_account':3}})
    monkeypatch.setitem(r.BU_ENV,'BU_CDP_URL','http://127.0.0.1:9340')
    monkeypatch.setattr(r,'sibling_bindings',lambda *a:[{'job':str(i),'error':{'tab':'retained'+str(i)}} for i in range(3)])
    monkeypatch.setattr(r,'_verified_terminal_unsent',lambda st:True)
    scripts=[]
    monkeypatch.setattr(r,'bu',lambda s,**k:scripts.append(s) or 'RELAY_JSON:{}')
    r.bu_json('print("observation")')
    assert '_tab_cap=8\n' in scripts[0]
    monkeypatch.setattr(r,'_verified_terminal_unsent',lambda st:False)
    r.bu_json('print("observation")')
    assert '_tab_cap=5\n' in scripts[1]


def test_singleton_contention_returns_busy_not_second_owner(tmp_path, monkeypatch):
    import pytest
    m=route();monkeypatch.setattr(m,'ROOT',tmp_path)
    with m.singleton():
        with pytest.raises(BlockingIOError):
            with m.singleton():raise AssertionError('two owners')


def test_changed_packet_cannot_reach_heavy_or_transport(tmp_path, monkeypatch):
    import pytest
    m=route();monkeypatch.setattr(m.relay,'JOBS',tmp_path)
    d=tmp_path/'job';d.mkdir()
    for n in ['PROMPT.md','MANIFEST.json']:(d/n).write_text(n)
    row={'job':'job','packet_sha256':{n:m.hash_file(d/n) for n in ['PROMPT.md','MANIFEST.json']}}
    (d/'PROMPT.md').write_text('substituted request')
    monkeypatch.setattr(m.subprocess,'run',lambda *a,**k:(_ for _ in ()).throw(AssertionError('external call')))
    with pytest.raises(ValueError,match='PACKET_DRIFT'):m.effect(row,'submit')


def test_dispatch_spaces_bound_polls_without_holding_heavy(tmp_path,monkeypatch):
    m=route();monkeypatch.setattr(m,'ROOT',tmp_path)
    for job in ['a','b','c']:m.save({'job':job,'state':'running','attempts':[]})
    monkeypatch.setattr(m.relay,'job_state',lambda job:{'status':'running'})
    monkeypatch.setattr(m.relay,'load_config',lambda:{'envelope':{'poll_stagger_s':20}})
    events=[]
    monkeypatch.setattr(m,'effect',lambda row,action:events.append(row['job']) or {'rc':3})
    monkeypatch.setattr(m.time,'sleep',lambda seconds:events.append(seconds))
    m.dispatch()
    assert events==['a',20,'b',20,'c',20]
