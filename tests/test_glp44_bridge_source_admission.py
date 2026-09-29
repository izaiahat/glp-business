"""Offline launch exclusion for the actual bridge worker path; no HTTP or providers."""
import fcntl
import importlib.util
import sys
import threading
import time
from pathlib import Path


BRIDGE = Path(__file__).resolve().parents[1] / 'ops/n8n-bridge/bridge.py'


def load_bridge():
    spec = importlib.util.spec_from_file_location('glp44_bridge_fixture', BRIDGE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cmd(marker, name):
    return {'name': name, 'cwd': str(marker.parent), 'timeout_s': 5,
            'argv': [sys.executable, '-c',
                     'from pathlib import Path; Path(%r).write_text("entered")' % str(marker)]}


def test_registered_reader_launch_defers_but_receiver_remains_available(tmp_path):
    bridge = load_bridge()
    bridge.SOURCE_ADMISSION_LOCK = str(tmp_path / 'source-admission.lock')
    marker = tmp_path / 'reader-entered'
    lock = open(bridge.SOURCE_ADMISSION_LOCK, 'a+')
    fcntl.flock(lock, fcntl.LOCK_EX)
    result = []
    thread = threading.Thread(target=lambda: result.append(
        bridge.run_command(cmd(marker, 'analytics_brain'))))
    thread.start()
    try:
        time.sleep(.15)
        assert not marker.exists(), 'selected reader launched under held exclusion'
        assert thread.is_alive(), 'admitted job was dropped instead of deferred'
        other = bridge.run_command(cmd(tmp_path / 'unrelated-entered', 'echo_test'))
        assert other['rc'] == 0 and (tmp_path / 'unrelated-entered').exists()
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert result[0]['rc'] == 0 and marker.read_text() == 'entered'
    # A released hold admits another affected reader without a queue/slot reset.
    assert bridge.run_command(cmd(tmp_path / 'post-release', 'affiliate_postback_receive'))['rc'] == 0
    assert (tmp_path / 'post-release').exists()


def test_all_actual_registered_source_readers_are_fenced(tmp_path):
    bridge = load_bridge()
    bridge.SOURCE_ADMISSION_LOCK = str(tmp_path / 'source-admission.lock')
    with open(bridge.SOURCE_ADMISSION_LOCK, 'a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for name in ('analytics_brain', 'affiliate_postback_receive',
                     'topic_data_score', 'glp_ads_monitor', 'glp_ads_monitor_daily_digest'):
            marker = tmp_path / name
            result = []
            thread = threading.Thread(target=lambda: result.append(bridge.run_command(cmd(marker, name))))
            thread.start()
            time.sleep(.04)
            assert not marker.exists(), name
            fcntl.flock(lock, fcntl.LOCK_UN)
            thread.join(timeout=5)
            assert not thread.is_alive() and result[0]['rc'] == 0 and marker.exists(), name
            fcntl.flock(lock, fcntl.LOCK_EX)


def test_admitted_child_finishes_before_exclusive_installer_and_error_releases(tmp_path):
    bridge = load_bridge()
    bridge.SOURCE_ADMISSION_LOCK = str(tmp_path / 'source-admission.lock')
    marker = tmp_path / 'running'
    release = tmp_path / 'finish'
    command = cmd(marker, 'affiliate_postback_receive')
    command['argv'] = [sys.executable, '-c',
                       'from pathlib import Path; import time; '
                       'Path(%r).touch(); '
                       'while_not_done = %r; '
                       'exec("while not Path(while_not_done).exists(): time.sleep(.01)")'
                       % (str(marker), str(release))]
    result = []
    thread = threading.Thread(target=lambda: result.append(bridge.run_command(command)))
    thread.start()
    try:
        for _ in range(100):
            if marker.exists():
                break
            time.sleep(.01)
        assert marker.exists(), 'child failed to enter'
        with open(bridge.SOURCE_ADMISSION_LOCK, 'a+') as installer:
            try:
                fcntl.flock(installer, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                raise AssertionError('installer entered while admitted child still ran')
    finally:
        release.touch()
        thread.join(timeout=5)
    assert not thread.is_alive() and result[0]['rc'] == 0
    with open(bridge.SOURCE_ADMISSION_LOCK, 'a+') as installer:
        fcntl.flock(installer, fcntl.LOCK_EX | fcntl.LOCK_NB)
    failure = cmd(tmp_path / 'never', 'analytics_brain')
    failure['argv'] = ['nonexistent-glp44-offline-executable']
    assert bridge.run_command(failure)['rc'] == 127
    with open(bridge.SOURCE_ADMISSION_LOCK, 'a+') as installer:
        fcntl.flock(installer, fcntl.LOCK_EX | fcntl.LOCK_NB)
