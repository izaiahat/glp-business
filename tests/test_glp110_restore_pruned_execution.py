import importlib.util
import json
from pathlib import Path
import sqlite3
import pytest

spec = importlib.util.spec_from_file_location('restore', Path(__file__).parents[1] / 'scripts/glp110_restore_pruned_execution.py')
restore = importlib.util.module_from_spec(spec)
spec.loader.exec_module(restore)


def test_exact_historical_unknown_insert_and_replay_refusal(monkeypatch):
    row = {'id': restore.EXECUTION, 'job_id': restore.JOB, 'status': 'unknown'}
    monkeypatch.setattr(restore, 'ROW', restore.digest(restore.canonical(row)))
    conn = sqlite3.connect(':memory:'); conn.row_factory = sqlite3.Row
    conn.execute('CREATE TABLE executions (id TEXT PRIMARY KEY, job_id TEXT, status TEXT)')
    conn.execute("INSERT INTO executions VALUES ('sibling', 'other', 'completed')")
    restore.insert_exact(conn, row)
    assert dict(conn.execute('SELECT * FROM executions WHERE id=?', (restore.EXECUTION,)).fetchone()) == row
    assert conn.execute("SELECT status FROM executions WHERE id='sibling'").fetchone()[0] == 'completed'
    with pytest.raises(RuntimeError, match='target_ledger_not_empty'):
        restore.insert_exact(conn, row)
    with pytest.raises(RuntimeError, match='historical_row_moved'):
        restore.insert_exact(conn, {**row, 'status': 'completed'})
    assert conn.execute('SELECT count(*) FROM executions').fetchone()[0] == 2


def test_unbound_evidence_refuses():
    with pytest.raises(RuntimeError, match='historical_evidence_moved'):
        restore.archived_row(b'{}', b'{}')


def test_restore_journal_is_exclusive(tmp_path):
    path = tmp_path / 'journal.jsonl'
    restore.append(path, {'event': 'intent'}, first=True)
    with pytest.raises(FileExistsError):
        restore.append(path, {'event': 'intent'}, first=True)
    restore.append(path, {'event': 'restored'})
    assert [json.loads(s)['event'] for s in path.read_text().splitlines()] == ['intent', 'restored']
