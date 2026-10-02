"""Collector registration and single-line failure holds in disposable crontabs."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest
import glp_ship_collector as collector
import maintenance_window as mw


def put(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)


def fixture(tmp_path):
    root, hermes = tmp_path / 'root', tmp_path / 'hermes'
    root.mkdir(); hermes.mkdir()
    sources = ('scripts/cron_bridge_action.py', 'scripts/glp_analytics_brain_cadence.py')
    for rel in sources:
        put(root / rel, b'# fixture source, never executed\n')
    interpreter = hermes / 'hermes-agent/venv/bin/python'
    command = (f"cd {root} && {interpreter} {root}/scripts/cron_bridge_action.py analytics_brain "
               f"--scheduled --cron '7 * * * *' --timezone America/Los_Angeles --arg phase=hourly "
               f">> {root}/reports/ops/n8n-to-cron-20260924/cron.log 2>&1")
    line = '7 * * * * ' + command
    drift_id = 'crontab:' + hashlib.sha256(line.encode()).hexdigest()[:16]
    manifest = {'schema': 'scheduled-source-drift-v1', 'jobs': [{'id': drift_id, 'surface': 'crontab', 'payloads': [],
                   'sources': [{'path': str(root / rel), 'source': rel,
                                'reviewed_sha256': hashlib.sha256((root / rel).read_bytes()).hexdigest()}
                               for rel in sources]}]}
    put(root / 'ops/scheduled-source-drift-manifest.json', json.dumps(manifest).encode())
    action = {'argv': [str(interpreter), str(root / sources[1])], 'cwd': str(root),
              'allowed_args': ['phase'], 'timeout_s': 600}
    put(root / 'ops/n8n-bridge/actions.json', json.dumps({'analytics_brain': action}).encode())
    class Backend(mw.LinuxBackend):
        def __init__(self):
            super().__init__(root, hermes, tmp_path / 'unused-journal')
            self.text = '# fixture crontab\n' + line + '\n0 0 * * * /fixture/unrelated\n'
            self.writes = []
        def inventory(self):
            entries = []
            for index, raw in enumerate(self.text.splitlines()):
                if not raw or raw.startswith('#'):
                    continue
                entries.append({'kind': 'crontab', 'id': str(index), 'command': raw.split(None, 5)[5],
                                'prior': {'line': raw, 'index': index, 'maintenance_marker': None},
                                'cwd': str(root), 'affected': True, 'trace': [], 'source_sha256': {}})
            return {'complete': True, 'errors': [], 'entries': entries}
        def command(self, argv, payload=None):
            if argv == ['/usr/bin/crontab', '-l']:
                return self.text
            assert argv == ['/usr/bin/crontab', '-']
            self.writes.append(payload)
            self.text = payload
            return ''
    backend = Backend()
    return SimpleNamespace(root=root, hermes=hermes, backend=backend, command=command, line=line,
                           job_id=drift_id, output=tmp_path / 'output')


def test_registration_is_readonly_and_binds_exact_hourly_control(tmp_path):
    f = fixture(tmp_path)
    before = f.backend.text
    record = collector.registration(f.root, f.hermes, backend=f.backend)
    assert record['job_id'] == f.job_id and record['command'] == f.command
    assert record['prior']['text'] == before
    assert set(record['retained_sources']) == {'scripts/cron_bridge_action.py', 'scripts/glp_analytics_brain_cadence.py'}
    assert not f.backend.writes


def test_success_runs_exact_command_with_manual_registered_flag(tmp_path):
    f = fixture(tmp_path)
    record = collector.registration(f.root, f.hermes, backend=f.backend)
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout='fixture transcript')
    result = collector.run_registered(f.root, f.hermes, record, f.output, backend=f.backend, runner=run)
    assert result['success'] and len(calls) == 1
    assert calls[0][0] == f.command
    assert calls[0][1]['env']['CRON_BRIDGE_MANUAL_REGISTERED'] == '1'
    assert result['runs'][0]['exit_code'] == 0 and result['runs'][0]['run_type'] == 'manual_registered'
    assert not f.backend.writes


@pytest.mark.parametrize('fault', ['failed', 'timeout', 'unknown'])
def test_failure_journals_and_pauses_only_selected_line(tmp_path, fault):
    f = fixture(tmp_path)
    record = collector.registration(f.root, f.hermes, backend=f.backend)
    original = f.backend.text
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        if fault == 'timeout':
            raise subprocess.TimeoutExpired(command, 30, output='fixture timed out')
        if fault == 'unknown':
            raise OSError('fixture unknown acknowledgement')
        return subprocess.CompletedProcess(command, 7, stdout='fixture failed')
    result = collector.run_registered(f.root, f.hermes, record, f.output, backend=f.backend, runner=run)
    assert not result['success'] and calls == [f.command]
    assert result['runs'][0]['exit_code'] == (7 if fault == 'failed' else None)
    assert len(f.backend.writes) == 1
    before, after = original.splitlines(), f.backend.text.splitlines()
    assert before[0] == after[0] and before[2] == after[2]
    assert after[1].startswith('# GLP44/73 maintenance ') and after[1].endswith(before[1])
    journal = mw.read_journal(result['pause_journal'])
    assert journal['state'] == 'recovery_required' and journal['reason']
    assert len(journal['entries']) == 1 and journal['entries'][0]['intent'] and journal['entries'][0]['paused']


@pytest.mark.parametrize('mutation', ['command', 'source', 'action'])
def test_changed_registration_never_runs(tmp_path, mutation):
    f = fixture(tmp_path)
    record = collector.registration(f.root, f.hermes, backend=f.backend)
    if mutation == 'command':
        f.backend.text = f.backend.text.replace('phase=hourly', 'phase=daily')
    elif mutation == 'source':
        (f.root / 'scripts/glp_analytics_brain_cadence.py').write_text('changed')
    else:
        path = f.root / 'ops/n8n-bridge/actions.json'
        doc = json.loads(path.read_text())
        doc['analytics_brain']['timeout_s'] += 1
        path.write_text(json.dumps(doc))
    def forbidden(*args, **kwargs):
        pytest.fail('must not run changed registration')
    result = collector.run_registered(f.root, f.hermes, record, f.output, backend=f.backend, runner=forbidden)
    assert not result['success']
    if mutation == 'command':
        assert not f.backend.writes and result['pause_error']


def test_shell_injection_or_duplicate_hourly_registration_refused(tmp_path):
    f = fixture(tmp_path)
    f.backend.text = f.backend.text.replace('phase=hourly', 'phase=hourly; echo injected')
    with pytest.raises(RuntimeError):
        collector.registration(f.root, f.hermes, backend=f.backend)
    f.backend.text = f.line + '\n' + f.line + '\n'
    with pytest.raises(RuntimeError, match='unique'):
        collector.registration(f.root, f.hermes, backend=f.backend)


def test_native_relative_action_argv_remains_supported(tmp_path):
    f = fixture(tmp_path)
    path = f.root / 'ops/n8n-bridge/actions.json'
    action = json.loads(path.read_text())
    action['analytics_brain']['argv'] = ['python3', 'scripts/glp_analytics_brain_cadence.py']
    path.write_text(json.dumps(action))
    assert collector.registration(f.root, f.hermes, backend=f.backend)['job_id'] == f.job_id


def test_malformed_hold_record_cannot_pause_an_unrelated_line(tmp_path):
    f = fixture(tmp_path)
    record = collector.registration(f.root, f.hermes, backend=f.backend)
    record['prior']['index'] = 2
    record['prior']['line'] = record['prior']['text'].splitlines()[2]
    result = collector.run_registered(f.root, f.hermes, record, f.output, backend=f.backend,
                                     runner=lambda *a, **kw: pytest.fail('must not execute'))
    assert not result['success'] and not f.backend.writes
    assert result['pause_error'] == 'collector_hold_registration_invalid'


def test_default_runner_timeout_signals_group_once_without_relaunch(monkeypatch):
    import signal
    events = []
    class Process:
        pid = 123456789
        returncode = -9
        def communicate(self, timeout):
            events.append(('communicate', timeout))
            if len([event for event in events if event[0] == 'communicate']) < 3:
                raise subprocess.TimeoutExpired('fixture', timeout)
            return 'fixture partial output', None
    def launch(argv, **kwargs):
        events.append(('launch', argv, kwargs))
        return Process()
    monkeypatch.setattr(collector.subprocess, 'Popen', launch)
    monkeypatch.setattr(collector.os, 'killpg', lambda pid, sig: events.append(('killpg', pid, sig)))
    with pytest.raises(subprocess.TimeoutExpired) as failure:
        collector._execute('fixture exact command', env={'fixture': '1'}, cwd='/fixture', timeout=1)
    assert failure.value.output == 'fixture partial output'
    assert len([event for event in events if event[0] == 'launch']) == 1
    assert [event for event in events if event[0] == 'killpg'] == [
        ('killpg', 123456789, signal.SIGTERM), ('killpg', 123456789, signal.SIGKILL)]
    assert events[0][2]['start_new_session'] is True


def test_registration_with_maintained_four_surface_inventory(tmp_path):
    import maintenance_inventory
    f = fixture(tmp_path)
    put(f.hermes / 'cron/jobs.json', b'{"jobs": []}')
    def probe(argv):
        if argv == ['crontab', '-l']:
            return f.backend.text
        if 'list-unit-files' in argv or 'list-units' in argv:
            return ''
        assert argv[3] == 'n8n-bridge.service'
        return ('LoadState=loaded\nActiveState=active\nUnitFileState=enabled\nMainPID=123\n'
                'ExecStart={ path=/usr/bin/true ; argv[]=true ; ignore_errors=no ; }\n')
    f.backend.inventory = lambda: maintenance_inventory.discover(f.root, f.hermes, probe=probe)
    record = collector.registration(f.root, f.hermes, backend=f.backend)
    assert record['job_id'] == f.job_id
    assert record['entry']['source_sha256'][str(f.root / 'scripts/cron_bridge_action.py')]
    assert not f.backend.writes
