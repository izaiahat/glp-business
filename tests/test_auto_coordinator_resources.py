"""Bounded slot/resource and exact input custody regressions."""
from pathlib import Path
import json
import os
import sys
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import auto_coordinator_loop as m
from test_auto_coordinator_loop import task, row, board, Backend


def test_writers_require_disjoint_areas_and_maximum_three():
    plan = [task(f'GLP-{i}', i, read_only=False, write_area=f'repo:area-{i}') for i in range(1, 6)]
    assert len(m.select(plan, [], board(*(p['issue'] for p in plan)), slots=7)[0]) == 3
    plan[1]['write_area'] = plan[0]['write_area']
    assert [a['issue'] for a in m.select(plan[:2], [], board('GLP-1', 'GLP-2'), slots=7)[0]] == ['GLP-1']


def test_heavy_requires_fresh_external_admission_not_implicit_refill():
    assert not m.select([task(heavy=True)], [], board('GLP-1'), slots=7)[0]
    assert not m.select([task(read_only=False)], [], board('GLP-1'), slots=7)[0]


def test_inputs_hash_bind_owner_and_reject_changed_result(tmp_path):
    plan = tmp_path / m.PLAN; plan.parent.mkdir(parents=True)
    plan.write_text(json.dumps({'issues': [task()]}))
    result = tmp_path / 'reports/ops/owner/RESULT.json'; result.parent.mkdir(parents=True)
    result.write_text(json.dumps({'issue': 'GLP-1', 'owner': 'OWNER', 'pid': 99999999}))
    p = tmp_path / 'reports/ops/fleet-inbox/GLP-1/native/INGEST.json'; p.parent.mkdir(parents=True)
    r = row(worker_result=str(result), evidence={str(result): m.digest(result.read_bytes())}, schema='fleet-ingest-v1')
    p.write_text(json.dumps(r))
    assert m.inputs(tmp_path)[1][0]['owner'] == 'OWNER'
    result.write_text('{}')
    with pytest.raises(ValueError, match='changed'):
        m.inputs(tmp_path)


def test_dead_owned_pid_not_just_plan_claim_is_required(tmp_path):
    plan = tmp_path / m.PLAN; plan.parent.mkdir(parents=True)
    plan.write_text(json.dumps({'issues': [task()]}))
    result = tmp_path / 'reports/ops/owner/RESULT.json'; result.parent.mkdir(parents=True)
    result.write_text(json.dumps({'issue': 'GLP-1', 'owner': 'OWNER', 'pid': os.getpid()}))
    p = tmp_path / 'reports/ops/fleet-inbox/GLP-1/native/INGEST.json'; p.parent.mkdir(parents=True)
    p.write_text(json.dumps(row(worker_result=str(result), evidence={str(result): m.digest(result.read_bytes())}, schema='fleet-ingest-v1')))
    assert m.inputs(tmp_path)[1][0]['status'] == 'LIVE_OWNER'


def test_old_non_plan_row_does_not_block_the_current_plan(tmp_path):
    plan = tmp_path / m.PLAN; plan.parent.mkdir(parents=True)
    plan.write_text(json.dumps({'issues': [task()]}))
    p = tmp_path / 'reports/ops/fleet-inbox/GLP-999/native/INGEST.json'; p.parent.mkdir(parents=True)
    p.write_text(json.dumps(row(issue='GLP-999', schema='fleet-ingest-v1', evidence={'reports/missing.json': 'bad'})))
    assert m.inputs(tmp_path)[1] == []


def test_child_command_has_no_delegate_or_tui_and_explicit_routing():
    cmd = m.worker_command(task(), Path('brief.md'))
    assert cmd[cmd.index('--provider') + 1] == 'openai-codex'
    assert cmd[cmd.index('-m') + 1] == 'gpt-6.1-sol'
    assert '--cli' in cmd and '--oneshot' in cmd and '--resume' not in cmd
    assert 'delegate' not in cmd[cmd.index('--toolsets') + 1]
