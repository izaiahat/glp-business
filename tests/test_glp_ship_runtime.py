"""Registered runtime probes use disposable files and injected native controls."""
from contextlib import contextmanager
import hashlib
import json
from types import SimpleNamespace

import pytest
import glp_ship_runtime as runtime


def fixture(tmp_path):
    root, hermes, output = (tmp_path / x for x in ('root', 'hermes', 'output'))
    root.mkdir()
    scripts = hermes / 'scripts'
    scripts.mkdir(parents=True)
    registry = hermes / 'cron/jobs.json'
    registry.parent.mkdir()
    jobs = []
    for jid in ('first', 'second'):
        path = scripts / (jid + '.py')
        path.write_text('# fixture never executes\n')
        path.chmod(0o755)
        jobs.append(dict(id=jid, script=path.name, script_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                         enabled=False, state='paused', no_agent=True, running=False,
                         run_claim=None, fire_claim=None))
    registry.write_text(json.dumps({'jobs': jobs}))
    events, locks = [], set()
    class Scheduler:
        def _current_cron_store(self):
            return SimpleNamespace(jobs_file=registry)
        @contextmanager
        def _fire_job_lock(self, jid):
            locks.add(jid)
            try:
                yield True
            finally:
                locks.remove(jid)
        def update(self, jid, enabled, state):
            document = json.loads(registry.read_text())
            job = next(j for j in document['jobs'] if j['id'] == jid)
            job.update(enabled=enabled, state=state)
            registry.write_text(json.dumps(document))
            return job
        def pause_job(self, jid, reason):
            assert reason
            events.append(('pause', jid))
            return self.update(jid, False, 'paused')
        def resume_job(self, jid):
            assert locks == {'first', 'second'}
            events.append(('resume', jid))
            return self.update(jid, True, 'scheduled')
    return SimpleNamespace(root=root, hermes=hermes, output=output, registry=registry,
                           scheduler=Scheduler(), events=events, locks=locks)


def test_group_runs_once_under_native_fences_then_resumes(tmp_path):
    f = fixture(tmp_path)
    def run(job):
        assert f.locks == {'first', 'second'}
        f.events.append(('run', job['id']))
        return True, 'fixture output'
    result = runtime.run_registered(f.root, f.hermes, ['first', 'second'], f.output,
                                    scheduler=f.scheduler, runner=run)
    assert result['success']
    assert f.events == [('run', 'first'), ('run', 'second'), ('resume', 'first'), ('resume', 'second')]
    assert [run['exit_code'] for run in result['runs']] == [0, 0]
    for run in result['runs']:
        assert 'fixture output' in open(run['output_path']).read()
        assert run['run_type'] == 'manual_registered'


@pytest.mark.parametrize('output,code,status', [('Script exited with code 7\nstderr:\nfailed', 7, 'failed'),
    ('Script timed out after 30s: fixture', None, 'timed_out'), ('Script execution failed: fixture', None, 'unknown')])
def test_failed_run_pauses_entire_group_without_retry(tmp_path, output, code, status):
    f = fixture(tmp_path)
    calls = []
    def run(job):
        calls.append(job['id'])
        return False, output
    result = runtime.run_registered(f.root, f.hermes, ['first', 'second'], f.output,
                                    scheduler=f.scheduler, runner=run)
    assert not result['success'] and calls == ['first']
    assert result['runs'][0]['exit_code'] == code
    assert result['runs'][0]['status'] == status
    assert f.events == [('pause', 'first'), ('pause', 'second')]
    assert all(job['enabled'] is False for job in json.loads(f.registry.read_text())['jobs'])


@pytest.mark.parametrize('mutation', ['hash', 'claim', 'agent', 'symlink', 'escape'])
def test_admission_failures_never_execute_and_pause_group(tmp_path, mutation):
    f = fixture(tmp_path)
    document = json.loads(f.registry.read_text())
    job = document['jobs'][0]
    if mutation == 'hash':
        job['script_sha256'] = '0' * 64
    elif mutation == 'claim':
        job['run_claim'] = {'owner': 'someone'}
    elif mutation == 'agent':
        job['no_agent'] = False
    elif mutation == 'escape':
        job['script'] = '../outside.py'
    else:
        path = f.hermes / 'scripts/first.py'
        path.unlink()
        path.symlink_to(f.hermes / 'scripts/second.py')
    f.registry.write_text(json.dumps(document))
    def forbidden(job):
        pytest.fail('runner must not be called')
    result = runtime.run_registered(f.root, f.hermes, ['first', 'second'], f.output,
                                    scheduler=f.scheduler, runner=forbidden)
    assert not result['success']
    assert f.events == [('pause', 'first'), ('pause', 'second')]


def test_second_failure_keeps_previously_successful_member_paused(tmp_path):
    f = fixture(tmp_path)
    calls = []
    def run(job):
        calls.append(job['id'])
        return (True, 'first succeeded') if job['id'] == 'first' else (False, 'Script exited with code 9')
    result = runtime.run_registered(f.root, f.hermes, ['first', 'second'], f.output,
                                    scheduler=f.scheduler, runner=run)
    assert not result['success'] and calls == ['first', 'second']
    assert [run['exit_code'] for run in result['runs']] == [0, 9]
    assert all(event[0] == 'pause' for event in f.events)


def test_resume_failure_repauses_every_member(tmp_path):
    f = fixture(tmp_path)
    original = f.scheduler.resume_job
    def resume(jid):
        if jid == 'second':
            raise RuntimeError('fixture resume failure')
        return original(jid)
    f.scheduler.resume_job = resume
    result = runtime.run_registered(f.root, f.hermes, ['first', 'second'], f.output,
                                    scheduler=f.scheduler, runner=lambda job: (True, 'done'))
    assert not result['success']
    assert f.events == [('resume', 'first'), ('pause', 'first'), ('pause', 'second')]
    assert not result['pause_errors']


def test_native_adapter_preserves_job_workdir_and_timeout_contract(monkeypatch):
    import sys
    job = {'script': 'registered.py', 'workdir': '/fixture', 'script_timeout_seconds': 123}
    calls = []
    def native(script, **kwargs):
        calls.append((script, kwargs))
        return True, 'native-redacted-output'
    monkeypatch.setitem(sys.modules, 'cron.scheduler_script', SimpleNamespace(_run_job_script=native))
    assert runtime._native_runner(job) == (True, 'native-redacted-output')
    assert calls == [('registered.py', {'workdir': '/fixture', 'job': job})]


def test_wrong_scheduler_store_never_mutates_other_profile(tmp_path):
    f = fixture(tmp_path)
    f.scheduler._current_cron_store = lambda: SimpleNamespace(jobs_file=tmp_path / 'wrong/jobs.json')
    with pytest.raises(runtime.RuntimeRefused, match='wrong_scheduler_store'):
        runtime.run_registered(f.root, f.hermes, ['first', 'second'], f.output, scheduler=f.scheduler)
    assert not f.events


def test_runner_exception_is_unknown_effect_and_never_retried(tmp_path):
    f = fixture(tmp_path)
    calls = []
    def interrupted(job):
        calls.append(job['id'])
        raise OSError('fixture lost runner acknowledgement')
    result = runtime.run_registered(f.root, f.hermes, ['first', 'second'], f.output,
                                    scheduler=f.scheduler, runner=interrupted)
    assert not result['success'] and calls == ['first']
    assert result['runs'][0]['status'] == 'unknown'
    assert result['runs'][0]['exit_code'] is None
    assert len(f.events) == 2 and all(event[0] == 'pause' for event in f.events)
