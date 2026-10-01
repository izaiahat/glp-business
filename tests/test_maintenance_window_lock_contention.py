"""Native lock regressions: disposable profiles only, no scheduler execution."""
import copy
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import maintenance_window as mw

NATIVE_ROOT = Path('/home/ubuntu/.hermes/hermes-agent')


@pytest.fixture
def native_window(tmp_path, monkeypatch):
    if not (NATIVE_ROOT / 'cron/jobs.py').is_file():
        pytest.skip('unchanged installed native scheduler required; no fake native success')
    home, root = tmp_path / 'hermes', tmp_path / 'root'
    (home / 'cron').mkdir(parents=True)
    root.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('HERMES_SKIP_ENV_FILE', '1')
    monkeypatch.syspath_prepend(str(NATIVE_ROOT))
    from cron import jobs as native
    from tools import cronjob_tools
    assert Path(native.__file__).resolve() == NATIVE_ROOT / 'cron/jobs.py'
    assert native._current_cron_store().jobs_file == home / 'cron/jobs.json'
    monkeypatch.setattr(cronjob_tools, '_notify_provider_jobs_changed_safe', lambda: None)
    original = {
        'enabled-fixture': {'id': 'enabled-fixture', 'name': 'disposable lock fixture',
            'prompt': 'fixture only', 'enabled': True, 'state': 'scheduled',
            'pending_slot': '2026-09-30T12:00:00Z', 'next_run_at': '2026-09-30T12:00:00Z',
            'schedule': {'kind': 'interval', 'minutes': 60}, 'script_sha256': 'a' * 64,
            'fixture_extension': {'preserve': [1, 2]}},
        'disabled-fixture': {'id': 'disabled-fixture', 'enabled': False,
            'schedule': {'kind': 'interval', 'minutes': 60}, 'script_sha256': 'b' * 64},
        'unrelated-fixture': {'id': 'unrelated-fixture', 'enabled': False,
            'state': 'paused', 'paused_reason': 'unrelated hold',
            'schedule': {'kind': 'interval', 'minutes': 60}},
    }
    (home / 'cron/jobs.json').write_text(json.dumps({'jobs': list(original.values())}))

    class NativeFixture(mw.LinuxBackend):
        # Only host discovery/census is fixture-scoped. Pause/tool/save/restore
        # and both registry/fire controls are the unchanged real native code.
        def scheduler(self):
            assert native._current_cron_store().jobs_file == home / 'cron/jobs.json'
            return native

        def inventory(self):
            return {'complete': True, 'errors': [], 'entries': [
                {'kind': 'hermes', 'id': jid, 'command': 'disposable fixture',
                 'affected': True, 'trace': []} for jid in ('disabled-fixture', 'enabled-fixture')]}

        def inflight(self, entries):
            from maintenance_admission import native_runs
            busy, owners = native_runs(native, entries, self.job)
            assert not owners
            return busy

    backend = NativeFixture(root, home, tmp_path / 'window.json')
    window = mw.MaintenanceWindow(backend, backend.journal, [root / 'source.lock'])
    saves = []
    saver = native._save_jobs_unlocked

    def observed_save(jobs, **kwargs):
        if any(row['id'] == 'enabled-fixture' and row.get('enabled') is True for row in jobs):
            data = mw.read_journal(window.journal)
            if 'keeper' in data:
                assert mw.locks_verified(data)
            lock_key = f"{native._current_cron_store().cron_dir.resolve()}::enabled-fixture"
            assert native._fire_fence_lock_state.held[lock_key] is True
        saves.append(copy.deepcopy(jobs))
        return saver(jobs, **kwargs)

    monkeypatch.setattr(native, '_save_jobs_unlocked', observed_save)
    yield window, backend, native, original, saves
    window.release_locks()


@contextmanager
def native_holder(home, *, fire=False, seconds=0.4, update=None):
    # A separate process enters the actual native critical section, signals
    # readiness and exits naturally. Never manipulates live locks or claims.
    script = '''
import os, sys, time
sys.path.insert(0, sys.argv[1])
from cron import jobs
from pathlib import Path
assert jobs._current_cron_store().jobs_file == Path(os.environ['HERMES_HOME']) / 'cron/jobs.json'
lock = jobs._fire_job_lock('enabled-fixture') if sys.argv[2] == 'fire' else jobs._jobs_lock()
with lock as acquired:
    if sys.argv[2] == 'fire':
        assert acquired
    print('HELD', flush=True)
    time.sleep(float(sys.argv[3]))
    import json
    update = json.loads(sys.argv[4])
    if update:
        assert sys.argv[2] == 'registry'
        rows = jobs.load_jobs()
        next(row for row in rows if row['id'] == update['id']).update(update)
        jobs._save_jobs_unlocked(rows)
'''
    proc = subprocess.Popen([sys.executable, '-B', '-c', script, str(NATIVE_ROOT),
                             'fire' if fire else 'registry', str(seconds), json.dumps(update)],
                            env={**os.environ, 'HERMES_HOME': str(home), 'HERMES_SKIP_ENV_FILE': '1'},
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == 'HELD'
        yield proc
    finally:
        output, error = proc.communicate(timeout=15)
        assert proc.returncode == 0, (output, error)


def assert_original(backend, original):
    assert {jid: backend.job(jid) for jid in original} == original


def test_actual_entry_waits_for_transient_native_registry_contention(native_window):
    window, backend, native, original, saves = native_window
    with native_holder(backend.hermes):
        started = time.monotonic()
        result = window.enter()
        assert time.monotonic() - started >= 0.25
    assert result['verified'] and result['state'] == 'active'
    assert len(saves) == 1  # Disabled row already paused: no redundant saver.
    data = mw.read_journal(window.journal)
    assert all(row['intent'] and row['paused'] for row in data['entries'])
    assert backend.job('disabled-fixture') == original['disabled-fixture']
    assert backend.job('unrelated-fixture') == original['unrelated-fixture']
    assert window.exit()['state'] == 'closed'
    assert len(saves) == 2
    assert_original(backend, original)


@pytest.mark.parametrize('lock_kind', ['registry', 'fire'])
def test_actual_restore_waits_for_transient_native_contention(native_window, lock_kind):
    window, backend, native, original, saves = native_window
    assert window.enter()['verified']
    assert len(saves) == 1
    with native_holder(backend.hermes, fire=lock_kind == 'fire'):
        started = time.monotonic()
        result = window.exit()
        assert time.monotonic() - started >= 0.25
    assert result['state'] == 'closed'
    data = mw.read_journal(window.journal)
    assert all(row['restore_intent'] and row['restored'] for row in data['entries'])
    assert len(saves) == 2  # No saver for already-restored disabled/absent fields.
    assert_original(backend, original)


def durable_intent(backend, jid):
    entry = {'kind': 'hermes', 'id': jid, 'affected': True}
    prior = backend.snapshot(entry)
    mw.atomic_json(backend.journal, {'schema': mw.SCHEMA, 'entries': [
        {'entry': entry, 'prior': prior, 'intent': True}]})
    return entry, prior


def test_registry_timeout_never_enters_native_saver(native_window):
    window, backend, native, original, saves = native_window
    entry, prior = durable_intent(backend, 'enabled-fixture')
    with native_holder(backend.hermes, seconds=5.6):
        started = time.monotonic()
        with pytest.raises(mw.WindowError, match='native_registry_lock_timeout'):
            backend.pause(entry, prior)
        elapsed = time.monotonic() - started
        assert 4.8 <= elapsed < 5.5
        assert saves == []
        assert_original(backend, original)
    assert getattr(native._jobs_lock_state, 'depth', 0) == 0


@pytest.mark.parametrize('operation', ['pause', 'restore'])
@pytest.mark.parametrize('field,value', [
    ('fixture_extension', {'foreign': True}),
    ('script_sha256', 'c' * 64),
    ('next_run_at', '2040-01-01T00:00:00Z'),
])
def test_wait_rechecks_preimages_before_any_save(native_window, operation, field, value):
    window, backend, native, original, saves = native_window
    # Disabled no-change rows must still check config/pin/control custody.
    jid = 'enabled-fixture' if operation == 'pause' else 'disabled-fixture'
    entry, prior = durable_intent(backend, jid)
    with native_holder(backend.hermes, update={'id': jid, field: value}):
        with pytest.raises(mw.WindowError):
            getattr(backend, operation)(entry, prior)
    assert saves == []
    assert backend.job(jid) == {**original[jid], field: value}
    assert backend.job('unrelated-fixture') == original['unrelated-fixture']


@pytest.mark.parametrize('operation', ['pause', 'restore'])
@pytest.mark.parametrize('landed', [False, True])
def test_ambiguous_native_save_is_not_replayed(native_window, monkeypatch, operation, landed):
    window, backend, native, original, saves = native_window
    entry, prior = durable_intent(backend, 'enabled-fixture')
    if operation == 'restore':
        backend.pause(entry, prior)
        saves.clear()
    preimage = backend.job(entry['id'])
    real_save = native._save_jobs_unlocked
    attempts = []

    def uncertain_save(jobs, **kwargs):
        attempts.append(copy.deepcopy(jobs))
        if landed:
            real_save(jobs, **kwargs)
        raise BlockingIOError('fixture: possibly landed save, acknowledgement lost')

    monkeypatch.setattr(native, '_save_jobs_unlocked', uncertain_save)
    with pytest.raises((mw.WindowError, BlockingIOError)):
        getattr(backend, operation)(entry, prior)
    assert len(attempts) == 1
    expected = copy.deepcopy(preimage)
    if landed:
        for key in mw.CONTROL_FIELDS:
            expected.pop(key, None)
        expected.update(prior['paused_fields'] if operation == 'pause' else prior['fields'])
    assert backend.job(entry['id']) == expected
    assert len(saves) == int(landed)
    if operation == 'restore' and landed:
        # An explicit later reconciliation reads exact restored fields under
        # both native fences, accepts them and never calls the saver again.
        backend.restore(entry, prior)
        assert len(attempts) == 1
    assert backend.job('disabled-fixture') == original['disabled-fixture']
    assert backend.job('unrelated-fixture') == original['unrelated-fixture']


def portable_scheduler(tmp_path):
    import threading
    from types import SimpleNamespace
    return SimpleNamespace(_jobs_file_lock=threading.RLock(),
                           _jobs_lock_file=lambda: tmp_path / '.jobs.lock',
                           _jobs_lock_state=SimpleNamespace(depth=0, load_stamp=None))


def test_yielded_blocking_error_is_never_an_acquisition_retry(tmp_path):
    scheduler = portable_scheduler(tmp_path)
    entries = []
    with pytest.raises(BlockingIOError, match='ambiguous effect'):
        with mw.native_registry_lock(scheduler):
            entries.append(True)
            raise BlockingIOError('ambiguous effect')
    assert entries == [True]
    assert scheduler._jobs_lock_state.depth == 0
    with mw.native_registry_lock(scheduler):
        assert scheduler._jobs_lock_state.depth == 1


def test_nested_lock_refusal_preserves_outer_native_depth(tmp_path):
    scheduler = portable_scheduler(tmp_path)
    with mw.native_registry_lock(scheduler):
        with pytest.raises(mw.WindowError, match='nested_registry_lock_refused'):
            with mw.native_registry_lock(scheduler):
                pytest.fail('nested entry accepted')
        assert scheduler._jobs_lock_state.depth == 1
    assert scheduler._jobs_lock_state.depth == 0


def test_portable_real_registry_flock_waits_before_yield(tmp_path):
    scheduler = portable_scheduler(tmp_path)
    script = "import fcntl,sys,time; f=open(sys.argv[1],'a+'); fcntl.flock(f,fcntl.LOCK_EX); print('HELD',flush=True); time.sleep(0.4)"
    proc = subprocess.Popen([sys.executable, '-c', script, str(scheduler._jobs_lock_file())],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == 'HELD'
        started = time.monotonic()
        with mw.native_registry_lock(scheduler):
            assert time.monotonic() - started >= 0.25
            assert scheduler._jobs_lock_state.depth == 1
    finally:
        output, error = proc.communicate(timeout=10)
        assert proc.returncode == 0, (output, error)
    assert scheduler._jobs_lock_state.depth == 0
