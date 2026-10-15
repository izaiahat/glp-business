"""Real worker/adapter functions with fake external boundaries, never live effects."""
import io
import json
import os
from pathlib import Path
import sys
import types
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import auto_coordinator_loop as m
from test_auto_coordinator_loop import task, row


def test_reviewer_ready_to_close_real_adapter_is_check_only(tmp_path, monkeypatch):
    import auto_coordinator_guard as guard
    import linear_orchestrator as lo
    p = task(proof_complete=True, proof_cmd='test -f reports/owned/RESULT.json')
    r = row(status='READY_TO_CLOSE', worker_result='reports/owned/RESULT.json')
    calls = []
    monkeypatch.setenv('LINEAR_ROLE', 'orchestrator')
    monkeypatch.setattr(m, 'inputs', lambda root: ([p], [r]))
    monkeypatch.setattr(guard, 'inspect_idle_tui', lambda **kw: {'status': 'IDLE'})
    monkeypatch.setattr(lo, 'issue', lambda issue: {'state': {'name': 'In Progress'}, 'labels': {'nodes': []}})
    def fake_run(argv, **kw):
        calls.append((argv, kw))
        return types.SimpleNamespace(returncode=0, stdout='GLP-1 PASS\n', stderr='')
    monkeypatch.setattr(m.subprocess, 'run', fake_run)
    b = m.InstalledBackend(tmp_path, tmp_path, 'own-token')
    result = m.run_pass(tmp_path, [p], [r], b)
    assert calls and any('--check-only' in argv for argv, _ in calls)
    assert all('--apply' not in argv for argv, _ in calls)
    assert all(kw['env']['LINEAR_ROLE'] == 'worker' for _, kw in calls)
    assert result['closure_ready'] == 1
    assert result['closed'] == result['dispatched'] == result['uncertain'] == 0
    assert result['actions'][0]['status'] == 'ready_for_coordinator'
    assert not (tmp_path / 'effects').exists()
    assert 'closure-ready: 1' in m.summary(result)


def test_close_effect_and_apply_call_are_refused_before_subprocess(tmp_path, monkeypatch):
    r = row(status='READY_TO_CLOSE', worker_result='reports/owned/RESULT.json')
    monkeypatch.setattr(m, 'inputs', lambda root: ([task()], [r]))
    def forbidden(*args, **kw):
        pytest.fail('closure must not reach subprocess')
    monkeypatch.setattr(m.subprocess, 'run', forbidden)
    b = m.InstalledBackend(tmp_path, tmp_path, 'own-token')
    with pytest.raises(ValueError, match='coordinator'):
        b.effect({'kind': 'close', 'issue': 'GLP-1', 'plan': task(), 'inbox': r})
    with pytest.raises(ValueError, match='check-only'):
        b.call([sys.executable, 'scripts/linear_close.py', 'GLP-1', 'RESULT.json', '--apply'])


def test_pre_effect_rechecks_exact_plan_and_ingest_bytes(tmp_path, monkeypatch):
    b = m.InstalledBackend(tmp_path, tmp_path, 'own-token')
    action = {'issue': 'GLP-1', 'plan': task(), 'inbox': row()}
    monkeypatch.setattr(m, 'inputs', lambda root: ([task(next_action='different action')], [row()]))
    with pytest.raises(ValueError, match='changed'):
        b.current(action)
    monkeypatch.setattr(m, 'inputs', lambda root: ([task()], [row(fingerprint='b' * 64)]))
    with pytest.raises(ValueError, match='changed'):
        b.current(action)


@pytest.mark.parametrize('tier,expected', [('fast', 0), ('normal', 10)])
def test_worker_natural_exit_retains_singleton_and_releases_lease(tmp_path, monkeypatch, capsys, tier, expected):
    calls, releases = [], []
    module = types.ModuleType('tools.delegation_admission')
    module.admission_problem = lambda: None
    lease = types.SimpleNamespace(token='child-token', release=lambda: releases.append(True))
    module.try_reserve_host_children = lambda n: ([lease], 1, 8)
    monkeypatch.setitem(sys.modules, 'tools.delegation_admission', module)
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path))
    cfg = tmp_path / '.hermes/config.yaml'; cfg.parent.mkdir()
    cfg.write_text('agent:\n  service_tier: ' + tier + '\n')
    request = tmp_path / 'WORK.json'
    request.write_text(json.dumps({'issue': 'GLP-1', 'owner': 'OWNER', 'kind': 'redispatch',
                                   'plan': task(), 'failure_text': 'exact failed proof'}))
    def fake_run(cmd, **kw):
        calls.append((cmd, kw))
        assert 'timeout' not in kw
        assert kw['env']['LINEAR_ROLE'] == 'worker'
        assert os.fstat(kw['pass_fds'][0])
        kw['stdout'].write('Offline boundary response; not a live output claim')
        return types.SimpleNamespace(returncode=0)
    monkeypatch.setattr(m.subprocess, 'run', fake_run)
    with (tmp_path / 'singleton').open('a') as lock:
        assert m.worker(request, lock.fileno()) == expected
    assert releases == [True]
    assert len(calls) == (expected == 0)
    ack = json.loads(capsys.readouterr().out)
    if expected == 0:
        terminal = m.load(tmp_path / 'WORK/RUN.json')
        assert terminal['exit_code'] == 0 and terminal['finished_at']
        assert ack['worker_result'] == str(tmp_path / 'WORK/RESULT.json')
        assert 'exact failed proof' in (tmp_path / 'WORK/BRIEF.md').read_text()
        assert not (tmp_path / 'WORK/RESULT.json').exists()  # never manufacture output
    else:
        assert ack['status'] == 'REFUSED'


def test_verified_dispatch_can_progress_to_exact_terminal_readiness(tmp_path):
    from test_auto_coordinator_loop import Backend
    b = Backend()
    original = b.effect
    def effect(a):
        if a['kind'] != 'close':
            b.calls.append((a['kind'], a['issue']))
            return {'worker_result': 'reports/owned/RESULT.json'}
        return original(a)
    b.effect = effect
    assert m.run_pass(tmp_path, [task()], [row()], b)['dispatched'] == 1
    r = row(status='READY_TO_CLOSE', worker_result='reports/owned/RESULT.json', fingerprint='b' * 64)
    assert m.run_pass(tmp_path, [task()], [r], b)['closure_ready'] == 1
    assert m.run_pass(tmp_path, [task()], [r], b)['closed'] == 0
    assert b.calls == [('redispatch', 'GLP-1'), ('preflight', 'GLP-1'), ('preflight', 'GLP-1')]
    assert m.load(tmp_path / 'effects/GLP-1.json')['action']['kind'] == 'redispatch'
