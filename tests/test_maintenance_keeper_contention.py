"""Real keeper/adopter/exit admission with source-only disposable schedulers."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import maintenance_window as mw
import maintenance_inventory as inv


def test_fresh_opaque_diamond_closure_is_linear_and_preserves_cycle_custody(tmp_path, monkeypatch):
    # Actual import tracing, not an injected delay: shared failed closures used
    # by retained n8n actions must not be reparsed at every incoming edge.
    root, hermes = tmp_path / 'root', tmp_path / 'hermes'
    root.mkdir()
    hermes.mkdir()
    for index in range(10):
        for side in ('left', 'right'):
            (root / f'{side}{index}.py').write_text(f'import left{index+1}\nimport right{index+1}\n')
    for side in ('left', 'right'):
        (root / f'{side}10.py').write_text('opaque_callback()\n')
    reads = []
    original = Path.read_text

    def read(path, *args, **kwargs):
        reads.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', read)
    tracer = inv.Tracer(root, hermes, affected=set())
    with pytest.raises(inv.InventoryError) as error:
        tracer.file(root / 'left0.py', set())
    assert len(error.value.errors) == 2
    assert len(reads) <= 21, f'shared opaque DAG reparsed {len(reads)} times'
    # Every discovery constructs a new tracer: neither classifications nor
    # source reads persist across verification boundaries.
    (root / 'right10.py').write_text('')
    reads.clear()
    with pytest.raises(inv.InventoryError) as error:
        inv.Tracer(root, hermes, affected=set()).file(root / 'left0.py', set())
    assert len(error.value.errors) == 1
    assert root / 'right10.py' in reads
    # A failed result visited under a cycle is context-dependent. It must not
    # hide an affected path when later visited as an independent entrypoint.
    target = root / 'target.py'
    target.write_text('')
    (root / 'a.py').write_text('import b\nimport target\n')
    (root / 'b.py').write_text('import a\nopaque_callback()\n')
    tracer = inv.Tracer(root, hermes, affected={target})
    reached, trace = tracer.file(root / 'a.py', set())
    assert reached
    assert {str(root / 'a.py'), str(root / 'b.py'), str(target)} <= set(trace)
    hit, trace = tracer.file(root / 'b.py', set())
    assert hit and str(target) in trace


WRAPPER = '''
import json, os, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import maintenance_window as mw
import maintenance_inventory as inv
class Backend:
    def __init__(self, root, hermes, journal):
        self.root, self.hermes = Path(root).resolve(), Path(hermes).resolve()
        self.journal = Path(journal)
        self.file = self.root / 'scheduler.json'
    def inventory(self):
        report = inv.discover(self.root, self.hermes, probe=self.probe)
        with (self.root / 'scans.jsonl').open('a') as f:
            f.write(json.dumps({'pid': os.getpid(), 'shape': mw.inventory_shape(report), 'coverage': report['coverage']})+'\\n')
        if '--keeper' in sys.argv and self.journal.exists():
            data = mw.read_journal(self.journal)
            gate = self.root / 'slow-verify'
            if data['state'] == 'active' and gate.exists():
                gate.unlink()
                (self.root / 'keeper-verifying').touch()
                time.sleep(0.8)  # Deterministically retain the actual keeper op lock.
        return report
    def probe(self, argv):
        if argv == ['crontab', '-l']: return ''
        if 'list-unit-files' in argv or 'list-units' in argv: return ''
        assert argv[:4] == ['systemctl', '--user', 'show', 'n8n-bridge.service']
        return 'LoadState=loaded\\nActiveState=inactive\\nUnitFileState=disabled\\nExecStart={ argv[]=/bin/true ; }\\n'
    def snapshot(self, entry): return json.loads(self.file.read_text())[entry['id']]
    def pause(self, entry, prior):
        data = json.loads(self.file.read_text()); data[entry['id']]['paused'] = True
        self.file.write_text(json.dumps(data))
    def paused(self, entry, prior): return self.snapshot(entry)['paused']
    def restore(self, entry, prior):
        assert mw.locks_verified(mw.read_journal(self.journal))
        data = json.loads(self.file.read_text()); data[entry['id']] = prior
        self.file.write_text(json.dumps(data))
        with (self.root / 'restores.jsonl').open('a') as f: f.write(entry['id']+'\\n')
    def inflight(self, entries): return []
mw.LinuxBackend = Backend
mw.__file__ = __file__
if sys.argv[2] == 'adopt':
    root, hermes, journal = map(Path, sys.argv[3:6])
    with mw.require_active_window(journal, root, hermes):
        (root/'adopter-held').touch()
        deadline = time.monotonic()+6
        while not (root/'release-adopter').exists():
            assert time.monotonic() < deadline
            time.sleep(0.01)
    print('ADOPTED', flush=True)
else:
    raise SystemExit(mw.main(sys.argv[2:]))
'''


def wait_for(predicate, seconds=6):
    deadline = time.monotonic() + seconds
    while not predicate():
        assert time.monotonic() < deadline, 'isolated fixture deadline'
        time.sleep(0.01)


def assert_fenced(data):
    assert mw.locks_verified(data)
    for row in data['locks']:
        with Path(row['path']).open('a+') as fd:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_pre_effect_deadline_and_ambiguous_body_are_not_replayed(tmp_path):
    journal = tmp_path / 'window.json'
    effects = []
    with mw.operation_lock(journal):
        started = time.monotonic()
        with pytest.raises(mw.WindowError, match='window_operation_busy'):
            with mw.operation_lock(journal, timeout=0.1):
                effects.append('must-not-run')
        assert time.monotonic() - started < 0.5
    assert effects == []
    error = BlockingIOError('ambiguous native save')
    with pytest.raises(BlockingIOError) as caught:
        with mw.operation_lock(journal, timeout=0.1):
            effects.append('once')
            raise error
    assert caught.value is error
    assert effects == ['once']


@pytest.mark.parametrize('restoration', ['exit', 'signal', 'expiry'])
def test_actual_keeper_adopter_and_restoration_have_bounded_fair_admission(tmp_path, restoration):
    root, hermes = tmp_path / 'root', tmp_path / 'hermes'
    (root / 'scripts').mkdir(parents=True, exist_ok=True)
    (root / 'ops/n8n-bridge').mkdir(parents=True)
    (hermes / 'cron').mkdir(parents=True)
    (root / 'scripts/opaque.py').write_text('import left0\nopaque_callback()\n')
    for index in range(12):
        for side in ('left', 'right'):
            (root / f'scripts/{side}{index}.py').write_text(f'import left{index+1}\nimport right{index+1}\n')
    for side in ('left', 'right'):
        (root / f'scripts/{side}12.py').write_text('opaque_callback()\n')
    command = 'python3 ' + str(root / 'scripts/opaque.py')
    (hermes / 'cron/jobs.json').write_text(json.dumps({'jobs': [
        {'id': 'fixture', 'script': str(root / 'scripts/opaque.py'), 'no_agent': True}]}))
    (root / 'ops/n8n-bridge/actions.json').write_text(json.dumps({'opaque': {'argv': command.split()}}))
    state = root / 'scheduler.json'
    original = {'fixture': {'paused': False}, 'n8n-bridge.service': {'paused': True}}
    state.write_text(json.dumps(original))
    wrapper = tmp_path / 'fixture_keeper.py'
    wrapper.write_text(WRAPPER)
    journal = tmp_path / 'window.json'
    env = {**os.environ, 'HOME': str(tmp_path), 'HERMES_HOME': str(hermes), 'HERMES_SKIP_ENV_FILE': '1', 'PYTHONDONTWRITEBYTECODE': '1'}
    argv = [sys.executable, '-B', str(wrapper), str(ROOT / 'scripts')]
    options = ['--root', str(root), '--hermes', str(hermes), '--journal', str(journal),
               '--drain-seconds', '0', '--window-seconds', '3' if restoration == 'expiry' else '20']
    keeper = adopter = exiting = None
    try:
        # Launch the real detached-owner path directly so its exact PID remains
        # our disposable subprocess. No production keeper or scheduler exists.
        keeper = subprocess.Popen([*argv, 'enter', '--keeper', *options], env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        wait_for(lambda: journal.exists() and mw.read_journal(journal)['state'] == 'active')
        assert_fenced(mw.read_journal(journal))
        (root / 'slow-verify').touch()
        wait_for((root / 'keeper-verifying').exists)
        started = time.monotonic()
        adopter = subprocess.Popen([*argv, 'adopt', str(root), str(hermes), str(journal)], env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        wait_for(lambda: (root / 'adopter-held').exists() or adopter.poll() is not None)
        assert (root / 'adopter-held').exists(), adopter.communicate(timeout=1)
        assert time.monotonic() - started < 3
        assert_fenced(mw.read_journal(journal))
        assert json.loads(state.read_text())['fixture']['paused']
        if restoration == 'exit':
            exiting = subprocess.Popen([*argv, 'exit', *options], env=env,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            time.sleep(0.15)
            assert exiting.poll() is None, 'exit must wait before effects, not refuse'
        elif restoration == 'signal':
            keeper.send_signal(signal.SIGTERM)  # Only our disposable PID.
            time.sleep(0.6)
        else:
            wait_for(lambda: time.time() >= mw.read_journal(journal)['expires_at'])
            time.sleep(0.6)
        assert mw.read_journal(journal)['state'] == 'active'
        assert json.loads(state.read_text())['fixture']['paused']
        assert not (root / 'restores.jsonl').exists()
        assert_fenced(mw.read_journal(journal))
        (root / 'release-adopter').touch()
        out, err = adopter.communicate(timeout=3)
        assert adopter.returncode == 0, out + err
        if exiting:
            out, err = exiting.communicate(timeout=4)
            assert exiting.returncode == 0, out + err
            assert json.loads(out)['state'] == 'closed'
        keeper.communicate(timeout=4)
        assert keeper.returncode == 0
        assert mw.read_journal(journal)['state'] == 'closed'
        assert json.loads(state.read_text()) == original
        assert sorted((root / 'restores.jsonl').read_text().splitlines()) == sorted(original)
        scans = [json.loads(line) for line in (root / 'scans.jsonl').read_text().splitlines()]
        assert any(row['pid'] == adopter.pid for row in scans), 'adopter must freshly scan the full cohort'
        assert sum(row['pid'] == keeper.pid for row in scans) >= 3
        assert all(set(row['coverage']) == {'hermes', 'crontab', 'systemd', 'n8n'} for row in scans)
        for row in mw.read_journal(journal)['locks']:
            with Path(row['path']).open('a+') as fd:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        (root / 'release-adopter').touch()
        for proc in (adopter, exiting):
            if proc is not None:
                proc.communicate(timeout=7)
        if keeper is not None and keeper.poll() is None:
            keeper.send_signal(signal.SIGTERM)
            keeper.communicate(timeout=7)
