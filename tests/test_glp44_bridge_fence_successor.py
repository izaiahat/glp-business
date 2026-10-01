"""Offline successor regressions for independently reproduced PR81 F1/F2."""
import fcntl
import importlib.util
import subprocess
import sys
import threading
import time
from pathlib import Path
import pytest

BRIDGE = Path(__file__).resolve().parents[1] / 'ops/n8n-bridge/bridge.py'
NAMES = ('analytics_brain', 'affiliate_postback_receive', 'topic_data_score',
         'glp_ads_monitor', 'glp_ads_monitor_daily_digest')


def setup(tmp_path):
    spec = importlib.util.spec_from_file_location('glp44_successor_fixture', BRIDGE)
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)
    bridge.SOURCE_ADMISSION_LOCK = str(tmp_path / 'fence.lock')
    bridge.RECEIPT_DIR = str(tmp_path / 'receipts')
    bridge.JOB_STATE_DIR = str(tmp_path / 'receipts/.job-state')
    bridge.STORE = bridge.JobStore()
    bridge.child_env = lambda job_id='': {'PATH': '/usr/bin:/bin'}
    bridge.log = lambda *args: None
    return bridge


def until(predicate, message):
    deadline = time.monotonic() + 8
    while not predicate():
        assert time.monotonic() < deadline, message
        time.sleep(.01)


def command(tmp_path, name, marker, release=None):
    code = 'from pathlib import Path; import time; Path(%r).touch(); ' % str(marker)
    if release is not None:
        code += 'exec("while not Path(%r).exists(): time.sleep(.01)")' % str(release)
    return {'name': name, 'cwd': str(tmp_path), 'timeout_s': 15,
            'argv': [sys.executable, '-c', code]}


def record(bridge, job_id, name):
    value = {'job_id': job_id, 'action': 'script', 'name': name,
             'status': 'queued', 'auth_scope': None}
    bridge.persist_job_record(value)
    bridge.STORE.create(job_id, value)


def test_fenced_waiters_keep_slots_free_for_unrelated_work(tmp_path):
    bridge = setup(tmp_path)
    threads, ids, markers = [], [], []
    with open(bridge.SOURCE_ADMISSION_LOCK, 'a+') as installer:
        fcntl.flock(installer, fcntl.LOCK_EX)
        try:
            for i, name in enumerate(NAMES):
                job_id = '%016x' % i
                marker = tmp_path / name
                ticket = bridge.STORE.enqueue(eligible=False)
                assert ticket is not None
                record(bridge, job_id, name)
                worker = threading.Thread(target=bridge.execute_queued_job,
                    args=(job_id, 'script', command(tmp_path, name, marker), None, ticket),
                    name=bridge.JOB_THREAD_PREFIX + job_id)
                ids.append(job_id)
                markers.append(marker)
                threads.append(worker)
                worker.start()
            until(lambda: bridge.STORE.queued_count() == 5, 'all fence waiters retained')
            assert bridge.STORE.running_count() == 0
            assert not any(marker.exists() for marker in markers)
            assert bridge.STORE.try_reserve(0), 'unrelated normal admission blocked'
            bridge.STORE.release()
            job_id = '0000000000000010'
            marker = tmp_path / 'unrelated'
            ticket = bridge.STORE.enqueue()
            assert ticket is not None
            record(bridge, job_id, 'echo_test')
            worker = threading.Thread(target=bridge.execute_queued_job,
                args=(job_id, 'script', command(tmp_path, 'echo_test', marker), None, ticket),
                name=bridge.JOB_THREAD_PREFIX + job_id)
            threads.append(worker)
            ids.append(job_id)
            markers.append(marker)
            worker.start()
            until(marker.exists, 'unrelated callback should run past ineligible tickets')
            until(lambda: bridge.STORE.event(job_id).is_set(), 'unrelated callback terminal')
            assert not any(p.exists() for p in markers[:-1])
            assert bridge.STORE.queued_count() == 5
        finally:
            fcntl.flock(installer, fcntl.LOCK_UN)
    for worker in threads:
        worker.join(15)
    assert not any(worker.is_alive() for worker in threads)
    assert all(bridge.STORE.event(i).is_set() and bridge.STORE.get(i)['rc'] == 0 for i in ids)
    assert all(bridge.load_durable_job(i)['rc'] == 0 for i in ids)
    assert all(marker.exists() for marker in markers)
    assert bridge.STORE.queued_count() == bridge.STORE.running_count() == 0


@pytest.mark.parametrize('failure', ['drain_start', 'unexpected_wait'])
def test_postspawn_failure_keeps_fence_and_slot_until_child_exit(tmp_path, monkeypatch, failure):
    bridge = setup(tmp_path)
    marker, release = tmp_path / 'child', tmp_path / 'release'
    job_id = '0000000000000020'
    spawned = []
    real_popen, real_start, real_wait = subprocess.Popen, threading.Thread.start, subprocess.Popen.wait
    def capture(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        spawned.append(proc)
        return proc
    def fail_drain(thread):
        if failure == 'drain_start' and getattr(thread, '_target', None) is bridge._drain:
            raise RuntimeError('injected drain start failure')
        return real_start(thread)
    def fail_wait(proc, *args, **kwargs):
        if failure == 'unexpected_wait' and kwargs.get('timeout') is not None:
            raise RuntimeError('injected unexpected wait failure')
        return real_wait(proc, *args, **kwargs)
    monkeypatch.setattr(real_popen, 'wait', fail_wait)
    monkeypatch.setattr(subprocess, 'Popen', capture)
    monkeypatch.setattr(threading.Thread, 'start', fail_drain)
    assert bridge.STORE.try_reserve()
    record(bridge, job_id, NAMES[0])
    worker = threading.Thread(target=bridge.execute_job,
        args=(job_id, 'script', command(tmp_path, NAMES[0], marker, release), None),
        name=bridge.JOB_THREAD_PREFIX + job_id)
    worker.start()
    try:
        until(marker.exists, 'child spawn')
        assert spawned[0].poll() is None
        until(lambda: worker.is_alive(), 'worker retained')
        with open(bridge.SOURCE_ADMISSION_LOCK, 'a+') as installer:
            try:
                fcntl.flock(installer, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                raise AssertionError('exclusive installer entered while child lived')
        assert bridge.STORE.running_count() == 1
        assert not bridge.STORE.event(job_id).is_set()
    finally:
        release.touch()
        worker.join(15)
        for proc in spawned:
            real_wait(proc, timeout=15)
            proc.stdout.close()
            proc.stderr.close()
    assert not worker.is_alive() and spawned[0].returncode == 0
    assert bridge.STORE.running_count() == 0
    assert bridge.STORE.event(job_id).is_set()
    assert bridge.STORE.get(job_id)['rc'] == 125
    with open(bridge.SOURCE_ADMISSION_LOCK, 'a+') as installer:
        fcntl.flock(installer, fcntl.LOCK_EX | fcntl.LOCK_NB)
