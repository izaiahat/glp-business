import importlib.util
from pathlib import Path
from types import SimpleNamespace
import pytest

SOURCE = Path(__file__).resolve().parents[1] / 'scripts/glp_monday_discovery_background.py'


def test_discovery_manual_run_detaches_registered_owner_without_420s_watchdog(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('background_discovery', SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []
    def spawn(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(pid=12345)
    monkeypatch.setattr(module.subprocess, 'Popen', spawn)
    monkeypatch.setattr(module.shutil, 'which', lambda name: '/trusted/bin/hermes')
    output = tmp_path / 'one-request'
    receipt = module.launch(output)
    assert receipt['pid'] == 12345
    assert receipt['status'] == 'DISPATCHED_NOT_COMPLETED'
    assert calls[0][0] == ['/trusted/bin/hermes', 'cron', 'run', '58202b06bdc0']
    assert calls[0][1]['start_new_session'] is True
    assert 'timeout' not in calls[0][1]
    assert calls[0][1]['stdin'] == module.subprocess.DEVNULL
    assert (output / 'INTENT.json').exists()
    assert (output / 'DISPATCH.json').exists()
    with pytest.raises(FileExistsError):
        module.launch(output)
    assert len(calls) == 1  # ambiguous dispatch can never be replayed in place
