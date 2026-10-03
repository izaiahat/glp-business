"""GLP341: maintenance joins native locking with real descriptor custody."""
from pathlib import Path
import fcntl
import importlib.util
from types import SimpleNamespace
import threading
import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('glp341_window', ROOT / 'scripts/maintenance_window.py')
window = importlib.util.module_from_spec(spec)
spec.loader.exec_module(window)


def test_maintenance_registry_lock_has_native_cross_process_custody(tmp_path):
    path = tmp_path / '.jobs.lock'
    state = SimpleNamespace(depth=0, cross_process=False, custody=None, load_stamp='old')
    scheduler = SimpleNamespace(_jobs_file_lock=threading.RLock(), _jobs_lock_state=state,
                                _jobs_lock_file=lambda: path)
    with window.native_registry_lock(scheduler):
        assert state.depth == 1
        assert state.cross_process is True
        held_path, handle = state.custody
        assert held_path == path
        assert handle.fileno() >= 0
        assert Path('/proc/self/fd/' + str(handle.fileno())).stat().st_ino == path.stat().st_ino
        with path.open('a+') as other:
            with pytest.raises(BlockingIOError):
                fcntl.flock(other.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert state.load_stamp is None
    assert state.depth == 0
    assert state.cross_process is False
    assert state.custody is None
    assert state.load_stamp is None
    assert handle.closed


def test_maintenance_registry_lock_cleans_custody_on_failure(tmp_path):
    state = SimpleNamespace(depth=0, cross_process=False, custody=None)
    scheduler = SimpleNamespace(_jobs_file_lock=threading.RLock(), _jobs_lock_state=state,
                                _jobs_lock_file=lambda: tmp_path / '.jobs.lock')
    with pytest.raises(RuntimeError, match='effect refused'):
        with window.native_registry_lock(scheduler):
            raise RuntimeError('effect refused')
    assert state.depth == 0 and state.cross_process is False and state.custody is None


def test_maintenance_registry_lock_still_refuses_nested_registry_lock(tmp_path):
    state = SimpleNamespace(depth=1, cross_process=True, custody=None)
    scheduler = SimpleNamespace(_jobs_file_lock=threading.RLock(), _jobs_lock_state=state,
                                _jobs_lock_file=lambda: tmp_path / '.jobs.lock')
    with pytest.raises(window.WindowError, match='nested_registry_lock_refused'):
        with window.native_registry_lock(scheduler):
            pytest.fail('nested custody admitted')
    assert state.depth == 1
