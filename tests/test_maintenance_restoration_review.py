"""PR #109 F2/F3 independent safety oracles; native mutations use scratch homes only."""
import datetime as dt
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'tests')]
import maintenance_window as mw
import test_maintenance_window as fixtures


def test_hermes_scheduling_cursor_movement_must_invalidate_active(tmp_path):
    backend = fixtures.hermes_backend(mw, tmp_path)
    window = fixtures.new_window(mw, tmp_path, backend)
    try:
        window.enter()
        backend.jobs['enabled']['next_run_at'] = '2040-01-01T00:00:00Z'
        assert not window.status()['verified'], 'ACTIVE verified despite changed durable due-time preimage'
    finally:
        window.release_locks()


def test_crontab_line_ending_movement_must_invalidate_active(tmp_path):
    backend = fixtures.crontab_backend(mw, tmp_path)
    window = fixtures.new_window(mw, tmp_path, backend)
    try:
        window.enter()
        backend.text = backend.text.replace('\r\n', '\n')
        assert not window.status()['verified'], 'ACTIVE verified despite changed CRLF crontab custody'
    finally:
        window.release_locks()


@pytest.mark.skipif(not Path('/home/ubuntu/.hermes/hermes-agent/cron/jobs.py').is_file(),
                    reason='installed Hermes native scheduler required for scratch integration oracle')
def test_real_native_pause_with_legacy_job_must_be_restorable(tmp_path, monkeypatch):
    home, root = tmp_path / 'hermes', tmp_path / 'root'
    (home / 'cron').mkdir(parents=True)
    root.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('HERMES_SKIP_ENV_FILE', '1')
    sys.path.insert(0, '/home/ubuntu/.hermes/hermes-agent')
    from cron import jobs as native
    registry = home / 'cron/jobs.json'
    assert native._current_cron_store().jobs_file == registry
    # Valid legacy record: native load_jobs accepts absent skill/skills fields.
    original = {'id': 'legacy', 'name': 'review-fixture', 'prompt': 'fixture only',
                'enabled': True, 'state': 'scheduled',
                'schedule': {'kind': 'interval', 'minutes': 60},
                'next_run_at': '2030-01-01T00:00:00Z'}
    registry.write_text(json.dumps({'jobs': [original]}))

    class NativeFixture(mw.LinuxBackend):
        def scheduler(self):
            return native
        def cron_tool(self, action, jid):
            # Call unchanged installed supported tool; block its unrelated provider notification.
            from tools import cronjob_tools
            with monkeypatch.context() as scoped:
                scoped.setattr(cronjob_tools, '_notify_provider_jobs_changed_safe', lambda: None)
                result = json.loads(cronjob_tools.cronjob(action=action, job_id=jid, reason=self.pause_reason()))
            assert result['success'] is True, result

    backend = NativeFixture(root, home, tmp_path / 'window.json')
    entry = {'kind': 'hermes', 'id': 'legacy', 'affected': True, 'command': 'fixture', 'trace': []}
    prior = backend.snapshot(entry)
    # A complete durable preimage exists before the fixture-only native mutation.
    mw.atomic_json(backend.journal, {'schema': mw.SCHEMA, 'entries': [{'entry': entry, 'prior': prior, 'intent': True}]})
    backend.pause(entry, prior)
    try:
        backend.restore(entry, prior)
    except mw.WindowError as exc:
        pytest.fail(f'native pause changed non-control fields, recovery refused: {exc}; '
                    f'added_fields={sorted(set(backend.job("legacy")) - set(original))}')
    assert backend.job('legacy') == original


@pytest.mark.skipif(not Path('/home/ubuntu/.hermes/hermes-agent/cron/jobs.py').is_file(),
                    reason='installed Hermes native scheduler required for scratch integration oracle')
def test_real_native_expired_one_shot_must_restore_exact_prior(tmp_path, monkeypatch):
    home, root = tmp_path / 'hermes', tmp_path / 'root'
    (home / 'cron').mkdir(parents=True)
    root.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('HERMES_SKIP_ENV_FILE', '1')
    sys.path.insert(0, '/home/ubuntu/.hermes/hermes-agent')
    from cron import jobs as native
    registry = home / 'cron/jobs.json'
    assert native._current_cron_store().jobs_file == registry
    now = dt.datetime.now(dt.timezone.utc)
    run_at = (now + dt.timedelta(seconds=30)).isoformat()
    original = {'id': 'one-shot', 'name': 'review-fixture', 'prompt': 'fixture only',
                'enabled': True, 'state': 'scheduled', 'skills': [], 'skill': None,
                'schedule': {'kind': 'once', 'run_at': run_at}, 'next_run_at': run_at}
    registry.write_text(json.dumps({'jobs': [original]}))

    class NativeFixture(mw.LinuxBackend):
        def scheduler(self):
            return native
        def cron_tool(self, action, jid):
            from tools import cronjob_tools
            with monkeypatch.context() as scoped:
                scoped.setattr(cronjob_tools, '_notify_provider_jobs_changed_safe', lambda: None)
                result = json.loads(cronjob_tools.cronjob(action=action, job_id=jid, reason=self.pause_reason()))
            mw.require(result.get('success') is True, 'hermes_' + action + '_failed')

    backend = NativeFixture(root, home, tmp_path / 'window.json')
    entry = {'kind': 'hermes', 'id': 'one-shot', 'affected': True, 'command': 'fixture', 'trace': []}
    prior = backend.snapshot(entry)
    mw.atomic_json(backend.journal, {'schema': mw.SCHEMA, 'entries': [{'entry': entry, 'prior': prior, 'intent': True}]})
    backend.pause(entry, prior)
    # Deterministic passage beyond native 120s grace; never wait or run a job.
    monkeypatch.setattr(native, '_hermes_now', lambda: now + dt.timedelta(minutes=10))
    try:
        backend.restore(entry, prior)
    except mw.WindowError as exc:
        pytest.fail(f'expired once-job cannot reach exact native save: {exc}; remains_enabled={backend.job("one-shot")["enabled"]}')
    assert backend.job('one-shot') == original


@pytest.mark.parametrize('field,value', [
    ('enabled', True), ('state', 'scheduled'),
    ('paused_at', '2040-01-01T00:00:00Z'), ('paused_reason', 'foreign hold'),
    ('next_run_at', '2040-01-01T00:00:00Z'), ('pending_slot', 'foreign-slot'),
])
def test_every_control_conflict_refuses_status_and_apply(tmp_path, monkeypatch, field, value):
    backend = fixtures.hermes_backend(mw, tmp_path)
    window = fixtures.new_window(mw, tmp_path, backend)
    monkeypatch.setattr(mw, 'LinuxBackend', lambda *_: backend)
    monkeypatch.setattr(mw, 'default_locks', lambda *_: window.lock_paths)
    try:
        window.enter()
        backend.jobs['enabled'][field] = value
        assert not window.status()['verified']
        with pytest.raises(mw.WindowError, match='active_maintenance_window_required'):
            with mw.require_active_window(window.journal, backend.root, backend.hermes):
                pytest.fail('apply admitted after restoration-conflicting control change')
    finally:
        window.release_locks()


@pytest.mark.parametrize('mutation', ['line-endings', 'new-row', 'final-newline'])
def test_every_crontab_byte_conflict_refuses_status_and_apply(tmp_path, monkeypatch, mutation):
    backend = fixtures.crontab_backend(mw, tmp_path)
    window = fixtures.new_window(mw, tmp_path, backend)
    monkeypatch.setattr(mw, 'LinuxBackend', lambda *_: backend)
    monkeypatch.setattr(mw, 'default_locks', lambda *_: window.lock_paths)
    try:
        window.enter()
        if mutation == 'line-endings':
            backend.text = backend.text.replace('\r\n', '\n')
        elif mutation == 'new-row':
            backend.text += '# operator addition\n'
        else:
            backend.text = backend.text.rstrip('\r\n')
        assert not window.status()['verified']
        with pytest.raises(mw.WindowError, match='active_maintenance_window_required'):
            with mw.require_active_window(window.journal, backend.root, backend.hermes):
                pytest.fail('apply admitted after restoration-conflicting crontab change')
    finally:
        window.release_locks()


def test_unrecognized_job_pin_invalidates_active_and_refuses_apply(tmp_path, monkeypatch):
    backend = fixtures.hermes_backend(mw, tmp_path)
    window = fixtures.new_window(mw, tmp_path, backend)
    backend.root, backend.hermes = tmp_path.resolve(), (tmp_path / 'hermes').resolve()
    monkeypatch.setattr(mw, 'LinuxBackend', lambda *_: backend)
    monkeypatch.setattr(mw, 'default_locks', lambda *_: window.lock_paths)
    try:
        window.enter()
        backend.jobs['enabled']['script_sha256'] = 'unreviewed-pin'
        assert not window.status()['verified']
        with pytest.raises(mw.WindowError):
            with mw.require_active_window(window.journal, backend.root, backend.hermes):
                pytest.fail('unknown pin admitted')
    finally:
        window.release_locks()
