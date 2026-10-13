"""RV-451b independent adverse probes (reviewer-authored; not PR source).

Run from a scratch tree whose tests/ holds this file next to the PR's
tests/test_auto_coordinator_receipt.py and whose scripts/ holds the verifier
under review (head 6ca98b56 or original a398e77a). Offline fixtures only.
"""
import json
import sqlite3
from pathlib import Path
import pytest

import test_auto_coordinator_receipt as t
m = t.m

S1_START = 1791766800 + 1   # fixture session s1 started_at
GUARD, EFFECT = 1791766803, 1791766804


def run1(args):
    with m.rodb(args.executions_db) as ex, m.rodb(args.state_db) as nat:
        return m.authenticate(Path(args.root), Path(args.first), ex, nat, Path(args.output_root))


def base(tmp_path):
    paths = t.fixture(tmp_path)
    args = t.native_fixture(tmp_path, paths)
    assert m.verify(args)['status'] == 'PASS'
    return paths, args


def set_ts(args, updates):
    with sqlite3.connect(args.state_db) as db:
        for mid, ts in updates:
            db.execute('UPDATE messages SET timestamp=? WHERE id=?', (ts, mid))


# ---- must REFUSE (chronology adversaries) ----
REFUSE = {
    'A1_eligibility_board_after_dispatch': [(11, EFFECT + 1)],
    'A2_readback_predates_session': [(11, S1_START - 2)],
    'A3_guard_after_dispatch': [(20, EFFECT + 1)],
    'A4_readback_between_guard_and_effect': [(11, GUARD + 0.5)],
    'A5_guard_and_inputs_before_session': [(11, S1_START - 3), (20, S1_START - 2)],
    'A6_effect_after_session_end': [(30, S1_START + 1700 + 1)],
    'A7_float_readback_after_guard': [(11, 1791766803.000001)],
}


@pytest.mark.parametrize('name', sorted(REFUSE))
def test_refuses(tmp_path, name):
    _, args = base(tmp_path)
    set_ts(args, REFUSE[name])
    with pytest.raises(ValueError, match='chronology'):
        m.verify(args)


def test_A8_readback_moved_to_other_session_refused(tmp_path):
    _, args = base(tmp_path)
    with sqlite3.connect(args.state_db) as db:
        db.execute("UPDATE messages SET session_id='s2' WHERE id=11")
    with pytest.raises(ValueError):
        m.verify(args)


# ---- must PASS (no false-REFUSE on genuine boundary orders) ----
PASS = {
    'B1_readback_equals_guard_ts_lower_id': [(11, GUARD)],
    'B2_readback_at_session_start': [(11, S1_START)],
    'B3_guard_equals_effect_ts_lower_id': [(20, EFFECT)],
    'B4_float_subsecond_order': [(11, 1791766802.25), (20, 1791766803.5), (30, 1791766803.75)],
}


@pytest.mark.parametrize('name', sorted(PASS))
def test_passes(tmp_path, name):
    _, args = base(tmp_path)
    set_ts(args, PASS[name])
    assert m.verify(args)['status'] == 'PASS'


def _add_ref(tmp_path, paths, args, name, data, mid, ts, eligibility_row=False):
    """Retain one more genuine readback in run 1 at native message `mid`."""
    d = m.load(paths[0] / 'DECISION.json')
    p = paths[0] / name
    t.put(p, data)
    rel = str(p.relative_to(tmp_path))
    ref = {'path': rel, 'sha256': m.sha(p.read_bytes()), 'native_message_id': mid}
    d['readbacks'].append(ref)
    if eligibility_row:
        d['eligibility'][0]['evidence_paths'].append(rel)
    t.put(paths[0] / 'DECISION.json', d)
    with sqlite3.connect(args.state_db) as db:
        db.execute('INSERT INTO messages VALUES(?,?,?,?,?,?,?)', (mid, 's1', 'tool', 'terminal',
                   json.dumps({'exit_code': 0, 'output': rel + ' ' + ref['sha256']}), None, ts))


def test_B5_post_effect_outcome_readback_allowed(tmp_path):
    """A non-precondition readback (worker/outcome) observed after dispatch."""
    paths, args = base(tmp_path)
    _add_ref(tmp_path, paths, args, 'outcome.json', {'status': 'DONE_READY_TO_CLOSE', 'note': 'post'}, 41, EFFECT + 100)
    assert run1(args)[1]['actions'][0]['issue'] == 'GLP-358'


def test_B6_identical_board_reread_after_effect_with_valid_early_copy(tmp_path):
    """Genuine trace: board read at 802 (before guard 803), dispatch at 804,
    then the unchanged board is re-read and retained at 904.  Early causal
    evidence exists; the extra later copy must not void the dispatch."""
    paths, args = base(tmp_path)
    board = m.load(paths[0] / 'DECISION.json')['linear_readback']
    _add_ref(tmp_path, paths, args, 'board-reread.json', board, 42, EFFECT + 100)
    assert run1(args)[1]['actions'][0]['issue'] == 'GLP-358'


def _two_dispatch_run(tmp_path, reread_board):
    """Run 1 dispatches GLP-358 (guard 803, effect 804) and then GLP-355
    (guard 850, effect 851).  AUTO-COORDINATOR.md: 'Recheck immediately before
    every dispatch/close'.  With reread_board the coordinator re-reads and
    retains the (unchanged) Linear board at 849, before the second guard."""
    t.put(tmp_path / 'reports/ops/fleet-inbox/GLP-355/lane/INGEST.json',
          {'issue': 'GLP-355', 'fingerprint': 'fp5', 'status': 'REDISPATCH'})
    paths = t.fixture(tmp_path)
    args = t.native_fixture(tmp_path, paths)
    d = m.load(paths[0] / 'DECISION.json')
    inp = m.load(paths[0] / 'INPUT.json')
    board = {'GLP-358': {'state': {'type': 'started'}}, 'GLP-355': {'state': {'type': 'started'}}}
    d['linear_readback'] = board
    bp = tmp_path / d['readbacks'][0]['path']
    t.put(bp, board)
    d['readbacks'][0]['sha256'] = m.sha(bp.read_bytes())
    d['eligibility'][1]['disposition'] = 'SELECT'
    inbox5 = next(x for x in inp['inbox'] if x['data']['issue'] == 'GLP-355')
    # fixture() binds inbox[0]; with a GLP-355 row sorted first, rebind GLP-358 exactly.
    d['actions'][0]['inbox_sha256'] = next(x for x in inp['inbox'] if x['data']['issue'] == 'GLP-358')['sha256']
    brief = 'GLP-355 owner5 ' + str(paths[0])
    d['actions'].append({'issue': 'GLP-355', 'owner': 'owner5', 'kind': 'dispatch', 'fingerprint': 'fp5',
                         'inbox_sha256': inbox5['sha256'], 'guard': {'status': 'IDLE'}, 'guard_message_id': 21,
                         'effect': {'message_id': 33, 'call_id': 'call2', 'brief': brief, 'child_session': 'child2'}})
    rows = [(21, 's1', 'tool', 'terminal', json.dumps({'exit_code': 0, 'output': '{"status": "IDLE"}'}), None, 1791766850),
            (33, 's1', 'assistant', None, None, json.dumps([{'id': 'call2', 'function': {'name': 'delegate_task',
             'arguments': json.dumps({'tasks': [{'goal': brief, 'provider': 'openai-codex', 'model': 'gpt-6.1-sol', 'speed': 'fast'}]})}}]), 1791766851),
            (34, 'child2', 'user', None, brief, None, 1791766860)]
    if reread_board:
        rp = paths[0] / 'board-recheck.json'
        t.put(rp, board)
        rel = str(rp.relative_to(tmp_path))
        d['readbacks'].append({'path': rel, 'sha256': m.sha(rp.read_bytes()), 'native_message_id': 44})
        rows.append((44, 's1', 'tool', 'terminal', json.dumps({'exit_code': 0, 'output': rel + ' ' + d['readbacks'][-1]['sha256']}), None, 1791766849))
    t.put(paths[0] / 'DECISION.json', d)
    text = '\n'.join(r['path'] + ' ' + r['sha256'] for r in d['readbacks'][:3])
    with sqlite3.connect(args.state_db) as db:
        db.execute('UPDATE messages SET content=? WHERE id=11', (json.dumps({'exit_code': 0, 'output': text}),))
        db.execute("INSERT INTO sessions VALUES('child2','subagent',1791766860,1791768000,'s1')")
        db.executemany('INSERT INTO messages VALUES(?,?,?,?,?,?,?)', rows)
    return args


def test_B7_two_dispatches_control(tmp_path):
    assert run1(_two_dispatch_run(tmp_path, False))[1]['actions'][1]['issue'] == 'GLP-355'


def test_B8_two_dispatches_with_mandated_board_recheck(tmp_path):
    """Genuine causal order (both effects preceded by an observed board).
    Must PASS; a refusal here is a new false-REFUSE."""
    assert run1(_two_dispatch_run(tmp_path, True))[1]['actions'][1]['issue'] == 'GLP-355'


def test_A9_only_board_copy_after_effect_refused(tmp_path):
    """Adversary: same as B6 but the early board copy is absent (only the late copy)."""
    paths, args = base(tmp_path)
    d = m.load(paths[0] / 'DECISION.json')
    board_ref = d['readbacks'][0]
    with sqlite3.connect(args.state_db) as db:
        db.execute('INSERT INTO messages VALUES(?,?,?,?,?,?,?)', (43, 's1', 'tool', 'terminal',
                   json.dumps({'exit_code': 0, 'output': board_ref['path'] + ' ' + board_ref['sha256']}), None, EFFECT + 100))
    board_ref['native_message_id'] = 43
    t.put(paths[0] / 'DECISION.json', d)
    with pytest.raises(ValueError, match='chronology'):
        run1(args)
