"""One offline bootstrap acceptance group. No privileged or production I/O.

The Linux boundary is simulated: nft/systemd/proc fixtures are not live kernel
acceptance. The resident bridge admission, queue, execution and durable readback
are real, using only harmless scratch children. PR81 F1/F2 are not rerun.
"""
from contextlib import contextmanager
import fcntl
import importlib.util
import json
from pathlib import Path
import sys
import threading
import socket
import subprocess
import urllib.request

import pytest


@pytest.fixture(autouse=True)
def deny_effectful_boundaries(monkeypatch):
    def denied(*a, **kw):
        pytest.fail('offline group attempted host command, signal, or network effect')
    monkeypatch.setattr(socket.socket, 'connect', denied)
    monkeypatch.setattr(urllib.request, 'urlopen', denied)
    monkeypatch.setattr(subprocess, 'run', denied)
    # Harmless Popen marker children must finish naturally, even on failure.
    import os
    monkeypatch.setattr(os, 'kill', denied)
    monkeypatch.setattr(os, 'killpg', denied)

ROOT = Path(__file__).resolve().parents[1]


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def activation():
    return load(ROOT / 'scripts/glp44_safe_bridge_activation.py', 'activation_fixture')


def test_linux_gate_has_exact_loopback_scope_and_get_only_bypass(activation, monkeypatch):
    assert hasattr(activation, 'LinuxHost'), 'missing concrete Linux bootstrap backend'
    host = activation.LinuxHost()
    calls = []
    def command(argv, payload=None):
        calls.append((argv, payload))
        if argv[-4:] == ['list', 'table', 'inet', activation.TABLE]:
            return json.dumps({'nftables': [
                {'metainfo': {'version': 'fixture'}},
                *[next(iter(row.values())) for row in activation.gate_commands()]]})
        return ''
    monkeypatch.setattr(host, 'command', command)
    host.close_ingress()
    host.verify_ingress()
    rules = [row['create']['rule'] for row in activation.gate_commands() if 'rule' in row['create']]
    assert len(rules) == 1
    expr = rules[0]['expr']
    assert {'match': {'op': '==', 'left': {'payload': {'protocol': 'ip', 'field': 'daddr'}}, 'right': '127.0.0.1'}} in expr
    assert {'match': {'op': '==', 'left': {'payload': {'protocol': 'tcp', 'field': 'dport'}}, 'right': 8791}} in expr
    assert {'match': {'op': '==', 'left': {'&': [{'payload': {'protocol': 'tcp', 'field': 'flags'}}, 'syn']}, 'right': 'syn'}} in expr
    assert {'match': {'op': '!=', 'left': {'meta': {'key': 'mark'}}, 'right': activation.MARK}} in expr
    assert expr[-1] == {'reject': {'type': 'tcp reset'}}
    assert all('flush' not in json.dumps(payload) for _, payload in calls)
    with pytest.raises(activation.Refused, match='readback_route'):
        host.get('/run')
    with pytest.raises(activation.Refused, match='readback_route'):
        host.get('/job/bad?inject=1')
    monkeypatch.setattr(host, 'command', lambda *a, **kw: '{"nftables": []}')
    with pytest.raises(activation.Refused, match='ingress_rule_drift'):
        host.verify_ingress()


def test_real_resident_queue_survives_gate_and_drains_before_restart(activation, tmp_path, monkeypatch):
    """Same unchanged bridge job machinery; external ingress is a fixture.

    No HTTP/provider/auth I/O: execute actual Handler POST body in-memory, then
    actual JobStore workers and harmless Popen children. Use unrelated action
    name so neither old nor repaired source-fence behavior is under test.
    """
    bridge = load(ROOT / 'ops/n8n-bridge/bridge.py', 'resident_bridge_fixture')
    bridge.RECEIPT_DIR = str(tmp_path / 'receipts')
    bridge.JOB_STATE_DIR = str(tmp_path / 'receipts/.job-state')
    bridge.MAX_CONCURRENT_JOBS = 1
    bridge.SYNC_WAIT_S = 0
    bridge.child_env = lambda job_id='': {'PATH': '/usr/bin:/bin'}
    bridge.post_callback = lambda *a, **kw: None
    bridge.log = lambda *a: None
    release = tmp_path / 'release'
    marker = tmp_path / 'launches'
    code = ('from pathlib import Path; import time; '
            f'p=Path({str(marker)!r}); f=p.open("a"); f.write("launch\\n"); f.close(); '
            f'exec("while not Path({str(release)!r}).exists(): time.sleep(.01)")')
    bridge.ACTIONS = {'fixture': {'argv': [sys.executable, '-c', code],
                                 'cwd': str(tmp_path), 'timeout_s': 20}}
    responses = []
    workers = []
    real_start = threading.Thread.start
    def start(thread):
        if thread.name.startswith(bridge.JOB_THREAD_PREFIX): workers.append(thread)
        return real_start(thread)
    monkeypatch.setattr(threading.Thread, 'start', start)

    class FixtureHost(activation.LinuxHost):
        closed = False
        restarted = False
        opened = False
        pid = 100
        def source(self): pass
        def identity(self): return (self.pid, self.pid * 10)
        def submit(self, origin, callback=False):
            assert origin in {'remote-tunnel', 'host-local'}
            if self.closed:
                return 503, {'error': 'fixture_network_gate'}
            handler = object.__new__(bridge.Handler)
            handler.path = '/run'
            handler._auth_scope = lambda **kw: bridge.GLOBAL_AUTH_SCOPE
            payload = {'action': 'script', 'name': 'fixture'}
            if callback: payload['callback_url'] = 'http://fixture.invalid/callback'
            handler._read_json = lambda: payload
            handler._send = lambda status, body, **kw: responses.append((status, body))
            handler.do_POST()
            return responses[-1]
        def close_ingress(self):
            self.closed = True
            for origin in ('remote-tunnel', 'host-local'):
                assert self.submit(origin)[0] == 503
        def verify_ingress(self): assert self.closed
        def health(self):
            return {'status': 'ok', 'jobs_running': bridge.STORE.running_count(),
                    'jobs_queued': bridge.STORE.queued_count()}
        def get(self, route):
            assert route.startswith('/job/')
            return bridge.load_durable_job(route.removeprefix('/job/'))
        def quiet(self, identity): return not any(w.is_alive() for w in workers)
        def journal(self, **fields): pass
        def restart(self):
            assert self.closed and self.quiet(self.identity())
            assert len(marker.read_text().splitlines()) == 2
            self.restarted = True
            self.pid = 200
            bridge.STORE = bridge.JobStore()  # new resident must use durable /job readback
        def open_ingress(self):
            assert self.restarted
            self.opened = True
            self.closed = False

    host = FixtureHost()
    monkeypatch.setattr(activation, 'STATES', Path(bridge.JOB_STATE_DIR))
    monkeypatch.setattr(activation, 'SOURCE_LOCK', tmp_path / 'source.lock')
    (tmp_path / 'source.lock').touch()
    assert hasattr(host, 'jobs'), 'missing actual durable drain reader'
    try:
        first = host.submit('host-local')[1]['job_id']
        second = host.submit('remote-tunnel', callback=True)[1]['job_id']
        assert first != second and bridge.STORE.queued_count() == 1
        # A single bounded test seam releases the real child after proving
        # closure did not drop, terminalize, or remove the accepted queue.
        def release_on_wait(seconds):
            assert host.closed and not host.restarted
            assert set(host.jobs()) == {first, second}
            assert any(v['status'] not in activation.TERMINAL for v in host.jobs().values())
            release.touch()
            for worker in workers: worker.join(5)
        from types import SimpleNamespace
        monkeypatch.setattr(activation, 'time', SimpleNamespace(
            monotonic=activation.time.monotonic, sleep=release_on_wait))
        activation.activate(host, (100, 1000), timeout=10)
        assert host.opened
        for job_id in (first, second):
            record = bridge.load_durable_job(job_id)
            assert record['status'] == 'done' and record['rc'] == 0
            assert not record.get('interrupted')
        # Post-cutover requests are admitted again, exactly once, no replay of
        # the old IDs. Existing terminal receipts stay byte-identical.
        snapshot = host.jobs()
        third = host.submit('host-local')[1]['job_id']
        for worker in workers: worker.join(5)
        assert third not in snapshot
        assert all(host.jobs()[k] == v for k, v in snapshot.items())
        assert len(marker.read_text().splitlines()) == 3
    finally:
        release.touch()
        for worker in workers: worker.join(5)
        assert not any(w.is_alive() for w in workers)


def test_linux_drain_rejects_inherited_sockets_threads_and_children(activation, tmp_path, monkeypatch):
    assert hasattr(activation.LinuxHost, 'quiet'), 'missing kernel drain witness'
    proc, cgroup = tmp_path / 'proc', tmp_path / 'cgroup'
    (proc / 'net').mkdir(parents=True)
    (proc / '100/task/100').mkdir(parents=True)
    (proc / '100/fd').mkdir()
    group = cgroup / 'user.slice/n8n-bridge.service'
    group.mkdir(parents=True)
    (group / 'cgroup.procs').write_text('100\n')
    (proc / '100/stat').write_text('100 (python3) ' + ' '.join(['S'] + ['0'] * 18 + ['1000']))
    (proc / '100/cmdline').write_bytes(b'/usr/bin/python3\0' + str(activation.BRIDGE).encode() + b'\0')
    (proc / '100/cgroup').write_text('0::/user.slice/n8n-bridge.service\n')
    (proc / '100/fd/3').symlink_to('socket:[123]')
    (proc / 'net/tcp').write_text('header\n 0: 0100007F:2257 00000000:0000 0A 0:0 0:0 0 1000 0 123\n')
    monkeypatch.setattr(activation, 'PROC', proc)
    monkeypatch.setattr(activation, 'CGROUP_ROOT', cgroup)
    host = activation.LinuxHost()
    properties = {'MainPID': '100', 'ControlGroup': '/user.slice/n8n-bridge.service',
                  'ActiveState': 'active', 'SubState': 'running',
                  'ExecStart': '{ path=/usr/bin/python3 ; argv[]=/usr/bin/python3 ' + str(activation.BRIDGE) + ' ; ignore_errors=no ; }'}
    monkeypatch.setattr(host, 'unit', lambda: properties)
    assert host.identity() == (100, 1000)
    assert host.quiet((100, 1000))
    (proc / '100/fd/4').symlink_to('socket:[124]')
    assert not host.quiet((100, 1000)), 'accepted/reset socket can retain buffered POST after leaving tcp table'
    (proc / '100/fd/4').unlink()
    (proc / 'net/tcp').write_text((proc / 'net/tcp').read_text() + ' 1: 0100007F:2257 0100007F:9999 01 0:0 0:0 0 1000 0 124\n')
    assert not host.quiet((100, 1000)), 'pre-gate socket may still POST'
    (proc / 'net/tcp').write_text('header\n 0: 0100007F:2257 00000000:0000 0A 0:0 0:0 0 1000 0 123\n')
    (proc / '100/task/101').mkdir()
    assert not host.quiet((100, 1000)), 'request or callback thread is still alive'
    (proc / '100/task/101').rmdir()
    (group / 'cgroup.procs').write_text('100\n200\n')
    assert not host.quiet((100, 1000)), 'detached child must drain, never be killed'
    (group / 'cgroup.procs').write_text('100\n')
    (proc / '100/stat').write_text('100 (python3) ' + ' '.join(['S'] + ['0'] * 18 + ['2000']))
    with pytest.raises(activation.Refused, match='resident_changed'):
        host.quiet((100, 1000))
    properties['ExecStart'] = '{ argv[]=/bin/sh -c unsafe ; }'
    with pytest.raises(activation.Refused, match='unit_exec'):
        host.identity()


@pytest.fixture
def journal_stdio_host(activation, tmp_path, monkeypatch):
    """Retained B1 shape; proc/ss are offline fixtures, not kernel acceptance."""
    proc = tmp_path / 'proc'
    (proc / 'net').mkdir(parents=True)
    (proc / '100/task/100').mkdir(parents=True)
    descriptors = proc / '100/fd'
    descriptors.mkdir()
    for fd, inode in [('1', '1021573081'), ('2', '1021573081'), ('3', '1021576100')]:
        (descriptors / fd).symlink_to('socket:[' + inode + ']')
    (proc / '100/stat').write_text('100 (python3) ' + ' '.join(['S'] + ['0'] * 18 + ['1000']))
    (proc / '100/cmdline').write_bytes(b'/usr/bin/python3\0' + str(activation.BRIDGE).encode() + b'\0')
    (proc / '100/cgroup').write_text('0::/user.slice/n8n-bridge.service\n')
    (proc / 'net/tcp').write_text(
        'header\n 0: 0100007F:2257 00000000:0000 0A 0:0 0:0 0 1000 0 1021576100\n')
    cgroup = tmp_path / 'cgroup'
    group = cgroup / 'user.slice/n8n-bridge.service'
    group.mkdir(parents=True)
    (group / 'cgroup.procs').write_text('100\n')
    states = tmp_path / '.job-state'
    states.mkdir()
    monkeypatch.setattr(activation, 'PROC', proc)
    monkeypatch.setattr(activation, 'CGROUP_ROOT', cgroup)
    monkeypatch.setattr(activation, 'STATES', states)
    host = activation.LinuxHost()
    monkeypatch.setattr(host, 'unit', lambda: {
        'MainPID': '100', 'ControlGroup': '/user.slice/n8n-bridge.service',
        'ActiveState': 'active', 'SubState': 'running',
        'ExecStart': '{ argv[]=/usr/bin/python3 ' + str(activation.BRIDGE) + ' ; }'})
    snapshot = {'ss': (
        'u_str ESTAB 0 0 * 1021573081 * 1021576528\n'
        'u_str ESTAB 0 0 /run/systemd/journal/stdout 1021576528 * 1021573081\n')}
    calls = []
    def command(argv, payload=None):
        assert argv == ['/usr/bin/ss', '--unix', '--all', '--numeric', '--no-header']
        assert payload is None
        calls.append(argv)
        return snapshot['ss']
    monkeypatch.setattr(host, 'command', command)
    return host, proc, group, states, snapshot, calls


def test_idle_bridge_with_inherited_journal_stdio_is_quiet(journal_stdio_host):
    host, proc, group, states, snapshot, calls = journal_stdio_host
    assert host.identity() == (100, 1000)
    assert host.jobs() == {}
    assert host.quiet((100, 1000)), 'B1: inherited journal stdio is not live bridge ingress'
    assert calls, 'journal peer must be positively identified, never exempted by fd number'


@pytest.mark.parametrize('busy', [
    'accepted_tcp', 'reset_tcp_descriptor', 'additional_listener', 'other_unix',
    'nonjournal_fd1', 'nonjournal_shared_stdio', 'journal_alias_fd4', 'extra_thread',
    'extra_cgroup_member', 'nested_cgroup_member',
])
def test_journal_stdio_does_not_hide_busy_bridge(journal_stdio_host, busy):
    host, proc, group, states, snapshot, calls = journal_stdio_host
    assert host.quiet((100, 1000)), 'negative controls must start from the quiet journal shape'
    fd = proc / '100/fd'
    if busy == 'accepted_tcp':
        (proc / 'net/tcp').write_text((proc / 'net/tcp').read_text()
            + ' 1: 0100007F:2257 0100007F:9999 01 0:0 0:0 0 1000 0 124\n')
        (fd / '4').symlink_to('socket:[124]')
    elif busy == 'reset_tcp_descriptor':
        (fd / '4').symlink_to('socket:[124]')  # no longer in tcp table
    elif busy == 'additional_listener':
        (fd / '4').symlink_to('socket:[125]')
        (proc / 'net/tcp').write_text((proc / 'net/tcp').read_text()
            + ' 1: 0100007F:9999 00000000:0000 0A 0:0 0:0 0 1000 0 125\n')
    elif busy == 'other_unix':
        (fd / '4').symlink_to('socket:[126]')
        snapshot['ss'] += 'u_str ESTAB 0 0 * 126 /run/other.sock 127\n'
    elif busy == 'nonjournal_fd1':
        (fd / '1').unlink()
        (fd / '1').symlink_to('socket:[126]')
        snapshot['ss'] += 'u_str ESTAB 0 0 * 126 /run/other.sock 127\n'
    elif busy == 'nonjournal_shared_stdio':
        snapshot['ss'] = snapshot['ss'].replace('/run/systemd/journal/stdout', '/run/other.sock')
    elif busy == 'journal_alias_fd4':
        (fd / '4').symlink_to('socket:[1021573081]')
    elif busy == 'extra_thread':
        (proc / '100/task/101').mkdir()
    elif busy == 'extra_cgroup_member':
        (group / 'cgroup.procs').write_text('100\n200\n')
    elif busy == 'nested_cgroup_member':
        (group / 'child').mkdir()
        (group / 'child/cgroup.procs').write_text('200\n')
    assert not host.quiet((100, 1000))


@pytest.mark.parametrize('evidence', [
    '', 'garbled',
    'u_str ESTAB 0 0 * 1021573081 * 1021576528\n',  # missing peer
    'u_str ESTAB 0 0 /run/systemd/journal/stdout 1021576528 * 1021573081\n',  # missing local
    'u_str ESTAB 0 0 * 1021573081 * 999\n'
    'u_str ESTAB 0 0 /run/systemd/journal/stdout 1021576528 * 1021573081\n',
    'u_str ESTAB 0 0 * 1021573081 * 1021576528\n'
    'u_str ESTAB 0 0 /run/systemd/journal/stdout 1021576528 * 999\n',
])
def test_unknown_or_nonreciprocal_journal_evidence_is_busy(journal_stdio_host, evidence):
    host, proc, group, states, snapshot, calls = journal_stdio_host
    assert host.quiet((100, 1000))
    snapshot['ss'] = evidence
    assert not host.quiet((100, 1000))


@pytest.mark.parametrize('change', ['local_datagram', 'peer_datagram', 'local_listen',
                                    'peer_listen', 'duplicate_local', 'duplicate_peer',
                                    'path_suffix', 'named_local'])
def test_only_unique_connected_journal_stream_is_exempt(journal_stdio_host, change):
    host, proc, group, states, snapshot, calls = journal_stdio_host
    assert host.quiet((100, 1000))
    local, peer = snapshot['ss'].splitlines(keepends=True)
    if change == 'local_datagram': local = local.replace('u_str', 'u_dgr')
    elif change == 'peer_datagram': peer = peer.replace('u_str', 'u_dgr')
    elif change == 'local_listen': local = local.replace('ESTAB', 'LISTEN')
    elif change == 'peer_listen': peer = peer.replace('ESTAB', 'LISTEN')
    elif change == 'duplicate_local': local += local
    elif change == 'duplicate_peer': peer += peer
    elif change == 'path_suffix': peer = peer.replace('/stdout', '/stdout.other')
    elif change == 'named_local': local = local.replace('0 0 *', '0 0 /run/other.sock')
    snapshot['ss'] = local + peer
    assert not host.quiet((100, 1000))


def test_unreadable_journal_peer_evidence_refuses(activation, journal_stdio_host, monkeypatch):
    host, proc, group, states, snapshot, calls = journal_stdio_host
    assert host.quiet((100, 1000))
    def failed_command(*args, **kwargs):
        raise activation.Refused('host_command_failed_gate_not_released')
    monkeypatch.setattr(host, 'command', failed_command)
    with pytest.raises(activation.Refused, match='host_command_failed'):
        host.quiet((100, 1000))


@pytest.mark.parametrize('status', ['queued', 'accepted', 'running'])
def test_journal_quiet_does_not_admit_nonterminal_jobs(
        activation, journal_stdio_host, monkeypatch, tmp_path, status):
    host, proc, group, states, snapshot, calls = journal_stdio_host
    assert host.quiet((100, 1000))
    job_id = 'c' * 16
    path = states / (job_id + '.json')
    path.write_text(json.dumps({'job_id': job_id, 'status': status,
                               '_bridge_pid': 100, '_bridge_start_ticks': 1000}))
    before = path.read_bytes()
    monkeypatch.setattr(host, 'verify_ingress', lambda: None)
    monkeypatch.setattr(host, 'health', lambda: {'status': 'ok', 'jobs_running': 0, 'jobs_queued': 0})
    assert not activation.drained(host, (100, 1000), {})
    # Also exercise the actual credential-free maintenance caller, whose job
    # guard is separate from quiet(); only unrelated host task census is faked.
    monkeypatch.syspath_prepend(str(ROOT / 'scripts'))
    window = load(ROOT / 'scripts/maintenance_window.py', 'journal_window_fixture')
    import maintenance_admission
    import maintenance_inventory
    monkeypatch.setattr(maintenance_admission, 'process_readers', lambda *a: [])
    monkeypatch.setattr(maintenance_inventory, 'affected_paths', lambda *a: set())
    journal = tmp_path / 'window.json'
    entry = {'kind': 'n8n', 'id': 'n8n-bridge.service'}
    journal.write_text(json.dumps({'schema': window.SCHEMA, 'entries': [
        {'entry': entry, 'prior': {'identity': [100, 1000]}}]}))
    backend = window.LinuxBackend(tmp_path, tmp_path / 'hermes', journal)
    monkeypatch.setattr(backend, 'bridge', lambda: host)
    assert backend.inflight([entry]) == [
        {'kind': 'n8n', 'reason': 'socket_thread_cgroup_or_job_not_drained'}]
    assert path.read_bytes() == before, 'drain checks must not terminalize or replay jobs'
    path.unlink()
    assert activation.drained(host, (100, 1000), {})
    assert backend.inflight([entry]) == []


def test_source_hash_and_source_lock_are_real(activation, tmp_path, monkeypatch):
    assert hasattr(activation.LinuxHost, 'source'), 'missing approved-byte binding'
    monkeypatch.setattr(activation, 'BRIDGE', ROOT / 'ops/n8n-bridge/bridge.py')
    monkeypatch.setattr(activation, 'SOURCE_LOCK', tmp_path / 'source.lock')
    (tmp_path / 'source.lock').touch()
    host = activation.LinuxHost()
    host.source()
    with host.exclusive_source():
        with pytest.raises(BlockingIOError):
            with host.exclusive_source(): pass
    changed = tmp_path / 'bridge.py'
    changed.write_bytes(activation.BRIDGE.read_bytes() + b'\n')
    monkeypatch.setattr(activation, 'BRIDGE', changed)
    with pytest.raises(activation.Refused, match='approved_bridge_bytes'):
        host.source()


@pytest.mark.parametrize('failure', ['gate', 'queued', 'counter_unknown', 'lost_id',
                                    'old_changed', 'source_drift', 'lock_busy',
                                    'restart_uncertain', 'same_instance', 'terminal_drift',
                                    'new_busy'])
def test_safe_refusal_never_opens_gate_or_repeats_restart(activation, failure):
    class Host:
        closed = False
        restarts = 0
        jobs_reads = 0
        source_reads = 0
        def source(self):
            self.source_reads += 1
            if failure == 'source_drift' and self.source_reads == 2: raise activation.Refused('bytes')
        def identity(self):
            if self.restarts: return (100, 1000) if failure == 'same_instance' else (200, 2000)
            return (101, 1010) if self.closed and failure == 'old_changed' else (100, 1000)
        def close_ingress(self): self.closed = True
        def verify_ingress(self):
            if failure == 'gate': raise activation.Refused('gate')
        def jobs(self):
            self.jobs_reads += 1
            if failure == 'lost_id' and self.jobs_reads > 1: return {}
            return {'a' * 16: {'status': 'queued' if failure == 'queued' else 'done',
                               'digest': 'changed' if failure == 'terminal_drift' and self.restarts else 'same'}}
        def health(self):
            return {'status': 'ok', 'jobs_running': None if failure == 'counter_unknown' else
                    (1 if failure == 'new_busy' and self.restarts else 0), 'jobs_queued': 0}
        def quiet(self, identity): return True
        def journal(self, **kw): pass
        @contextmanager
        def exclusive_source(self):
            if failure == 'lock_busy': raise BlockingIOError()
            yield
        def restart(self):
            self.restarts += 1
            if failure == 'restart_uncertain': raise activation.Refused('restart_uncertain')
        def open_ingress(self): pytest.fail('failure must retain ingress gate')
    host = Host()
    with pytest.raises((activation.Refused, BlockingIOError)):
        activation.activate(host, (100, 1000), timeout=0)
    assert host.closed and host.restarts <= 1


def test_cli_default_is_inert_and_apply_is_explicit(activation, monkeypatch, capsys):
    assert hasattr(activation, 'main'), 'missing supported CLI entry'
    monkeypatch.setattr(activation.LinuxHost, 'command', lambda *a, **kw: pytest.fail('host effect'))
    monkeypatch.setattr(activation, 'read_regular', lambda *a: pytest.fail('host file read'))
    assert activation.main([]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan['status'] == 'SOURCE_ONLY_PLAN' and plan['bridge_sha256'] == activation.APPROVED_BRIDGE_SHA256
    assert activation.main(['activate', '--pid', '100', '--start-ticks', '1000']) == 2
    assert 'apply_required' in capsys.readouterr().out


def test_linux_restart_is_single_and_journal_durable(activation, tmp_path, monkeypatch):
    assert hasattr(activation.LinuxHost, 'restart'), 'missing concrete existing-unit restart'
    monkeypatch.setattr(activation, 'CONTROL', tmp_path)
    (tmp_path / 'activation.jsonl').touch(mode=0o600)
    host = activation.LinuxHost()
    calls = []
    monkeypatch.setattr(host, 'command', lambda argv, payload=None: calls.append(argv) or '')
    monkeypatch.setattr(host, 'identity', lambda: (200, 2000))
    monkeypatch.setattr(host, 'health', lambda: {'status': 'ok'})
    host.journal(phase='RESTARTING', old_identity=(100, 1000))
    host.restart()
    assert calls == [activation.SYSTEMCTL + ['restart', activation.UNIT]]
    assert json.loads((tmp_path / 'activation.jsonl').read_text())['phase'] == 'RESTARTING'
    monkeypatch.setattr(host, 'command', lambda *a: (_ for _ in ()).throw(activation.Refused('uncertain')))
    with pytest.raises(activation.Refused, match='uncertain'): host.restart()


def test_source_lock_bootstrap_creates_same_inode_for_runtime(activation, tmp_path, monkeypatch):
    lock = tmp_path / 'source.lock'
    monkeypatch.setattr(activation, 'SOURCE_LOCK', lock)
    ownership = []
    monkeypatch.setattr(activation.os, 'fchown', lambda fd, uid, gid: ownership.append((uid, gid)))
    with activation.LinuxHost().exclusive_source():
        inode = lock.stat().st_ino
    assert ownership == [(1000, 1000)]
    with activation.LinuxHost().exclusive_source():
        assert lock.stat().st_ino == inode
    assert len(ownership) == 1


def test_drain_reader_is_scoped_to_exact_resident_not_historical_jobs(activation, tmp_path, monkeypatch):
    states = tmp_path / '.job-state'
    states.mkdir()
    job_id = 'b' * 16
    (states / (job_id + '.json')).write_text(json.dumps({
        'job_id': job_id, 'status': 'queued', '_bridge_pid': 99, '_bridge_start_ticks': 990}))
    monkeypatch.setattr(activation, 'STATES', states)
    host = activation.LinuxHost()
    host.resident_identity = (100, 1000)
    assert host.jobs() == {}, 'historical nonterminal IDs are not this resident drain'
    host.resident_identity = (99, 990)
    assert host.jobs()[job_id]['status'] == 'queued'
    before = (states / (job_id + '.json')).read_bytes()
    assert host.jobs()[job_id]['status'] == 'queued'
    assert (states / (job_id + '.json')).read_bytes() == before


def test_terminal_readback_must_match_retained_job(activation, monkeypatch):
    assert hasattr(activation.LinuxHost, 'readback')
    host = activation.LinuxHost()
    job_id = 'f' * 16
    monkeypatch.setattr(host, 'get', lambda route: {'job_id': job_id, 'status': 'done', 'rc': 0})
    host.readback({job_id: {'status': 'done', 'rc': 0}})
    monkeypatch.setattr(host, 'get', lambda route: {'job_id': job_id, 'status': 'error', 'rc': 125})
    with pytest.raises(activation.Refused, match='terminal_http_readback_mismatch'):
        host.readback({job_id: {'status': 'done', 'rc': 0}})


def test_marked_reader_only_sends_get_and_closes(activation, tmp_path, monkeypatch):
    token = tmp_path / 'nonsecret-fixture-token'
    token.write_text('offline-fixture-not-a-credential')
    token.chmod(0o600)
    monkeypatch.setattr(activation, 'TOKEN', token)
    events = []
    class Socket:
        def settimeout(self, value): pass
        def setsockopt(self, level, key, value): events.append(('mark', value))
        def connect(self, address): events.append(('connect', address))
        def close(self): events.append(('close',))
    class Connection:
        def __init__(self, *a, **kw): pass
        def request(self, method, route, headers):
            assert method == 'GET' and route == '/health'
            assert headers['Connection'] == 'close'
            events.append(('request', method, route))
        def getresponse(self):
            from types import SimpleNamespace
            return SimpleNamespace(status=200, read=lambda n: b'{"status":"ok"}')
        def close(self): events.append(('http_close',))
    monkeypatch.setattr(activation.socket, 'socket', lambda *a: Socket())
    monkeypatch.setattr(activation.http.client, 'HTTPConnection', Connection)
    assert activation.LinuxHost().health() == {'status': 'ok'}
    assert events[0] == ('mark', activation.MARK)
    assert events[1] == ('connect', ('127.0.0.1', 8791))
    assert events[-1] == ('close',)


def test_cutover_orders_exclusion_drain_source_ex_restart_readback():
    path = ROOT / 'scripts/glp44_safe_bridge_activation.py'
    assert path.is_file(), 'no old-instance local+remote bootstrap exclusion path'
    mod = load(path, 'activation_fixture')
    from contextlib import contextmanager

    class Host:
        def __init__(self):
            self.events = []
            self.pid = 100
        def source(self): self.events.append('bytes')
        def identity(self): return (self.pid, self.pid * 10)
        def close_ingress(self): self.events.append('close')
        def verify_ingress(self): self.events.append('gate_readback')
        def jobs(self): return {}
        def health(self): return {'status': 'ok', 'jobs_running': 0, 'jobs_queued': 0}
        def quiet(self, identity): return True
        @contextmanager
        def exclusive_source(self):
            self.events.append('source_ex')
            yield
            self.events.append('source_release')
        def restart(self):
            self.events.append('restart')
            self.pid = 200
        def readback(self, retained): self.events.append('terminal_http_readback')
        def open_ingress(self): self.events.append('open')
        def journal(self, **fields): self.events.append(fields['phase'])

    host = Host()
    result = mod.activate(host, (100, 1000), timeout=0)
    assert result['new_identity'] == (200, 2000)
    events = host.events
    assert events.index('close') < events.index('DRAINED') < events.index('source_ex')
    assert events.index('source_ex') < events.index('restart') < events.index('VERIFIED')
    assert events.index('restart') < events.index('terminal_http_readback') < events.index('VERIFIED')
    assert events.index('VERIFIED') < events.index('open') < events.index('source_release')
