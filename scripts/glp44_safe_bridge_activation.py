#!/usr/bin/env python3
"""Narrow GLP44 old-instance bootstrap; applying requires separate approval.

No bridge hot patch: close new TCP admission outside the old resident, let its
unchanged JobStore finish accepted work, then restart the existing user unit.
The only bypass is a privileged marked GET reader, never a /run producer.
Any uncertainty leaves ingress closed; no kill, replay or automatic rollback.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import time

BUSINESS = Path('/home/ubuntu/business')
BRIDGE = BUSINESS / 'ops/n8n-bridge/bridge.py'
SOURCE_LOCK = BUSINESS / 'ops/glp/glp44-source-admission.lock'
STATES = BUSINESS / 'reports/ops/n8n-bridge-jobs/.job-state'
CONTROL = Path('/run/glp44-bridge-activation')
TABLE = 'glp44_bridge_activation'
MARK = 0x474C5044
UNIT = 'n8n-bridge.service'
TOKEN = Path('/home/ubuntu/.hermes/secrets/n8n-bridge/token')
NFT = '/usr/sbin/nft'
PROC = Path('/proc')
CGROUP_ROOT = Path('/sys/fs/cgroup')
SYSTEMCTL = ['/usr/sbin/runuser', '-u', 'ubuntu', '--', '/usr/bin/env',
             'XDG_RUNTIME_DIR=/run/user/1000', '/usr/bin/systemctl', '--user']


def gate_commands():
    # Independent of conntrack/NOTRACK. Existing TCP streams are not reset:
    # pre-boundary HTTP handlers, accepted IDs and queued work drain normally.
    expr = [
        {'match': {'op': '==', 'left': {'payload': {'protocol': 'ip', 'field': 'daddr'}}, 'right': '127.0.0.1'}},
        {'match': {'op': '==', 'left': {'payload': {'protocol': 'tcp', 'field': 'dport'}}, 'right': 8791}},
        {'match': {'op': '!=', 'left': {'meta': {'key': 'mark'}}, 'right': MARK}},
        {'match': {'op': '==', 'left': {'&': [{'payload': {'protocol': 'tcp', 'field': 'flags'}}, 'syn']}, 'right': 'syn'}},
        {'reject': {'type': 'tcp reset'}},
    ]
    return [
        {'create': {'table': {'family': 'inet', 'name': TABLE}}},
        {'create': {'chain': {'family': 'inet', 'table': TABLE, 'name': 'output',
                             'type': 'filter', 'hook': 'output', 'prio': 0, 'policy': 'accept'}}},
        {'create': {'rule': {'family': 'inet', 'table': TABLE, 'chain': 'output', 'expr': expr}}},
    ]


class LinuxHost:
    """Fixed target, stdlib only. Nothing effectful happens at import time."""

    def command(self, argv, payload=None):
        proc = subprocess.run(argv, input=payload, text=True, capture_output=True,
                              timeout=90, check=False,
                              env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'})
        require(proc.returncode == 0, 'host_command_failed_gate_not_released')
        return proc.stdout  # never print stderr or arbitrary host command data

    def close_ingress(self):
        # One atomic nft transaction. CREATE refuses a pre-existing table;
        # never flush/replace/steal someone else's rules or a retained hold.
        self.command([NFT, '--json', '--file', '-'], json.dumps({'nftables': gate_commands()}))

    def verify_ingress(self):
        raw = json.loads(self.command([NFT, '--json', 'list', 'table', 'inet', TABLE]))
        observed = []
        for row in raw['nftables']:
            if 'metainfo' in row:
                continue
            require(len(row) == 1, 'ingress_rule_drift')
            kind, value = next(iter(row.items()))
            observed.append({kind: {k: v for k, v in value.items() if k != 'handle'}})
        require(observed == [r['create'] for r in gate_commands()], 'ingress_rule_drift')

    def open_ingress(self):
        self.verify_ingress()
        self.command([NFT, 'delete', 'table', 'inet', TABLE])

    def get(self, route):
        require(route == '/health' or re.fullmatch(r'/job/[0-9a-f]{16}', route), 'readback_route')
        # Explicitly no generic URL/method/payload API, proxies or redirects.
        # Token stays in memory and HTTP headers; not argv, environment or logs.
        fd = os.open(TOKEN, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'r') as handle:
            require(stat.S_IMODE(os.fstat(handle.fileno()).st_mode) == 0o600, 'token_mode')
            token = handle.read().strip()
        require(bool(token) and '\n' not in token and '\r' not in token, 'token_invalid')
        conn = http.client.HTTPConnection('127.0.0.1', 8791, timeout=15)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.settimeout(15)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_MARK, MARK)
            sock.connect(('127.0.0.1', 8791))
            conn.sock = sock
            conn.request('GET', route, headers={'Authorization': 'Bearer ' + token,
                                              'Connection': 'close'})
            response = conn.getresponse()
            require(response.status == 200, 'readback_http_status')
            value = json.loads(response.read(4 * 1024 * 1024))
            require(isinstance(value, dict), 'readback_shape')
            return value
        finally:
            conn.close()
            sock.close()

    def health(self):
        return self.get('/health')

    def readback(self, retained):
        for job_id, before in retained.items():
            after = self.get('/job/' + job_id)
            require(after.get('job_id') == job_id and after.get('status') == before['status']
                    and after.get('rc') == before['rc'] and not after.get('interrupted'),
                    'terminal_http_readback_mismatch')

    def source(self):
        require(hashlib.sha256(read_regular(BRIDGE)).hexdigest() == APPROVED_BRIDGE_SHA256,
                'approved_bridge_bytes_mismatch')

    def unit(self):
        names = ['MainPID', 'ControlGroup', 'ActiveState', 'SubState', 'ExecStart',
                 'ExecStartPre', 'ExecStartPost', 'ExecStop', 'ExecStopPost']
        text = self.command(SYSTEMCTL + ['show', UNIT, '--property=' + ','.join(names)])
        return dict(line.split('=', 1) for line in text.splitlines() if '=' in line)

    def identity(self):
        unit = self.unit()
        expected = '/usr/bin/python3 ' + str(BRIDGE)
        require(re.search(r'argv\[\]=' + re.escape(expected) + r'\s+;', unit.get('ExecStart', '')),
                'unit_exec_mismatch')
        require(all(not unit.get(k) for k in ('ExecStartPre', 'ExecStartPost', 'ExecStop', 'ExecStopPost')),
                'unit_extra_lifecycle_commands')
        require(unit.get('ActiveState') == 'active' and unit.get('SubState') == 'running', 'unit_not_running')
        pid = int(unit['MainPID'])
        require(pid > 1, 'unit_pid_invalid')
        base = PROC / str(pid)
        require((base / 'cmdline').read_bytes() == b'/usr/bin/python3\0' + str(BRIDGE).encode() + b'\0',
                'resident_command_mismatch')
        ticks = int((base / 'stat').read_text().rsplit(') ', 1)[1].split()[19])
        group = unit['ControlGroup']
        require(group.startswith('/') and '..' not in Path(group).parts
                and group.endswith('/' + UNIT), 'unit_cgroup_invalid')
        self.group = CGROUP_ROOT / group.lstrip('/')
        require((base / 'cgroup').read_text().strip() == '0::' + group, 'resident_cgroup_mismatch')
        if not hasattr(self, 'resident_identity'):
            self.resident_identity = (pid, ticks)
        # Authenticate the actual loopback listener, not just a service label.
        listeners = [row for row in self.tcp_rows() if row[3] == '0A']
        descriptors = {os.readlink(path) for path in (base / 'fd').iterdir()}
        require(len(listeners) == 1 and 'socket:[' + listeners[0][9] + ']' in descriptors,
                'resident_listener_mismatch')
        return pid, ticks

    def tcp_rows(self):
        # Includes unaccepted backlog sockets, slow request bodies and FIN_WAIT;
        # a counter-only idle snapshot cannot see those future admissions.
        rows = [line.split() for line in (PROC / 'net/tcp').read_text().splitlines()[1:]]
        require(all(len(row) >= 10 for row in rows), 'tcp_readback_invalid')
        return [row for row in rows if row[1] == '0100007F:2257']

    def quiet(self, identity):
        require(self.identity() == identity, 'resident_changed')
        rows = self.tcp_rows()
        if any(row[3] not in {'0A', '06'} for row in rows):
            return False
        pid = identity[0]
        descriptors = {p.name: os.readlink(p) for p in (PROC / str(pid) / 'fd').iterdir()}
        sockets = {target for target in descriptors.values() if target.startswith('socket:[')}
        listener = {'socket:[' + row[9] + ']' for row in rows if row[3] == '0A'}
        # systemd supplies one connected journal stream shared by stdout/stderr.
        # FD numbers or /proc/net/unix membership alone do not identify its peer.
        # Exempt only this exact pair (no additional alias), after reciprocal ss
        # evidence identifies a connected AF_UNIX stream to journald's stdout.
        stdio = descriptors.get('1', '')
        if (re.fullmatch(r'socket:\[[0-9]+\]', stdio)
                and descriptors.get('2') == stdio
                and {fd for fd, target in descriptors.items() if target == stdio} == {'1', '2'}):
            unix = [line.split() for line in self.command(
                ['/usr/bin/ss', '--unix', '--all', '--numeric', '--no-header']).splitlines()]
            inode = stdio[8:-1]
            local = [row for row in unix if len(row) >= 8 and row[5] == inode]
            if (len(local) == 1 and local[0][:2] == ['u_str', 'ESTAB']
                    and local[0][4] == '*' and local[0][6] == '*'
                    and local[0][7].isdigit()):
                peer = [row for row in unix if len(row) >= 8 and row[5] == local[0][7]]
                if (len(peer) == 1 and peer[0][:2] == ['u_str', 'ESTAB']
                        and peer[0][4] == '/run/systemd/journal/stdout'
                        and peer[0][6:8] == ['*', inode]):
                    sockets.remove(stdio)
        # A reset accepted socket may leave /proc/net/tcp while retaining a
        # buffered POST and a live descriptor before its handler thread starts.
        if sockets != listener or len(listener) != 1:
            return False
        if {p.name for p in (PROC / str(pid) / 'task').iterdir()} != {str(pid)}:
            return False
        # Service subtree only, NOT whole-host idle. Includes detached children
        # and callback threads before KillMode=control-group could signal them.
        members = set()
        for path in self.group.rglob('cgroup.procs'):
            members.update(int(value) for value in path.read_text().split())
        return members == {pid}

    def jobs(self):
        # Never import bridge.load_durable_job here: it can manufacture an
        # interrupted terminal record. This reader is strictly non-mutating.
        require(STATES.is_dir(), 'job_state_directory_missing')
        result = {}
        for path in sorted(STATES.glob('*.json')):
            data = read_regular(path)
            rec = json.loads(data)
            owner = getattr(self, 'resident_identity', None)
            if owner and (rec.get('_bridge_pid'), rec.get('_bridge_start_ticks')) != owner:
                continue
            job_id = path.stem
            require(re.fullmatch(r'[0-9a-f]{16}', job_id)
                    and rec.get('job_id') == job_id, 'job_record_identity')
            require(rec.get('status') in TERMINAL | {'queued', 'accepted', 'running'}, 'job_record_status')
            receipt_hash = None
            if rec['status'] in TERMINAL:
                receipt = Path(rec.get('receipt_path', ''))
                require(receipt.parent == STATES.parent, 'terminal_receipt_path')
                receipt_data = read_regular(receipt)
                terminal = json.loads(receipt_data)
                require(terminal.get('job_id') == job_id
                        and terminal.get('status') == rec['status']
                        and terminal.get('rc') == rec.get('rc'), 'terminal_receipt_mismatch')
                require(not terminal.get('interrupted'), 'ambiguous_interrupted_job')
                receipt_hash = hashlib.sha256(receipt_data).hexdigest()
            result[job_id] = {'status': rec['status'], 'rc': rec.get('rc'),
                              'state_sha256': hashlib.sha256(data).hexdigest(),
                              'receipt_sha256': receipt_hash}
        return result

    def restart(self):
        # Exactly one systemctl mutation. A timeout/error is ambiguous: never
        # repeat it, undo the gate, or signal the old/new process here.
        self.command(SYSTEMCTL + ['restart', UNIT])
        deadline = time.monotonic() + 30
        while True:
            try:
                self.identity()
                require(self.health().get('status') == 'ok', 'new_health_invalid')
                return
            except (OSError, ValueError, Refused):
                require(time.monotonic() < deadline, 'restart_readback_timeout')
                time.sleep(1)

    def journal(self, **fields):
        fd = os.open(CONTROL / 'activation.jsonl', os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
        with os.fdopen(fd, 'a') as handle:
            handle.write(json.dumps(dict(fields, bridge_sha256=APPROVED_BRIDGE_SHA256), sort_keys=True) + '\n')
            handle.flush()
            os.fsync(handle.fileno())

    @contextmanager
    def exclusive_source(self):
        # Persistent canonical inode, nonblocking. Never take child locks first
        # or unlink it. A first bootstrap creates it for the existing uid 1000
        # runtime, not root; subsequent opens preserve that same inode/owner.
        created = False
        try:
            fd = os.open(SOURCE_LOCK, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            created = True
        except FileExistsError:
            fd = os.open(SOURCE_LOCK, os.O_RDWR | os.O_NOFOLLOW)
        try:
            if created:
                os.fchown(fd, 1000, 1000)
            require(stat.S_ISREG(os.fstat(fd).st_mode), 'source_lock_type')
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            os.close(fd)


def read_regular(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as handle:
        require(stat.S_ISREG(os.fstat(handle.fileno()).st_mode), 'not_regular')
        return handle.read()


APPROVED_BRIDGE_SHA256 = 'b02f156a8d247acc02c61c6dbc24c365ee6b22a9a7afc79646b30c49c897f072'
TERMINAL = {'done', 'error'}


class Refused(RuntimeError):
    pass


def require(condition, reason):
    if not condition:
        raise Refused(reason)


def drained(host, identity, retained):
    require(host.identity() == identity, 'resident_changed')
    host.verify_ingress()
    health = host.health()
    current = host.jobs()
    require(set(retained) <= set(current), 'accepted_id_disappeared')
    retained.update(current)
    return (health.get('status') == 'ok'
            and health.get('jobs_running') == 0
            and health.get('jobs_queued') == 0
            and all(r.get('status') in TERMINAL for r in retained.values())
            and host.quiet(identity))


def activate(host, expected_identity, timeout=3600):
    """Existing admission/drain + source EX + exact-unit restart, once only."""
    host.source()
    require(host.identity() == expected_identity, 'old_identity_mismatch')
    retained = host.jobs()
    host.journal(phase='CLOSING', old_identity=expected_identity)
    host.close_ingress()
    # Deliberately no finally-open: failures and interrupted/uncertain restart
    # require the installation owner to reconcile the exact retained gate.
    deadline = time.monotonic() + timeout
    while not drained(host, expected_identity, retained):
        require(time.monotonic() < deadline, 'drain_timeout_gate_retained')
        time.sleep(min(1, max(0, deadline - time.monotonic())))
    host.journal(phase='DRAINED', accepted_ids=sorted(retained))
    with host.exclusive_source():
        host.source()
        require(drained(host, expected_identity, retained), 'drain_changed')
        # No downstream child locks are acquired. EX precedes restart/readback.
        host.journal(phase='RESTARTING', old_identity=expected_identity)
        host.restart()
        new_identity = host.identity()
        require(new_identity != expected_identity, 'restart_not_new_instance')
        host.source()
        host.verify_ingress()
        health = host.health()
        require(health.get('status') == 'ok' and health.get('jobs_running') == 0
                and health.get('jobs_queued') == 0, 'new_health_invalid')
        after = host.jobs()
        require(set(retained) <= set(after), 'accepted_id_disappeared')
        require(all(after[k] == v for k, v in retained.items()), 'terminal_record_changed')
        host.readback(retained)
        result = {'phase': 'VERIFIED', 'old_identity': expected_identity,
                  'new_identity': new_identity, 'accepted_ids': sorted(retained),
                  'bridge_sha256': APPROVED_BRIDGE_SHA256}
        host.journal(**result)
        host.open_ingress()
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', nargs='?', choices=['plan', 'activate', 'status'], default='plan')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--pid', type=int)
    parser.add_argument('--start-ticks', type=int)
    parser.add_argument('--wait-seconds', type=int, default=3600)
    parser.add_argument('--job-id')
    args = parser.parse_args(argv)
    if args.mode == 'plan':
        print(json.dumps({'status': 'SOURCE_ONLY_PLAN', 'bridge_sha256': APPROVED_BRIDGE_SHA256,
                          'target': str(BRIDGE), 'gate': gate_commands(),
                          'runtime_authorization': False}, sort_keys=True))
        return 0
    try:
        require(args.mode != 'activate' or args.apply, 'apply_required_separate_operator_authority')
        require(os.geteuid() == 0, 'root_network_capability_required')
        require(0 <= args.wait_seconds <= 86400, 'invalid_wait')
        CONTROL.mkdir(mode=0o700, exist_ok=True)
        info = CONTROL.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0
                and stat.S_IMODE(info.st_mode) == 0o700, 'control_directory_custody')
        fd = os.open(CONTROL / 'controller.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            host = LinuxHost()
            if args.mode == 'status':
                route = '/job/' + args.job_id if args.job_id else '/health'
                value = host.get(route)
                print(json.dumps({k: value[k] for k in ('job_id', 'status', 'rc', 'jobs_running', 'jobs_queued') if k in value}))
                return 0
            require(args.pid and args.start_ticks, 'exact_old_identity_required')
            # One attempt per custody journal. Refuse resume/retry after ANY
            # uncertain effect; reconcile this journal and retained nft table.
            journal = os.open(CONTROL / 'activation.jsonl',
                              os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            os.close(journal)
            result = activate(host, (args.pid, args.start_ticks), timeout=args.wait_seconds)
            print(json.dumps(result, sort_keys=True))
            return 0
        finally:
            os.close(fd)
    except Exception as exc:
        print(json.dumps({'status': 'REFUSED_RECONCILE_EXACT_STATE',
                          'reason': str(exc) if isinstance(exc, Refused) else type(exc).__name__,
                          'gate_automatically_released': False,
                          'receipt': str(CONTROL / 'activation.jsonl')}, sort_keys=True))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
