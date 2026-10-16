"""Offline GLP-358 loop: all effects are fakes, never live scheduler/Linear."""
import copy
import json
from pathlib import Path
import sys
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import auto_coordinator_loop as m


def task(issue='GLP-1', order=1, **kw):
    p = dict({'issue': issue, 'order': order, 'owner': 'OWNER', 'executor': 'sol',
              'worktree': '/home/ubuntu/business/.worktrees/owned-' + issue, 'read_only': True, 'heavy': False,
              'next_action': 'Repair the assigned producer'}, **kw)
    p['depends_on'] = p.get('depends_on', [])
    return p


def board(*issues):
    return {i: {'state': 'In Progress', 'held': False, 'live_owner': False} for i in issues}


def row(issue='GLP-1', status='REDISPATCH', **kw):
    return dict({'issue': issue, 'status': status, 'owner': 'OWNER', 'fingerprint': 'a' * 64,
                 'source': 'native', 'identity': 'source-1', 'failure_text': 'exact proof failure'}, **kw)


class Backend:
    def __init__(self):
        self.calls = []
        self.states = board('GLP-1', 'GLP-2', 'GLP-3')
        self.idle = True
        self.preflight_ok = True
        self.fail = None
    def guard(self):
        return {'status': 'IDLE' if self.idle else 'DEFERRED_TUI_CHILDREN'}
    def observe(self, ids):
        return {i: copy.deepcopy(self.states[i]) for i in ids}
    def preflight(self, action):
        self.calls.append(('preflight', action['issue']))
        return self.preflight_ok
    def effect(self, action):
        self.calls.append((action['kind'], action['issue']))
        if self.fail:
            raise self.fail
        if action['kind'] == 'close':
            self.states[action['issue']]['state'] = 'Done'
        return {'target': action['issue'], 'observed': 'fake-test-effect'}


def test_numeric_plan_order_same_owner_failure_and_seven_child_slots():
    plan = [task('GLP-2', 2), task('GLP-1', 1)]
    actions, skipped = m.select(plan, [row('GLP-2'), row()], board('GLP-1', 'GLP-2'), slots=7)
    assert [(a['issue'], a['kind'], a['owner']) for a in actions] == [
        ('GLP-1', 'redispatch', 'OWNER'), ('GLP-2', 'redispatch', 'OWNER')]
    assert actions[0]['failure_text'] == 'exact proof failure'
    assert not skipped
    assert len(m.select([task(f'GLP-{i}', i) for i in range(1, 10)], [row(f'GLP-{i}') for i in range(1, 10)],
                        board(*(f'GLP-{i}' for i in range(1, 10))), slots=7)[0]) == 7


@pytest.mark.parametrize('inbox', [[], [row(source='pro')], [row(source='dot')]])
def test_reviewer_empty_or_advisory_only_inbox_has_zero_effects(tmp_path, inbox):
    actions, skipped = m.select([task()], inbox, board('GLP-1'), slots=7)
    assert actions == []
    assert skipped == [{'issue': 'GLP-1', 'reason': 'NO_ELIGIBLE_INBOX_ROW'}]
    b = Backend()
    result = m.run_pass(tmp_path, [task()], inbox, b)
    assert result['closed'] == result['dispatched'] == result['uncertain'] == 0
    assert b.calls == []
    assert not (tmp_path / 'effects').exists()


@pytest.mark.parametrize('field,value', [('held', True), ('live_owner', True), ('state', 'Done')])
def test_done_hold_and_live_owner_are_not_dispatched(field, value):
    b = board('GLP-1'); b['GLP-1'][field] = value
    assert not m.select([task()], [row()], b, slots=7)[0]


def test_unknown_dependency_or_owner_blocks_without_inventing_assignment():
    p = task(); p['depends_on'] = ['PR review not reconciled']
    assert not m.select([p], [], board('GLP-1'), slots=7)[0]
    p = task(); del p['owner']
    assert not m.select([p], [], board('GLP-1'), slots=7)[0]
    assert not m.select([task()], [row(owner='OTHER')], board('GLP-1'), slots=7)[0]


@pytest.mark.parametrize('source', ['pro', 'dot'])
def test_support_advisories_are_not_closed_consumed_or_used_as_failures(source):
    advisory = row(status='READY_TO_CLOSE', source=source)
    actions, _ = m.select([task()], [advisory, row()], board('GLP-1'), slots=7)
    assert actions[0]['kind'] == 'redispatch'
    assert actions[0]['inbox']['source'] == 'native'
    assert advisory['status'] == 'READY_TO_CLOSE'


def test_duplicate_and_unknown_effect_rows_fail_closed():
    for rows in ([row(), row(fingerprint='b' * 64)], [row(status='UNKNOWN')], [row(status='NEEDS_REVIEWER')]):
        assert not m.select([task()], rows, board('GLP-1'), slots=7)[0]
    with pytest.raises(ValueError, match='duplicate'):
        m.select([task(), task(order=2)], [], board('GLP-1'), slots=7)


def test_support_owned_tickets_stay_open_even_with_ready_row():
    for executor in ('pro', 'rook', 'finch'):
        assert not m.select([task(executor=executor)], [row(status='READY_TO_CLOSE')], board('GLP-1'), slots=7)[0]


def test_ready_runs_proof_and_reports_coordinator_readiness_without_effects(tmp_path):
    b = Backend()
    r = row(status='READY_TO_CLOSE', worker_result='reports/worker/RESULT.json')
    first = m.run_pass(tmp_path, [task()], [r], b)
    second = m.run_pass(tmp_path, [task()], [r], b)
    assert first['closed'] == second['closed'] == 0
    assert first['closure_ready'] == second['closure_ready'] == 1
    assert b.calls == [('preflight', 'GLP-1'), ('preflight', 'GLP-1')]
    assert b.states['GLP-1']['state'] == 'In Progress'
    assert not (tmp_path / 'effects').exists()
    assert r['status'] == 'READY_TO_CLOSE'  # ingestion cursor/source is not rewritten
    assert len(m.summary(first).splitlines()) <= 10


def test_preflight_refusal_does_not_close(tmp_path):
    b = Backend(); b.preflight_ok = False
    m.run_pass(tmp_path, [task()], [row(status='READY_TO_CLOSE')], b)
    assert b.calls == [('preflight', 'GLP-1')]


@pytest.mark.parametrize('error', [TimeoutError('ambiguous'), OSError('unknown')])
def test_ambiguous_effect_is_latched_even_if_fingerprint_changes(tmp_path, error):
    b = Backend(); b.fail = error
    first = m.run_pass(tmp_path, [task()], [row()], b)
    b.fail = None
    second = m.run_pass(tmp_path, [task()], [row(fingerprint='b' * 64)], b)
    assert first['uncertain'] == 1 and second['dispatched'] == 0
    assert b.calls == [('redispatch', 'GLP-1')]


def test_unacknowledged_intent_and_success_dispatch_both_block_refill(tmp_path):
    b = Backend()
    first = m.run_pass(tmp_path, [task()], [row()], b)
    assert first['dispatched'] == 1
    second = m.run_pass(tmp_path, [task()], [row()], b)
    assert second['dispatched'] == 0 and len(b.calls) == 1


def test_tui_active_or_single_writer_lock_prevents_every_effect(tmp_path):
    b = Backend(); b.idle = False
    assert m.run_pass(tmp_path, [task()], [row()], b)['status'] == 'DEFERRED_TUI_CHILDREN'
    assert not b.calls
    import fcntl
    with (tmp_path / '.loop.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        b.idle = True
        assert m.run_pass(tmp_path, [task()], [], b)['status'] == 'BUSY'
    assert not b.calls


def test_guard_and_board_are_refreshed_immediately_before_effect(tmp_path):
    b = Backend()
    def preflight(action):
        b.idle = False
        return True
    b.preflight = preflight
    result = m.run_pass(tmp_path, [task()], [row(status='READY_TO_CLOSE')], b)
    assert result['closed'] == result['closure_ready'] == 0
    assert not b.calls


@pytest.mark.parametrize('remaining,cost,category,allowed', [
    (4.99, 0, 'reviewer', False), (5, 1, 'reviewer', False),
    (200, 45, 'reviewer', True), (200, 45.01, 'reviewer', False),
    (200, 30, 'batch', True), (200, 100, 'opus', True),
    (200, 5, 'haiku', True), (200, 1, 'reserve', False),
    (float('nan'), 1, 'opus', False), (200, -1, 'opus', False)])
def test_api_floor_and_confirmed_split(remaining, cost, category, allowed):
    assert m.api_allowed({'remaining_usd': remaining, 'spent': {}}, category, cost) is allowed


def test_routes_are_fixed_explicit_and_never_paid_fallback():
    assert m.route('sol') == dict(provider='openai-codex', model='gpt-6.1-sol', effort='medium', speed='fast')
    assert m.route('astra')['speed'] == 'standard'
    for unsafe in ('api', 'cloud', 'pro', 'rook', 'finch', 'sol or api'):
        with pytest.raises(ValueError):
            m.route(unsafe)


def test_wrapper_has_no_model_coordinator_or_tui_transport():
    root = Path(__file__).resolve().parents[1]
    runner = (root / 'scripts/auto_coordinator_runner.py').read_text()
    assert 'auto_coordinator_loop' in runner
    assert '--yolo' not in runner and '--query-file' not in runner
    text = (root / 'scripts/auto_coordinator_loop.py').read_text()
    assert 'send-keys' not in text and 'tui_send' not in text
    assert 'cron resume' not in text
