"""Direct bridge-style cadence admission, with no collector or network calls."""
import fcntl
import importlib.util
import json
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
CADENCE = ROOT / "scripts/glp_analytics_brain_cadence.py"
WRAPPER = ROOT / "scripts/cron_bridge_action.py"


def test_direct_cadence_cannot_bypass_installer_reader_lock(tmp_path, capsys):
    spec = importlib.util.spec_from_file_location("glp44_cadence_under_test", CADENCE)
    assert spec is not None and spec.loader is not None
    cadence = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cadence)
    lock_path = tmp_path / "analytics_brain.lock"
    called = []

    def harmless_run(phase, **kwargs):
        with lock_path.open("a") as contender:
            try:
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass  # The cadence owns the reader fence across its run.
            else:
                raise AssertionError("reader lock was released before the collector")
        called.append(phase)
        return {"status": "PASS_FIXTURE", "phase": phase, "output_count": 0}, 0

    # Exactly the bridge's cadence CLI path: no cron_bridge_action.run_action.
    with patch.object(cadence, "READER_LOCK", lock_path), patch.object(cadence, "run", harmless_run):
        with lock_path.open("a") as installer_lock:
            fcntl.flock(installer_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            assert cadence.main(["--phase", "hourly", "--fixture", str(tmp_path / "unused.json")]) == 75
            assert called == []
            assert json.loads(capsys.readouterr().out)["status"] == "REFUSED_READER_EXCLUSION"
        assert cadence.main(["--phase", "hourly", "--fixture", str(tmp_path / "unused.json")]) == 0
        assert called == ["hourly"]
        assert json.loads(capsys.readouterr().out)["status"] == "PASS_FIXTURE"

    # The cron caller must not retain the same lock across its bridge wait:
    # that would deadlock the cadence child on its own reader exclusion.
    assert 'f"{action}.state.lock" if action == "analytics_brain"' in WRAPPER.read_text()
