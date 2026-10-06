"""Synthetic policy/admission tests. No nft/systemd calls or network probes.

packet_allowed is a modeled predicate, NOT receiving-kernel denial evidence.
"""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import socket
import stat
from types import SimpleNamespace

import pytest
from test_rook_mcp_events import module

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / 'ops/rook-mcp/finch_network.py'
spec = importlib.util.spec_from_file_location('synthetic_finch_network', PATH)
n = importlib.util.module_from_spec(spec)
spec.loader.exec_module(n)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(n.time, 'time', lambda: 1000)
    monkeypatch.setattr(n.subprocess, 'run', lambda *a, **k: pytest.fail('no vendor commands in offline tests'))
    monkeypatch.setattr(socket.socket, 'connect', lambda *a: pytest.fail('no network'))
    monkeypatch.setattr(socket.socket, 'bind', lambda *a: pytest.fail('no listeners'))


@pytest.fixture
def facts():
    return dict(schema='finch-network-facts-v1', verified_by='synthetic-independent-reviewer', evidence='synthetic-only',
                verified_at=900, valid_until=2000, boot_id='00000000-0000-0000-0000-000000000001',
                unified_cgroup_v2=True, nft_socket_cgroupv2=True, nft_inet=True, conntrack_direction=True,
                legacy_user_unit_disabled=True, namespace_ids={'net': 71, 'cgroup': 72}, nft_version='nftables synthetic',
                dns=[dict(address='127.0.0.53', port=53, transports=['udp', 'tcp'])], ingress_peers=['127.0.0.1'],
                host_addresses=['127.0.0.1', '172.31.32.48', '8.7.6.5', '2606:4700::1234'],
                control_networks=['8.7.6.0/24', '2606:4700:1::/48'])


def packet(**changes):
    value = dict(cgroup_match=True, destination='1.1.1.1', source='172.31.32.48', protocol='tcp', port=443,
                 source_port=44000, state='new', direction='original', route_type='unicast')
    value.update(changes)
    return value


@pytest.mark.parametrize('destination', ['1.1.1.1', '2606:4700:4700::1111', '::ffff:1.1.1.1'])
@pytest.mark.parametrize('state', ['new', 'established'])
def test_public_tls_positive(facts, destination, state):
    assert n.packet_allowed(facts, packet(destination=destination, state=state)) == (True, 'public-https')


@pytest.mark.parametrize('protocol', ['udp', 'tcp'])
@pytest.mark.parametrize('state', ['new', 'established'])
def test_exact_stub_dns_positive(facts, protocol, state):
    assert n.packet_allowed(facts, packet(destination='127.0.0.53', port=53, protocol=protocol, state=state, route_type='local')) == (True, 'verified-dns')


def test_explicit_external_ipv6_resolver(facts):
    facts['dns'] = [dict(address='2606:4700:4700::1111', port=53, transports=['tcp'])]
    assert n.packet_allowed(facts, packet(destination='2606:4700:4700::1111', port=53))[0]
    assert not n.packet_allowed(facts, packet(destination='2606:4700:4700::1111', port=53, protocol='udp'))[0]


def reply(**changes):
    return packet(destination='127.0.0.1', source='127.0.0.1', source_port=18806, port=44000,
                  direction='reply', state='established', original_destination='127.0.0.1', original_port=18806,
                  route_type='local', **changes)


def test_incoming_http_reply_positive(facts):
    assert n.packet_allowed(facts, reply()) == (True, 'incoming-http-reply')


@pytest.mark.parametrize('field,value', [('direction', 'original'), ('original_destination', '127.0.0.2'),
    ('original_port', 22), ('state', 'new'), ('state', 'invalid'), ('source_port', 22),
    ('protocol', 'udp'), ('destination', '127.0.0.2'), ('source', '127.0.0.2')])
def test_incoming_reply_is_not_a_broker_escape(facts, field, value):
    p = reply(); p[field] = value
    assert not n.packet_allowed(facts, p)[0]


@pytest.mark.parametrize('destination', ['127.0.0.1', '127.0.0.2', '10.0.0.1', '172.31.32.48', '192.168.1.1',
    '100.64.0.1', '100.127.255.254', '169.254.169.254', '169.254.170.2', '0.0.0.0', '224.0.0.1',
    '8.7.6.5', '8.7.6.9', '192.0.0.8', '198.18.0.1', '255.255.255.255',
    '::1', '::', 'fe80::1', 'fc00::1', 'fd00:ec2::254', 'ff02::1', '64:ff9b::a00:1',
    '2002:7f00:1::', '2001::1', '2606:4700::1234', '2606:4700:1::2', '::ffff:127.0.0.1',
    '::ffff:169.254.169.254', '::ffff:100.64.0.1'])
def test_local_private_tailnet_metadata_mapped_control_denied(facts, destination):
    assert not n.packet_allowed(facts, packet(destination=destination, state='established'))[0]


@pytest.mark.parametrize('port', [22, 53, 80, 18806, 8443, 9120, 9324, 9325, 9337, 9340, 9341, 9342, 9343, 9350])
def test_no_public_non_tls_or_source_port_bypass(facts, port):
    assert not n.packet_allowed(facts, packet(port=port, source_port=18806))[0]


@pytest.mark.parametrize('change', [dict(route_type='local'), dict(route_type='broadcast'), dict(protocol='udp'),
    dict(direction='reply'), dict(state='related'), dict(state='untracked'), dict(state='invalid')])
def test_no_route_protocol_conntrack_fallback(facts, change):
    assert not n.packet_allowed(facts, packet(**change))[0]


def test_other_uid1000_services_unaffected(facts):
    assert n.packet_allowed(facts, packet(cgroup_match=False, destination='127.0.0.1', port=22)) == (True, 'unrelated-cgroup')


def test_render_order_and_exact_service_scope(facts):
    text = n.render(facts)
    assert 'socket cgroupv2 level 2 @finch_cgroups jump finch_output' in text
    assert 'ct direction reply ct state established ct original ip daddr 127.0.0.1 ct original proto-dst 18806' in text
    assert 'tcp sport 18806' in text and 'dport 18806' not in text
    assert text.index('finch:metadata') < text.index('finch:verified-dns') < text.index('finch:local-route') < text.index('finch:public-https')
    assert all(x not in text for x in ('skuid', 'flush ruleset', 'ct state established accept', 'related, established'))
    assert text.count('hook output') == 1 and 'policy accept' in text and 'finch:default-deny' in text


@pytest.mark.parametrize('field', ['schema', 'verified_by', 'evidence', 'verified_at', 'valid_until', 'boot_id',
    'unified_cgroup_v2', 'nft_socket_cgroupv2', 'nft_inet', 'conntrack_direction', 'legacy_user_unit_disabled',
    'namespace_ids', 'nft_version', 'dns', 'ingress_peers', 'host_addresses', 'control_networks'])
def test_every_receiving_fact_mandatory(facts, field):
    facts.pop(field)
    with pytest.raises(ValueError): n.validate_facts(facts)


@pytest.mark.parametrize('field,value', [('valid_until', 1000), ('valid_until', 90000), ('verified_at', 1001),
    ('valid_until', float('nan')), ('valid_until', True), ('evidence', ''), ('nft_socket_cgroupv2', False),
    ('legacy_user_unit_disabled', False), ('namespace_ids', {'net': 71}), ('dns', []), ('ingress_peers', []),
    ('host_addresses', []), ('ingress_peers', ['100.64.0.1']), ('ingress_peers', ['127.0.0.1', '127.0.0.1']),
    ('control_networks', ['10.0.0.1/24']), ('host_addresses', ['::ffff:127.0.0.1'])])
def test_stale_unsafe_ambiguous_facts_refused(facts, field, value):
    facts[field] = value
    with pytest.raises(ValueError): n.validate_facts(facts)


@pytest.mark.parametrize('entry', [dict(address='169.254.169.254', port=53, transports=['udp']),
    dict(address='fd00:ec2::254', port=53, transports=['tcp']), dict(address='127.0.0.53', port=443, transports=['tcp']),
    dict(address='127.0.0.53', port=53, transports=['udp', 'udp']), dict(address='127.0.0.53', port=53, transports=['raw'])])
def test_dns_is_not_an_arbitrary_exception(facts, entry):
    facts['dns'] = [entry]
    with pytest.raises(ValueError): n.validate_facts(facts)


def kernel(elements=None):
    return {'nftables': [{'metainfo': {'version': 'synthetic'}},
        {'table': {'family': 'inet', 'name': n.TABLE, 'handle': 1}},
        {'set': {'family': 'inet', 'table': n.TABLE, 'name': n.SET, 'type': 'cgroupsv2', 'handle': 2, 'elem': elements or []}},
        {'rule': {'family': 'inet', 'table': n.TABLE, 'chain': 'finch_output', 'handle': 3,
                  'expr': [{'counter': {'packets': 3, 'bytes': 66}}, {'reject': {'type': 'icmpx', 'expr': 'admin-prohibited'}}]}}]}


def test_readback_fingerprint_ignores_only_volatile_fields():
    a = kernel([12]); b = kernel([13]); b['nftables'][3]['rule']['expr'][0]['counter']['bytes'] = 100
    assert n.table_fingerprint(a) == n.table_fingerprint(b)
    b['nftables'][3]['rule']['expr'][-1] = {'accept': None}
    assert n.table_fingerprint(a) != n.table_fingerprint(b)
    b = deepcopy(a); b['nftables'].reverse()
    assert n.table_fingerprint(a) != n.table_fingerprint(b)


@pytest.mark.parametrize('elements', [[1, 1], [True], ['other.slice/broker.service'], [-1], [0]])
def test_ambiguous_duplicate_membership_refused(elements):
    with pytest.raises(ValueError): n.membership(kernel(elements))


def test_live_nft_cgroup_path_representation_resolves_only_fixed_path(monkeypatch):
    monkeypatch.setattr(n.os, 'stat', lambda path: SimpleNamespace(st_ino=91) if path == '/sys/fs/cgroup' + n.CGROUP else pytest.fail('arbitrary stat'))
    assert n.membership(kernel([n.CGROUP[1:]])) == [91]


@contextmanager
def unlocked():
    yield


@pytest.fixture
def guarded(monkeypatch, facts):
    writes, commands = [], []
    monkeypatch.setattr(n, 'privileged_inputs', lambda: (facts, 'a' * 64, 'b' * 64))
    monkeypatch.setattr(n, 'owner_lock', unlocked)
    monkeypatch.setattr(n, 'write_root', lambda path, value: writes.append((path, value)))
    monkeypatch.setattr(n.Path, 'read_text', lambda path: '0::' + n.CGROUP)
    monkeypatch.setattr(n.Path, 'lstat', lambda path: SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0, st_ino=91))
    table = kernel([91])
    receipt = dict(schema='finch-network-installed-v1', approval_reference='synthetic-prior-approval', boot_id=facts['boot_id'], facts_sha256='a' * 64, contract_sha256='b' * 64,
                   guard_sha256=n.SOURCE_SHA256, render_sha256=n.digest(n.render(facts).encode()), kernel_sha256=n.table_fingerprint(table))
    monkeypatch.setattr(n, 'read_json', lambda path: (receipt, 'c' * 64))
    monkeypatch.setattr(n, 'kernel_table', lambda: table)
    monkeypatch.setattr(n, 'run', lambda argv, **kw: commands.append((argv, kw)) or '')
    return writes, commands, table, receipt


def test_start_admits_exact_current_kernel_membership(guarded):
    writes, commands, _, _ = guarded
    n.admit()
    assert not commands and writes[0][0] == n.ADMISSION and writes[0][1]['cgroup_id'] == 91


@pytest.mark.parametrize('fault', ['empty', 'stale', 'duplicate', 'policy', 'source', 'contract', 'facts', 'render', 'schema', 'cgroup'])
def test_every_restart_refuses_missing_stale_or_mutated_coverage(monkeypatch, guarded, fault):
    writes, commands, table, receipt = guarded
    if fault in {'empty', 'stale', 'duplicate'}: table['nftables'][2]['set']['elem'] = {'empty': [], 'stale': [92], 'duplicate': [91, 91]}[fault]
    elif fault == 'policy': table['nftables'][3]['rule']['expr'] = [{'accept': None}]
    elif fault == 'cgroup': monkeypatch.setattr(n.Path, 'read_text', lambda path: '0::/user.slice/wrong.service')
    else: receipt[{'source': 'guard_sha256', 'contract': 'contract_sha256', 'facts': 'facts_sha256', 'render': 'render_sha256', 'schema': 'schema'}[fault]] = 'wrong'
    with pytest.raises(ValueError): n.admit()
    assert not writes and not commands


def test_install_requires_reference_before_any_work():
    with pytest.raises(ValueError): n.install(None)


def test_install_only_atomic_own_table_no_start_or_flush(monkeypatch, guarded):
    writes, commands, table, _ = guarded
    table['nftables'][2]['set']['elem'] = []
    monkeypatch.setattr(n.Path, 'exists', lambda path: False)
    def run(argv, **kw):
        commands.append((argv, kw))
        if argv[0] == '/usr/bin/systemctl': return 'inactive\n'
        if 'tables' in argv: return '{"nftables": []}'
        return ''
    monkeypatch.setattr(n, 'run', run)
    n.install('synthetic-already-approved-flow-reference')
    applied = [c for c in commands if c[0] == [n.NFT, '--file', '-']]
    assert len(applied) == 1 and applied[0][1]['data'].startswith('table inet finch_mcp_egress')
    assert not any('flush' in str(c) or 'start' in c[0] for c in commands)
    assert commands[-2][0] == [n.NFT, '--check', '--file', '-']
    assert writes[0][0] == n.RECEIPT


def test_install_fails_on_active_service_before_effect(monkeypatch, guarded):
    writes, commands, _, _ = guarded
    monkeypatch.setattr(n, 'run', lambda *a, **kw: 'active')
    with pytest.raises(ValueError): n.install('synthetic-reference')
    assert not writes and not commands


def test_no_missing_or_untrusted_privileged_inputs(monkeypatch):
    monkeypatch.setattr(n.os, 'geteuid', lambda: 1000)
    with pytest.raises(ValueError): n.privileged_inputs()


def test_unit_native_precheck_root_guard_then_confined_application():
    unit = (ROOT / 'ops/rook-mcp/finch-mcp-adapter.system.service').read_text()
    execs = [s for s in unit.splitlines() if s.startswith(('ExecCondition=', 'ExecStart='))]
    assert execs[0] == 'ExecCondition=+/usr/bin/sha256sum --check --status /etc/finch-mcp/network-helper.sha256'
    assert execs[1] == 'ExecCondition=+/usr/bin/env -i PATH=/usr/bin:/bin LANG=C.UTF-8 /usr/bin/python3 -I -S -B /usr/local/libexec/finch-mcp/finch_network.py admit'
    assert all('=+' not in s for s in execs[2:])
    assert 'NFTSet=cgroup:inet:finch_mcp_egress:finch_cgroups' in unit.split('[Service]')[1].split('[Install]')[0]
    assert 'EnvironmentFile=' not in unit and 'PassEnvironment=' not in unit
    assert 'ReadWritePaths=/etc' not in unit and all('/home/' not in s for s in execs[:2])


@pytest.fixture
def admission(monkeypatch, facts):
    m = module(); contract = {'network_guard_sha256': hashlib.sha256(b'guard').hexdigest(),
                             'network_facts_sha256': hashlib.sha256(json.dumps(facts).encode()).hexdigest()}
    value = dict(schema='finch-network-admission-v1', unit=n.UNIT, cgroup=n.CGROUP, cgroup_id=91,
                 boot_id=facts['boot_id'], facts_sha256=contract['network_facts_sha256'], guard_sha256=contract['network_guard_sha256'],
                 contract_sha256='b' * 64, kernel_sha256='c' * 64, expires=facts['valid_until'], nonce='d' * 32)
    monkeypatch.setattr(m, '_finch_root_bytes', lambda path, limit: b'guard' if path == m.FINCH_NETWORK_GUARD else
                        json.dumps(facts if path == m.FINCH_NETWORK_FACTS else value).encode())
    monkeypatch.setattr(m, '_finch_kernel_read', lambda leaf: '0::' + n.CGROUP)
    monkeypatch.setattr(m.Path, 'read_text', lambda path: facts['boot_id'])
    monkeypatch.setattr(m.Path, 'lstat', lambda path: SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0, st_ino=91))
    monkeypatch.setattr(m.os, 'stat', lambda path: SimpleNamespace(st_ino=facts['namespace_ids'][str(path).split('/')[-1]]))
    return m, contract, value


def test_adapter_requires_current_root_network_admission(admission):
    m, contract, value = admission
    m._finch_network_admission(contract, 'b' * 64)


@pytest.mark.parametrize('field,value', [('schema', 'grant'), ('unit', 'other.service'), ('cgroup', '/wrong'), ('cgroup_id', 92),
    ('cgroup_id', True), ('boot_id', 'old-boot'), ('facts_sha256', '0' * 64), ('guard_sha256', '0' * 64),
    ('contract_sha256', '0' * 64), ('kernel_sha256', ''), ('expires', 999), ('expires', float('nan')), ('nonce', 'bad')])
def test_adapter_rejects_admission_mismatch(admission, field, value):
    m, contract, receipt = admission; receipt[field] = value
    with pytest.raises(ValueError): m._finch_network_admission(contract, 'b' * 64)


@pytest.mark.parametrize('previous_boot,allowed', [('00000000-0000-0000-0000-000000000001', False),
    ('00000000-0000-0000-0000-000000000002', True), ('missing', False)])
def test_explicit_install_recreates_absent_table_only_after_verified_boot_change(monkeypatch, guarded, previous_boot, allowed):
    writes, commands, table, receipt = guarded
    receipt['boot_id'] = previous_boot
    table['nftables'][2]['set']['elem'] = []
    monkeypatch.setattr(n.Path, 'exists', lambda path: True)
    def run(argv, **kw):
        commands.append((argv, kw))
        return 'inactive' if argv[0] == '/usr/bin/systemctl' else '{"nftables": []}' if 'tables' in argv else ''
    monkeypatch.setattr(n, 'run', run)
    if allowed: n.install('synthetic-approved-new-boot-install')
    else:
        with pytest.raises(ValueError): n.install('synthetic-reference')
    effects = [c for c in commands if c[0] == [n.NFT, '--file', '-']]
    assert len(effects) == int(allowed) and len(writes) == int(allowed)
    if allowed: assert 'delete table' not in effects[0][1]['data']


def test_prior_boot_cannot_reuse_previous_approval_reference(monkeypatch, guarded):
    writes, commands, _, receipt = guarded
    receipt['boot_id'] = '00000000-0000-0000-0000-000000000002'
    monkeypatch.setattr(n.Path, 'exists', lambda path: True)
    monkeypatch.setattr(n, 'run', lambda argv, **kw: 'inactive' if argv[0] == '/usr/bin/systemctl' else '{"nftables": []}')
    with pytest.raises(ValueError, match='current explicit adoption'): n.install('synthetic-prior-approval')
    assert not writes


def test_nonsecret_admission_is_readable_under_actual_service_umask(monkeypatch, tmp_path):
    target = tmp_path / 'network-admission.json'
    monkeypatch.setattr(n, 'ADMISSION', target)
    original = n.Path.lstat
    # Synthetic root ancestry only; file creation/mode/umask/fsync are real in
    # an isolated temporary directory, never /run or an actual approval store.
    parents = {target.parent, *target.parent.parents}
    def ancestry(path):
        value = original(path)
        return SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0) if path in parents else value
    monkeypatch.setattr(n.Path, 'lstat', ancestry)
    old = n.os.umask(0o077)
    try: n.write_root(target, {'schema': 'synthetic-nonsecret-admission'})
    finally: n.os.umask(old)
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    assert json.loads(target.read_text()) == {'schema': 'synthetic-nonsecret-admission'}
