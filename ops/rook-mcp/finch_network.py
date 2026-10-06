#!/usr/bin/python3
"""Fixed Finch cgroup nft policy. Default modes are read-only/offline.

Installation/admission are parent-owned privileged operations, never assistant
approval or file-review grants. No UID-wide rules or global ruleset flush.
"""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import time

TABLE = 'finch_mcp_egress'
SET = 'finch_cgroups'
UNIT = 'finch-mcp-adapter.service'
CGROUP = '/system.slice/' + UNIT
FACTS = Path('/etc/finch-mcp/network-facts.json')
CONTRACT = Path('/etc/finch-mcp/receiving.json')
RECEIPT = Path('/etc/finch-mcp/network-installed.json')
ADMISSION = Path('/run/finch-mcp/network-admission.json')
PROGRAM = Path('/usr/local/libexec/finch-mcp/finch_network.py')
NFT = '/usr/sbin/nft'
SOURCE = Path(__file__).read_bytes()
SOURCE_SHA256 = hashlib.sha256(SOURCE).hexdigest()
BOOTSTRAP_OK = sys._getframe().f_code == compile(SOURCE, __file__, 'exec', dont_inherit=True, optimize=sys.flags.optimize)
ENV = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}
BLOCK4 = ('0.0.0.0/8', '10.0.0.0/8', '100.64.0.0/10', '127.0.0.0/8', '169.254.0.0/16',
          '172.16.0.0/12', '192.0.0.0/24', '192.0.2.0/24', '192.88.99.0/24', '192.168.0.0/16',
          '198.18.0.0/15', '198.51.100.0/24', '203.0.113.0/24', '224.0.0.0/4', '240.0.0.0/4')
BLOCK6 = ('2001::/23', '2001:db8::/32', '2002::/16', '3fff::/20')
METADATA = {'169.254.169.254', 'fd00:ec2::254'}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def digest(value):
    return hashlib.sha256(value).hexdigest()


def closed_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value: raise ValueError('duplicate JSON key')
        value[key] = item
    return value


def root_bytes(path, limit=32768):
    """No symlink, writable ancestor or single-file replacement substitution."""
    pins = []
    try:
        if not path.is_absolute() or '..' in path.parts: raise ValueError('unsafe root path')
        parent = None
        for index, name in enumerate(('/', *path.parts[1:])):
            directory = index < len(path.parts) - 1
            fd = os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
                         | (os.O_DIRECTORY if directory else 0), dir_fd=parent)
            pins.append((fd, parent, name, directory, os.fstat(fd)))
            value = pins[-1][-1]
            if (value.st_uid != 0 or value.st_mode & 0o022
                    or not (stat.S_ISDIR(value.st_mode) if directory else stat.S_ISREG(value.st_mode))
                    or (not directory and value.st_nlink != 1)):
                raise ValueError('root custody required')
            parent = fd
        data = os.pread(pins[-1][0], limit + 1, 0)
        if not 1 <= len(data) <= limit: raise ValueError('bounded root record required')
        for fd, parent, name, directory, before in pins:
            fields = ('st_dev', 'st_ino', 'st_uid', 'st_gid', 'st_mode')
            if not directory: fields += ('st_nlink', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
            for after in (os.fstat(fd), os.stat(name, dir_fd=parent, follow_symlinks=False)):
                if any(getattr(before, k) != getattr(after, k) for k in fields):
                    raise ValueError('root record changed')
        return data
    finally:
        for fd, *_ in reversed(pins): os.close(fd)


def read_json(path):
    raw = root_bytes(path)
    return json.loads(raw.decode('ascii'), object_pairs_hook=closed_object), digest(raw)


def address(value):
    if not isinstance(value, str): raise ValueError('canonical address required')
    parsed = ipaddress.ip_address(value)
    if str(parsed) != value or '%' in value or getattr(parsed, 'ipv4_mapped', None):
        raise ValueError('canonical non-mapped address required')
    return parsed


def validate_facts(facts, now=None):
    now = time.time() if now is None else now
    required = {'schema', 'verified_by', 'evidence', 'verified_at', 'valid_until', 'boot_id',
                'unified_cgroup_v2', 'nft_socket_cgroupv2', 'nft_inet', 'conntrack_direction',
                'namespace_ids', 'nft_version', 'legacy_user_unit_disabled', 'dns', 'ingress_peers', 'host_addresses', 'control_networks'}
    if not isinstance(facts, dict) or set(facts) != required or facts['schema'] != 'finch-network-facts-v1':
        raise ValueError('complete verified network facts required')
    if any(facts[k] is not True for k in ('unified_cgroup_v2', 'nft_socket_cgroupv2', 'nft_inet', 'conntrack_direction', 'legacy_user_unit_disabled')):
        raise ValueError('required receiving features unverified')
    for key in ('verified_by', 'evidence', 'nft_version'):
        if not isinstance(facts[key], str) or not facts[key].strip() or len(facts[key]) > 512:
            raise ValueError('verification provenance required')
    if (any(type(facts[k]) not in (int, float) or not math.isfinite(facts[k]) for k in ('verified_at', 'valid_until'))
            or not facts['verified_at'] <= now < facts['valid_until'] <= facts['verified_at'] + 86400):
        raise ValueError('fresh bounded receiving facts required')
    if not isinstance(facts['boot_id'], str) or not re.fullmatch(r'[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}', facts['boot_id']):
        raise ValueError('verified boot identity required')
    ns = facts['namespace_ids']
    if not isinstance(ns, dict) or set(ns) != {'net', 'cgroup'} or any(type(v) is not int or v <= 0 for v in ns.values()):
        raise ValueError('verified host namespaces required')
    for field in ('dns', 'ingress_peers', 'host_addresses', 'control_networks'):
        if not isinstance(facts[field], list) or len(facts[field]) > 64:
            raise ValueError('bounded explicit network list required')
    if not facts['dns'] or not facts['ingress_peers'] or not facts['host_addresses']:
        raise ValueError('resolver, tunnel peer and host addresses cannot be guessed')
    seen = set()
    for dns in facts['dns']:
        if (not isinstance(dns, dict) or set(dns) != {'address', 'port', 'transports'} or dns['port'] != 53
                or not isinstance(dns['transports'], list) or not dns['transports']
                or len(set(dns['transports'])) != len(dns['transports']) or not set(dns['transports']) <= {'tcp', 'udp'}):
            raise ValueError('exact DNS UDP/TCP53 facts required')
        ip = address(dns['address'])
        if ip.is_unspecified or ip.is_multicast or str(ip) in METADATA or str(ip) in seen:
            raise ValueError('unsafe or duplicate resolver')
        seen.add(str(ip))
    for peer in facts['ingress_peers']:
        ip = address(peer)
        if ip.version != 4 or not ip.is_loopback:
            raise ValueError('only verified IPv4 loopback tunnel peers supported')
    if len(set(facts['ingress_peers'])) != len(facts['ingress_peers']): raise ValueError('duplicate peer')
    for value in facts['host_addresses']: address(value)
    for value in facts['control_networks']:
        if not isinstance(value, str) or str(ipaddress.ip_network(value, strict=True)) != value:
            raise ValueError('canonical control network required')
    return facts


def blocked_networks(facts, version):
    values = [ipaddress.ip_network(s) for s in (BLOCK4 if version == 4 else BLOCK6)]
    for value in [*facts['host_addresses'], *facts['control_networks']]:
        parsed = ipaddress.ip_network(value, strict=True)
        if parsed.version == version: values.append(parsed)
    return list(ipaddress.collapse_addresses(values))


def render(facts):
    validate_facts(facts)
    lines = [f'table inet {TABLE} {{', f'  set {SET} {{ type cgroupsv2; }}']
    for version in (4, 6):
        elements = ', '.join(str(n) for n in blocked_networks(facts, version))
        lines.append(f'  set blocked{version} {{ type ipv{version}_addr; flags interval; elements = {{ {elements} }}; }}')
    peers = ', '.join(facts['ingress_peers'])
    lines += ['  chain output {', '    type filter hook output priority 10; policy accept;',
              f'    socket cgroupv2 level 2 @{SET} jump finch_output', '  }', '  chain finch_output {',
              '    ct state invalid counter drop comment "finch:invalid"',
              '    ct direction reply ct state established ct original ip daddr 127.0.0.1 '
              f'ct original proto-dst 18806 ip saddr 127.0.0.1 ip daddr {{ {peers} }} '
              'tcp sport 18806 counter accept comment "finch:incoming-http-reply"',
              '    ip daddr 169.254.169.254 counter reject with icmpx type admin-prohibited comment "finch:metadata4"',
              '    ip6 daddr fd00:ec2::254 counter reject with icmpx type admin-prohibited comment "finch:metadata6"']
    for dns in facts['dns']:
        family = 'ip' if address(dns['address']).version == 4 else 'ip6'
        for transport in dns['transports']:
            lines.append(f'    ct direction original ct state {{ new, established }} {family} daddr {dns["address"]} '
                         f'{transport} dport 53 counter accept comment "finch:verified-dns"')
    lines += ['    fib daddr type != unicast counter reject with icmpx type admin-prohibited comment "finch:local-route"',
              '    ip daddr @blocked4 counter reject with icmpx type admin-prohibited comment "finch:blocked4"',
              '    ip6 daddr @blocked6 counter reject with icmpx type admin-prohibited comment "finch:blocked6"',
              '    ip6 daddr != 2000::/3 counter reject with icmpx type admin-prohibited comment "finch:nonpublic6"',
              '    ct direction original ct state { new, established } tcp dport 443 counter accept comment "finch:public-https"',
              '    counter reject with icmpx type admin-prohibited comment "finch:default-deny"', '  }', '}']
    return '\n'.join(lines) + '\n'


def packet_allowed(facts, packet):
    """Offline predicate oracle, not proof of deployed kernel enforcement."""
    validate_facts(facts)
    if packet.get('cgroup_match') is not True: return True, 'unrelated-cgroup'
    if packet.get('state') == 'invalid': return False, 'invalid'
    dst = ipaddress.ip_address(packet['destination'])
    dst = getattr(dst, 'ipv4_mapped', None) or dst
    if (packet.get('direction') == 'reply' and packet.get('state') == 'established'
            and packet.get('original_destination') == '127.0.0.1' and packet.get('original_port') == 18806
            and packet.get('source') == '127.0.0.1' and str(dst) in facts['ingress_peers']
            and packet.get('protocol') == 'tcp' and packet.get('source_port') == 18806):
        return True, 'incoming-http-reply'
    if str(dst) in METADATA: return False, 'metadata'
    original = packet.get('direction') == 'original' and packet.get('state') in {'new', 'established'}
    for dns in facts['dns']:
        if original and str(dst) == dns['address'] and packet.get('port') == 53 and packet.get('protocol') in dns['transports']:
            return True, 'verified-dns'
    if packet.get('route_type') != 'unicast': return False, 'local-route'
    if any(dst in n for n in blocked_networks(facts, dst.version)): return False, 'blocked-address'
    if dst.version == 6 and dst not in ipaddress.ip_network('2000::/3'): return False, 'nonpublic6'
    return (True, 'public-https') if original and packet.get('protocol') == 'tcp' and packet.get('port') == 443 else (False, 'default-deny')


def run(argv, *, data=None):
    result = subprocess.run(argv, input=data, text=True, capture_output=True, env=ENV, timeout=15, check=False)
    if result.returncode: raise ValueError('vendor command failed; no automatic effect retry')
    return result.stdout


def kernel_table():
    return json.loads(run([NFT, '--json', '--numeric', 'list', 'table', 'inet', TABLE]))


def table_fingerprint(value):
    """Preserve rule order and all policy fields; ignore counters/handles and
    dynamic membership only, which is checked separately at each admission.
    """
    def clean(item):
        if isinstance(item, list): return [clean(v) for v in item]
        if not isinstance(item, dict): return item
        out = {k: clean(v) for k, v in item.items() if k != 'handle'}
        if 'counter' in out and isinstance(out['counter'], dict):
            out['counter'] = {k: v for k, v in out['counter'].items() if k not in {'packets', 'bytes'}}
        if 'set' in out and out['set'].get('name') == SET:
            out['set'].pop('elem', None)
        return out
    if not isinstance(value, dict) or set(value) != {'nftables'} or not isinstance(value['nftables'], list):
        raise ValueError('unknown nft readback format')
    return digest(canonical(clean([v for v in value['nftables'] if 'metainfo' not in v])).encode())


def membership(value):
    sets = [v['set'] for v in value.get('nftables', []) if isinstance(v, dict) and 'set' in v and v['set'].get('name') == SET]
    if len(sets) != 1 or sets[0].get('type') != 'cgroupsv2': raise ValueError('missing cgroup set')
    elements = sets[0].get('elem', [])
    # nft may render a live cgroupsv2 element as its exact canonical path.
    # Resolve only this fixed root-administered service path, never input paths.
    if isinstance(elements, list):
        elements = [os.stat('/sys/fs/cgroup' + CGROUP).st_ino if v == CGROUP[1:] else v for v in elements]
    if not isinstance(elements, list) or any(type(v) is not int or v <= 0 for v in elements):
        raise ValueError('numeric cgroup membership required')
    if len(set(elements)) != len(elements): raise ValueError('duplicate cgroup membership')
    return elements


def verify_kernel_facts(facts):
    if Path('/proc/sys/kernel/random/boot_id').read_text().strip() != facts['boot_id']:
        raise ValueError('receiving boot changed')
    for name, expected in facts['namespace_ids'].items():
        current = os.stat('/proc/self/ns/' + name)
        host = os.stat('/proc/1/ns/' + name)
        if current.st_ino != expected or current.st_ino != host.st_ino or current.st_dev != host.st_dev:
            raise ValueError('receiving namespace changed')
    if run([NFT, '--version']).strip() != facts['nft_version']:
        raise ValueError('nft version changed')


def privileged_inputs():
    if (os.geteuid() != 0 or Path(__file__) != PROGRAM or not BOOTSTRAP_OK
            or not sys.flags.isolated or not sys.flags.no_site or not sys.flags.dont_write_bytecode
            or root_bytes(PROGRAM, 65536) != SOURCE):
        raise ValueError('verified root-owned isolated helper required')
    contract, contract_sha = read_json(CONTRACT)
    facts, facts_sha = read_json(FACTS)
    validate_facts(facts)
    if contract.get('network_guard_sha256') != SOURCE_SHA256 or contract.get('network_facts_sha256') != facts_sha:
        raise ValueError('receiving network source/facts not accepted')
    verify_kernel_facts(facts)
    return facts, facts_sha, contract_sha


def write_root(path, value):
    # Fixed destinations only. Root-owned parent custody must pre-exist.
    if path not in (RECEIPT, ADMISSION): raise ValueError('fixed receipt target required')
    for parent in (path.parent, *path.parent.parents):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError('root receipt parent required')
    if path.exists(): root_bytes(path)
    temp = path.with_name('.' + path.name + '.' + secrets.token_hex(12))
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(canonical(value).encode()); output.flush(); os.fsync(output.fileno())
        os.replace(temp, path)
        parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try: os.fsync(parent_fd)
        finally: os.close(parent_fd)
    finally:
        if temp.exists(): temp.unlink()


@contextmanager
def owner_lock():
    # Parent's approved installation creates this root-owned directory/file.
    path = Path('/run/finch-mcp/network.lock')
    for parent in (path.parent, *path.parent.parents):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError('root lock ancestry required')
    fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077 or info.st_nlink != 1:
            raise ValueError('root network lock custody required')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally: os.close(fd)


def install(approval_reference):
    if not isinstance(approval_reference, str) or not 1 <= len(approval_reference) <= 512:
        raise ValueError('existing approved runtime-flow reference required; this is not approval')
    facts, facts_sha, contract_sha = privileged_inputs()
    with owner_lock():
        # Parent must have drained and disabled the legacy user unit separately.
        status = run(['/usr/bin/systemctl', 'show', UNIT, '--property=ActiveState', '--value']).strip()
        if status not in {'inactive', 'failed'}: raise ValueError('system service must be fully stopped')
        if facts.get('legacy_user_unit_disabled') is not True:
            # Intentionally not inferable from an absence of current packets.
            raise ValueError('verified legacy-user exclusion required')
        text = render(facts)
        previous = None
        existing = json.loads(run([NFT, '--json', 'list', 'tables']))
        present = any(v.get('table', {}).get('family') == 'inet' and v.get('table', {}).get('name') == TABLE
                      for v in existing['nftables'])
        if RECEIPT.exists():
            previous, _ = read_json(RECEIPT)
            if previous.get('schema') != 'finch-network-installed-v1':
                raise ValueError('unknown installed receipt')
            if previous.get('boot_id') != facts['boot_id'] and approval_reference == previous.get('approval_reference'):
                raise ValueError('new boot requires a current explicit adoption approval reference')
            if present:
                table = kernel_table()
                if table_fingerprint(table) != previous.get('kernel_sha256') or membership(table):
                    raise ValueError('managed table preimage changed or service member remains')
            elif (not re.fullmatch(r'[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}', previous.get('boot_id', ''))
                  or previous['boot_id'] == facts['boot_id']):
                raise ValueError('unexplained same-boot table loss; no automatic repair')
        elif present:
            raise ValueError('unowned table collision')
        transaction = (f'delete table inet {TABLE}\n' if present else '') + text
        run([NFT, '--check', '--file', '-'], data=transaction)
        run([NFT, '--file', '-'], data=transaction)  # One atomic scoped transaction; never flush ruleset.
        actual = kernel_table()
        if membership(actual): raise ValueError('unexpected cgroup member after installation')
        write_root(RECEIPT, {'schema': 'finch-network-installed-v1', 'boot_id': facts['boot_id'], 'facts_sha256': facts_sha,
                            'guard_sha256': SOURCE_SHA256, 'render_sha256': digest(text.encode()),
                            'kernel_sha256': table_fingerprint(actual), 'contract_sha256': contract_sha,
                            'approval_reference': approval_reference})


def admit():
    facts, facts_sha, contract_sha = privileged_inputs()
    with owner_lock():
        if Path('/proc/self/cgroup').read_text().strip() != '0::' + CGROUP:
            raise ValueError('wrong or ambiguous service cgroup')
        group = Path('/sys/fs/cgroup' + CGROUP)
        info = group.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError('service cgroup custody unsafe')
        stored, _ = read_json(RECEIPT)
        actual = kernel_table()
        if (stored.get('schema') != 'finch-network-installed-v1' or stored.get('boot_id') != facts['boot_id']
                or stored.get('facts_sha256') != facts_sha
                or stored.get('guard_sha256') != SOURCE_SHA256 or stored.get('contract_sha256') != contract_sha
                or stored.get('render_sha256') != digest(render(facts).encode())
                or stored.get('kernel_sha256') != table_fingerprint(actual) or membership(actual) != [info.st_ino]):
            raise ValueError('missing, stale or incomplete kernel network coverage')
        write_root(ADMISSION, {'schema': 'finch-network-admission-v1', 'unit': UNIT, 'cgroup': CGROUP,
                              'cgroup_id': info.st_ino, 'boot_id': facts['boot_id'], 'facts_sha256': facts_sha,
                              'guard_sha256': SOURCE_SHA256, 'contract_sha256': contract_sha,
                              'kernel_sha256': stored['kernel_sha256'], 'expires': facts['valid_until'],
                              'nonce': secrets.token_hex(16)})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['render', 'install', 'admit'])
    parser.add_argument('--approval-reference')
    args = parser.parse_args()
    try:
        if args.mode == 'render':
            facts, _ = read_json(FACTS); print(render(facts), end='')
        elif args.mode == 'install': install(args.approval_reference)
        else: admit()
        return 0
    except Exception:
        print('Finch network operation refused; verify source, facts, custody and existing approval. No automatic retry.', file=sys.stderr)
        return 78


if __name__ == '__main__':
    raise SystemExit(main())
