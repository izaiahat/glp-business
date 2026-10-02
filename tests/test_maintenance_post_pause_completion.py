"""F09's receiving timestamps/interval/counters, native completion after pause.

Only disposable homes are writable; payload execution and resume are forbidden.
The ledger is produced by native occurrence APIs, not a fabricated success.
"""
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
NATIVE = os.environ.get('GLP_NATIVE_CHECKOUT', '/home/ubuntu/.hermes/hermes-agent')


@pytest.mark.parametrize('case', [
    'receiving', 'lost_ack', 'failed', 'manual', 'missing', 'unrelated', 'unknown',
    'handoff', 'error', 'hold', 'config', 'pin', 'cursor', 'claim', 'counter',
])
def test_post_pause_completion(tmp_path, case):
    if not Path(NATIVE, 'cron/jobs.py').is_file():
        pytest.skip('native checkout unavailable')
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
    from datetime import datetime
    import hashlib
    import json
    import sqlite3

    base = Path(directory).resolve()
    home, root = base / 'home', base / 'business'
    (home / 'cron').mkdir(parents=True)
    root.mkdir()
    source = root / 'never-executed.sh'
    source.write_text('# inert fixture\n')
    sys.path[:0] = [str(ROOT / 'scripts'), native]
    from cron import jobs as scheduler, executions
    import maintenance_window as mw
    assert scheduler._current_cron_store().jobs_file == home / 'cron/jobs.json'
    assert scheduler._ensure_croniter()
    clock = [datetime.fromisoformat('2026-10-01T23:30:43.920713-07:00')]
    scheduler._hermes_now = executions._hermes_now = lambda: clock[0]
    # The claim belongs to the dispatcher; execution.pid is the worker PID.
    scheduler._machine_id = lambda: 'fixture-dispatcher:3990514'
    original = {'id': 'b0c800751b88', 'schedule': {'kind': 'interval', 'minutes': 10,
                'display': 'every 10m'}, 'enabled': True, 'state': 'scheduled',
                'paused_at': None, 'paused_reason': None,
                'next_run_at': clock[0].isoformat(), 'repeat': {'completed': 9250, 'times': None},
                'no_agent': True, 'script': str(source), 'fire_claim': None,
                'script_sha256': hashlib.sha256(source.read_bytes()).hexdigest()}
    scheduler._save_jobs_unlocked([copy.deepcopy(original)])
    # Real due/claim/running APIs recreate the already-running receiving case.
    clock[0] = datetime.fromisoformat('2026-10-01T23:31:42.880353-07:00')
    assert [j['id'] for j in scheduler.get_due_jobs()] == [original['id']]
    execution = executions.create_execution(original['id'], source='builtin')
    clock[0] = datetime.fromisoformat('2026-10-01T23:31:42.924491-07:00')
    claimed = scheduler.claim_job_for_fire(original['id'], return_job=True)
    assert isinstance(claimed, dict)
    executions.set_execution_occurrence(execution['id'], claimed['_scheduled_instant'])
    assert claimed['next_run_at'] == '2026-10-01T23:41:42.924491-07:00'
    clock[0] = datetime.fromisoformat('2026-10-01T23:31:43.696817-07:00')
    assert executions.mark_execution_running(execution['id'])
    clock[0] = datetime.fromisoformat('2026-10-01T23:31:43.737638-07:00')
    assert scheduler.heartbeat_fire_claim(original['id'], expected_owner=claimed['fire_claim']['by'])
    database = home / 'cron/executions.db'
    entry = {'kind': 'hermes', 'id': original['id'], 'affected': True,
             'command': str(source), 'trace': [str(source)],
             'prior': copy.deepcopy(scheduler.load_jobs()[0])}
    journal = base / 'window.json'
    calls, saves = [], []
    depth = [0]
    fire = scheduler._fire_job_lock
    @contextmanager
    def tracked_fire(jid):
        with fire(jid) as acquired:
            depth[0] += int(acquired)
            try: yield acquired
            finally: depth[0] -= int(acquired)
    scheduler._fire_job_lock = tracked_fire
    class Backend(mw.LinuxBackend):
        def scheduler(self): return scheduler
        def inventory(self): return {'entries': [entry], 'complete': True, 'errors': []}
        def inflight(self, entries): return []
        def cron_tool(self, action, jid):
            assert action == 'pause', 'resume/replay forbidden'
            calls.append(action)
            assert scheduler.pause_job(jid, reason=self.pause_reason())
    backend = Backend(root, home, journal)
    clock[0] = datetime.fromisoformat('2026-10-01T23:32:37.612722-07:00')
    prior = backend.snapshot(entry)
    prior['captured_at'] = clock[0].timestamp()
    prior['paused_fields']['paused_at'] = '2026-10-02T06:32:37Z'
    data = {'schema': mw.SCHEMA, 'root': str(root), 'hermes': str(home),
            'journal': str(journal), 'state': 'entering', 'created_at': 1790922758.2952433,
            'expires_at': clock[0].timestamp() + 3600, 'source_sha256': {},
            'control_tools': {'retained-controller': 'unchanged-approval-hash'},
            'entries': [{'entry': entry, 'prior': prior, 'intent': True, 'paused': False,
                         'restored': False, 'restore_intent': False}]}
    mw.atomic_json(journal, data)
    backend.pause(entry, prior)
    data['entries'][0]['paused'] = True
    data['state'], data['error'] = 'recovery_required', 'active_window_verification_failed'
    mw.atomic_json(journal, data)
    clock[0] = datetime.fromisoformat('2026-10-01T23:33:33.963982-07:00')
    assert scheduler.mark_job_run(original['id'], True,
                                  expected_fire_owner=claimed['fire_claim']['by'])
    clock[0] = datetime.fromisoformat('2026-10-01T23:33:33.979256-07:00')
    assert executions.finish_execution(execution['id'], success=True, delivery_outcome='suppressed')
    current = backend.job(entry['id'])
    assert current['next_run_at'] == '2026-10-01T23:43:33.963982-07:00'
    assert current['repeat'] == {'completed': 9251, 'times': None}
    assert not backend.paused(entry, prior)  # ACTIVE remains fail closed.
    alterations = {'hold': ('paused_reason', 'foreign hold'), 'config': ('no_agent', False),
                   'pin': ('script_sha256', 'moved'), 'cursor': ('next_run_at', '2030-01-01T00:00:00Z'),
                   'claim': ('fire_claim', {'by': 'live', 'at': clock[0].isoformat()}),
                   'counter': ('repeat', {'completed': 9252, 'times': None})}
    if case in alterations:
        jobs = scheduler.load_jobs()
        k, v = alterations[case]
        jobs[0][k] = v
        scheduler._save_jobs_unlocked(jobs)
    with sqlite3.connect(database) as db:
        if case == 'missing': db.execute('DELETE FROM executions')
        if case in ('failed', 'unknown'): db.execute('UPDATE executions SET status=?', (case,))
        if case == 'manual': db.execute("UPDATE executions SET source='manual', scheduled_instant=NULL")
        if case == 'unrelated': db.execute("UPDATE executions SET job_id='other'")
        if case == 'handoff': db.execute('UPDATE executions SET handoff_pending=1')
        if case == 'error': db.execute("UPDATE executions SET error='uncertain'")
    before, ledger_before = (home / 'cron/jobs.json').read_bytes(), database.read_bytes()
    # Effect guard covers the entire actual maintained restoration path.
    def audit(event, args):
        if event in ('subprocess.Popen', 'socket.connect', 'socket.bind', 'os.kill'):
            raise AssertionError('effect denied: ' + event)
        if event == 'open':
            target, mode, flags = args
            if isinstance(target, (str, bytes, os.PathLike)) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT):
                assert Path(os.fsdecode(target)).resolve().is_relative_to(base), ('outside write', target)
        if event in ('os.rename', 'os.remove', 'os.rmdir', 'os.mkdir', 'os.chmod'):
            for target in (args[:2] if event == 'os.rename' else args[:1]):
                if isinstance(target, (str, bytes, os.PathLike)):
                    assert Path(os.fsdecode(target)).resolve().is_relative_to(base)
    sys.addaudithook(audit)
    saver = scheduler._save_jobs_unlocked
    def observed_save(jobs, **kwargs):
        assert depth[0] > 0 and scheduler._jobs_lock_state.depth > 0
        recorded = mw.read_journal(journal)['entries'][0]['prior']['post_pause_cursor_progress']
        target = next(j for j in jobs if j['id'] == entry['id'])
        assert recorded['to'] == target['next_run_at']
        assert recorded['executions'] == [execution['id']]
        saves.append(copy.deepcopy(target))
        result = saver(jobs, **kwargs)
        if case == 'lost_ack' and len(saves) == 1:
            raise RuntimeError('lost acknowledgement after durable native save')
        return result
    scheduler._save_jobs_unlocked = observed_save
    window = mw.MaintenanceWindow(backend, journal, [base / 'source.lock'])
    if case in ('receiving', 'lost_ack'):
        expected = copy.deepcopy(current)
        for k in mw.CONTROL_FIELDS:
            if k == 'next_run_at': continue
            if k in prior['fields']: expected[k] = copy.deepcopy(prior['fields'][k])
            else: expected.pop(k, None)
        if case == 'lost_ack':
            with pytest.raises(mw.WindowError, match='restore_failed'): window.exit()
            assert backend.job(entry['id']) == expected
        window.exit()
        assert backend.job(entry['id']) == expected
        closed = mw.read_journal(journal)
        assert closed['state'] == 'closed' and closed['entries'][0]['restored']
        assert closed['control_tools'] == data['control_tools']
        assert closed['entries'][0]['prior']['fields'] == prior['fields']
        assert len(saves) == 1
        window.exit()
        assert len(saves) == 1
    else:
        with pytest.raises(mw.WindowError): window.exit()
        assert (home / 'cron/jobs.json').read_bytes() == before
        assert not saves
    assert calls == ['pause'] and database.read_bytes() == ledger_before and depth == [0]
