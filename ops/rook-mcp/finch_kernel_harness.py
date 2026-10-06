#!/usr/bin/python3
"""Opt-in, parent-approved target-kernel fixtures. NEVER ordinary CI/runtime startup.

packet-fixture: disposable network namespaces/cgroup, synthetic sinks only.
admission-fixture: explicitly approved STOPPED production SYSTEM unit fixture;
replaces downstream application commands, never executes Finch or reads tokens.
These are separate claims. An isolated namespace cannot pass real admission.
"""
import argparse
from contextlib import contextmanager
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import select
import signal
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
import time

PROGRAM = Path('/usr/local/libexec/finch-mcp/finch_kernel_harness.py')
NETWORK = Path('/usr/local/libexec/finch-mcp/finch_network.py')
NETWORK_SHA = 'f006d767cb4bae30ad4fb919c4ac06104dc72fe2b61bfc894052b7f051669155'
UNIT_PATH = Path('/etc/systemd/system/finch-mcp-adapter.service')
UNIT_SHA = '37fb96ae615a652778adc8e3b3e0c1c909a63ed2e2545762596e24cd6800d243'
DROPIN_DIR = Path('/run/systemd/system/finch-mcp-adapter.service.d')
DROPIN = DROPIN_DIR / '90-finch-kernel-fixture.conf'
ENV = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}
IP, NFT, SYSTEMCTL = '/usr/sbin/ip', '/usr/sbin/nft', '/usr/bin/systemctl'
SOURCE = Path(__file__).read_bytes()
BOOTSTRAP = sys._getframe().f_code == compile(SOURCE, __file__, 'exec', dont_inherit=True, optimize=sys.flags.optimize)
DEADLINE = None
OWNED_NAMESPACES = {}
OWNED_CGROUPS = {}
DNS_QUERY = bytes.fromhex('123401000001000000000000') + b'\x05finch\x07invalid\x00\x00\x01\x00\x01'
PAYLOAD = b'finch-owned-synthetic-sink'
# (tag, namespace side, address, port, protocol). No real endpoints or routes.
SINKS = [
    ('dns-tcp', 'client', '127.0.0.53', 53, 'tcp'), ('dns-udp', 'client', '127.0.0.53', 53, 'udp'),
    ('tls4', 'sink', '1.1.1.1', 443, 'tls'), ('tls6', 'sink', '2606:4700:4700::1111', 443, 'tls'),
    ('loopback-broker', 'client', '127.0.0.2', 22, 'tcp'),
    ('local-public', 'client', '8.7.6.5', 443, 'tcp'),
    ('private', 'sink', '10.1.2.3', 443, 'tcp'), ('tailnet', 'sink', '100.64.1.2', 443, 'tcp'),
    ('metadata4', 'sink', '169.254.169.254', 80, 'tcp'), ('metadata6', 'sink', 'fd00:ec2::254', 80, 'tcp'),
    ('udp-broker', 'client', '127.0.0.2', 9120, 'udp'),
    ('loopback6', 'client', '::1', 8443, 'tcp'),
    ('private6', 'sink', 'fd12:3456::1', 443, 'tcp'),
    ('linklocal4', 'sink', '169.254.42.42', 443, 'tcp'),
    ('incoming-http', 'client', '127.0.0.1', 18806, 'tcp')]


def require(value, message):
    if not value: raise ValueError(message)


def cleanup_budget():
    global DEADLINE
    DEADLINE = time.monotonic() + 30


def root_directory(path):
    for parent in (path, *path.parents):
        value = parent.lstat()
        require(stat.S_ISDIR(value.st_mode) and value.st_uid == 0 and not value.st_mode & 0o022, 'root-owned nonwritable ancestry required')


def timeout():
    left = 20 if DEADLINE is None else DEADLINE - time.monotonic()
    require(left > 0, 'bounded harness deadline exceeded')
    return min(20, left)


def identity(path):
    info = path.stat()
    return info.st_dev, info.st_ino


def verify_namespace(ns):
    path = Path('/run/netns') / ns
    require(ns in OWNED_NAMESPACES, 'namespace was not created by this invocation')
    entry = path.lstat()
    require(stat.S_ISREG(entry.st_mode) and entry.st_uid == 0, 'namespace path replaced or symlinked')
    require(identity(path) == OWNED_NAMESPACES[ns],
            'namespace creation identity changed; manual recovery required')


def command(argv, *, data=None, check=True):
    if argv and argv[0] == IP and '-n' in argv:
        verify_namespace(argv[argv.index('-n') + 1])
    result = subprocess.run([str(v) for v in argv], input=data, text=True, capture_output=True,
                            env=ENV, timeout=timeout(), close_fds=True)
    if check: require(result.returncode == 0, 'vendor command failed: ' + str(argv[:4]))
    return result.stdout


def in_ns(ns, argv, **kwargs):
    require(re.fullmatch(r'finch-(client|sink)-[a-f0-9]{12}', ns), 'owned namespace name required')
    verify_namespace(ns)
    return command([IP, 'netns', 'exec', ns, *argv], **kwargs)


def load_network():
    # Native sha256sum + independently verified root import closure must precede
    # executing this harness. Recheck descriptor-pinned source once imported.
    raw = NETWORK.read_bytes()
    require(hashlib.sha256(raw).hexdigest() == NETWORK_SHA, 'accepted guard bytes required')
    spec = importlib.util.spec_from_file_location('finch_kernel_network', NETWORK)
    value = importlib.util.module_from_spec(spec)
    exec(compile(raw, str(NETWORK), 'exec', dont_inherit=True), value.__dict__)
    require(value.root_bytes(NETWORK, 65536) == raw, 'guard root custody changed')
    return value


@contextmanager
def exclusive_fixture():
    # Root-only singleton. A failed fixture retains the lock as a manual
    # recovery hold rather than permitting a blind rerun over uncertain state.
    root_directory(Path('/run'))
    path = Path('/run/finch-kernel-harness.lock')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    success = False
    try:
        os.write(fd, (str(os.getpid()) + '\n').encode()); os.fsync(fd)
        yield
        success = True
    finally:
        info = os.fstat(fd); os.close(fd)
        if success:
            current = path.lstat()
            require((current.st_dev, current.st_ino) == (info.st_dev, info.st_ino), 'fixture lock replaced')
            path.unlink()


def authorize(args):
    require(args.approved_fixture and isinstance(args.approval_reference, str)
            and 1 <= len(args.approval_reference) <= 512, 'explicit existing approved fixture flow required')
    require(os.geteuid() == 0 and Path(__file__) == PROGRAM and BOOTSTRAP
            and sys.flags.isolated and sys.flags.no_site and sys.flags.dont_write_bytecode,
            'installed root-owned isolated harness required')
    n = load_network()
    require(n.root_bytes(PROGRAM, 65536) == SOURCE, 'harness root custody changed')
    require(os.stat('/proc/self/ns/net').st_ino == os.stat('/proc/1/ns/net').st_ino
            and os.stat('/proc/self/ns/cgroup').st_ino == os.stat('/proc/1/ns/cgroup').st_ino,
            'controller must begin in verified host namespaces')
    return n


def counters(ns, table):
    value = json.loads(in_ns(ns, [NFT, '--json', '--numeric', 'list', 'table', 'inet', table]))
    result = {}
    for item in value['nftables']:
        rule = item.get('rule', {})
        label = rule.get('comment')
        if label is None: continue
        values = [expr['counter']['packets'] for expr in rule.get('expr', []) if 'counter' in expr]
        require(len(values) == 1 and type(values[0]) is int and label not in result, 'unique kernel counters required')
        result[label] = values[0]
    return result


def evidence(before, after, sink_before, sink_after, reason, allowed, succeeded):
    require(succeeded is allowed, 'unexpected socket result')
    require(after.get(reason, 0) > before.get(reason, 0), 'expected OUTPUT rule did not count packets')
    require(sink_after > sink_before if allowed else sink_after == sink_before,
            'positive sink not reached or denied sink observed packets')
    return {'allowed': allowed, 'socket_success': succeeded, 'output_rule': reason,
            'output_packets': after[reason] - before.get(reason, 0), 'sink_packets': sink_after - sink_before}


def input_policy(sinks):
    lines = ['table inet finch_probe_input { chain input { type filter hook input priority -10; policy accept;']
    for tag, _, address, port, protocol in sinks:
        family = 'ip6' if ':' in address else 'ip'
        transport = 'udp' if protocol == 'udp' else 'tcp'
        lines.append(f'{family} daddr {address} {transport} dport {port} counter comment "sink:{tag}"')
    return '\n'.join([*lines, '} }\n'])


def response(tag, data):
    if tag.startswith('dns-'):
        query = data[2:] if tag == 'dns-tcp' else data
        require(query == DNS_QUERY, 'only synthetic .invalid DNS query supported')
        reply = query[:2] + b'\x81\x80' + query[4:]
        return len(reply).to_bytes(2, 'big') + reply if tag == 'dns-tcp' else reply
    if tag == 'incoming-http':
        return b'HTTP/1.0 200 OK\r\nContent-Length: 2\r\n\r\nok'
    return data


def process_start(pid):
    return Path('/proc/' + str(pid) + '/stat').read_text().rpartition(') ')[2].split()[19]


def worker_context(fd):
    require(type(fd) is int and fd > 2, 'inherited controller context fd required')
    info = os.fstat(fd)
    require(stat.S_ISFIFO(info.st_mode) and info.st_uid == 0, 'root controller pipe required')
    require(select.select([fd], [], [], 3)[0], 'controller context timeout')
    raw = os.read(fd, 4097)
    require(select.select([fd], [], [], 3)[0], 'controller did not close context pipe')
    require(0 < len(raw) <= 4096 and os.read(fd, 1) == b'', 'bounded one-shot controller context required')
    os.close(fd)
    context = json.loads(raw)
    require(set(context) == {'parent_pid', 'parent_start', 'namespace', 'namespace_identity', 'cgroup', 'source_sha256'},
            'closed internal fixture context required')
    parent = os.getppid()
    require(context['parent_pid'] == parent and context['parent_start'] == process_start(parent), 'controller lifetime changed')
    args = Path('/proc/' + str(parent) + '/cmdline').read_bytes().split(b'\0')
    expected = [b'/usr/bin/python3', b'-I', b'-S', b'-B', str(PROGRAM).encode(), b'packet-fixture']
    require(args[:6] == expected and b'--approved-fixture' in args and b'--approval-reference' in args,
            'worker parent is not an explicitly approved isolated packet fixture')
    ns = context['namespace']
    require(isinstance(ns, str) and re.fullmatch(r'finch-(client|sink)-[a-f0-9]{12}', ns), 'owned fixture namespace required')
    expected_id = tuple(context['namespace_identity'])
    require(identity(Path('/proc/self/ns/net')) == expected_id == identity(Path('/run/netns') / ns)
            and identity(Path('/proc/self/ns/net')) != identity(Path('/proc/1/ns/net')),
            'worker is not in its controller-created namespace')
    require(Path('/proc/self/cgroup').read_text().strip() == context['cgroup']
            and context['source_sha256'] == hashlib.sha256(SOURCE).hexdigest(), 'worker placement/source mismatch')
    return context


def worker(context_fd):
    # Internal workers are always inside an owned fixture namespace, never the
    # real service-admission phase. They wait on stdin until cgroup placement.
    require(os.geteuid() == 0 and Path(__file__) == PROGRAM and BOOTSTRAP
            and sys.flags.isolated and sys.flags.no_site and sys.flags.dont_write_bytecode,
            'installed isolated privileged fixture worker required')
    worker_context(context_fd)  # Before reading a job or creating any socket.
    signal.alarm(160)  # Bounded even if the approved controller disappears.
    raw = sys.stdin.buffer.readline(32769)
    require(len(raw) <= 32768, 'bounded fixture command required')
    job = json.loads(raw)
    valid = {s[0]: s for s in SINKS}
    if job['role'] == 'server':
        sockets = []
        for tag in job['tags']:
            _, _, address, port, protocol = valid[tag]
            af = socket.AF_INET6 if ':' in address else socket.AF_INET
            sock = socket.socket(af, socket.SOCK_DGRAM if protocol == 'udp' else socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if af == socket.AF_INET6: sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            sock.bind((address, port)); sock.settimeout(0.3); sockets.append(sock)
            context = None
            if protocol != 'udp': sock.listen(8)
            if protocol == 'tls':
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                context.load_cert_chain(job['certificate'], job['key'])
            def serve(s=sock, kind=protocol, label=tag, ctx=context):
                until = time.monotonic() + 150
                while time.monotonic() < until:
                    try:
                        if kind == 'udp':
                            data, peer = s.recvfrom(4096); s.sendto(response(label, data), peer)
                        else:
                            conn, _ = s.accept(); conn.settimeout(2)
                            try:
                                if ctx: conn = ctx.wrap_socket(conn, server_side=True)
                                size = len(DNS_QUERY) + 2 if label == 'dns-tcp' else len(b'GET /synthetic HTTP/1.0\r\n\r\n') if label == 'incoming-http' else len(PAYLOAD)
                                data = b''
                                while len(data) < size:
                                    part = conn.recv(size - len(data))
                                    if not part: break
                                    data += part
                                conn.sendall(response(label, data))
                            finally: conn.close()
                    except (TimeoutError, OSError, ValueError): pass
            threading.Thread(target=serve, daemon=True).start()
        print(json.dumps({'ready': True}), flush=True)
        # Controller closes stdin/terminates only its own fixture children.
        sys.stdin.buffer.readline()
        for sock in sockets: sock.close()
        return
    require(job['role'] == 'client' and job['tag'] in valid, 'fixed fixture client required')
    tag, _, address, port, protocol = valid[job['tag']]
    if job.get('mapped'):
        require(':' not in address, 'IPv4 mapping requires IPv4 target'); address = '::ffff:' + address
    af = socket.AF_INET6 if ':' in address else socket.AF_INET
    sock = socket.socket(af, socket.SOCK_DGRAM if protocol == 'udp' else socket.SOCK_STREAM)
    sock.settimeout(1.5)
    success = False
    try:
        if job.get('source_port'):
            require(job['source_port'] == 18806 and af == socket.AF_INET, 'fixed source-port bypass fixture only')
            sock.bind(('127.0.0.3', 18806))
        query = DNS_QUERY if tag.startswith('dns-') else b'GET /synthetic HTTP/1.0\r\n\r\n' if tag == 'incoming-http' else PAYLOAD
        if tag == 'dns-tcp': query = len(query).to_bytes(2, 'big') + query
        if protocol == 'udp':
            sock.sendto(query, (address, port))  # Explicitly UNCONNECTED UDP.
            received = sock.recvfrom(4096)[0]
        else:
            sock.connect((address, port))
            if protocol == 'tls':
                context = ssl.create_default_context(cafile=job['certificate'])
                sock = context.wrap_socket(sock, server_hostname='finch.invalid')
            sock.sendall(query); received = b''
            size = len(response(tag, query))
            while len(received) < size:
                part = sock.recv(size - len(received))
                if not part: break
                received += part
        success = received == response(tag, query)
    except (OSError, ValueError): pass
    finally: sock.close()
    print(json.dumps({'success': success, 'unconnected_udp': protocol == 'udp', 'self_signed_fixture_tls': protocol == 'tls'}), flush=True)


def start_worker(ns, job, children, group=None, server=False):
    verify_namespace(ns)
    read_fd, write_fd = os.pipe2(os.O_CLOEXEC)
    try:
        proc = subprocess.Popen([IP, 'netns', 'exec', ns, '/usr/bin/python3', '-I', '-S', '-B', str(PROGRAM),
                                 '_worker', '--internal-context-fd', str(read_fd)],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                pass_fds=(read_fd,), env=ENV, text=True, close_fds=True)
        children.append(proc)
        if group:
            require(str(group) in OWNED_CGROUPS and identity(group) == OWNED_CGROUPS[str(group)], 'cgroup creation identity changed')
            (group / 'cgroup.procs').write_text(str(proc.pid))
            expected_group = '0::' + str(group).removeprefix('/sys/fs/cgroup')
            require(Path(f'/proc/{proc.pid}/cgroup').read_text().strip() == expected_group,
                    'worker cgroup placement failed before command/socket creation')
        else: expected_group = Path('/proc/self/cgroup').read_text().strip()
        context = dict(parent_pid=os.getpid(), parent_start=process_start(os.getpid()), namespace=ns,
                       namespace_identity=OWNED_NAMESPACES[ns], cgroup=expected_group,
                       source_sha256=hashlib.sha256(SOURCE).hexdigest())
        raw = json.dumps(context).encode()
        require(len(raw) <= 4096 and os.write(write_fd, raw) == len(raw), 'internal context write failed')
    finally:
        os.close(read_fd); os.close(write_fd)
    proc.stdin.write(json.dumps(job) + '\n'); proc.stdin.flush()
    ready, _, _ = select.select([proc.stdout], [], [], timeout())
    require(ready, 'fixture worker did not answer')
    line = proc.stdout.readline()
    require(line, 'fixture worker exited without evidence')
    value = json.loads(line)
    if server: require(value == {'ready': True}, 'sink readiness required')
    else: require(proc.wait(timeout=timeout()) == 0, 'fixture client failed')
    return value


def fixture_facts(n):
    now = time.time()
    # Render-only synthetic facts. Never published into real receiving custody.
    return dict(schema='finch-network-facts-v1', verified_by='isolated-synthetic-fixture', evidence='NOT-production-admission',
                verified_at=now - 1, valid_until=now + 300, boot_id='00000000-0000-0000-0000-000000000001',
                unified_cgroup_v2=True, nft_socket_cgroupv2=True, nft_inet=True, conntrack_direction=True,
                legacy_user_unit_disabled=True, namespace_ids={'net': 1, 'cgroup': 1}, nft_version='render-only-fixture',
                dns=[{'address': '127.0.0.53', 'port': 53, 'transports': ['tcp', 'udp']}], ingress_peers=['127.0.0.1'],
                host_addresses=['127.0.0.1', '192.0.2.1', '2001:db8:123::1'], control_networks=[])


def packet_fixture(n):
    token = os.urandom(6).hex()
    namespaces = {'client': 'finch-client-' + token, 'sink': 'finch-sink-' + token}
    parent = Path('/sys/fs/cgroup/finch-kernel-' + token); group = parent / 'app'
    children, created_ns, evidence_rows = [], [], []
    root_directory(parent.parent)
    parent.mkdir(); OWNED_CGROUPS[str(parent)] = identity(parent)
    try:
        group.mkdir(); OWNED_CGROUPS[str(group)] = identity(group)
        for ns in namespaces.values():
            command([IP, 'netns', 'add', ns])
            OWNED_NAMESPACES[ns] = identity(Path('/run/netns') / ns); created_ns.append(ns)
            command([IP, '-n', ns, 'link', 'set', 'lo', 'up'])
        left, right = 'fc' + token[:8], 'fs' + token[:8]
        # Creation/movement are entirely in owned namespaces, no host links.
        in_ns(namespaces['client'], [IP, 'link', 'add', left, 'type', 'veth', 'peer', 'name', right, 'netns', namespaces['sink']])
        for side, dev, addr4, addr6 in [('client', left, '192.0.2.1/24', '2001:db8:123::1/64'),
                                      ('sink', right, '192.0.2.2/24', '2001:db8:123::2/64')]:
            ns = namespaces[side]
            command([IP, '-n', ns, 'addr', 'add', addr4, 'dev', dev])
            command([IP, '-n', ns, '-6', 'addr', 'add', addr6, 'dev', dev, 'nodad'])
            command([IP, '-n', ns, 'link', 'set', dev, 'up'])
        for address in sorted({s[2] for s in SINKS if s[1] == 'sink'} | {'8.7.6.5'}):
            side = 'client' if address == '8.7.6.5' else 'sink'
            prefix, family = ('/128', '-6') if ':' in address else ('/32', '-4')
            command([IP, '-n', namespaces[side], family, 'addr', 'add', address + prefix, 'dev', 'lo', *(['nodad'] if family == '-6' else [])])
            if side == 'sink':
                via = '2001:db8:123::2' if family == '-6' else '192.0.2.2'
                command([IP, '-n', namespaces['client'], family, 'route', 'add', address + prefix, 'via', via])
        # No default route/physical interface exists in either namespace.
        for ns in namespaces.values():
            for family in ('-4', '-6'):
                require(not command([IP, '-n', ns, family, 'route', 'show', 'default']).strip(), 'fixture has external route')
            own = [s for s in SINKS if namespaces[s[1]] == ns]
            in_ns(ns, [NFT, '--file', '-'], data=input_policy(own))
        facts = fixture_facts(n)
        text = n.render(facts) + f'add element inet {n.TABLE} {n.SET} {{ "{str(group).removeprefix("/sys/fs/cgroup/")}" }}\n'
        in_ns(namespaces['client'], [NFT, '--check', '--file', '-'], data=text)
        in_ns(namespaces['client'], [NFT, '--file', '-'], data=text)
        with tempfile.TemporaryDirectory(prefix='finch-synthetic-tls-') as tmp:
            os.chmod(tmp, 0o700)
            cert, key = str(Path(tmp) / 'fixture.crt'), str(Path(tmp) / 'fixture.key')
            in_ns(namespaces['sink'], ['/usr/bin/openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-sha256',
                                      '-config', '/dev/null', '-keyout', key, '-out', cert, '-days', '1', '-subj', '/CN=finch.invalid',
                                      '-addext', 'subjectAltName=DNS:finch.invalid'])
            for side in namespaces:
                tags = [s[0] for s in SINKS if s[1] == side and s[0] != 'incoming-http']
                start_worker(namespaces[side], dict(role='server', tags=tags, certificate=cert, key=key), children, server=True)
            start_worker(namespaces['client'], dict(role='server', tags=['incoming-http'], certificate=cert, key=key), children, group, server=True)
            cases = [(tag, True, 'finch:verified-dns', {}) for tag in ('dns-tcp', 'dns-udp')]
            cases += [(tag, True, 'finch:public-https', {}) for tag in ('tls4', 'tls6')]
            cases += [('incoming-http', True, 'finch:incoming-http-reply', {})]
            cases += [(tag, False, reason, {}) for tag, reason in [('loopback-broker', 'finch:local-route'),
                      ('local-public', 'finch:local-route'), ('private', 'finch:blocked4'), ('tailnet', 'finch:blocked4'),
                      ('metadata4', 'finch:metadata4'), ('metadata6', 'finch:metadata6'), ('udp-broker', 'finch:local-route'),
                      ('loopback6', 'finch:local-route'), ('private6', 'finch:nonpublic6'), ('linklocal4', 'finch:blocked4')]]
            cases += [('loopback-broker', False, 'finch:local-route', {'mapped': True}),
                      ('metadata4', False, 'finch:metadata4', {'mapped': True}),
                      ('loopback-broker', False, 'finch:local-route', {'source_port': 18806}),
                      ('udp-broker', False, 'finch:local-route', {'source_port': 18806})]
            for tag, allowed, reason, extra in cases:
                side = next(s[1] for s in SINKS if s[0] == tag); sink_ns = namespaces[side]
                job = dict(role='client', tag=tag, certificate=cert, **extra)
                # First prove each sink is reachable outside the selected cgroup.
                # Incoming HTTP is the reverse flow: listener itself is selected.
                control_before = counters(sink_ns, 'finch_probe_input')['sink:' + tag]
                policy_control_before = counters(namespaces['client'], n.TABLE)
                control_job = {k: v for k, v in job.items() if k != 'source_port'}
                control = start_worker(namespaces['client'], control_job, children)
                require(control['success'], 'unrelated-cgroup positive control failed')
                time.sleep(0.05)
                require(counters(sink_ns, 'finch_probe_input')['sink:' + tag] > control_before, 'control sink counter failed')
                if tag != 'incoming-http':
                    require(counters(namespaces['client'], n.TABLE) == policy_control_before, 'unrelated cgroup touched Finch rules')
                before = counters(namespaces['client'], n.TABLE)
                sink_before = counters(sink_ns, 'finch_probe_input')['sink:' + tag]
                # Incoming request originates outside; reply socket is inside.
                result = start_worker(namespaces['client'], job, children, None if tag == 'incoming-http' else group)
                time.sleep(0.05)
                row = evidence(before, counters(namespaces['client'], n.TABLE), sink_before,
                               counters(sink_ns, 'finch_probe_input')['sink:' + tag], reason, allowed, result['success'])
                evidence_rows.append({'case': tag, **extra, **row})
        return {'phase': 'isolated-packet-fixture', 'production_admission': False, 'cases': evidence_rows,
                'cgroup_id': group.stat().st_ino, 'self_signed_tls_only': True}
    finally:
        cleanup_budget()
        for proc in children:
            if proc.poll() is None:
                proc.terminate()
                try: proc.wait(timeout=3)
                except subprocess.TimeoutExpired: proc.kill(); proc.wait(timeout=3)
        # Delete only names/cgroups created by this invocation. Cleanup failure
        # is a failure, never a pass. No host firewall/service is touched here.
        for ns in reversed(created_ns):
            verify_namespace(ns)
            command([IP, 'netns', 'delete', ns]); del OWNED_NAMESPACES[ns]
        for path in (group, parent):
            if str(path) in OWNED_CGROUPS:
                require(identity(path) == OWNED_CGROUPS[str(path)], 'cgroup changed; no automatic removal')
                path.rmdir(); del OWNED_CGROUPS[str(path)]


def unit_property(name):
    return command([SYSTEMCTL, 'show', 'finch-mcp-adapter.service', '--property=' + name, '--value']).strip()


def marker_program(nonce, port):
    # No imports of Finch, auth/config/token reads, external target or retry.
    return ("import socket,signal;signal.alarm(120);print('FINCH_FIXTURE_APP_" + nonce + "',flush=True);"
            "s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);"
            "print('FINCH_FIXTURE_PACKET_" + nonce + "',flush=True);"
            "\ntry:s.sendto(b'finch-owned-fixture',('127.0.0.1'," + str(port) + "))\nexcept OSError:pass\n"
            "signal.pause()")


def quote_unit(value):
    # systemd quoted command argument, no shell or variable/specifier expansion.
    require('$' not in value and '%' not in value, 'unit substitution forbidden')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n') + '"'


def fixture_dropin(unit, mutation_file, nonce, port):
    conditions = [s for s in unit.splitlines() if s.startswith('ExecCondition=')]
    require(len(conditions) == 3 and all(s.startswith('ExecCondition=+') for s in conditions[:2])
            and '/finch_adapter_launch.py --check-only' in conditions[2], 'exact reviewed condition chain required')
    # Retain the exact native checksum and real admission conditions. The
    # application-only check condition/start are replaced, not executed.
    pre = ['ExecCondition=+/usr/bin/stat --format=FINCH_FIXTURE_CGROUP_' + nonce + '_%%i /sys/fs/cgroup/system.slice/finch-mcp-adapter.service']
    if mutation_file: pre.append('ExecCondition=+/usr/sbin/nft --file ' + str(mutation_file))
    return '\n'.join(['[Service]', 'Restart=no', 'ExecCondition=', *pre, *conditions[:2],
                      'ExecStart=', 'ExecStart=/usr/bin/python3 -I -S -B -c ' + quote_unit(marker_program(nonce, port)), ''])


def verify_effective_commands(start, conditions, mutation, nonce, port):
    def argv(value):
        result = re.findall(r'argv\[\]=(.*?) ; ignore_errors=no', value, flags=re.S)
        # systemctl versions may print the actual newline or its C escape.
        return [item.replace('\\n', '\n') for item in result]
    expected_start = '/usr/bin/python3 -I -S -B -c ' + marker_program(nonce, port)
    expected_conditions = ['/usr/bin/stat --format=FINCH_FIXTURE_CGROUP_' + nonce + '_%i /sys/fs/cgroup/system.slice/finch-mcp-adapter.service']
    if mutation: expected_conditions.append('/usr/sbin/nft --file ' + str(mutation))
    expected_conditions += ['/usr/bin/sha256sum --check --status /etc/finch-mcp/network-helper.sha256',
                            '/usr/bin/env -i PATH=/usr/bin:/bin LANG=C.UTF-8 /usr/bin/python3 -I -S -B /usr/local/libexec/finch-mcp/finch_network.py admit']
    require(argv(start) == [expected_start] and argv(conditions) == expected_conditions,
            'effective application/condition composition changed; no start')


def mutations(n, stale_path):
    name = str(stale_path).removeprefix('/sys/fs/cgroup/')
    return {
        'absent-table': f'delete table inet {n.TABLE}\n',
        'empty-membership': f'flush set inet {n.TABLE} {n.SET}\n',
        'stale-membership': f'flush set inet {n.TABLE} {n.SET}\nadd element inet {n.TABLE} {n.SET} {{ "{name}" }}\n',
        'extra-membership': f'add element inet {n.TABLE} {n.SET} {{ "{name}" }}\n',
        'changed-fingerprint': f'add rule inet {n.TABLE} finch_output counter drop comment "fixture-drift"\n',
    }


def skipped_evidence(properties, journal, nonce, previous, current):
    require(re.search(r'\{[^{}]*path=/usr/bin/env[^{}]*finch_network\.py admit[^{}]*status=78[^{}]*\}', properties['ExecCondition']),
            'real admission did not exit78')
    require(properties['ActiveState'] in {'inactive', 'failed'} and properties['MainPID'] == '0',
            'later application unexpectedly active')
    require('FINCH_FIXTURE_APP_' + nonce not in journal and 'FINCH_FIXTURE_PACKET_' + nonce not in journal,
            'post-admission application/packet marker ran')
    require(previous == current, 'refused admission changed its receipt')
    return {'admission_exit': 78, 'active_state': properties['ActiveState'], 'unit_result': properties['Result'],
            'later_app_marker': False, 'later_packet_marker': False,
            'note': 'ExecCondition78 may skip the unit without Result=failed'}


def host_counters(n):
    result = {}
    for item in n.kernel_table()['nftables']:
        rule = item.get('rule', {})
        if 'comment' in rule:
            for expr in rule.get('expr', []):
                if 'counter' in expr: result[rule['comment']] = expr['counter']['packets']
    return result


def fixture_membership(n, stale):
    # Guard itself remains strict. Only this observer translates its own
    # deliberately injected extra/stale fixture path for post-stop evidence.
    value = n.kernel_table()
    for item in value['nftables']:
        item = item.get('set', {})
        if item.get('name') == n.SET and isinstance(item.get('elem'), list):
            item['elem'] = [stale.stat().st_ino if v == str(stale).removeprefix('/sys/fs/cgroup/') else v for v in item['elem']]
    return n.membership(value)


def assert_owned_table_preimage(n, table, accepted_hash, case, stale):
    """Refuse unknown administrator changes before restoring anything.

    The parent must still exclude all other root network/unit writers during
    the fixture: the native CLI does not offer a caller-pinned atomic CAS.
    """
    cleaned = table
    if n.table_fingerprint(table) != accepted_hash:
        require(case == 'changed-fingerprint', 'unknown policy drift; no automatic restoration')
        rows = table['nftables']
        owned = [(i, item['rule']) for i, item in enumerate(rows)
                 if item.get('rule', {}).get('comment') == 'fixture-drift']
        require(len(owned) == 1, 'ambiguous fixture drift rule')
        index, rule = owned[0]
        expr = rule.get('expr', [])
        require(rule.get('family') == 'inet' and rule.get('table') == n.TABLE and rule.get('chain') == 'finch_output'
                and set(rule) <= {'family', 'table', 'chain', 'handle', 'comment', 'expr'}
                and len(expr) == 2 and set(expr[0]) == {'counter'} and expr[1] == {'drop': None}
                and index == max(i for i, item in enumerate(rows) if item.get('rule', {}).get('chain') == 'finch_output'),
                'changed drift rule is not this fixture preimage')
        cleaned = dict(table, nftables=rows[:index] + rows[index + 1:])
        require(n.table_fingerprint(cleaned) == accepted_hash, 'concurrent policy change; no automatic restoration')
    observed = []
    for item in table['nftables']:
        entry = item.get('set', {})
        if entry.get('name') == n.SET:
            observed = entry.get('elem', [])
            observed = [stale.stat().st_ino if v == str(stale).removeprefix('/sys/fs/cgroup/') else v for v in observed]
    expected = [stale.stat().st_ino] if case in {'stale-membership', 'extra-membership'} else []
    require(observed == expected, 'unknown membership change; no automatic restoration')


def admission_fixture(n, approved_production_unit):
    require(approved_production_unit, 'explicit approved STOPPED production-unit fixture required')
    facts, _, _ = n.privileged_inputs()  # REAL host namespace/custody guard; never mocked/weakened.
    unit_raw = n.root_bytes(UNIT_PATH, 16384)
    require(hashlib.sha256(unit_raw).hexdigest() == UNIT_SHA,
            'exact reviewed SYSTEM unit must already be provisioned')
    require(unit_property('FragmentPath') == str(UNIT_PATH) and not unit_property('DropInPaths'), 'unexpected unit/drop-ins')
    require(unit_property('ActiveState') == 'inactive' and unit_property('MainPID') == '0', 'Finch must already be gracefully stopped')
    require(not Path('/sys/fs/cgroup' + n.CGROUP).exists() and not n.membership(n.kernel_table()),
            'real service cgroup/member must already be removed')
    stored, _ = n.read_json(n.RECEIPT)
    require(stored.get('kernel_sha256') == n.table_fingerprint(n.kernel_table()), 'accepted kernel preimage required')
    accepted_render = n.render(facts)
    require(stored.get('render_sha256') == n.digest(accepted_render.encode()), 'installed rules differ from accepted rendering')
    require(not DROPIN.exists() and not DROPIN_DIR.exists(), 'refuse preexisting runtime drop-in directory')
    unit = unit_raw.decode('utf8')
    rows, token = [], os.urandom(6).hex()
    stale = Path('/sys/fs/cgroup/finch-admission-' + token)
    # These resources are explicit target-unit test effects, never silent setup.
    root_directory(stale.parent); root_directory(DROPIN_DIR.parent); root_directory(Path('/run/finch-mcp'))
    owned_dropin = made_directory = made_stale = False
    stale_identity = None
    table_touched = restore_failed = False
    expected_dropin = None
    known_case = None
    started_invocation = None
    udp = None
    def owned_sink():
        require(udp is not None and udp.getsockname() == ('127.0.0.1', port)
                and udp.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR) == 0
                and udp.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT) == 0,
                'owned exclusive loopback sink changed; no packet attempt')
    def receipt():
        return n.root_bytes(n.ADMISSION, 4096) if n.ADMISSION.exists() else None
    def journal(invocation):
        require(re.fullmatch(r'[a-f0-9]{32}', invocation), 'exact current invocation ID required')
        return command(['/usr/bin/journalctl', '_SYSTEMD_INVOCATION_ID=' + invocation, '--no-pager', '--output=cat'])
    def check_unit_preimage():
        if made_stale: require(identity(stale) == stale_identity, 'stale fixture cgroup changed; manual recovery required')
        require(hashlib.sha256(n.root_bytes(UNIT_PATH, 16384)).hexdigest() == UNIT_SHA
                and unit_property('FragmentPath') == str(UNIT_PATH), 'unit changed; manual recovery required')
        if owned_dropin:
            require(n.root_bytes(DROPIN, 16384).decode() == expected_dropin
                    and set(DROPIN_DIR.iterdir()) == {DROPIN}
                    and unit_property('DropInPaths') in {'', str(DROPIN)},
                    'drop-in changed; no overwrite or automatic cleanup')
    def stop_fixture():
        check_unit_preimage()
        # Only this invocation's replacement marker may be active. Never stop
        # or kill an actual Finch process/in-flight request on an error path.
        pid = unit_property('MainPID')
        if unit_property('ActiveState') == 'activating' or unit_property('ControlPID') != '0':
            require(started_invocation is not None and unit_property('InvocationID') == started_invocation,
                    'uncertain activating/control process ownership; manual recovery required')
        if pid != '0':
            require(token.encode() in Path('/proc/' + pid + '/cmdline').read_bytes(), 'foreign service process; manual recovery required')
        command([SYSTEMCTL, 'stop', n.UNIT])
        until = time.monotonic() + 3
        while Path('/sys/fs/cgroup' + n.CGROUP).exists() and time.monotonic() < until: time.sleep(0.05)
        require(unit_property('MainPID') == '0' and not Path('/sys/fs/cgroup' + n.CGROUP).exists(), 'fixture cgroup not removed after stop')
    def restore_table():
        nonlocal table_touched, restore_failed, known_case
        require(not restore_failed, 'table restoration failed; manual recovery required, no retry')
        tables = json.loads(command([NFT, '--json', 'list', 'tables']))['nftables']
        present = any(x.get('table', {}).get('family') == 'inet' and x.get('table', {}).get('name') == n.TABLE for x in tables)
        if present:
            assert_owned_table_preimage(n, n.kernel_table(), stored['kernel_sha256'], known_case, stale)
        else:
            require(known_case == 'absent-table', 'unexplained table loss; manual recovery required')
        table_touched = True
        try:
            command([NFT, '--file', '-'], data=(f'delete table inet {n.TABLE}\n' if present else '') + accepted_render)
            require(n.table_fingerprint(n.kernel_table()) == stored['kernel_sha256'] and not n.membership(n.kernel_table()),
                    'managed table restoration failed')
            known_case = None
        except Exception:
            restore_failed = True
            raise
    try:
        stale.mkdir(); made_stale = True; stale_identity = identity(stale)
        DROPIN_DIR.mkdir(mode=0o755); made_directory = True
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); udp.bind(('127.0.0.1', 0)); udp.settimeout(0.15)
        port = udp.getsockname()[1]
        owned_sink()
        with tempfile.TemporaryDirectory(prefix='finch-admission-fixture-', dir='/run/finch-mcp') as temp:
            changes = mutations(n, stale)
            last_nonce = None
            for case in ['positive-start', *changes, 'positive-restart']:
                stop_fixture()
                restore_table()
                nonce = token + '-' + case
                mutation = None
                if case in changes:
                    mutation = Path(temp) / (case + '.nft'); mutation.write_text(changes[case]); mutation.chmod(0o600)
                content = fixture_dropin(unit, mutation, nonce, port)
                if not owned_dropin:
                    fd = os.open(DROPIN, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
                    with os.fdopen(fd, 'w') as out: out.write(content)
                    owned_dropin = True
                else:
                    check_unit_preimage()
                    DROPIN.write_text(content)
                expected_dropin = content
                command([SYSTEMCTL, 'daemon-reload'])
                previous = receipt()
                command([SYSTEMCTL, 'reset-failed', n.UNIT])
                check_unit_preimage()
                require(unit_property('DropInPaths') == str(DROPIN), 'exact singleton effective fixture drop-in required')
                verify_effective_commands(unit_property('ExecStart'), unit_property('ExecCondition'), mutation, nonce, port)
                owned_sink()
                known_case = case
                command([SYSTEMCTL, 'start', n.UNIT], check=False)
                started_invocation = unit_property('InvocationID')
                require(re.fullmatch(r'[a-f0-9]{32}', started_invocation), 'current start invocation unknown')
                limit = time.monotonic() + 5
                while unit_property('ActiveState') == 'activating' and time.monotonic() < limit: time.sleep(0.05)
                props = {key: unit_property(key) for key in ('ExecCondition', 'ActiveState', 'MainPID', 'Result')}
                invocation = unit_property('InvocationID')
                current = receipt(); logs = journal(invocation)
                while 'FINCH_FIXTURE_CGROUP_' + nonce not in logs and time.monotonic() < limit:
                    time.sleep(0.05); logs = journal(invocation)
                observed = re.findall(r'FINCH_FIXTURE_CGROUP_' + re.escape(nonce) + r'_(\d+)', logs)
                require(len(observed) == 1, 'unique pre-admission current cgroup observation required')
                started_id = int(observed[0])
                if case in changes:
                    row = skipped_evidence(props, logs, nonce, previous, current)
                    if case != 'absent-table':
                        require(all(v == 0 for v in host_counters(n).values()), 'packet counted after refused admission')
                    row['before_guard_cgroup_id'] = started_id
                else:
                    require(props['ActiveState'] == 'active' and props['MainPID'] != '0', 'positive admission did not start marker')
                    # Type=simple start returns before main exec; wait boundedly for both markers.
                    while ('FINCH_FIXTURE_PACKET_' + nonce not in logs) and time.monotonic() < limit:
                        time.sleep(0.05); logs = journal(invocation)
                    require('FINCH_FIXTURE_APP_' + nonce in logs and 'FINCH_FIXTURE_PACKET_' + nonce in logs, 'missing post-admission markers')
                    data = json.loads(current)
                    actual_id = Path('/sys/fs/cgroup' + n.CGROUP).stat().st_ino
                    require(data['cgroup_id'] == actual_id == started_id and n.membership(n.kernel_table()) == [actual_id], 'current SYSTEM membership mismatch')
                    require(data['nonce'] != last_nonce, 'fresh admission nonce required on restart')
                    require(host_counters(n).get('finch:local-route', 0) > 0, 'post-admission synthetic packet did not hit deny rule')
                    last_nonce = data['nonce']
                    row = {'actual_cgroup_id': actual_id, 'nonce': last_nonce, 'later_app_marker': True,
                           'later_packet_marker': True, 'id_change_assumed': False}
                try: udp.recvfrom(1024); raise ValueError('owned denied UDP sink observed a packet')
                except socket.timeout: pass
                row['owned_denied_sink_packets'] = 0
                stop_fixture()
                if case != 'absent-table':
                    remaining = fixture_membership(n, stale)
                    expected = [stale.stat().st_ino] if case in {'stale-membership', 'extra-membership'} else []
                    require(started_id not in remaining and remaining == expected, 'systemd did not remove stopped service membership')
                rows.append({'case': case, **row})
                restore_table()
        return {'phase': 'real-SYSTEM-admission-fixture', 'actual_Finch_started': False,
                'production_adoption': False, 'cases': rows}
    finally:
        cleanup_budget()
        if udp: udp.close()
        if owned_dropin: stop_fixture()
        if table_touched: restore_table()
        if owned_dropin:
            check_unit_preimage()
            DROPIN.unlink()
        if made_directory: DROPIN_DIR.rmdir()
        if made_stale: stale.rmdir()
        if owned_dropin:
            command([SYSTEMCTL, 'daemon-reload'])
            require(not unit_property('DropInPaths') and unit_property('ActiveState') == 'inactive', 'unit restoration requires manual recovery')
        # Leave the latest nonsecret test admission with its now-removed cgroup;
        # never restore stale authority. A later real start must re-admit itself.


def main(argv=None):
    global DEADLINE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=['plan', 'packet-fixture', 'admission-fixture', '_worker'])
    parser.add_argument('--approved-fixture', action='store_true')
    parser.add_argument('--approved-stopped-production-unit', action='store_true')
    parser.add_argument('--approval-reference')
    parser.add_argument('--internal-context-fd', type=int, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.phase == 'plan':
        print(json.dumps({'packet-fixture': 'isolated namespaces/cgroup, synthetic DNS/TLS/HTTP and deny sinks; no production admission claim',
                          'admission-fixture': 'approved STOPPED real SYSTEM unit, temporary downstream marker override and managed-table fault injection; no real Finch/token/state access',
                          'approval': 'Separate exact runtime approval and verified root/native/import custody required; flags/references are not approval.'}))
        return 0
    try:
        if args.phase == '_worker': worker(args.internal_context_fd); return 0
        n = authorize(args)
        DEADLINE = time.monotonic() + 180
        def interrupted(*_): raise RuntimeError('fixture interrupted; cleanup required')
        signal.signal(signal.SIGTERM, interrupted)
        with exclusive_fixture():
            result = packet_fixture(n) if args.phase == 'packet-fixture' else admission_fixture(n, args.approved_stopped_production_unit)
        print(json.dumps({'result': 'PASS', 'approval_reference': args.approval_reference, **result}, sort_keys=True))
        return 0
    except Exception as error:
        print(json.dumps({'result': 'REFUSED_OR_FAILED', 'reason': str(error), 'automatic_retry': False}), file=sys.stderr)
        return 78


if __name__ == '__main__':
    raise SystemExit(main())
