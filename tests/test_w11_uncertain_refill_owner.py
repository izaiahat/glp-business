import importlib.util
import sqlite3
import subprocess
import pytest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / 'scripts/glp_seo_buffer_dispatch.py'
spec = importlib.util.spec_from_file_location('dispatch_w11', SOURCE)
assert spec is not None and spec.loader is not None
dispatch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dispatch)


def test_unknown_live_owner_remains_capacity_not_idle(tmp_path, monkeypatch):
    db = tmp_path / 'executions.db'
    with sqlite3.connect(db) as c:
        c.execute('CREATE TABLE executions(id TEXT,job_id TEXT,status TEXT,pid INT,process_started_at INT)')
        c.execute('INSERT INTO executions VALUES(?,?,?,?,?)', ('exact-uncertain', dispatch.REFILL_JOB_ID, 'unknown', 777, 12345))
    monkeypatch.setattr(dispatch, 'EXECUTIONS_DB', db)
    monkeypatch.setattr(dispatch, '_pid_starttime', lambda pid: 12345)
    monkeypatch.setattr(dispatch.subprocess, 'run', lambda *a, **k: subprocess.CompletedProcess(a, 0, 'exact-uncertain unknown job=' + dispatch.REFILL_JOB_ID, ''))
    launched = []
    monkeypatch.setattr(dispatch, 'launch_refill', lambda: launched.append(True))
    assert dispatch.execute('status')['status'] == 'PASS_RUNNING'
    receipt = dispatch.execute('dispatch')
    assert receipt['status'] == 'PASS_ALREADY_RUNNING'
    assert receipt['running_execution_ids'] == ['exact-uncertain']
    assert launched == []


def test_unavailable_owner_census_refuses_before_dispatch(tmp_path, monkeypatch):
    monkeypatch.setattr(dispatch, 'EXECUTIONS_DB', tmp_path / 'absent.db')
    monkeypatch.setattr(dispatch.subprocess, 'run', lambda *a, **k: subprocess.CompletedProcess(a, 0, '', ''))
    launched = []
    monkeypatch.setattr(dispatch, 'launch_refill', lambda: launched.append(True))
    with pytest.raises(RuntimeError, match='uncertain_owner_census_unavailable'):
        dispatch.execute('dispatch')
    assert launched == []


def test_unknown_exited_or_reused_pid_does_not_occupy_slot(tmp_path, monkeypatch):
    db = tmp_path / 'executions.db'
    with sqlite3.connect(db) as c:
        c.execute('CREATE TABLE executions(id TEXT,job_id TEXT,status TEXT,pid INT,process_started_at INT)')
        c.execute('INSERT INTO executions VALUES(?,?,?,?,?)', ('old-uncertain', dispatch.REFILL_JOB_ID, 'unknown', 777, 12345))
    monkeypatch.setattr(dispatch, 'EXECUTIONS_DB', db)
    monkeypatch.setattr(dispatch.subprocess, 'run', lambda *a, **k: subprocess.CompletedProcess(a, 0, 'old-uncertain unknown job=' + dispatch.REFILL_JOB_ID, ''))
    for current in (None, 99999):
        monkeypatch.setattr(dispatch, '_pid_starttime', lambda pid: current)
        receipt = dispatch.execute('status')
        assert receipt['status'] == 'PASS_IDLE'
        assert receipt['ignored_stale_execution_ids'] == ['old-uncertain']
