"""Offline harness wiring/fail-closed tests. NEVER invokes target-kernel modes."""
from contextlib import contextmanager
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import re
import socket
import stat
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'ops/rook-mcp/finch_kernel_harness.py'
spec = importlib.util.spec_from_file_location('finch_kernel_harness_synthetic', SOURCE)
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(h, 'DEADLINE', None)
    monkeypatch.setattr(h, 'OWNED_NAMESPACES', {})
    monkeypatch.setattr(h, 'OWNED_CGROUPS', {})
    monkeypatch.setattr(h.subprocess, 'run', lambda *a, **k: pytest.fail('vendor execution prohibited in CI'))
    monkeypatch.setattr(h.subprocess, 'Popen', lambda *a, **k: pytest.fail('process execution prohibited in CI'))
    monkeypatch.setattr(socket.socket, 'connect', lambda *a: pytest.fail('network prohibited in CI'))
    monkeypatch.setattr(socket.socket, 'bind', lambda *a: pytest.fail('listener prohibited in CI'))


def test_plan_is_readonly_without_approval_or_root(capsys):
    assert h.main(['plan']) == 0
    assert 'Separate exact runtime approval' in capsys.readouterr().out


@pytest.mark.parametrize('phase', ['packet-fixture', 'admission-fixture'])
def test_effect_modes_refuse_absent_explicit_fixture_approval(phase, capsys):
    assert h.main([phase]) == 78
    assert 'existing approved fixture' in capsys.readouterr().err


def test_approval_string_alone_is_insufficient():
    with pytest.raises(ValueError): h.authorize(SimpleNamespace(approved_fixture=False, approval_reference='synthetic'))


def test_harness_binds_unchanged_accepted_runtime_sources():
    assert hashlib.sha256((ROOT / 'ops/rook-mcp/finch_network.py').read_bytes()).hexdigest() == h.NETWORK_SHA
    assert hashlib.sha256((ROOT / 'ops/rook-mcp/finch-mcp-adapter.system.service').read_bytes()).hexdigest() == h.UNIT_SHA
    text = SOURCE.read_text()
    assert 'n.privileged_inputs()' in text
    assert 'network-facts.json' not in text and 'finch.token' not in text
    assert 'sock.sendto(query, (address, port))' in text and 'PROTOCOL_TLS_SERVER' in text
    assert "'--config'" not in text  # openssl uses explicit -config /dev/null below
    assert "'-config', '/dev/null'" in text


@pytest.mark.parametrize('succeeded', [False, True])
def test_denial_needs_both_output_counter_and_zero_sink_packets(succeeded):
    args = ({'deny': 0}, {'deny': 1}, 10, 10, 'deny', False, succeeded)
    if succeeded:
        with pytest.raises(ValueError): h.evidence(*args)
    else: assert h.evidence(*args)['sink_packets'] == 0
    with pytest.raises(ValueError): h.evidence({'deny': 0}, {'deny': 1}, 10, 11, 'deny', False, False)
    with pytest.raises(ValueError): h.evidence({'deny': 0}, {'deny': 0}, 10, 10, 'deny', False, False)


def test_positive_needs_success_counter_and_sink_packets():
    assert h.evidence({'allow': 0}, {'allow': 2}, 0, 2, 'allow', True, True)['allowed']
    with pytest.raises(ValueError): h.evidence({'allow': 0}, {'allow': 2}, 0, 0, 'allow', True, True)


def test_input_counters_cover_udp_tcp_ipv4_ipv6_before_delivery():
    text = h.input_policy(h.SINKS)
    assert 'hook input priority -10' in text and text.count('counter comment') == len(h.SINKS)
    assert 'udp dport 53' in text and 'tcp dport 53' in text
    assert 'ip6 daddr fd00:ec2::254 tcp dport 80' in text
    assert 'policy accept' in text


def test_production_dropin_retains_exact_real_guard_and_never_runs_finch():
    unit = (ROOT / 'ops/rook-mcp/finch-mcp-adapter.system.service').read_text()
    text = h.fixture_dropin(unit, Path('/run/finch-mcp/synthetic.nft'), 'synthetic-case', 49151)
    originals = [s for s in unit.splitlines() if s.startswith('ExecCondition=')]
    assert all(s in text for s in originals[:2]) and originals[2] not in text
    assert 'finch_adapter_launch.py' not in text
    assert 'Restart=no' in text and 'ExecStart=/usr/bin/python3 -I -S -B -c ' in text
    assert 'ExecCondition=+/usr/bin/stat --format=FINCH_FIXTURE_CGROUP_synthetic-case_%%i' in text
    assert 'AppArmorProfile=' not in text and 'User=' not in text  # inherited unchanged
    assert text.index('stat --format') < text.index('/usr/sbin/nft') < text.index('sha256sum') < text.index('finch_network.py admit') < text.index('ExecStart=/')
    assert '127.0.0.1' in text and '49151' in text


@pytest.mark.parametrize('value', ['$HOME', '%i', 'a\n%foo'])
def test_marker_cannot_inject_systemd_substitution(value):
    with pytest.raises(ValueError): h.quote_unit(value)


def test_markers_compile_and_contain_only_owned_loopback_packet():
    code = h.marker_program('synthetic', 49151)
    compile(code, '<synthetic-marker>', 'exec')
    assert 'signal.alarm(120)' in code and code.index('signal.alarm(120)') < code.index('socket.socket')
    assert "('127.0.0.1',49151)" in code and 'finch.token' not in code and 'load_rook_token' not in code
    assert code.index('FINCH_FIXTURE_APP') < code.index('FINCH_FIXTURE_PACKET') < code.index('s.sendto')


def test_fault_mutations_are_only_owned_table_or_set():
    n = SimpleNamespace(TABLE='finch_mcp_egress', SET='finch_cgroups')
    mutations = h.mutations(n, Path('/sys/fs/cgroup/finch-admission-synthetic'))
    assert set(mutations) == {'absent-table', 'empty-membership', 'stale-membership', 'extra-membership', 'changed-fingerprint'}
    assert 'flush ruleset' not in str(mutations) and 'skuid' not in str(mutations)
    assert mutations['absent-table'] == 'delete table inet finch_mcp_egress\n'
    assert 'flush set' not in mutations['extra-membership']


def properties(**changes):
    values = dict(ExecCondition='{ path=/usr/bin/env ; argv[]=/usr/bin/env -i /usr/bin/python3 -I -S -B /usr/local/libexec/finch-mcp/finch_network.py admit ; code=exited ; status=78 }',
                  ActiveState='inactive', MainPID='0', Result='success')
    values.update(changes)
    return values


def test_exit78_is_skipped_success_not_misreported_as_failed():
    value = h.skipped_evidence(properties(), '', 'synthetic', b'old', b'old')
    assert value['unit_result'] == 'success' and value['active_state'] == 'inactive' and value['admission_exit'] == 78


@pytest.mark.parametrize('fault', ['other-exit78', 'app-marker', 'packet-marker', 'receipt', 'pid', 'active'])
def test_any_later_activity_or_wrong_condition_refuses_skip_claim(fault):
    p, log, current = properties(), '', b'old'
    if fault == 'other-exit78': p['ExecCondition'] = '{ path=/usr/sbin/nft ; status=78 }'
    if fault == 'app-marker': log = 'FINCH_FIXTURE_APP_synthetic'
    if fault == 'packet-marker': log = 'FINCH_FIXTURE_PACKET_synthetic'
    if fault == 'receipt': current = b'new'
    if fault == 'pid': p['MainPID'] = '42'
    if fault == 'active': p['ActiveState'] = 'active'
    with pytest.raises(ValueError): h.skipped_evidence(p, log, 'synthetic', b'old', current)


def test_real_admission_flag_required_before_root_inputs():
    n = SimpleNamespace(privileged_inputs=lambda: pytest.fail('should not inspect receiving facts'))
    with pytest.raises(ValueError): h.admission_fixture(n, False)


def test_worker_placement_precedes_first_socket_command(monkeypatch, tmp_path):
    group = tmp_path / 'group'; group.mkdir()
    events = []
    class Input(io.StringIO):
        def write(self, value):
            assert (group / 'cgroup.procs').read_text() == '4242'
            events.append('command-after-placement'); return super().write(value)
    proc = SimpleNamespace(pid=4242, stdin=Input(), stdout=io.StringIO('{"success":true}\n'), wait=lambda **kw: 0)
    monkeypatch.setattr(h.subprocess, 'Popen', lambda *a, **kw: proc)
    monkeypatch.setattr(h, 'verify_namespace', lambda *a: None)
    monkeypatch.setattr(h, 'process_start', lambda *a: 'synthetic-start')
    h.OWNED_NAMESPACES['finch-client-abcdef123456'] = (1, 2)
    h.OWNED_CGROUPS[str(group)] = h.identity(group)
    read = h.Path.read_text
    monkeypatch.setattr(h.Path, 'read_text', lambda path, *a, **kw: '0::' + str(group) if str(path) == '/proc/4242/cgroup' else read(path, *a, **kw))
    monkeypatch.setattr(h.select, 'select', lambda *a: ([proc.stdout], [], []))
    assert h.start_worker('finch-client-abcdef123456', {'role': 'client'}, [], group)['success']
    assert events == ['command-after-placement']


def test_worker_refuses_standalone_unisolated_invocation_before_reading_job(monkeypatch):
    monkeypatch.setattr(h.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(h, '__file__', str(h.PROGRAM)); monkeypatch.setattr(h, 'BOOTSTRAP', True)
    monkeypatch.setattr(h.os, 'stat', lambda *a: SimpleNamespace(st_ino=1))
    with pytest.raises(ValueError, match='isolated privileged'): h.worker(None)


@pytest.fixture
def admission_model(monkeypatch, tmp_path):
    """Mock manager/kernel/filesystem observations only, no live service effects."""
    unit = tmp_path / 'unit.service'; unit.write_bytes((ROOT / 'ops/rook-mcp/finch-mcp-adapter.system.service').read_bytes())
    dropdir = tmp_path / 'dropins'; dropin = dropdir / '90-fixture.conf'
    monkeypatch.setattr(h, 'UNIT_PATH', unit); monkeypatch.setattr(h, 'DROPIN_DIR', dropdir); monkeypatch.setattr(h, 'DROPIN', dropin)
    monkeypatch.setattr(h, 'root_directory', lambda *a: None)
    monkeypatch.setattr(h, 'identity', lambda path: (1, 91))
    original_lstat = h.Path.lstat
    monkeypatch.setattr(h.Path, 'lstat', lambda path: SimpleNamespace(st_mode=h.stat.S_IFREG | 0o444, st_uid=0) if str(path).startswith('/run/netns/') else original_lstat(path))
    monkeypatch.setattr(h.os, 'urandom', lambda count: b'\xab' * count)
    monkeypatch.setattr(h.time, 'sleep', lambda *a: None)
    actual_tempdir = h.tempfile.TemporaryDirectory
    monkeypatch.setattr(h.tempfile, 'TemporaryDirectory', lambda **kw: actual_tempdir(prefix=kw['prefix'], dir=tmp_path))
    state = dict(active=False, group=False, groups=set(), members=[], present=True, counter=0, admission=None,
                 case=None, calls=[], restore_failure=False, applies=0, starts=0)
    cgroup = '/system.slice/finch-mcp-adapter.service'
    stale = '/sys/fs/cgroup/finch-admission-' + 'ab' * 6
    read_bytes, exists, mkdir, rmdir, stat = h.Path.read_bytes, h.Path.exists, h.Path.mkdir, h.Path.rmdir, h.Path.stat
    def fake_exists(path):
        if str(path) == '/synthetic-admission': return state['admission'] is not None
        if str(path) == '/sys/fs/cgroup' + cgroup: return state['group']
        if str(path).startswith('/sys/fs/cgroup/finch-admission-'): return str(path) in state['groups']
        return exists(path)
    monkeypatch.setattr(h.Path, 'exists', fake_exists)
    monkeypatch.setattr(h.Path, 'mkdir', lambda path, *a, **kw: state['groups'].add(str(path)) if str(path) == stale else mkdir(path, *a, **kw))
    monkeypatch.setattr(h.Path, 'rmdir', lambda path: state['groups'].remove(str(path)) if str(path) == stale else rmdir(path))
    monkeypatch.setattr(h.Path, 'stat', lambda path, *a, **kw: SimpleNamespace(st_dev=1, st_ino=92 if str(path) == stale else 91) if str(path).startswith('/sys/fs/cgroup/') else stat(path, *a, **kw))
    monkeypatch.setattr(h.Path, 'read_bytes', lambda path: b'python\x00' + b'ab' * 6 if str(path) == '/proc/4242/cmdline' else read_bytes(path))
    class Sink:
        def bind(self, address): assert address == ('127.0.0.1', 0)
        def settimeout(self, value): pass
        def getsockname(self): return '127.0.0.1', 49151
        def getsockopt(self, level, option): return 0
        def recvfrom(self, limit): raise socket.timeout()
        def close(self): pass
    monkeypatch.setattr(h.socket, 'socket', lambda *a: Sink())
    def table():
        assert state['present']
        return {'nftables': [{'set': {'name': 'finch_cgroups', 'type': 'cgroupsv2', 'elem': list(state['members'])}},
                             {'rule': {'comment': 'finch:local-route', 'expr': [{'counter': {'packets': state['counter']}}]}}]}
    def membership(value):
        return value['nftables'][0]['set']['elem']
    facts = {}
    stored = dict(kernel_sha256='accepted-static', render_sha256=hashlib.sha256(b'rendered\n').hexdigest())
    n = SimpleNamespace(CGROUP=cgroup, UNIT='finch-mcp-adapter.service', TABLE='finch_mcp_egress', SET='finch_cgroups',
        ADMISSION=Path('/synthetic-admission'), RECEIPT=Path('/synthetic-installed'),
        privileged_inputs=lambda: (facts, 'facts', 'contract'), root_bytes=lambda path, limit: state['admission'] if str(path) == '/synthetic-admission' else read_bytes(path),
        membership=membership, kernel_table=table, read_json=lambda path: (stored, 'sha'),
        table_fingerprint=lambda value: 'accepted-static', render=lambda f: 'rendered\n', digest=lambda b: hashlib.sha256(b).hexdigest())
    def prop(name):
        if name in {'ExecStart', 'ExecCondition'} and dropin.exists():
            text = dropin.read_text()
            if 'FINCH_FIXTURE_CGROUP_' not in text: return 'foreign edit'
            nonce = re.search(r'FINCH_FIXTURE_CGROUP_([^ ]+)_%%i', text)[1]
            if name == 'ExecStart':
                commands = ['/usr/bin/python3 -I -S -B -c ' + h.marker_program(nonce, 49151)]
            else:
                commands = [line.split('=', 1)[1].lstrip('+').replace('%%i', '%i') for line in text.splitlines() if line.startswith('ExecCondition=') and line != 'ExecCondition=']
            return ' ; '.join('{ path=' + cmd.split()[0] + ' ; argv[]=' + cmd + ' ; ignore_errors=no ; status=78 }' for cmd in commands)
        return {'FragmentPath': str(unit), 'DropInPaths': str(dropin) if dropin.exists() else '',
                'ControlPID': '0', 'MainPID': '4242' if state['active'] else '0', 'ActiveState': 'active' if state['active'] else 'inactive',
                'ExecCondition': properties()['ExecCondition'], 'Result': 'success', 'InvocationID': 'd' * 32}[name]
    monkeypatch.setattr(h, 'unit_property', prop)
    def command(argv, **kw):
        state['calls'].append((argv, kw))
        if argv[:2] == [h.SYSTEMCTL, 'start']:
            state['starts'] += 1
            text = dropin.read_text()
            nonce = re.search(r'FINCH_FIXTURE_CGROUP_([^ ]+)_%%i', text)[1]
            case = nonce.split('-', 1)[1]; state['case'] = case
            state['active'] = state['group'] = case.startswith('positive')
            state['members'] = [91] if state['active'] else []
            if case == 'absent-table': state['present'] = False
            if case in {'stale-membership', 'extra-membership'}: state['members'] = [stale.removeprefix('/sys/fs/cgroup/')]
            if state['active']:
                state['counter'] = 1
                state['admission'] = json.dumps({'cgroup_id': 91, 'nonce': str(state['starts']).zfill(32)}).encode()
        elif argv[:2] == [h.SYSTEMCTL, 'stop']:
            state['active'] = state['group'] = False; state['members'] = [v for v in state['members'] if v != 91]
        elif argv[0] == '/usr/bin/journalctl':
            assert argv[1] == '_SYSTEMD_INVOCATION_ID=' + 'd' * 32
            nonce = 'ab' * 6 + '-' + state['case']
            return 'FINCH_FIXTURE_CGROUP_' + nonce + '_91\n' + ('FINCH_FIXTURE_APP_' + nonce + '\nFINCH_FIXTURE_PACKET_' + nonce if state['active'] else '')
        elif argv[:3] == [h.NFT, '--json', 'list']:
            return json.dumps({'nftables': [{'table': {'family': 'inet', 'name': n.TABLE}}] if state['present'] else []})
        elif argv[:2] == [h.NFT, '--file']:
            state['applies'] += 1
            if state['restore_failure']: raise ValueError('synthetic restoration error')
            assert 'flush ruleset' not in kw['data']
            state['present'] = True; state['members'] = []; state['counter'] = 0
        return ''
    monkeypatch.setattr(h, 'command', command)
    return n, state, dropdir


def test_real_manager_fixture_flow_faults_restart_and_restoration_are_bounded(admission_model):
    n, state, dropdir = admission_model
    result = h.admission_fixture(n, True)
    assert result['actual_Finch_started'] is False and result['production_adoption'] is False
    assert len(result['cases']) == 7 and state['starts'] == 7
    assert result['cases'][0]['actual_cgroup_id'] == result['cases'][-1]['actual_cgroup_id'] == 91
    assert result['cases'][0]['nonce'] != result['cases'][-1]['nonce']
    assert all(row['admission_exit'] == 78 and row['unit_result'] == 'success' for row in result['cases'][1:-1])
    assert state['present'] and not state['members'] and not state['active'] and not state['groups'] and not dropdir.exists()
    assert all('finch.token' not in str(c) and 'finch_adapter_launch.py' not in str(c) for c in state['calls'])


def test_active_finch_refuses_before_fixture_mutation(admission_model):
    n, state, dropdir = admission_model; state['active'] = True
    with pytest.raises(ValueError, match='gracefully stopped'): h.admission_fixture(n, True)
    assert not state['calls'] and not dropdir.exists()


def test_preexisting_dropins_refuse_without_replacement(admission_model):
    n, state, dropdir = admission_model; dropdir.mkdir()
    with pytest.raises(ValueError, match='preexisting'): h.admission_fixture(n, True)
    assert not state['calls'] and dropdir.exists()


def test_uncertain_table_restore_is_not_retried(admission_model):
    n, state, _ = admission_model; state['restore_failure'] = True
    with pytest.raises(ValueError, match='manual recovery'): h.admission_fixture(n, True)
    assert state['applies'] == 1 and state['starts'] == 0


def test_packet_fixture_wiring_is_namespace_only_and_cleans_its_resources(monkeypatch, tmp_path):
    calls, groups, values = [], set(), {}
    monkeypatch.setattr(h, 'root_directory', lambda *a: None)
    monkeypatch.setattr(h, 'identity', lambda path: (1, 91))
    original_lstat = h.Path.lstat
    monkeypatch.setattr(h.Path, 'lstat', lambda path: SimpleNamespace(st_mode=h.stat.S_IFREG | 0o444, st_uid=0) if str(path).startswith('/run/netns/') else original_lstat(path))
    monkeypatch.setattr(h.os, 'urandom', lambda count: b'\xab' * count)
    monkeypatch.setattr(h.time, 'sleep', lambda *a: None)
    mkdir, rmdir, exists, stat = h.Path.mkdir, h.Path.rmdir, h.Path.exists, h.Path.stat
    monkeypatch.setattr(h.Path, 'mkdir', lambda path, *a, **kw: groups.add(str(path)) if str(path).startswith('/sys/fs/cgroup/') else mkdir(path, *a, **kw))
    monkeypatch.setattr(h.Path, 'rmdir', lambda path: groups.remove(str(path)) if str(path).startswith('/sys/fs/cgroup/') else rmdir(path))
    monkeypatch.setattr(h.Path, 'exists', lambda path: str(path) in groups if str(path).startswith('/sys/fs/cgroup/') else exists(path))
    monkeypatch.setattr(h.Path, 'stat', lambda path, *a, **kw: SimpleNamespace(st_ino=91) if str(path).startswith('/sys/fs/cgroup/') else stat(path, *a, **kw))
    def command(argv, **kwargs): calls.append((argv, kwargs)); return ''
    monkeypatch.setattr(h, 'command', command)
    reasons = {'dns-tcp': 'finch:verified-dns', 'dns-udp': 'finch:verified-dns', 'tls4': 'finch:public-https',
               'tls6': 'finch:public-https', 'incoming-http': 'finch:incoming-http-reply',
               'loopback-broker': 'finch:local-route', 'udp-broker': 'finch:local-route', 'local-public': 'finch:local-route',
               'loopback6': 'finch:local-route', 'private6': 'finch:nonpublic6', 'linklocal4': 'finch:blocked4',
               'private': 'finch:blocked4', 'tailnet': 'finch:blocked4', 'metadata4': 'finch:metadata4', 'metadata6': 'finch:metadata6'}
    policy = {reason: 0 for reason in reasons.values()}
    sinks = {'sink:' + entry[0]: 0 for entry in h.SINKS}
    monkeypatch.setattr(h, 'counters', lambda ns, table: dict(sinks if table == 'finch_probe_input' else policy))
    def worker(ns, job, children, group=None, server=False):
        if server: return {'ready': True}
        tag = job['tag']; selected = group is not None or tag == 'incoming-http'
        allowed = not selected or tag in {'dns-tcp', 'dns-udp', 'tls4', 'tls6', 'incoming-http'}
        if selected: policy[reasons[tag]] += 1
        if allowed: sinks['sink:' + tag] += 1
        return {'success': allowed}
    monkeypatch.setattr(h, 'start_worker', worker)
    n = SimpleNamespace(TABLE='finch_mcp_egress', SET='finch_cgroups', render=lambda f: 'synthetic-policy\n')
    result = h.packet_fixture(n)
    assert result['production_admission'] is False and result['self_signed_tls_only'] is True
    assert len(result['cases']) == 19 and not groups
    assert all(row['sink_packets'] == 0 for row in result['cases'] if not row['allowed'])
    assert len([row for row in result['cases'] if row.get('mapped')]) == 2
    assert len([row for row in result['cases'] if row.get('source_port') == 18806]) == 2
    assert all(argv[0] == h.IP for argv, _ in calls)
    nft_calls = [(argv, kw) for argv, kw in calls if h.NFT in argv]
    assert nft_calls and all(argv[:3] == [h.IP, 'netns', 'exec'] for argv, _ in nft_calls)
    assert not any('default' in argv and 'add' in argv for argv, _ in calls)
    deleted = [argv for argv, _ in calls if argv[:3] == [h.IP, 'netns', 'delete']]
    assert len(deleted) == 2 and all('finch-' in argv[-1] for argv in deleted)


def test_namespace_name_cannot_select_host_or_arbitrary_namespace():
    for name in ('', 'default', 'production', 'finch-client-../../x', 'finch-client-ABCDEF123456'):
        with pytest.raises(ValueError): h.in_ns(name, [h.NFT, 'list', 'tables'])


def test_synthetic_dns_wire_response_is_bounded_and_not_real_resolution():
    assert h.response('dns-udp', h.DNS_QUERY)[2:4] == b'\x81\x80'
    tcp = len(h.DNS_QUERY).to_bytes(2, 'big') + h.DNS_QUERY
    assert h.response('dns-tcp', tcp)[:2] == tcp[:2]
    with pytest.raises(ValueError): h.response('dns-udp', b'not-a-fixture-domain')


def test_failure_exit_does_not_emit_success(capsys):
    assert h.main(['admission-fixture', '--approval-reference', 'synthetic']) == 78
    text = capsys.readouterr().err
    assert 'REFUSED_OR_FAILED' in text and '"automatic_retry": false' in text and '"result": "PASS"' not in text


@pytest.mark.parametrize('fault', ['foreign-rule', 'changed-own-rule', 'extra-member', 'duplicate-member'])
def test_cleanup_refuses_unknown_root_admin_preimage(monkeypatch, fault):
    spec = importlib.util.spec_from_file_location('cleanup_network_synthetic', ROOT / 'ops/rook-mcp/finch_network.py')
    n = importlib.util.module_from_spec(spec); spec.loader.exec_module(n)
    table = {'nftables': [{'set': {'name': n.SET, 'type': 'cgroupsv2', 'elem': []}},
                          {'rule': {'family': 'inet', 'table': n.TABLE, 'chain': 'finch_output',
                                    'expr': [{'counter': {'packets': 0, 'bytes': 0}}, {'drop': None}], 'comment': 'original'}}]}
    accepted = n.table_fingerprint(table)
    stale = Path('/sys/fs/cgroup/finch-admission-synthetic')
    monkeypatch.setattr(h.Path, 'stat', lambda *a: SimpleNamespace(st_ino=92))
    own = {'rule': {'family': 'inet', 'table': n.TABLE, 'chain': 'finch_output',
                    'expr': [{'counter': {'packets': 0, 'bytes': 0}}, {'drop': None}], 'comment': 'fixture-drift'}}
    table['nftables'].append(own)
    h.assert_owned_table_preimage(n, table, accepted, 'changed-fingerprint', stale)
    if fault == 'foreign-rule': table['nftables'][1]['rule']['comment'] = 'administrator-change'
    if fault == 'changed-own-rule': own['rule']['expr'][-1] = {'accept': None}
    if fault == 'extra-member': table['nftables'][0]['set']['elem'] = [777]
    if fault == 'duplicate-member': table['nftables'][0]['set']['elem'] = [92, 92]
    with pytest.raises(ValueError): h.assert_owned_table_preimage(n, table, accepted, 'changed-fingerprint', stale)


def test_administrator_dropin_edit_is_not_overwritten_or_deleted(monkeypatch, admission_model):
    n, state, dropdir = admission_model
    original = h.command
    changed = False
    def mutate(argv, **kwargs):
        nonlocal changed
        result = original(argv, **kwargs)
        if argv[:2] == [h.SYSTEMCTL, 'start'] and not changed:
            h.DROPIN.write_text('[Service]\n# foreign administrator change\n')
            changed = True
        return result
    monkeypatch.setattr(h, 'command', mutate)
    with pytest.raises(ValueError, match='drop-in changed'): h.admission_fixture(n, True)
    assert 'foreign administrator change' in h.DROPIN.read_text()
    assert state['starts'] == 1


def test_uncertain_activating_service_is_not_stopped(monkeypatch, admission_model):
    n, state, _ = admission_model
    original = h.command
    real_property = h.unit_property
    uncertain = False
    def command(argv, **kwargs):
        nonlocal uncertain
        if argv[:2] == [h.SYSTEMCTL, 'start']:
            uncertain = True
            raise TimeoutError('synthetic start uncertainty')
        return original(argv, **kwargs)
    monkeypatch.setattr(h, 'command', command)
    monkeypatch.setattr(h, 'unit_property', lambda key: 'activating' if uncertain and key == 'ActiveState' else
                        '123' if uncertain and key == 'ControlPID' else real_property(key))
    with pytest.raises(ValueError, match='uncertain activating'): h.admission_fixture(n, True)
    assert len([c for c, _ in state['calls'] if c[:2] == [h.SYSTEMCTL, 'stop']]) == 1  # initial inactive fixture check only


def test_namespace_replacement_is_refused_before_any_vendor_call(monkeypatch):
    h.OWNED_NAMESPACES['finch-client-abcdef123456'] = (1, 2)
    monkeypatch.setattr(h.Path, 'lstat', lambda path: SimpleNamespace(st_mode=h.stat.S_IFREG | 0o444, st_uid=0))
    monkeypatch.setattr(h, 'identity', lambda path: (1, 3))
    with pytest.raises(ValueError, match='creation identity changed'):
        h.in_ns('finch-client-abcdef123456', [h.NFT, 'list', 'tables'])


@pytest.mark.parametrize('fault', [None, 'parent', 'start', 'namespace', 'source', 'parent-command', 'placement'])
def test_worker_context_binds_controller_namespace_source_and_cgroup(monkeypatch, fault):
    context = dict(parent_pid=4242, parent_start='777', namespace='finch-client-abcdef123456',
                   namespace_identity=[1, 2], cgroup='0::/synthetic/app', source_sha256=hashlib.sha256(h.SOURCE).hexdigest())
    if fault == 'parent': context['parent_pid'] = 999
    if fault == 'start': context['parent_start'] = 'wrong'
    if fault == 'namespace': context['namespace_identity'] = [1, 9]
    if fault == 'source': context['source_sha256'] = '0' * 64
    if fault == 'placement': context['cgroup'] = '0::/wrong'
    chunks = iter([json.dumps(context).encode(), b''])
    monkeypatch.setattr(h, 'os', SimpleNamespace(fstat=lambda fd: SimpleNamespace(st_mode=0o010000, st_uid=0),
                                               read=lambda fd, size: next(chunks), close=lambda fd: None, getppid=lambda: 4242))
    monkeypatch.setattr(h.select, 'select', lambda *a: ([99], [], []))
    monkeypatch.setattr(h, 'process_start', lambda pid: '777')
    args = [b'/usr/bin/python3', b'-I', b'-S', b'-B', str(h.PROGRAM).encode(), b'packet-fixture', b'--approved-fixture', b'--approval-reference', b'synthetic']
    if fault == 'parent-command': args[5] = b'_worker'
    monkeypatch.setattr(h.Path, 'read_bytes', lambda path: b'\0'.join(args))
    monkeypatch.setattr(h.Path, 'read_text', lambda path: '0::/synthetic/app')
    monkeypatch.setattr(h, 'identity', lambda path: (1, 9) if str(path) == '/proc/1/ns/net' else (1, 2))
    if fault:
        with pytest.raises(ValueError): h.worker_context(99)
    else: assert h.worker_context(99)['namespace'] == context['namespace']


def test_effective_exec_change_refuses_before_start(monkeypatch, admission_model):
    n, state, _ = admission_model
    original = h.unit_property
    monkeypatch.setattr(h, 'unit_property', lambda key: '{ path=/bin/sh ; argv[]=/bin/sh -c real-finch ; ignore_errors=no }' if key == 'ExecStart' else original(key))
    with pytest.raises(ValueError, match='composition changed'): h.admission_fixture(n, True)
    assert state['starts'] == 0


def test_namespace_symlink_is_refused_even_if_target_identity_matches(monkeypatch):
    h.OWNED_NAMESPACES['finch-client-abcdef123456'] = (1, 2)
    monkeypatch.setattr(h.Path, 'lstat', lambda path: SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_uid=0))
    monkeypatch.setattr(h, 'identity', lambda path: (1, 2))
    with pytest.raises(ValueError, match='symlinked'): h.verify_namespace('finch-client-abcdef123456')


def rendered_counter_readback():
    """Real accepted renderer -> synthetic nft JSON boundary; no kernel calls.

    Keep one JSON rule per rendered counter, including repeated DNS comments.
    Only the vendor readback is synthetic; the harness counter reader is real.
    """
    spec = importlib.util.spec_from_file_location('counter_network_synthetic', ROOT / 'ops/rook-mcp/finch_network.py')
    n = importlib.util.module_from_spec(spec); spec.loader.exec_module(n)
    text = n.render(h.fixture_facts(n))
    rows = []
    dns_transports = []
    for line in text.splitlines():
        label = re.search(r'\bcounter\b.*\bcomment "([^"]+)"$', line)
        if label is None: continue
        expr = [{'counter': {'packets': 0, 'bytes': 0}}]
        if label[1] == 'finch:verified-dns':
            transport = re.search(r'\b(tcp|udp) dport 53\b', line)[1]
            dns_transports.append(transport)
            expr.insert(0, {'match': {'op': '==', 'left': {'payload': {'protocol': transport, 'field': 'dport'}}, 'right': 53}})
        rows.append({'rule': {'family': 'inet', 'table': n.TABLE, 'chain': 'finch_output',
                              'expr': expr, 'comment': label[1]}})
    assert sorted(dns_transports) == ['tcp', 'udp']
    return {'nftables': rows}


def test_real_renderer_tcp_udp_dns_rules_reach_real_counter_reader(monkeypatch):
    snapshot = rendered_counter_readback()
    calls = []
    def readback(ns, argv):
        calls.append((ns, argv))
        return json.dumps(snapshot)
    monkeypatch.setattr(h, 'in_ns', readback)
    ns, table = 'finch-client-abcdef123456', 'finch_mcp_egress'
    # This first real reader call failed on the published head before any
    # packet case because the two legitimate zero-count DNS rules share a tag.
    before = h.counters(ns, table)
    assert before['finch:verified-dns'] == 0
    total = 0
    for transport, packets in [('tcp', 3), ('udp', 7)]:
        rule = next(item['rule'] for item in snapshot['nftables']
                    if item['rule']['comment'] == 'finch:verified-dns'
                    and item['rule']['expr'][0]['match']['left']['payload']['protocol'] == transport)
        rule['expr'][-1]['counter']['packets'] += packets
        after = h.counters(ns, table)
        total += packets
        assert after['finch:verified-dns'] == total
        assert h.evidence(before, after, 0, packets, 'finch:verified-dns', True, True)['output_packets'] == packets
        assert all(after[label] == before[label] for label in before if label != 'finch:verified-dns')
        before = after
    assert calls == [(ns, [h.NFT, '--json', '--numeric', 'list', 'table', 'inet', table])] * 3


def read_synthetic_counter_rules(monkeypatch, snapshot, reader):
    if reader == 'host': return h.host_counters(SimpleNamespace(kernel_table=lambda: snapshot))
    monkeypatch.setattr(h, 'in_ns', lambda *a: json.dumps(snapshot))
    return h.counters('finch-client-abcdef123456', 'finch_mcp_egress')


@pytest.mark.parametrize('reader', ['packet', 'host'])
@pytest.mark.parametrize('bad_counter', [True, -1, 1.5, None, '1'])
def test_each_repeated_dns_rule_still_requires_valid_packet_count(monkeypatch, bad_counter, reader):
    snapshot = rendered_counter_readback()
    dns = [item['rule'] for item in snapshot['nftables'] if item['rule']['comment'] == 'finch:verified-dns']
    dns[0]['expr'][-1]['counter']['packets'] = 11
    dns[1]['expr'][-1]['counter']['packets'] = bad_counter
    with pytest.raises(ValueError): read_synthetic_counter_rules(monkeypatch, snapshot, reader)


@pytest.mark.parametrize('reader', ['packet', 'host'])
def test_repeated_dns_rule_cannot_hide_multiple_counters(monkeypatch, reader):
    snapshot = rendered_counter_readback()
    rule = next(item['rule'] for item in snapshot['nftables'] if item['rule']['comment'] == 'finch:verified-dns')
    rule['expr'].append({'counter': {'packets': 1, 'bytes': 1}})
    with pytest.raises(ValueError): read_synthetic_counter_rules(monkeypatch, snapshot, reader)


@pytest.mark.parametrize('reader', ['packet', 'host'])
@pytest.mark.parametrize('label', ['finch:metadata4', 'sink:dns-tcp'])
def test_duplicate_denial_or_sink_labels_still_refused(monkeypatch, label, reader):
    rule = {'rule': {'comment': label, 'expr': [{'counter': {'packets': 0, 'bytes': 0}}]}}
    with pytest.raises(ValueError): read_synthetic_counter_rules(monkeypatch, {'nftables': [rule, rule]}, reader)


@pytest.mark.parametrize('transport', ['tcp', 'udp'])
def test_real_renderer_host_reader_never_loses_dns_packet_evidence(transport):
    snapshot = rendered_counter_readback()
    dns = [item['rule'] for item in snapshot['nftables'] if item['rule']['comment'] == 'finch:verified-dns']
    rule = next(rule for rule in dns if rule['expr'][0]['match']['left']['payload']['protocol'] == transport)
    rule['expr'][-1]['counter']['packets'] = 7
    totals = h.host_counters(SimpleNamespace(kernel_table=lambda: snapshot))
    # TCP appears first: the old host reader overwrote [7, 0] with zero,
    # incorrectly allowing the negative-admission all-zero evidence predicate.
    assert totals['finch:verified-dns'] == 7
    assert not all(value == 0 for value in totals.values())
