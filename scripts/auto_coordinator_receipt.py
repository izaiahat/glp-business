#!/usr/bin/env python3
"""Retain causal coordinator inputs; verify them without dispatch or live writes.

Decision facts must come from actual readbacks. Native messages authenticate the
selected effects. No old receipt is upgraded and no missing field is inferred.
"""
import argparse
import datetime as dt
import hashlib
import json
import math
import os
import shlex
from pathlib import Path
import sqlite3
import sys
sys.dont_write_bytecode = True

JOB = '13d3fff8734a'
PLAN = 'reports/ops/fable-plan-2-20261009/PLAN.json'
SCHEMA = 'glp-coordinator-causal-v1'


def require(ok, why):
    if not ok:
        raise ValueError(why)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def load(path):
    return json.loads(Path(path).read_bytes())


def exclusive(path, data):
    with Path(path).open('xb') as f:
        f.write(json.dumps(data, sort_keys=True, indent=2).encode() + b'\n')
        f.flush()
        os.fsync(f.fileno())


def binding(root, path):
    p = Path(path)
    p = p if p.is_absolute() else root / p
    require(p.resolve().is_relative_to(root.resolve()) and not p.is_symlink(), 'foreign/symlink input')
    data = p.read_bytes()
    return {'path': str(p.relative_to(root)), 'sha256': sha(data), 'raw': data.decode(), 'data': json.loads(data)}


def begin(root, guard):
    """Called only after existing runner admission; does not change its guards."""
    base = root / 'reports/ops/auto-coordinator/causal'
    base.mkdir(parents=True, exist_ok=True)
    previous = sorted(base.glob('*/DECISION.json'), key=lambda p: p.stat().st_mtime_ns)
    directory = base / (dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ') + '-%s' % os.getpid())
    directory.mkdir()
    plan = binding(root, PLAN)
    require(guard['status'] == 'IDLE', 'receipt requires admitted IDLE guard')
    inbox = [binding(root, p) for p in sorted((root / 'reports/ops/fleet-inbox').glob('*/*/INGEST.json'))]
    prior = binding(root, previous[-1]) if previous else None
    exclusive(directory / 'INPUT.json', {'schema': SCHEMA, 'captured_at': dt.datetime.now(dt.timezone.utc).isoformat(),
              'guard': guard, 'plan': plan, 'inbox': inbox, 'previous': prior})
    return directory


def key(action):
    return (action['issue'], action['owner'], action['kind'], action['fingerprint'])


def validate(root, directory):
    inp = load(directory / 'INPUT.json')
    decision = load(directory / 'DECISION.json')
    require(inp['schema'] == decision['schema'] == SCHEMA, 'unsupported receipt schema')
    require(decision['input_sha256'] == sha((directory / 'INPUT.json').read_bytes()), 'input binding changed')
    require(inp['guard']['status'] == 'IDLE', 'input guard not idle')
    plan = inp['plan']['data']['issues']
    captured = [inp['plan'], *inp['inbox']] + ([inp['previous']] if inp['previous'] else [])
    for ref in captured:
        require(sha(ref['raw'].encode()) == ref['sha256'] and json.loads(ref['raw']) == ref['data'], 'captured bytes/data mismatch')
    require(decision['plan_sha256'] == inp['plan']['sha256'], 'PLAN binding missing')
    rows = decision['eligibility']
    expected = [(p['order'], p['issue']) for p in sorted(plan, key=lambda p: p['order'])]
    require(len(set(x[1] for x in expected)) == len(expected), 'duplicate PLAN issue')
    require([(r['order'], r['issue']) for r in rows] == expected, 'not exact complete numeric PLAN order')
    board = decision['linear_readback']
    refs = decision['readbacks']
    require(refs, 'actual dynamic readback artifacts missing')
    for ref in refs:
        require(binding(root, ref['path'])['sha256'] == ref['sha256'], 'dynamic readback bytes changed')
    require(any(load(root / ref['path']) == board for ref in refs), 'Linear snapshot is not a bound readback')
    selected = decision['actions']
    selected_issues = [a['issue'] for a in selected]
    require(len(selected_issues) == len(set(selected_issues)), 'duplicate issue effect')
    require(selected_issues == [r['issue'] for r in rows if r['disposition'] == 'SELECT'], 'selection/action mismatch')
    for r, p in zip(rows, sorted(plan, key=lambda p: p['order'])):
        state = board[r['issue']]['state']['type']
        require(type(r['live_owner']) is bool and type(r['dependencies_met']) is bool and type(r['held']) is bool, 'unknown eligibility input')
        require(r['evidence_paths'] and all(x in [ref['path'] for ref in refs] for x in r['evidence_paths']), 'eligibility evidence absent')
        observed = [load(root / x).get(r['issue']) for x in r['evidence_paths']]
        require(any(isinstance(x, dict) and all(x.get(k) == r[k] for k in ('live_owner', 'dependencies_met', 'held')) for x in observed), 'eligibility booleans not bound to actual input snapshot')
        disposition = r['disposition']
        if disposition == 'SKIP_DONE':
            require(state in ('completed', 'canceled'), 'Done skip contradicts board')
        elif disposition == 'SKIP_LIVE_OWNER':
            require(r['live_owner'], 'live-owner skip without readback')
        elif disposition == 'SKIP_DEPENDENCY':
            require(not r['dependencies_met'], 'unmet dependency not retained')
        elif disposition == 'SKIP_HOLD':
            require(r['held'] and r['reason'], 'hold not retained')
        elif disposition == 'SELECT':
            require(state not in ('completed', 'canceled') and not r['live_owner'] and r['dependencies_met'] and not r['held'], 'ineligible selection')
        elif disposition == 'SKIP_RECONCILED':
            require(inp['previous'] is not None and any(a['issue'] == r['issue'] for a in inp['previous']['data']['actions']), 'reconciliation predecessor action absent')
        else:
            raise ValueError('unknown/unjustified skip: ' + disposition)
    previous = inp['previous']
    reconciliation = decision['reconciliation']
    if previous:
        prior_actions = previous['data']['actions']
        require(len(reconciliation) == len(prior_actions), 'prior logical actions not fully reconciled')
        for old, rec in zip(prior_actions, reconciliation):
            require(tuple(rec['logical_key']) == key(old), 'logical identity changed')
            require(rec['prior_effect'] == old['effect'], 'prior effect identity changed')
            require(rec['disposition'] == 'UNCHANGED_NO_REPEAT', 'not unchanged second-run reconciliation')
            require(key(old) not in [key(a) for a in selected], 'repeated logical effect')
            require(any(x['data']['issue'] == old['issue'] and x['data']['fingerprint'] == old['fingerprint'] and x['sha256'] == old['inbox_sha256'] for x in inp['inbox']), 'previous logical input changed')
            require(rec['readback_path'] in [ref['path'] for ref in refs], 'reconciliation readback absent')
    else:
        require(reconciliation == [], 'unexpected predecessor reconciliation')
    for action in selected:
        require(action['kind'] in ('dispatch', 'close'), 'unsupported effect')
        matches = [x for x in inp['inbox'] if x['data']['issue'] == action['issue'] and x['data']['fingerprint'] == action['fingerprint']]
        require(len(matches) == 1 and action['inbox_sha256'] == matches[0]['sha256'], 'selected inbox binding absent')
        require(action['guard']['status'] == 'IDLE' and action['guard_message_id'], 'pre-effect guard missing')
        require(action['effect']['message_id'] and action['owner'], 'effect/owner binding absent')
        if action['kind'] == 'close':
            require(matches[0]['data']['status'] == 'READY_TO_CLOSE', 'closure not from poller READY')
    return inp, decision


def rodb(path):
    # Never let SQLite create/update a live WAL/SHM sidecar. A nonempty WAL
    # needs an owner-retained coherent checkpoint/snapshot, not a guessed merge.
    path = Path(path)
    wal = Path(str(path) + '-wal')
    require(not wal.exists() or wal.stat().st_size == 0, 'nonempty WAL: supply a coherent retained native DB snapshot')
    before = path.stat()
    data = path.read_bytes()
    after = path.stat()
    require((before.st_ino, before.st_size, before.st_mtime_ns) == (after.st_ino, after.st_size, after.st_mtime_ns), 'native DB changed during read')
    require(not wal.exists() or wal.stat().st_size == 0, 'WAL appeared during read')
    # Volatile copy only: permit deserialization of a checkpointed WAL header.
    require(data[:16] == b'SQLite format 3\x00', 'not a native SQLite snapshot')
    data = data[:18] + b'\x01\x01' + data[20:]
    db = sqlite3.connect(':memory:')
    db.deserialize(data)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    return db


def closer_call(native, session, message, issue, worker, flag):
    """Authenticate a direct closer invocation, not stdout from a log/no-op."""
    require(message and message['role'] == 'tool' and message['tool_name'] == 'terminal', 'not actual closer output')
    require('tool_call_id' in message.keys() and message['tool_call_id'], 'native closer invocation binding absent')
    matches = []
    for row in native.execute('SELECT * FROM messages WHERE session_id=? AND role=? AND id<? AND tool_calls IS NOT NULL',
                              (session, 'assistant', message['id'])):
        for call in json.loads(row['tool_calls']):
            if call['id'] == message['tool_call_id']:
                matches.append((row, call))
    require(len(matches) == 1, 'native closer invocation binding absent or ambiguous')
    invoked, call = matches[0]
    require(call['function']['name'] == 'terminal', 'not native terminal closer invocation')
    words = shlex.split(json.loads(call['function']['arguments'])['command'])
    require(words[:1] == ['LINEAR_ROLE=orchestrator'] and len(words) == 6 and
            Path(words[1]).name in ('python3', 'python') and
            words[2] == 'scripts/linear_close.py' and words[3:] == [issue, worker, flag],
            'not direct gated linear_close invocation')
    require(invoked['timestamp'] <= message['timestamp'], 'closer invocation chronology absent')
    return invoked


def meaningful_business_output(value):
    """Require a typed observation, not container presence or execution status."""
    if isinstance(value, dict):
        metadata = {'status', 'state', 'success', 'ok', 'exit_code', 'rc', 'http_status', 'status_code'}
        return any(isinstance(k, str) and k.strip() and k.strip().lower() not in metadata and
                   meaningful_business_output(v) for k, v in value.items())
    if isinstance(value, list):
        return any(meaningful_business_output(v) for v in value)
    # bool is an int subclass, but it is not a business count.
    if type(value) in (int, float):
        return value != 0 and (type(value) is int or math.isfinite(value))
    if isinstance(value, str):
        return bool(value.strip()) and value.strip().lower() not in {
            'ok', 'pass', '200', 'failed', 'failure', 'success', 'true', 'false'}
    return False


def authenticate(root, directory, executions, native, output_root):
    inp, decision = validate(root, directory)
    execution = executions.execute('SELECT * FROM executions WHERE id=?', (decision['execution_id'],)).fetchone()
    require(execution and execution['job_id'] == JOB and execution['source'] == 'builtin' and execution['scheduled_instant'] and
            execution['status'] == 'completed' and execution['delivery_outcome'] == 'delivered', 'completed delivered natural execution absent')
    session = native.execute('SELECT * FROM sessions WHERE id=?', (decision['coordinator_session'],)).fetchone()
    require(session and session['source'] == 'oneshot', 'native coordinator session absent')
    start = dt.datetime.fromisoformat(execution['started_at']).timestamp()
    finish = dt.datetime.fromisoformat(execution['finished_at']).timestamp()
    require(start <= session['started_at'] <= finish and session['ended_at'] is not None and session['ended_at'] <= finish, 'session outside execution')
    output = Path(decision['native_output'])
    require(output.resolve().is_relative_to(output_root.resolve()), 'foreign native output')
    text = output.read_text()
    require(decision['coordinator_session'] in text and str(directory) in text, 'native output lacks session/receipt binding')
    require(load(directory / 'RUNNER-TERMINAL.json')['coordinator_rc'] == 0, 'runner did not complete successfully')
    for ref in decision['readbacks']:
        observed = native.execute('SELECT * FROM messages WHERE id=? AND session_id=?', (ref['native_message_id'], session['id'])).fetchone()
        require(observed and observed['role'] == 'tool' and observed['tool_name'] == 'terminal', 'native input readback absent')
        payload = json.loads(observed['content'])
        require(payload['exit_code'] == 0 and ref['path'] in payload['output'] and ref['sha256'] in payload['output'], 'native input path/digest binding absent')
    for rec in decision['reconciliation']:
        prior = rec['prior_effect']
        if 'child_session' in prior:
            child = native.execute('SELECT * FROM sessions WHERE id=?', (prior['child_session'],)).fetchone()
            require(child and child['ended_at'] is not None and child['ended_at'] < session['started_at'], 'prior child not terminal before second run')
            result = load(root / rec['readback_path'])
            require(result['status'] == 'DONE_READY_TO_CLOSE', 'failed continuation is not unchanged completed-output proof')
            require(result.get('issue') == rec['logical_key'][0] and result.get('owner') == rec['logical_key'][1] and
                    result.get('child_session') == child['id'] and result.get('logical_key') == rec['logical_key'],
                    'worker result issue/child/action identity mismatch')
            result_sha = binding(root, rec['readback_path'])['sha256']
            final = native.execute("SELECT * FROM messages WHERE session_id=? AND role='assistant' ORDER BY id DESC LIMIT 1", (child['id'],)).fetchone()
            require(final and rec['readback_path'] in (final['content'] or '') and result_sha in final['content'] and
                    child['started_at'] <= final['timestamp'] <= child['ended_at'], 'worker result not bound to genuine child final output')
            output = result.get('output_receipt') or {}
            require(output.get('path') and binding(root, output['path'])['sha256'] == output.get('sha256'), 'accepted worker output receipt absent')
            observed = load(root / output['path'])
            require(observed.get('issue') == result['issue'] and observed.get('logical_key') == rec['logical_key'] and
                    isinstance(observed.get('output'), dict) and observed['output'] and
                    meaningful_business_output(observed['output']),
                    'accepted worker output is not a concrete bound observation')
        else:
            require(load(root / rec['readback_path'])['state']['type'] == 'completed', 'prior Done no longer observed')
    actual_calls = []
    actual_tasks = []
    for message in native.execute('SELECT tool_calls FROM messages WHERE session_id=? AND tool_calls IS NOT NULL', (session['id'],)):
        for call in json.loads(message['tool_calls']):
            if call.get('function', {}).get('name') == 'delegate_task':
                actual_calls.append(call['id'])
                tasks = json.loads(call['function']['arguments']).get('tasks', [])
                actual_tasks.extend((call['id'], task['goal']) for task in tasks)
    dispatches = [a for a in decision['actions'] if a['kind'] == 'dispatch']
    require(sorted(set(actual_calls)) == sorted(set(a['effect']['call_id'] for a in dispatches)) and
            len(actual_calls) == len(set(actual_calls)), 'unreported or duplicated native dispatch')
    expected_tasks = [(a['effect']['call_id'], a['effect']['brief']) for a in dispatches]
    require(sorted(actual_tasks) == sorted(expected_tasks) and len(actual_tasks) == len(set(actual_tasks)),
            'unreported or ambiguous batched task/child')
    actual_closures = []
    for message in native.execute("SELECT id,content FROM messages WHERE session_id=? AND role='tool' AND tool_name='terminal'", (session['id'],)):
        payload = json.loads(message['content'])
        if payload.get('exit_code') == 0 and any(r['issue'] + ' Done ' in payload.get('output', '') for r in decision['eligibility']):
            actual_closures.append(message['id'])
    require(sorted(actual_closures) == sorted(a['effect']['message_id'] for a in decision['actions'] if a['kind'] == 'close'), 'unreported or repeated native closure')
    for a in decision['actions']:
        effect = a['effect']
        m = native.execute('SELECT * FROM messages WHERE id=? AND session_id=?', (effect['message_id'], session['id'])).fetchone()
        g = native.execute('SELECT * FROM messages WHERE id=? AND session_id=?', (a['guard_message_id'], session['id'])).fetchone()
        require(m and g and g['id'] < m['id'] and g['role'] == 'tool', 'causal native guard/effect absent')
        guard = json.loads(g['content'])
        require(guard['exit_code'] == 0 and '"status": "IDLE"' in guard['output'], 'native pre-effect guard failed')
        # A digest printed after dispatch is not evidence available at admission.
        # Only preconditions belong here; Done/worker outcome reads may be later.
        row = next(r for r in decision['eligibility'] if r['issue'] == a['issue'])
        preconditions = set(row['evidence_paths'])
        preconditions.update(ref['path'] for ref in decision['readbacks']
                             if load(root / ref['path']) == decision['linear_readback'])
        require(session['started_at'] <= g['timestamp'] <= m['timestamp'] <= session['ended_at'],
                'pre-effect evidence chronology: guard/effect outside causal order')
        for ref in decision['readbacks']:
            if ref['path'] in preconditions:
                observed = native.execute('SELECT timestamp FROM messages WHERE id=? AND session_id=?',
                                          (ref['native_message_id'], session['id'])).fetchone()
                require(observed and session['started_at'] <= observed['timestamp'] <= g['timestamp'],
                        'pre-effect evidence chronology: input was not observed before guard')
        if a['kind'] == 'dispatch':
            calls = json.loads(m['tool_calls'])
            call = next(c for c in calls if c['id'] == effect['call_id'])
            require(call['function']['name'] == 'delegate_task', 'not actual native dispatch')
            tasks = json.loads(call['function']['arguments'])['tasks']
            task = next(t for t in tasks if t['goal'] == effect['brief'])
            require(a['issue'] in task['goal'] and a['owner'] in task['goal'] and str(directory) in task['goal'], 'dispatch lacks causal input/owner binding')
            require(task['provider'] == 'openai-codex' and task['model'] == 'gpt-6.1-sol' and task['speed'] == 'fast', 'unexpected route')
            child = native.execute('SELECT * FROM sessions WHERE id=?', (effect['child_session'],)).fetchone()
            require(child and child['parent_session_id'] == session['id'] and child['source'] == 'subagent', 'child not native descendant')
            require(native.execute('SELECT 1 FROM messages WHERE session_id=? AND role=? AND content=?', (child['id'], 'user', task['goal'])).fetchone(), 'child brief mismatch')
        else:
            worker = effect.get('worker_result')
            require(worker, 'native closer invocation/worker binding absent')
            invoked = closer_call(native, session['id'], m, a['issue'], worker, '--apply')
            preflight = native.execute('SELECT * FROM messages WHERE id=? AND session_id=?',
                                       (effect['preflight_message_id'], session['id'])).fetchone()
            gate_call = closer_call(native, session['id'], preflight, a['issue'], worker, '--check-only')
            gate = json.loads(preflight['content'])
            require(gate['exit_code'] == 0 and a['issue'] + ' PASS gate=prevalidated' in gate['output'] and
                    g['timestamp'] < gate_call['timestamp'] <= preflight['timestamp'] < invoked['timestamp'], 'new OPEN closer gate absent')
            result = json.loads(m['content'])
            require(result['exit_code'] == 0 and a['issue'] + ' Done ' in result['output'], 'actual new closure absent')
            require(effect['done_readback'] in [r['path'] for r in decision['readbacks']], 'Done readback not bound')
            done = load(root / effect['done_readback'])
            before = decision['linear_readback'][a['issue']]
            require(done['identifier'] == a['issue'] and done['id'] == before['id'] and done['state']['type'] == 'completed' and
                    before['state']['type'] not in ('completed', 'canceled'), 'OPEN-to-Done readback missing')
            changed = dt.datetime.fromisoformat(done['updatedAt'].replace('Z', '+00:00')).timestamp()
            require(invoked['timestamp'] <= changed <= m['timestamp'], 'historical Done/no-op is not a new transition')
            done_ref = next(r for r in decision['readbacks'] if r['path'] == effect['done_readback'])
            done_message = native.execute('SELECT timestamp FROM messages WHERE id=? AND session_id=?',
                                          (done_ref['native_message_id'], session['id'])).fetchone()
            require(done_message and m['timestamp'] <= done_message['timestamp'] <= finish, 'Done readback precedes closure')
    children = [r['id'] for r in native.execute("SELECT id FROM sessions WHERE parent_session_id=? AND source='subagent'", (session['id'],))]
    require(sorted(children) == sorted(a['effect']['child_session'] for a in dispatches) and
            len(children) == len(set(a['effect']['child_session'] for a in dispatches)), 'unreported or ambiguous native child')
    return execution, decision


def prerequisite_closure(root):
    base = root / 'reports/ops/fable-plan-2-20261009/lanes/C1'
    ready = load(base / 'NATURAL-GLP355-READY.json')['row']
    typed = load(base / 'GLP-355-worker/GLP-355/RESULT.json')
    done = load(base / 'FINAL-GLP355-DONE.json')
    worker = typed['worker_result']['path']
    require(ready['issue'] == 'GLP-355' and ready['status'] == 'READY_TO_CLOSE', 'prerequisite poller READY absent')
    require(ready['checks']['proof']['rc'] == ready['checks']['linear_close']['rc'] == 0, 'prerequisite gates failed')
    require(ready['worker_result'] == worker and binding(root, worker)['sha256'] == typed['worker_result']['sha256'] == ready['evidence'][worker], 'prerequisite typed worker binding changed')
    require(done['identifier'] == 'GLP-355' and done['state']['type'] == 'completed', 'prerequisite Done readback absent')
    return worker


def verify(args):
    root = Path(args.root)
    worker = prerequisite_closure(root)
    first, second = Path(args.first), Path(args.second)
    require(all(p.resolve().is_relative_to(root.resolve()) and not p.is_symlink() for p in (first, second)), 'foreign/symlink receipt directory')
    with rodb(args.executions_db) as executions, rodb(args.state_db) as native:
        e1, d1 = authenticate(root, first, executions, native, Path(args.output_root))
        e2, d2 = authenticate(root, second, executions, native, Path(args.output_root))
        require(e1['id'] != e2['id'] and e1['finished_at'] < e2['started_at'], 'second natural run not subsequent')
        require(d1['actions'], 'first run has no real action')
        inp2 = load(second / 'INPUT.json')
        require(inp2['previous'] and inp2['previous']['path'] == str((first / 'DECISION.json').relative_to(root)) and
                inp2['previous']['sha256'] == sha((first / 'DECISION.json').read_bytes()), 'second does not reconcile exact first decision')
        require(d2['actions'] == [], 'second has new effects, not unchanged-input idempotence proof')
        require(load(first / 'INPUT.json')['plan']['sha256'] == inp2['plan']['sha256'], 'PLAN changed between proof runs')
        for field in ('closure_message', 'activation_message'):
            require(getattr(args, field), 'timestamped preactivation closure/resume native messages required')
        close = native.execute('SELECT * FROM messages WHERE id=?', (args.closure_message,)).fetchone()
        resume = native.execute('SELECT * FROM messages WHERE id=?', (args.activation_message,)).fetchone()
        require(close and resume and close['timestamp'] < resume['timestamp'] <
                dt.datetime.fromisoformat(e1['started_at']).timestamp() and close['role'] == resume['role'] == 'tool',
                'activation ordering absent: prerequisite must precede first natural execution')
        c, r = json.loads(close['content']), json.loads(resume['content'])
        require(c['exit_code'] == r['exit_code'] == 0 and 'GLP-355 Done ' + worker in c['output'] and 'Resumed job:' in r['output'] and JOB in r['output'], 'real prerequisite closure/resume absent')
    return {'status': 'PASS', 'natural_actions': len(d1['actions']), 'second_run_repeated_effects': 0, 'writes': 0}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=['verify'])
    p.add_argument('--root', default='/home/ubuntu/business')
    p.add_argument('--first', required=True)
    p.add_argument('--second', required=True)
    p.add_argument('--executions-db', default='/home/ubuntu/.hermes/cron/executions.db')
    p.add_argument('--state-db', default='/home/ubuntu/.hermes/state.db')
    p.add_argument('--output-root', default='/home/ubuntu/.hermes/cron/output/13d3fff8734a')
    p.add_argument('--closure-message', type=int)
    p.add_argument('--activation-message', type=int)
    try:
        print(json.dumps(verify(p.parse_args(argv)), sort_keys=True))
        return 0
    except (ValueError, KeyError, TypeError, OSError, StopIteration, sqlite3.Error) as e:
        print(json.dumps({'status': 'INCOMPLETE', 'reason': str(e), 'writes': 0}))
        return 3


if __name__ == '__main__':
    sys.exit(main())
