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


def test_verified_dispatch_can_progress_to_exact_terminal_close(tmp_path):
    from test_auto_coordinator_loop import Backend
    b = Backend()
    original = b.effect
    def effect(a):
        if a['kind'] != 'close':
            b.calls.append((a['kind'], a['issue']))
            return {'worker_result': 'reports/owned/RESULT.json'}
        return original(a)
    b.effect = effect
    assert m.run_pass(tmp_path, [task()], [], b)['dispatched'] == 1
    assert m.run_pass(tmp_path, [task()], [row(status='READY_TO_CLOSE', worker_result='reports/owned/RESULT.json')], b)['closed'] == 1
    assert m.run_pass(tmp_path, [task()], [row(status='READY_TO_CLOSE', worker_result='reports/owned/RESULT.json')], b)['closed'] == 0
