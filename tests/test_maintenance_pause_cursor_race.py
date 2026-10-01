"""The actual snapshot -> completed native tick -> pause race, effect-denied.

Each case imports the unchanged native scheduler in a fresh disposable home.
No scheduler payload, cronjob resume, gateway, network or live control runs.
"""
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
NATIVE = os.environ.get('GLP_NATIVE_CHECKOUT', '/home/ubuntu/.hermes/hermes-agent')
LEGACY = '09ca37b43e5105d48c4bb14b2a96396dce7043eb'


@pytest.mark.parametrize('case', [
    'race', 'legacy_exit', 'legacy_chain', 'config', 'pin', 'hold', 'presence',
    'no_ledger', 'unknown', 'manual', 'wrong_slot', 'bad_order', 'bad_counter',
    'arbitrary_cursor', 'live_claim', 'ambiguous_pause', 'legacy_wrong_error',
    'legacy_wrong_tool', 'legacy_wrong_frontier',
])
def test_native_snapshot_pause_race(tmp_path, case):
    if not Path(NATIVE, 'cron/jobs.py').is_file():
        pytest.skip('unchanged native scheduler checkout unavailable')
    env = dict(os.environ, HERMES_HOME=str(tmp_path / 'home'), HERMES_SKIP_ENV_FILE='1',
               PYTHONDONTWRITEBYTECODE='1', HERMES_TIMEZONE='America/Los_Angeles')
    code = ('import importlib.util; s=importlib.util.spec_from_file_location("race",'
            + repr(str(Path(__file__).resolve())) + '); m=importlib.util.module_from_spec(s);'
            's.loader.exec_module(m); m.exercise(' + repr(str(tmp_path)) + ','
            + repr(case) + ',' + repr(NATIVE) + ')')
    result = subprocess.run([sys.executable, '-B', '-c', code], env=env,
                            capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr


def exercise(directory, case, native):
    import copy
    from contextlib import contextmanager
    from datetime import datetime, timedelta
    import hashlib
    import json
    import sqlite3

    base = Path(directory).resolve()
    home = base / 'home'
    root = base / 'business'
    (home / 'cron').mkdir(parents=True)
    root.mkdir()
    source = root / 'payload.py'
    source.write_text('# never executed\n')
    sys.path[:0] = [str(ROOT / 'scripts'), native]
    from cron import jobs as scheduler, executions
    import maintenance_window as mw
    assert scheduler._current_cron_store().jobs_file == home / 'cron/jobs.json'
    assert scheduler._ensure_croniter()  # dependency import before effect guard
    clock = [datetime.fromisoformat('2030-10-01T13:54:59-07:00')]
    scheduler._hermes_now = executions._hermes_now = lambda: clock[0]
    original = {'id': 'f918ef069c4a', 'name': 'Lane memory headroom guardian',
                'schedule': {'kind': 'cron', 'expr': '*/5 * * * *'},
                'enabled': True, 'state': 'scheduled', 'paused_at': None,
                'paused_reason': None, 'next_run_at': '2030-10-01T13:55:00-07:00',
                'repeat': {'times': None, 'completed': 10831},
                'script': str(source), 'script_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                'no_agent': True, 'fire_claim': None}
    scheduler._save_jobs_unlocked([copy.deepcopy(original)])
    entry = {'kind': 'hermes', 'id': original['id'], 'command': str(source),
             'affected': True, 'trace': [str(source)],
             'prior': {k: copy.deepcopy(v) for k, v in original.items()
                       if k in ('repeat', 'enabled', 'state', 'next_run_at', 'schedule')}}
    journal = base / 'window.json'
    calls = []
    fire_depth = [0]
    fire = scheduler._fire_job_lock
    @contextmanager
    def tracked_fire(jid):
        with fire(jid) as acquired:
            fire_depth[0] += int(acquired)
            try: yield acquired
            finally: fire_depth[0] -= int(acquired)
    scheduler._fire_job_lock = tracked_fire

    class Backend(mw.LinuxBackend):
        def scheduler(self): return scheduler
        def inventory(self): return {'entries': [copy.deepcopy(entry)], 'complete': True, 'errors': []}
        def inflight(self, entries): return []  # no payloads/dispatch in this fixture
        def cron_tool(self, action, jid):
            assert action == 'pause', 'resume/reset/replay forbidden'
            calls.append(action)
            assert scheduler.pause_job(jid, reason=self.pause_reason())
            if case == 'ambiguous_pause':
                # Pause actually landed; misleading text must not prove a no-write refusal.
                raise mw.WindowError('pause_preimage_changed')
    backend = Backend(root, home, journal)
    window = mw.MaintenanceWindow(backend, journal, [base / 'source.lock'])

    # Retain authentic old controller bytes for the old journal/tool pin; do not
    # change live control tools or invent a rejection receipt after the fact.
    legacy_bytes = subprocess.check_output(['git', '-C', str(ROOT), 'show',
                                          LEGACY + ':scripts/maintenance_window.py'])
    legacy_path = base / 'old-tools/maintenance_window.py'
    legacy_path.parent.mkdir()
    legacy_path.write_bytes(legacy_bytes)

    # After setup, deny effects outside the disposable fixture, including child
    # processes/network/signals. Native registry/ledger/flock writes stay real.
    def audit(event, args):
        if event in ('subprocess.Popen', 'socket.connect', 'socket.bind', 'os.kill'):
            raise AssertionError('effect denied: ' + event)
        if event == 'open':
            target, mode, flags = args
            if isinstance(target, (str, bytes, os.PathLike)) and (flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT)):
                assert Path(os.fsdecode(target)).resolve().is_relative_to(base), ('outside write', target)
        if event in ('os.rename', 'os.remove', 'os.rmdir', 'os.mkdir', 'os.chmod'):
            paths = args[:2] if event == 'os.rename' else args[:1]
            for target in paths:
                if isinstance(target, (str, bytes, os.PathLike)):
                    assert Path(os.fsdecode(target)).resolve().is_relative_to(base), ('outside mutation', target)
    sys.addaudithook(audit)

    def tick(at='2030-10-01T13:55:42-07:00'):
        clock[0] = datetime.fromisoformat(at)
        assert [j['id'] for j in scheduler.get_due_jobs()] == [entry['id']]
        record = executions.create_execution(entry['id'], source='builtin')
        claimed = scheduler.claim_job_for_fire(entry['id'], return_job=True)
        assert claimed
        executions.set_execution_occurrence(record['id'], claimed['_scheduled_instant'])
        clock[0] += timedelta(seconds=1)
        assert executions.mark_execution_running(record['id'])
        clock[0] += timedelta(microseconds=536412)
        assert scheduler.mark_job_run(entry['id'], True,
                                      expected_fire_owner=claimed['fire_claim']['by'])
        clock[0] += timedelta(microseconds=35404)
        assert executions.finish_execution(record['id'], success=True)
        return record['id']

    prior = backend.snapshot(entry)
    if case.startswith('legacy_'):
        prior.pop('repeat')
        prior.pop('captured_at')  # exact old snapshot schema uses inventory repeat
    data = {'schema': mw.SCHEMA, 'root': str(root), 'hermes': str(home),
            'journal': str(journal), 'state': 'recovery_required',
            'created_at': datetime.fromisoformat('2030-10-01T13:55:36-07:00').timestamp(),
            'source_sha256': {str(source): hashlib.sha256(source.read_bytes()).hexdigest()},
            'control_tools': {str(legacy_path): hashlib.sha256(legacy_bytes).hexdigest()},
            'inventory_shape': mw.inventory_shape(backend.inventory()), 'locks': [],
            'expires_at': clock[0].timestamp() + 300,
            'entries': [{'entry': entry, 'prior': prior, 'intent': True, 'paused': False,
                         'restored': False, 'restore_intent': True}],
            'error': 'pause_preimage_changed'}
    mw.atomic_json(journal, data)
    execution = tick()
    after_tick = backend.job(entry['id'])
    assert after_tick['next_run_at'] == '2030-10-01T14:00:00-07:00'
    assert after_tick['last_run_at'] == '2030-10-01T13:55:43.536412-07:00'
    if case == 'legacy_chain':
        tick('2030-10-01T14:00:42-07:00')
        tick('2030-10-01T14:05:42-07:00')
        after_tick = backend.job(entry['id'])
        assert after_tick['next_run_at'] == '2030-10-01T14:10:00-07:00'

    alterations = {'config': ('no_agent', False), 'pin': ('script_sha256', 'moved'),
                   'hold': ('disabled_reason', 'operator hold'), 'presence': ('pending_slot', None),
                   'bad_counter': ('repeat', {'times': None, 'completed': 10900}),
                   'arbitrary_cursor': ('next_run_at', '2030-10-01T14:10:00-07:00'),
                   'live_claim': ('fire_claim', {'at': clock[0].isoformat(), 'by': 'live'})}
    if case in alterations:
        current = scheduler.load_jobs()
        key, value = alterations[case]
        current[0][key] = value
        scheduler._save_jobs_unlocked(current)
    database = scheduler._current_cron_store().cron_dir / 'executions.db'
    with sqlite3.connect(database) as db:
        if case == 'no_ledger': db.execute('DELETE FROM executions')
        if case == 'unknown': db.execute("UPDATE executions SET status='unknown'")
        if case == 'manual': db.execute('UPDATE executions SET scheduled_instant=NULL')
        if case == 'wrong_slot': db.execute("UPDATE executions SET scheduled_instant='2030-10-01T20:50:00+00:00'")
        if case == 'bad_order': db.execute("UPDATE executions SET claimed_at='2030-10-01T13:56:00-07:00'")
    registry_before = (home / 'cron/jobs.json').read_bytes()
    ledger_before = database.read_bytes()
    if case == 'legacy_wrong_error': data['error'] = 'unacknowledged_pause'
    if case == 'legacy_wrong_tool': data['control_tools'][str(legacy_path)] = '0' * 64
    if case == 'legacy_wrong_frontier':
        data['entries'].append(copy.deepcopy(data['entries'][0]))
    mw.atomic_json(journal, data)
    saves = []
    def install_saver_probe():
        save = scheduler._save_jobs_unlocked
        def fenced_save(jobs, *args, **kwargs):
            assert fire_depth[0] > 0 and scheduler._jobs_lock_state.depth > 0
            target = next(j for j in jobs if j['id'] == entry['id'])
            recorded = mw.read_journal(journal)['entries'][0]['prior']
            assert recorded['fields']['next_run_at'] == target['next_run_at']
            saves.append(copy.deepcopy(target))
            return save(jobs, *args, **kwargs)
        scheduler._save_jobs_unlocked = fenced_save

    if case == 'race':
        # Inject the completed tick precisely after every snapshot, before first
        # pause, using the real maintained controller and native manager/saver.
        scheduler._save_jobs_unlocked([copy.deepcopy(original)])
        with sqlite3.connect(database) as db: db.execute('DELETE FROM executions')
        journal.unlink()
        original_snapshot = backend.snapshot
        def snapshot_then_tick(e):
            value = original_snapshot(e)
            tick()
            install_saver_probe()
            return value
        backend.snapshot = snapshot_then_tick
        window.enter()
        assert window.status()['verified']
        durable = mw.read_journal(journal)['entries'][0]['prior']
        assert durable['fields']['next_run_at'] == after_tick['next_run_at']
        assert durable['paused_fields']['next_run_at'] == after_tick['next_run_at']
        assert backend.job(entry['id'])['enabled'] is False
        assert calls == ['pause']
        window.exit()
        assert backend.job(entry['id']) == after_tick
        assert mw.read_journal(journal)['state'] == 'closed'
    elif case in ('legacy_exit', 'legacy_chain'):
        # Current-tool exit accepts the old immutable tool pin without changing
        # that pin, sources, baseline or guard; closure is strictly no-write.
        install_saver_probe()
        window.exit()
        closed = mw.read_journal(journal)
        assert closed['state'] == 'closed' and closed['entries'][0]['restored']
        assert closed['control_tools'] == data['control_tools']
        assert closed['entries'][0]['prior'] == prior
        assert (home / 'cron/jobs.json').read_bytes() == registry_before
        assert calls == []
    elif case == 'ambiguous_pause':
        # Saver lost acknowledgement after an actual pause: preserve ordinary
        # paused-ownership reconciliation, never classify paused=False alone.
        window._take_locks(data)
        install_saver_probe()
        try:
            with pytest.raises(mw.WindowError, match='pause_preimage_changed'):
                backend.pause(entry, prior)
            assert calls == ['pause']
            assert backend.job(entry['id'])['enabled'] is False
            data['error'] = 'unacknowledged_pause'
            mw.atomic_json(journal, data)
            backend.restore(entry, prior)
            assert backend.job(entry['id']) == after_tick
        finally: window.release_locks()
    else:
        install_saver_probe()
        if not case.startswith('legacy_'):
            with pytest.raises(mw.WindowError): backend.pause(entry, prior)
        with pytest.raises(mw.WindowError): window.exit()
        assert mw.read_journal(journal)['state'] == 'recovery_required'
        assert (home / 'cron/jobs.json').read_bytes() == registry_before
        assert calls == []
    assert len(saves) == (2 if case in ('race', 'ambiguous_pause') else 0)
    assert database.read_bytes() == ledger_before or case == 'race'
    assert fire_depth == [0]
