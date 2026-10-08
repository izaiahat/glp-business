#!/usr/bin/env python3
"""Narrow GLP44 old-instance bootstrap; applying requires separate approval.

No bridge hot patch: close new TCP admission outside the old resident, let its
unchanged JobStore finish accepted work, then restart the existing user unit.
The original bridge-upgrade path bypasses the gate only for privileged GETs.
GLP338 adds one fixed pre-admission rejection to read cached registry mode keys;
it never creates an error case, ACK, job, callback or producer execution.
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
        {'add': {'rule': {'family': 'inet', 'table': TABLE, 'chain': 'output', 'expr': expr}}},
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
        require(observed == [next(iter(r.values())) for r in gate_commands()], 'ingress_rule_drift')

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
            # Parse the census before endpoint selection: dropping malformed
            # rows first can hide a second observation of either journal inode.
            # Unknown row shapes (including shifted/trailing fields) stay busy.
            if any(len(row) != 8 or not all(row[i].isdigit() for i in (2, 3, 5, 7))
                   for row in unix):
                return False
            inode = stdio[8:-1]
            local = [row for row in unix if row[5] == inode]
            if (len(local) == 1 and local[0][:2] == ['u_str', 'ESTAB']
                    and local[0][4] == '*' and local[0][6] == '*'
                    and local[0][7].isdigit()):
                peer = [row for row in unix if row[5] == local[0][7]]
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
            handle.write(json.dumps(dict(fields, bridge_sha256=getattr(self, 'bridge_sha256', APPROVED_BRIDGE_SHA256)), sort_keys=True) + '\n')
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

# Independently accepted PR219 registry generation, NOT a bridge upgrade or a
# caller-supplied expected-hash override. The original GLP44 pin stays fixed.
REGISTRY = BUSINESS / 'ops/n8n-bridge/actions.json'
REGISTRY_REL = 'ops/n8n-bridge/actions.json'
REGISTRY_BRIDGE_SHA256 = 'a2ef8399f4d14c3d9c14e9691bcd44edab5a8ed50d7761d271ebe6fe88a25b8a'
REGISTRY_BEFORE = '335c4f0cc2b2933ac6ddda4a3f9eb674e8699a08534a71ea54038ee8ceb401c7'
REGISTRY_AFTER = '32d9d635a43761cc425f5275b682cc8929f7c4916536bc169358a3023cff2a47'
REGISTRY_ACCEPTANCE = '39f09d41c32d7f4ada0a128b2288bbfe0c4eb22d4983605a91f7686a30294bcb'


class RegistryHost(LinuxHost):
    """Exact GLP338 registry generation; all resident drain predicates inherited."""

    def __init__(self):
        self.checkout = Path(__file__).resolve().parents[1]
        self.imported = False

    def source(self):
        require(hashlib.sha256(read_regular(BRIDGE)).hexdigest() == REGISTRY_BRIDGE_SHA256,
                'unchanged_registry_bridge_mismatch')
        expected = REGISTRY_AFTER if self.imported else REGISTRY_BEFORE
        require(hashlib.sha256(read_regular(REGISTRY)).hexdigest() == expected,
                'registry_generation_mismatch')

    def identity(self):
        identity = super().identity()
        # A new PID plus the disk registry is not enough if ExecStart's runtime
        # environment loads another registry. Never log the rest of environ.
        entries = read_regular(PROC / str(identity[0]) / 'environ').split(b'\0')
        for key, expected in ((b'N8N_BRIDGE_ACTIONS_FILE', str(REGISTRY).encode()),
                              (b'N8N_BRIDGE_PORT', b'8791')):
            values = [row.split(b'=', 1)[1] for row in entries if row.startswith(key + b'=')]
            require(not values or values == [expected], 'registry_startup_environment')
        return identity

    @property
    def bridge_sha256(self):
        return REGISTRY_BRIDGE_SHA256

    def journal(self, **fields):
        super().journal(**dict(fields, registry_before=REGISTRY_BEFORE,
                               registry_after=REGISTRY_AFTER,
                               registry_acceptance=REGISTRY_ACCEPTANCE))

    def importer(self):
        # Execute only this independently reviewed importer, never an installed
        # module or caller-provided adapter. No bytecode cache or CLI child.
        import types
        path = self.checkout / 'scripts/sync_from_repo.py'
        data = read_regular(path)
        require(hashlib.sha256(data).hexdigest() ==
                '29464d1a9d60d944e6aa13c32805372eed813bb4850c11c50807d2ba926d0dd1',
                'registry_importer_generation_mismatch')
        module = types.ModuleType('registry_scoped_importer')
        module.__file__ = str(path)
        exec(compile(data, str(path), 'exec'), module.__dict__)
        return module

    def validate_checkout(self):
        # Root executes an operator-reviewed ubuntu-owned checkout. Explicitly
        # trust only this directory for Git's ownership check; no global config.
        git = ['/usr/bin/git', '-c', 'safe.directory=' + str(self.checkout),
               '-C', str(self.checkout)]
        require(not self.command(git + ['status', '--porcelain', '--untracked-files=no']).strip(),
                'registry_checkout_not_clean')
        rel = 'scripts/glp44_safe_bridge_activation.py'
        require(self.command(git + ['show', 'HEAD:' + rel]).encode() == read_regular(self.checkout / rel),
                'registry_producer_not_committed')
        require(hashlib.sha256(read_regular(self.checkout / REGISTRY_REL)).hexdigest() == REGISTRY_AFTER,
                'registry_candidate_mismatch')

    @contextmanager
    def exclusive_source(self):
        self.syncer = self.importer()
        # Maintained writer lock first; same live lease reaches sync(), including
        # its preview and post-apply no-adoption check. Old importers/direct
        # writers must be excluded by the receiving owner before invocation.
        with self.syncer.source_lock(BUSINESS) as owner:
            self.owner = owner
            try:
                self.validate_checkout()
                with super().exclusive_source():
                    yield
            finally:
                self.owner = None

    def scoped_sync(self, apply, adoption):
        # The root controller owns nft/token/journal custody. The existing
        # importer writes as workspace uid/gid 1000 so registry and sync-state
        # ownership remain usable by ordinary imports. No child/second lease.
        import threading
        require(threading.active_count() == 1, 'registry_controller_must_be_single_threaded')
        require(os.geteuid() == 0, 'registry_controller_root_required')
        gid = os.getegid()
        try:
            os.setegid(1000)
            os.seteuid(1000)
            return self.syncer.sync(BUSINESS, self.checkout, apply, adoption,
                                    (REGISTRY_REL,), _source_owner=self.owner)
        finally:
            os.seteuid(0)
            os.setegid(gid)

    def restart(self):
        require(getattr(self, 'owner', None) is not None and not self.imported,
                'registry_handoff_required_no_replay')
        only = (REGISTRY_REL,)
        require(self.scoped_sync(False, only) == 0, 'registry_preview_refused')
        self.source()
        require(drained(self, self.resident_identity, {}), 'registry_preimport_drain_changed')
        self.journal(phase='IMPORTING_REGISTRY')
        # An exception/ack loss here leaves the gate closed and journal intact.
        # No replacement bridge, raw copy, baseline manufacture or retry.
        require(self.scoped_sync(True, only) == 0, 'registry_import_uncertain')
        self.imported = True
        self.source()
        require(self.scoped_sync(False, ()) == 0, 'registry_zero_diff_refused')
        require(drained(self, self.resident_identity, {}), 'registry_prerestart_drain_changed')
        self.journal(phase='REGISTRY_IMPORTED_RESTARTING')
        super().restart()

    def cached_registration(self):
        """Fixed pre-admission rejection, not a synthetic error case/job.

        /actions omits enum values. On this pinned bridge, an invalid mode is
        rejected by build_script_command before spill files, ticket allocation,
        case creation or child launch. There is no arbitrary POST/payload API.
        Exact registry bytes held across startup bind flag values; this genuine
        cached response independently confirms the newly loaded enum keys.
        """
        self.source()
        identity = self.identity()
        require(self.imported and identity != self.resident_identity,
                'registry_probe_requires_new_instance')
        self.verify_ingress()
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
            conn.request('POST', '/run', body=json.dumps({
                'action': 'script', 'name': 'error_fixer',
                'args': {'mode': '__glp338_registry_readback__'}}), headers={
                    'Authorization': 'Bearer ' + token,
                    'Content-Type': 'application/json', 'Connection': 'close'})
            response = conn.getresponse()
            require(response.status == 400, 'cached_registry_status_uncertain')
            expected = {'error': "invalid value '__glp338_registry_readback__' for arg 'mode' "
                        "in action 'error_fixer' (allowed: full, intake, notification-ack, sweep, tier2)"}
            require(json.loads(response.read(4096)) == expected, 'cached_registry_mismatch')
        finally:
            conn.close()
            sock.close()
        require(self.identity() == identity, 'cached_registry_resident_changed')
        self.source()
        self.verify_ingress()

    def readback(self, retained):
        super().readback(retained)
        self.cached_registration()


# GLP80 is a separate fixed successor, not a relaxation of the GLP338 edge.
# The accepted PR267 source cohort is received atomically under the same source
# lease before the existing unit loads its registry. Old caller/control evidence
# remains immutable; receiving-owner exclusion still covers pre-lock readers.
PRICING_BEFORE = {
    REGISTRY_REL: '32d9d635a43761cc425f5275b682cc8929f7c4916536bc169358a3023cff2a47',
    'ops/scheduled-source-drift-manifest.json': '6e57371aabd589a670dbd0583cd7c0e4847fd818cc258f7fdd27355a4ded0b55',
    'scripts/cron_bridge_action.py': 'ba31d4048228616b2a87bc4690ad99a28bdf9bc03e09090c3171cf7595974d3d',
    'scripts/glp_price_freshness.py': None,
    'tools/glp/price_evidence_delta.py': '7b9fa0dd2d5b1d0c1ea1f10c4dbff6da3c2b626a7e7bd18d7357b980238ee93a',
}
PRICING_AFTER = {
    REGISTRY_REL: '0593f4c6c25c3be7726e8c3b5c5cd77c14c3857b09c25ab66e999dc18da67fb1',
    'ops/scheduled-source-drift-manifest.json': '855fa1f97b1c975f2ba49c322d4ae45ce3229627acf8cd234ff3696d257599c7',
    'scripts/cron_bridge_action.py': '0eca433891b84588407d4a64c467422f1b069604380493283aa3b828e13595c1',
    'scripts/glp_price_freshness.py': 'c6d5ea10fffcc4243b98f41576c2905a1a7e67d7ee117415a52e438573943acd',
    'tools/glp/price_evidence_delta.py': 'ff56e5f7fb8f9a4db812597e4ce423a08b0aefeb306870d6667ba9de4a1ea8ce',
}


class PricingRegistryHost(RegistryHost):
    """Only accepted GLP80 five-path adoption; all actual drain gates inherited."""

    def source(self):
        require(hashlib.sha256(read_regular(BRIDGE)).hexdigest() == REGISTRY_BRIDGE_SHA256,
                'unchanged_registry_bridge_mismatch')
        for rel, expected in (PRICING_AFTER if self.imported else PRICING_BEFORE).items():
            path = BUSINESS / rel
            require(not path.is_symlink(), 'pricing_symlink_refused:' + rel)
            actual = hashlib.sha256(read_regular(path)).hexdigest() if path.exists() else None
            require(actual == expected, 'pricing_generation_mismatch:' + rel)

    def journal(self, **fields):
        LinuxHost.journal(self, **dict(fields, pricing_before=PRICING_BEFORE,
                                     pricing_after=PRICING_AFTER,
                                     accepted_source_merge='1dcafc20a9b5722bb2249a1b8911d66e0e49ea40'))

    def validate_checkout(self):
        git = ['/usr/bin/git', '-c', 'safe.directory=' + str(self.checkout), '-C', str(self.checkout)]
        require(not self.command(git + ['status', '--porcelain', '--untracked-files=no']).strip(),
                'registry_checkout_not_clean')
        rel = 'scripts/glp44_safe_bridge_activation.py'
        require(self.command(git + ['show', 'HEAD:' + rel]).encode() == read_regular(self.checkout / rel),
                'registry_producer_not_committed')
        for rel, expected in PRICING_AFTER.items():
            require(hashlib.sha256(read_regular(self.checkout / rel)).hexdigest() == expected,
                    'pricing_candidate_mismatch:' + rel)

    def scoped_sync(self, apply, adoption):
        import threading
        require(not adoption, 'pricing_no_baseline_override')
        require(threading.active_count() == 1, 'registry_controller_must_be_single_threaded')
        require(os.geteuid() == 0, 'registry_controller_root_required')
        gid = os.getegid()
        try:
            os.setegid(1000)
            os.seteuid(1000)
            return self.syncer.sync(BUSINESS, self.checkout, apply, (),
                                    tuple(PRICING_AFTER), _source_owner=self.owner)
        finally:
            os.seteuid(0)
            os.setegid(gid)

    def restart(self):
        require(getattr(self, 'owner', None) is not None and not self.imported,
                'registry_handoff_required_no_replay')
        require(self.scoped_sync(False, ()) == 0, 'registry_preview_refused')
        self.source()
        require(drained(self, self.resident_identity, {}), 'registry_preimport_drain_changed')
        self.journal(phase='IMPORTING_PRICING_REGISTRY')
        require(self.scoped_sync(True, ()) == 0, 'registry_import_uncertain')
        self.imported = True
        self.source()
        require(self.scoped_sync(False, ()) == 0, 'registry_zero_diff_refused')
        require(drained(self, self.resident_identity, {}), 'registry_prerestart_drain_changed')
        self.journal(phase='PRICING_REGISTRY_IMPORTED_RESTARTING')
        LinuxHost.restart(self)


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
                  'bridge_sha256': getattr(host, 'bridge_sha256', APPROVED_BRIDGE_SHA256)}
        if isinstance(host, PricingRegistryHost):
            result['registry_sha256'] = PRICING_AFTER[REGISTRY_REL]
            result['received_source_sha256'] = PRICING_AFTER
        host.journal(**result)
        host.open_ingress()
    return result


def main(argv=None):
    global CONTROL
    previous_control = CONTROL
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', nargs='?', choices=['plan', 'activate', 'status', 'registry-plan',
                        'activate-registry', 'pricing-registry-plan', 'activate-pricing-registry'], default='plan')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--pid', type=int)
    parser.add_argument('--start-ticks', type=int)
    parser.add_argument('--wait-seconds', '--wait', type=int, default=3600)
    parser.add_argument('--job-id')
    args = parser.parse_args(argv)
    if args.mode == 'pricing-registry-plan':
        print(json.dumps({'status': 'SOURCE_ONLY_PLAN', 'before': PRICING_BEFORE,
                          'after': PRICING_AFTER, 'importer_sha256': '29464d1a9d60d944e6aa13c32805372eed813bb4850c11c50807d2ba926d0dd1',
                          'runtime_authorization': False}, sort_keys=True))
        return 0
    if args.mode == 'registry-plan':
        print(json.dumps({'status': 'SOURCE_ONLY_PLAN', 'bridge_sha256': REGISTRY_BRIDGE_SHA256,
                          'registry_before': REGISTRY_BEFORE, 'registry_after': REGISTRY_AFTER,
                          'registry_acceptance': REGISTRY_ACCEPTANCE, 'target': str(REGISTRY),
                          'runtime_authorization': False}, sort_keys=True))
        return 0
    if args.mode == 'plan':
        print(json.dumps({'status': 'SOURCE_ONLY_PLAN', 'bridge_sha256': APPROVED_BRIDGE_SHA256,
                          'target': str(BRIDGE), 'gate': gate_commands(),
                          'runtime_authorization': False}, sort_keys=True))
        return 0
    try:
        require(args.mode not in {'activate', 'activate-registry', 'activate-pricing-registry'} or args.apply,
                'apply_required_separate_operator_authority')
        require(os.geteuid() == 0, 'root_network_capability_required')
        require(0 <= args.wait_seconds <= 86400, 'invalid_wait')
        if args.mode == 'activate-pricing-registry':
            CONTROL = Path('/run/glp80-price-registry-activation')
        CONTROL.mkdir(mode=0o700, exist_ok=True)
        info = CONTROL.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0
                and stat.S_IMODE(info.st_mode) == 0o700, 'control_directory_custody')
        fd = os.open(CONTROL / 'controller.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            host = (PricingRegistryHost() if args.mode == 'activate-pricing-registry' else
                    RegistryHost() if args.mode == 'activate-registry' else LinuxHost())
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
    finally:
        CONTROL = previous_control


if __name__ == '__main__':
    raise SystemExit(main())
