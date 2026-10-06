"""Offline receiving-policy/launcher regressions. No MAC load or host mutation.

Kernel-reported status and root custody are synthetic inputs. These tests do
not substitute for receiver-side compiled-policy and live denial acceptance.
"""
import errno
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import stat
import struct
import subprocess
import sys
from types import SimpleNamespace

import pytest
from test_rook_mcp_events import module
from test_rook_communication import TestFinchCommunication as _Finch

ROOT = Path(__file__).resolve().parents[1]
LAUNCH = ROOT / 'ops/rook-mcp/finch_adapter_launch.py'
PROFILE = ROOT / 'ops/rook-mcp/finch-mcp-receiving-v1.apparmor'
UNIT = ROOT / 'ops/rook-mcp/finch-mcp-adapter.system.service'


def launcher():
    spec = importlib.util.spec_from_file_location('synthetic_finch_launcher', LAUNCH)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def kernel_status(**changes):
    values = {'Name': 'python', 'Uid': '1000 1000 1000 1000', 'Gid': '1000 1000 1000 1000',
              'NoNewPrivs': '1', 'Seccomp': '2', 'CapInh': '0000000000000000',
              'CapPrm': '0000000000000000', 'CapEff': '0000000000000000', 'CapAmb': '0000000000000000', 'CapBnd': '0000000000000000'}
    values.update(changes)
    return ''.join(key + ':\t' + value + '\n' for key, value in values.items() if value is not None)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(socket.socket, 'connect', lambda *a, **k: pytest.fail('network denied'))
    monkeypatch.setattr(socket.socket, 'bind', lambda *a, **k: pytest.fail('bind denied'))
    monkeypatch.setattr(module(), 'load_rook_token', lambda *a: pytest.fail('token read denied'))


def kernel_fixture(monkeypatch, *, status=None, label=None):
    m = module()
    monkeypatch.setattr(m.os, 'getresuid', lambda: (1000, 1000, 1000))
    monkeypatch.setattr(m.os, 'getresgid', lambda: (1000, 1000, 1000))
    monkeypatch.setattr(m, '_finch_kernel_read', lambda leaf:
                        (status or kernel_status()) if leaf == 'status' else (label or m.FINCH_CONFINEMENT_LABEL + '\n'))
    monkeypatch.setattr(m, '_finch_receiving_contract', lambda: ({}, 'a' * 64))
    return m


def test_kernel_enforced_identity_and_empty_capabilities_required(monkeypatch):
    assert kernel_fixture(monkeypatch)._finch_confinement() == 'a' * 64


@pytest.mark.parametrize('field,value', [
    ('Uid', '1000 0 1000 1000'), ('Uid', '1000 1000 0 1000'), ('Uid', '1000 1000 1000 0'),
    ('Uid', '1001 1001 1001 1001'), ('Gid', '1000 1000 1000 0'), ('NoNewPrivs', '0'),
    ('NoNewPrivs', None), ('Seccomp', '0'), ('Seccomp', '1'), ('Seccomp', None),
    ('CapEff', '0000000000000001'), ('CapPrm', '0000000000000001'),
    ('CapInh', '0000000000000001'), ('CapAmb', '0000000000000001'), ('CapEff', None), ('CapBnd', '0000000000000001'), ('CapBnd', None),
    ('CapEff', '0'), ('CapEff', '0000000000000000 extra')])
def test_privilege_or_unknown_kernel_state_fails_closed(monkeypatch, field, value):
    m = kernel_fixture(monkeypatch, status=kernel_status(**{field: value}))
    with pytest.raises(m.RpcError, match='FinchReceivingConfinementRequired'): m._finch_confinement()


@pytest.mark.parametrize('label', ['unconfined', 'finch-mcp-receiving-v1 (complain)',
    'other (enforce)', 'finch-mcp-receiving-v1//child (enforce)', 'finch-mcp-receiving-v1 (enforce)\nextra'])
def test_unconfined_complain_other_or_stacked_profile_refused(monkeypatch, label):
    m = kernel_fixture(monkeypatch, label=label)
    with pytest.raises(m.RpcError, match='FinchReceivingConfinementRequired'): m._finch_confinement()


def test_duplicate_kernel_field_and_identity_disagreement_refused(monkeypatch):
    m = kernel_fixture(monkeypatch, status=kernel_status() + 'NoNewPrivs: 1\n')
    with pytest.raises(m.RpcError): m._finch_confinement()
    m = kernel_fixture(monkeypatch)
    monkeypatch.setattr(m.os, 'getresuid', lambda: (1000, 1000, 0))
    with pytest.raises(m.RpcError): m._finch_confinement()


@pytest.mark.parametrize('failure', [OSError(errno.EACCES, 'denied'), OSError(errno.ENOENT, 'missing'), UnicodeError('invalid')])
def test_unreadable_kernel_or_contract_is_not_a_soft_failure(monkeypatch, failure):
    m = kernel_fixture(monkeypatch)
    def broken(*args): raise failure
    monkeypatch.setattr(m, '_finch_kernel_read', broken)
    with pytest.raises(m.RpcError): m._finch_confinement()


def contract_fixture(monkeypatch):
    m = module()
    value = {'schema': 'finch-receiving-contract-v1', 'source_commit': 'a' * 40,
             'source_sha256': m.FINCH_SOURCE_SHA256, 'launcher_sha256': hashlib.sha256(LAUNCH.read_bytes()).hexdigest(),
             'profile_sha256': hashlib.sha256(PROFILE.read_bytes()).hexdigest(),
             'profile_abi_sha256': hashlib.sha256(b'synthetic-feature-abi').hexdigest(),
             'receiver_sha256': hashlib.sha256((ROOT / 'scripts/receive_one_discord_message.py').read_bytes()).hexdigest(),
             'poster_sha256': hashlib.sha256((ROOT / 'scripts/post_discord_message.py').read_bytes()).hexdigest()}
    monkeypatch.setattr(m, 'FINCH_LAUNCHER', LAUNCH)
    monkeypatch.setattr(m, '_finch_root_bytes', lambda path, limit:
                        PROFILE.read_bytes() if path == m.FINCH_APPARMOR_POLICY else
                        b'synthetic-feature-abi' if path == m.FINCH_APPARMOR_ABI else json.dumps(value).encode())
    return m, value


def test_receiving_contract_binds_source_closure_launcher_and_policy(monkeypatch):
    m, value = contract_fixture(monkeypatch)
    actual, digest = m._finch_receiving_contract()
    assert actual == value and digest == hashlib.sha256(json.dumps(value).encode()).hexdigest()


@pytest.mark.parametrize('field', ['source_sha256', 'launcher_sha256', 'profile_sha256', 'profile_abi_sha256', 'receiver_sha256', 'poster_sha256'])
def test_receiving_byte_drift_denied(monkeypatch, field):
    m, value = contract_fixture(monkeypatch); value[field] = '0' * 64
    with pytest.raises(ValueError): m._finch_receiving_contract()


@pytest.mark.parametrize('change', ['extra', 'missing', 'bad-head', 'nonhash', 'wrong-schema', 'duplicate'])
def test_contract_is_closed_and_never_a_client_grant(monkeypatch, change):
    m, value = contract_fixture(monkeypatch)
    if change == 'extra': value['approved'] = True
    if change == 'missing': value.pop('launcher_sha256')
    if change == 'bad-head': value['source_commit'] = 'main'
    if change == 'nonhash': value['source_sha256'] = True
    if change == 'wrong-schema': value['schema'] = 'finch-whole-file-review-v1'
    if change == 'duplicate':
        monkeypatch.setattr(m, '_finch_root_bytes', lambda *a: b'{"schema":"x",' + json.dumps(value).encode()[1:])
    with pytest.raises((ValueError, TypeError)): m._finch_receiving_contract()


def test_application_owned_configuration_rejected_without_reading_it(tmp_path):
    path = tmp_path / 'receiving.json'; path.write_text('{}')
    with pytest.raises(ValueError): module()._finch_root_bytes(path, 4096)


def startup_fixture(monkeypatch):
    m = launcher()
    monkeypatch.setattr(m, 'sys', SimpleNamespace(flags=SimpleNamespace(isolated=1, no_site=1, dont_write_bytecode=1), executable=m.PYTHON))
    monkeypatch.setattr(m, '__file__', str(m.LAUNCHER))
    monkeypatch.setattr(m.os, 'environ', {})
    monkeypatch.setattr(m.Path, 'read_text', lambda path, *a:
                        kernel_status() if path.name == 'status' else 'finch-mcp-receiving-v1 (enforce)\n')
    null = SimpleNamespace(st_dev=1, st_ino=2, st_mode=stat.S_IFCHR)
    journal = SimpleNamespace(st_dev=1, st_ino=3, st_mode=stat.S_IFSOCK)
    closed = set()
    monkeypatch.setattr(m.os, 'stat', lambda *a: null)
    def fstat(fd):
        if fd in closed or fd == 99: raise OSError(errno.EBADF, 'closed')
        return null if fd == 0 else journal
    monkeypatch.setattr(m.os, 'fstat', fstat)
    monkeypatch.setattr(m.os, 'listdir', lambda path: ['0', '1', '2', '8', '9', '99'])
    monkeypatch.setattr(m.os, 'close', lambda fd: closed.add(fd))
    class Stream:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def getsockopt(self, level, option, length=None):
            return struct.pack('3i', 123, 0, 0) if option == socket.SO_PEERCRED else socket.SOCK_STREAM
        def getpeername(self): return '/run/systemd/journal/stdout'
    stream = Stream()
    monkeypatch.setattr(m.socket, 'fromfd', lambda *a: stream)
    return m, closed, stream


def test_startup_preserves_only_verified_journal_and_null_stdio(monkeypatch):
    m, closed, _ = startup_fixture(monkeypatch)
    m.isolate_startup()
    assert closed == {8, 9, 99} and set(m.os.environ) == {'PATH', 'HOME', 'TMPDIR', 'LANG'}


@pytest.mark.parametrize('name', ['LD_PRELOAD', 'LD_AUDIT', 'LD_UNRECOGNIZED', 'PYTHONPATH', 'PYTHONHOME',
    'PYTHONSTARTUP', 'PYTHONINSPECT', 'PYTHONUNRECOGNIZED', 'GLIBC_TUNABLES'])
def test_loader_or_python_environment_cannot_be_silently_accepted(monkeypatch, name):
    m, closed, _ = startup_fixture(monkeypatch); m.os.environ[name] = 'untrusted'
    with pytest.raises(ValueError, match='isolated_startup_required'): m.isolate_startup()
    assert not closed


@pytest.mark.parametrize('flag', ['isolated', 'no_site', 'dont_write_bytecode'])
def test_all_isolated_interpreter_flags_required(monkeypatch, flag):
    m, _, _ = startup_fixture(monkeypatch); setattr(m.sys.flags, flag, 0)
    with pytest.raises(ValueError): m.isolate_startup()


@pytest.mark.parametrize('kind', ['other-peer', 'nonroot-peer', 'datagram', 'pipe-output', 'close-error', 'fd-remains'])
def test_unknown_or_uncertain_inherited_descriptor_fails_closed(monkeypatch, kind):
    m, closed, stream = startup_fixture(monkeypatch)
    if kind == 'other-peer': stream.getpeername = lambda: '/run/user/1000/systemd/private'
    if kind == 'nonroot-peer': stream.getsockopt = lambda *a: struct.pack('3i', 123, 1000, 1000)
    if kind == 'datagram': stream.getsockopt = lambda *a: struct.pack('3i', 123, 0, 0) if len(a) == 3 else socket.SOCK_DGRAM
    if kind == 'pipe-output':
        real = m.os.fstat
        monkeypatch.setattr(m.os, 'fstat', lambda fd: SimpleNamespace(st_dev=1, st_ino=3, st_mode=stat.S_IFIFO) if fd in (1, 2) else real(fd))
    if kind == 'close-error':
        def close(fd): raise OSError(errno.EINTR, 'uncertain')
        monkeypatch.setattr(m.os, 'close', close)
    if kind == 'fd-remains': monkeypatch.setattr(m.os, 'close', lambda fd: None)
    with pytest.raises(ValueError): m.isolate_startup()


def test_unit_enforces_before_python_and_retains_existing_lifecycle():
    value = UNIT.read_text()
    assert 'NoNewPrivileges=yes' in value and 'AppArmorProfile=finch-mcp-receiving-v1' in value
    assert 'AppArmorProfile=-' not in value and '-I -S -B' in value
    assert 'User=1000' in value and 'Group=1000' in value
    assert 'Restart=always' in value and 'RestartSec=30' in value and 'TimeoutStopSec=infinity' in value
    assert 'CapabilityBoundingSet=\n' in value and 'AmbientCapabilities=\n' in value
    for setting in ('MemoryHigh=256M', 'MemoryMax=384M', 'CPUQuota=50%', 'TasksMax=64',
                    'StartLimitIntervalSec=15min', 'StartLimitBurst=5', 'LogRateLimitIntervalSec=60', 'LogRateLimitBurst=30'):
        assert setting in value
    assert 'SystemCallArchitectures=native' in value and 'io_uring_setup' in value
    deny = '\n'.join(line.removeprefix('SystemCallFilter=~') for line in value.splitlines() if line.startswith('SystemCallFilter=~'))
    assert '@keyring' in deny
    for call in ('msgget', 'msgsnd', 'msgrcv', 'msgctl', 'shmget', 'shmat', 'shmdt', 'shmctl',
                 'semget', 'semctl', 'semop', 'semtimedop', 'mq_open', 'mq_unlink',
                 'mq_timedsend', 'mq_timedreceive', 'mq_notify', 'mq_getsetattr',
                 'process_vm_readv', 'process_vm_writev', 'process_madvise', 'process_mrelease',
                 'pidfd_open', 'pidfd_getfd', 'pidfd_send_signal'):
        assert call in deny.split()
    assert '@ipc' not in deny and 'pipe' not in deny.split() and 'pipe2' not in deny.split()
    assert 'StandardOutput=journal' in value and 'StandardError=inherit' in value
    for name in ('LD_PRELOAD', 'LD_AUDIT', 'PYTHONPATH', 'PYTHONHOME', 'DBUS_SESSION_BUS_ADDRESS', 'SSH_AUTH_SOCK'):
        assert name in value.split('UnsetEnvironment=', 1)[1]


def test_policy_has_no_privilege_transition_or_external_unix_allowance():
    lines = [line.strip() for line in PROFILE.read_text().splitlines() if line.strip() and not line.lstrip().startswith('#')]
    policy = '\n'.join(lines)
    assert '#include' not in policy and 'ux' not in policy and 'Ux' not in policy and 'change_profile' not in policy
    assert 'abi <abi/4.0>,' in lines and '/etc/apparmor.d/abi/4.0 r,' in lines
    assert 'deny capability,' in policy and 'deny dbus,' in policy and 'deny ptrace (read, trace),' in policy
    assert 'peer=(label=finch-mcp-receiving-v1,addr=none)' in policy
    assert '/run/systemd/journal/stdout rw,' in policy
    for route in ('/run/user/', '/run/dbus/', 'docker.sock', 'SSH_AUTH_SOCK', '/proc/[0-9]*/mem', '/etc/sudoers'):
        assert route not in policy
    writable = [line for line in lines if ' rw' in line or ' rwk' in line]
    assert len(writable) == 3
    assert any('rook.sqlite3' in line and 'owner ' in line for line in writable)


def test_policy_allows_exact_readonly_ancestry_used_by_descriptor_pinning():
    lines = {line.strip() for line in PROFILE.read_text().splitlines()}
    for leaf in ('/etc/finch-mcp/receiving.json', '/etc/apparmor.d/finch-mcp-receiving-v1', '/etc/apparmor.d/abi/4.0',
                 '/etc/finch-file-reviews/discord-301.json'):
        for parent in Path(leaf).parents:
            directory = str(parent).rstrip('/') + '/'
            assert directory + ' r,' in lines
            assert directory + ' rw,' not in lines


@pytest.fixture
def launch_fixture(monkeypatch, tmp_path):
    l = launcher(); m = module()
    checkout = tmp_path / 'checkout'; (checkout / 'scripts').mkdir(parents=True)
    sources = {name: b'# synthetic source\n' for name in ('rook_mcp_events', 'receive_one_discord_message', 'post_discord_message')}
    for name, data in sources.items(): (checkout / 'scripts' / (name + '.py')).write_bytes(data)
    python = tmp_path / 'venv/bin/python'
    packages = python.parent.parent / 'lib' / f'python{sys.version_info.major}.{sys.version_info.minor}' / 'site-packages'
    packages.mkdir(parents=True)
    monkeypatch.setattr(l, 'PYTHON', str(python)); monkeypatch.setattr(l, 'CHECKOUT', checkout)
    monkeypatch.setattr(l.sys, 'path', list(sys.path))
    head = 'a' * 40
    contract = {'source_commit': head, 'launcher_sha256': hashlib.sha256(l._SOURCE).hexdigest(),
                **{key: hashlib.sha256(sources[name]).hexdigest() for name, key in (
                    ('rook_mcp_events', 'source_sha256'), ('receive_one_discord_message', 'receiver_sha256'), ('post_discord_message', 'poster_sha256'))}}
    b = {'source_commit': head, 'tunnel_id': 'tunnel_6abf0b2046f08191a69a16d7c5d196c5',
         'guild_id': m.GUILD, 'bot_id': '1555375358478655621', 'finch_channel_id': '1555387641871081607',
         'dots_shared_channel_id': '1555387660644782121', 'token_path': '/home/ubuntu/.hermes/secrets/dot-bots/finch.token',
         'allowed_sender_ids': [m.OPERATOR, m.BOT, m.ROOK_BOT]}
    auth = {'matching_resources': [{'identifier': l.AUDIENCE, 'signing_alg': 'RS256', 'enforce_policies': True,
             'token_dialect': 'access_token_authz', 'scopes': [{'value': s} for s in l.REQUIRED]}],
            'finch_permissions': [{'resource_server_identifier': l.AUDIENCE, 'permission_name': s} for s in l.REQUIRED],
            'verified_operator': {'email': 'izaiahtilton1@gmail.com', 'email_verified': True, 'user_id': 'synthetic-subject'}}
    calls = []
    monkeypatch.setattr(l, 'isolate_startup', lambda: calls.append(('isolated',)))
    monkeypatch.setattr(l, 'receiving_contract', lambda: contract)
    monkeypatch.setattr(l, 'private_json', lambda p: b if p.name == 'nonsecret-bindings.json' else auth)
    def git(args, **kwargs):
        calls.append(('git', args, kwargs))
        return head + '\n' if args[-1] == 'HEAD' else ''
    monkeypatch.setattr(l.subprocess, 'check_output', git)
    def read(args, *, finch):
        calls.append(('fresh-read', args))
        return {'structuredContent': {'messages': [{'message_id': '700' if args['channel_id'] == finch.private_channel else '800'}]}}
    adapter = SimpleNamespace(FinchIdentity=m.FinchIdentity, _finch_confinement=lambda: calls.append(('confined',)),
                              read_messages=read, main=lambda argv: calls.append(('main', argv)) or 0)
    monkeypatch.setattr(l, 'load_adapter', lambda *a: adapter)
    return l, calls, contract, b, auth


def test_full_launcher_retains_identity_state_fresh_cursors_and_bounded_lifetime(launch_fixture):
    l, calls, _, _, _ = launch_fixture
    assert l.launch() == 0
    assert calls[0] == ('isolated',)
    assert [c[0] for c in calls] == ['isolated', 'git', 'git', 'confined', 'fresh-read', 'fresh-read', 'main']
    argv = calls[-1][1]
    for flag, value in [('--state-dir', str(l.STATE)), ('--port', '18806'),
                        ('--operator-subject', 'synthetic-subject'), ('--max-run-seconds', '57600'),
                        ('--finch-after-message', '700'), ('--dots-shared-after-message', '800'),
                        ('--finch-token-path', '/home/ubuntu/.hermes/secrets/dot-bots/finch.token')]:
        assert argv[argv.index(flag) + 1] == value
    assert '--overnight-pilot' in argv and '--after-message' not in argv
    for _, command, options in [c for c in calls if c[0] == 'git']:
        assert 'core.fsmonitor=false' in command and 'core.hooksPath=/dev/null' in command
        assert options['env']['GIT_CONFIG_GLOBAL'] == '/dev/null' and options['env']['GIT_CONFIG_NOSYSTEM'] == '1'
        assert 'GIT_SSH_COMMAND' not in options['env'] and 'LD_PRELOAD' not in options['env']


@pytest.mark.parametrize('metadata', [False, True])
def test_check_only_has_no_token_network_adapter_or_state_effect(launch_fixture, metadata):
    l, calls, _, _, _ = launch_fixture
    assert l.launch(metadata_only=metadata, check_only=True) == 0
    assert not any(call[0] in {'fresh-read', 'main'} for call in calls)


def test_metadata_only_never_upgrades_to_full(launch_fixture):
    l, calls, _, _, _ = launch_fixture
    assert l.launch(metadata_only=True) == 0
    argv = calls[-1][1]
    assert '--metadata-only' in argv and '--state-dir' not in argv and '--operator-subject' not in argv
    assert argv[argv.index('--max-run-seconds') + 1] == '300'
    assert not any(c[0] == 'fresh-read' for c in calls)


@pytest.mark.parametrize('fault', ['head', 'source', 'launcher', 'receiver', 'poster', 'binding', 'rbac', 'operator'])
def test_bad_source_contract_or_auth_blocks_before_cursor_read(launch_fixture, fault):
    l, calls, contract, b, auth = launch_fixture
    if fault == 'head': contract['source_commit'] = 'b' * 40
    if fault in ('source', 'launcher', 'receiver', 'poster'): contract[fault + '_sha256'] = '0' * 64
    if fault == 'binding': b['bot_id'] = '999'
    if fault == 'rbac': auth['finch_permissions'] = []
    if fault == 'operator': auth['verified_operator']['email_verified'] = False
    with pytest.raises(ValueError): l.launch()
    assert not any(c[0] in {'fresh-read', 'main'} for c in calls)


def test_verified_byte_loader_does_not_reopen_replaced_source(monkeypatch, tmp_path):
    l = launcher(); monkeypatch.setattr(l, 'CHECKOUT', tmp_path)
    name = 'synthetic_verified_module'
    sources = {name: b'VALUE = "reviewed"\n', 'rook_mcp_events': b'VALUE = "reviewed adapter"\n'}
    old = sys.modules.get('rook_mcp_events')
    try:
        (tmp_path / 'scripts').mkdir()
        (tmp_path / 'scripts' / (name + '.py')).write_text('raise RuntimeError("unreviewed disk replacement")\n')
        assert l.load_adapter(sources, [(name, ''), ('rook_mcp_events', '')]).VALUE == 'reviewed adapter'
        assert sys.modules[name].VALUE == 'reviewed'
    finally:
        sys.modules.pop(name, None)
        if old is None: sys.modules.pop('rook_mcp_events', None)
        else: sys.modules['rook_mcp_events'] = old


def test_isolated_python_does_not_execute_cwd_or_pythonpath_sitecustomize(tmp_path):
    marker = tmp_path / 'executed'
    (tmp_path / 'sitecustomize.py').write_text(f'open({str(marker)!r}, "w").write("bad")\n')
    (tmp_path / 'usercustomize.py').write_text(f'open({str(marker)!r}, "w").write("bad")\n')
    result = subprocess.run([sys.executable, '-I', '-S', '-B', '-c', 'import sys; assert sys.flags.isolated and sys.flags.no_site'],
                            cwd=tmp_path, env={'PATH': '/usr/bin:/bin', 'PYTHONPATH': str(tmp_path)}, capture_output=True)
    assert result.returncode == 0 and not marker.exists()


class TestReceivingPreservesCustody:
    setup = _Finch.setup
    callback = _Finch.callback
    secret = _Finch.secret
    http_call = _Finch.http_call
    call = _Finch.call
    subscribe = _Finch.subscribe
    event = _Finch.event
    ready = _Finch.ready
    reopen = _Finch.reopen
    args = _Finch.args

    def test_pending_ready_request_survives_restart_with_fresh_poll_cursors(self, monkeypatch):
        monkeypatch.setattr(self.m, 'load_rook_token', lambda path:
                            'synthetic-finch-token' if path == self.dot.token_path else pytest.fail('unexpected token path'))
        self.ready()
        before = self.app.store.get('requests', 'discord-301')
        self.cursors = {'901': '700', '902': '800'}
        self.reopen()
        assert self.app.store.get('requests', 'discord-301') == before
        assert self.app.ingest(self.event('302'), self.cursors) == 0
        status, result = self.call('reply_to_finch_request', self.args(file=False))
        assert status == 200 and result['result']['isError'] is False
        self.reopen()
        before_http = len(self.http)
        assert self.call('reply_to_finch_request', self.args(file=False))[1] == result
        assert len(self.http) == before_http
