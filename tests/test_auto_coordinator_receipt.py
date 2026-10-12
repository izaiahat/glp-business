"""Offline source-contract fixtures ONLY; never natural acceptance evidence."""
import argparse
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import pytest

SOURCE = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SOURCE))
import auto_coordinator_receipt as m


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    return str(path)


def fixture(root):
    put(root / m.PLAN, {'issues': [{'issue': 'GLP-358', 'order': -2}, {'issue': 'GLP-355', 'order': -1}]})
    put(root / 'reports/ops/fleet-inbox/GLP-358/lane/INGEST.json', {'issue': 'GLP-358', 'fingerprint': 'fp', 'status': 'REDISPATCH'})
    paths = []
    old = {}
    for n in (1, 2):
        directory = m.begin(root, {'status': 'IDLE'})
        inp = m.load(directory / 'INPUT.json')
        board = {'GLP-358': {'state': {'type': 'started'}}, 'GLP-355': {'state': {'type': 'completed'}}}
        eligibility = [{'order': -2, 'issue': 'GLP-358', 'disposition': 'SELECT' if n == 1 else 'SKIP_RECONCILED',
                        'live_owner': False, 'dependencies_met': True, 'held': False, 'reason': '', 'evidence_paths': []},
                       {'order': -1, 'issue': 'GLP-355', 'disposition': 'SKIP_DONE', 'live_owner': False,
                        'dependencies_met': True, 'held': False, 'reason': '', 'evidence_paths': []}]
        facts = {r['issue']: {k: r[k] for k in ('live_owner', 'dependencies_met', 'held')} for r in eligibility}
        refs = []
        for name, data in [('board', board), ('eligibility', facts), ('worker', {'status': 'DONE_READY_TO_CLOSE'})]:
            p = directory / (name + '.json')
            put(p, data)
            refs.append({'path': str(p.relative_to(root)), 'sha256': m.sha(p.read_bytes()), 'native_message_id': 10 + n})
        for row in eligibility:
            row['evidence_paths'] = [refs[1]['path']]
        brief = 'GLP-358 owner ' + str(directory)
        action = {'issue': 'GLP-358', 'owner': 'owner', 'kind': 'dispatch', 'fingerprint': 'fp',
                  'inbox_sha256': inp['inbox'][0]['sha256'], 'guard': {'status': 'IDLE'}, 'guard_message_id': 20,
                  'effect': {'message_id': 30, 'call_id': 'call', 'brief': brief, 'child_session': 'child'}}
        rec = [] if n == 1 else [{'logical_key': list(m.key(old)), 'prior_effect': old['effect'],
                                  'disposition': 'UNCHANGED_NO_REPEAT', 'readback_path': refs[2]['path']}]
        decision = {'schema': m.SCHEMA, 'input_sha256': m.sha((directory / 'INPUT.json').read_bytes()),
                    'plan_sha256': inp['plan']['sha256'], 'eligibility': eligibility, 'linear_readback': board,
                    'readbacks': refs, 'actions': [action] if n == 1 else [], 'reconciliation': rec,
                    'execution_id': 'e%s' % n, 'coordinator_session': 's%s' % n,
                    'native_output': str(root / 'outputs' / ('%s.md' % n))}
        put(directory / 'DECISION.json', decision)
        put(directory / 'RUNNER-TERMINAL.json', {'coordinator_rc': 0})
        Path(decision['native_output']).parent.mkdir(exist_ok=True)
        Path(decision['native_output']).write_text('s%s %s' % (n, directory))
        old = action
        paths.append(directory)
    return paths


def native_fixture(root, paths):
    worker = put(root / 'real-result.json', {'status': 'DONE_READY_TO_CLOSE'})
    worker_sha = m.sha(Path(worker).read_bytes())
    base = root / 'reports/ops/fable-plan-2-20261009/lanes/C1'
    put(base / 'NATURAL-GLP355-READY.json', {'row': {'issue': 'GLP-355', 'status': 'READY_TO_CLOSE', 'worker_result': worker,
        'checks': {'proof': {'rc': 0}, 'linear_close': {'rc': 0}}, 'evidence': {worker: worker_sha}}})
    put(base / 'GLP-355-worker/GLP-355/RESULT.json', {'worker_result': {'path': worker, 'sha256': worker_sha}})
    put(base / 'FINAL-GLP355-DONE.json', {'identifier': 'GLP-355', 'state': {'type': 'completed'}})
    db = sqlite3.connect(root / 'executions.db')
    db.execute('CREATE TABLE executions(id,job_id,source,scheduled_instant,status,delivery_outcome,started_at,finished_at)')
    for n in (1, 2):
        db.execute('INSERT INTO executions VALUES(?,?,?,?,?,?,?,?)', ('e%s' % n, m.JOB, 'builtin', '2026-10-12T0%s:00:00+00:00' % n,
                   'completed', 'delivered', '2026-10-12T0%s:00:00+00:00' % n, '2026-10-12T0%s:30:00+00:00' % n))
    db.commit(); db.close()
    db = sqlite3.connect(root / 'state.db')
    db.execute('CREATE TABLE sessions(id,source,started_at,ended_at,parent_session_id)')
    db.execute('CREATE TABLE messages(id,session_id,role,tool_name,content,tool_calls,timestamp)')
    for n, path in enumerate(paths, 1):
        start = 1791766800 + (n - 1) * 3600  # 2026-10-12 01:00 UTC
        db.execute('INSERT INTO sessions VALUES(?,?,?,?,?)', ('s%s' % n, 'oneshot', start + 1, start + 1700, None))
        decision = m.load(path / 'DECISION.json')
        text = '\n'.join(r['path'] + ' ' + r['sha256'] for r in decision['readbacks'])
        db.execute('INSERT INTO messages VALUES(?,?,?,?,?,?,?)', (10+n, 's%s' % n, 'tool', 'terminal', json.dumps({'exit_code': 0, 'output': text}), None, start + 2))
    db.execute('INSERT INTO sessions VALUES(?,?,?,?,?)', ('child', 'subagent', 1791766810, 1791768000, 's1'))
    action = m.load(paths[0] / 'DECISION.json')['actions'][0]
    call = {'id': 'call', 'function': {'name': 'delegate_task', 'arguments': json.dumps({'tasks': [{'goal': action['effect']['brief'], 'provider': 'openai-codex', 'model': 'gpt-6.1-sol', 'speed': 'fast'}]})}}
    messages = [(20, 's1', 'tool', 'terminal', json.dumps({'exit_code': 0, 'output': '{"status": "IDLE"}'}), None, 1791766803),
                (30, 's1', 'assistant', None, None, json.dumps([call]), 1791766804),
                (31, 'child', 'user', None, action['effect']['brief'], None, 1791766810),
                (1, 'old', 'tool', 'terminal', json.dumps({'exit_code': 0, 'output': 'GLP-355 Done ' + worker}), None, 1),
                (2, 'old', 'tool', 'terminal', json.dumps({'exit_code': 0, 'output': 'Resumed job: hourly (' + m.JOB + ')'}), None, 2)]
    db.executemany('INSERT INTO messages VALUES(?,?,?,?,?,?,?)', messages)
    db.commit(); db.close()
    return argparse.Namespace(root=str(root), first=str(paths[0]), second=str(paths[1]), executions_db=str(root/'executions.db'),
                              state_db=str(root/'state.db'), output_root=str(root/'outputs'), closure_message=1, activation_message=2)


def test_exact_order_and_input_binding(tmp_path):
    paths = fixture(tmp_path)
    assert m.validate(tmp_path, paths[0])[1]['actions'][0]['issue'] == 'GLP-358'
    decision = m.load(paths[0] / 'DECISION.json')
    decision['eligibility'].reverse()
    put(paths[0] / 'DECISION.json', decision)
    with pytest.raises(ValueError, match='numeric PLAN order'):
        m.validate(tmp_path, paths[0])


def test_unchanged_logical_identity_and_no_repeated_effect(tmp_path):
    paths = fixture(tmp_path)
    assert m.validate(tmp_path, paths[1])[1]['actions'] == []
    decision = m.load(paths[1] / 'DECISION.json')
    decision['reconciliation'][0]['logical_key'][3] = 'different'
    put(paths[1] / 'DECISION.json', decision)
    with pytest.raises(ValueError, match='logical identity changed'):
        m.validate(tmp_path, paths[1])


def test_complete_native_fixture_and_read_only_verifier(tmp_path):
    paths = fixture(tmp_path)
    args = native_fixture(tmp_path, paths)
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in tmp_path.rglob('*') if p.is_file()}
    assert m.verify(args) == {'status': 'PASS', 'natural_actions': 1, 'second_run_repeated_effects': 0, 'writes': 0}
    assert before == {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in before}
    args.activation_message = None
    with pytest.raises(ValueError, match='timestamped'):
        m.verify(args)


def test_nonempty_wal_never_silently_omitted_or_written(tmp_path):
    db = sqlite3.connect(tmp_path / 'native.db')
    db.execute('CREATE TABLE example(id)'); db.commit(); db.close()
    wal = tmp_path / 'native.db-wal'
    wal.write_bytes(b'nonempty retained WAL')
    before = {p: p.read_bytes() for p in tmp_path.iterdir()}
    with pytest.raises(ValueError, match='nonempty WAL'):
        m.rodb(tmp_path / 'native.db')
    assert before == {p: p.read_bytes() for p in tmp_path.iterdir()}


def test_unreported_second_run_dispatch_is_refused(tmp_path):
    paths = fixture(tmp_path)
    args = native_fixture(tmp_path, paths)
    db = sqlite3.connect(args.state_db)
    db.execute('INSERT INTO messages VALUES(?,?,?,?,?,?,?)', (40, 's2', 'assistant', None, None,
        json.dumps([{'id': 'extra', 'function': {'name': 'delegate_task', 'arguments': '{}'}}]), 1791770405))
    db.commit(); db.close()
    with pytest.raises(ValueError, match='unreported or duplicated native dispatch'):
        m.verify(args)


def test_runner_deferral_preserves_receipt_boundary(monkeypatch):
    import auto_coordinator_runner as runner
    monkeypatch.setattr(runner.guard, 'inspect', lambda: {'status': 'DEFERRED_TUI_CHILDREN'})
    monkeypatch.setattr(runner.receipt, 'begin', lambda *a: pytest.fail('captured despite deferral'))
    assert runner.main() == 10
