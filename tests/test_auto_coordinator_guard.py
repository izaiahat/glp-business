"""Offline TUI child-ownership regression; scheduler must defer, never compete."""
import importlib.util
import json
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / 'scripts/auto_coordinator_guard.py'


def load():
    spec = importlib.util.spec_from_file_location('auto_guard', SOURCE)
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_live_tui_children_defer_and_stale_pid_reuse_does_not(tmp_path):
    m = load()
    budget = tmp_path / 'budget.json'
    budget.write_text(json.dumps([{'pid': 42, 'start': '100', 'token': 'one'},
                                 {'pid': 42, 'start': '100', 'token': 'two'}]))
    before = budget.read_bytes()
    assert m.inspect(budget, tick=lambda pid: '100')['status'] == 'DEFERRED_TUI_CHILDREN'
    assert m.inspect(budget, tick=lambda pid: '101')['status'] == 'IDLE'
    assert budget.read_bytes() == before
    budget.write_text('broken')
    assert m.inspect(budget)['status'] == 'DEFERRED_UNKNOWN'
    assert m.inspect(tmp_path / 'missing')['status'] == 'DEFERRED_UNKNOWN'
