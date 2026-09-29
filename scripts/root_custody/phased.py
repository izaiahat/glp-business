#!/usr/bin/env python3
"""Maintained stage-2 successor. Default is an offline plan, NOT authority.
One explicit foreground `advance` executes one durable verification/mutation unit.
The phase-limited caller may explicitly request <=8 same-phase read-only units;
that finite exception keeps ONE aggregate deadline and fresh per-unit admission.
No live wrapper is imported. No client is constructed until authorized execution.
"""
import time
import signal
ENTRY_STARTED = time.monotonic()
# CLI import/admission time is part of work, not an untimed prefix. Imports of
# this offline module do not install a process-wide alarm.
if __name__ == '__main__':
    def _entry_timeout(signum, frame):
        raise TimeoutError('Bounded initialization')
    signal.signal(signal.SIGALRM, _entry_timeout)
    signal.setitimer(signal.ITIMER_REAL, 75)

import argparse
import ast
from collections import Counter
from contextlib import contextmanager
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from types import SimpleNamespace
from botocore.config import Config
from botocore.exceptions import ClientError

D = Path(__file__).resolve().parent
# Historical policy inputs remain immutable, private and outside curated Git.
INPUTS = Path('/home/ubuntu/business/reports/ops')
SOURCE = INPUTS / 'root-custody-stage2-retry-20260926'
CAPTURE = INPUTS / 'root-custody-stage12-20260926'
EXPECTED = '625c2e3d3e5e7a018c0ead1db53ec99d096af16ee3fc547ea1162a317b98ae4b'
PINS = {SOURCE / 'retry.py': '86a470220166b7fd3d94ce7ed535710411165b4d97bda2793dfe7a6b8a32363e',
        CAPTURE / 'capture_preimage.py': '600089b5dc8554716f7d9a0fc2401ef2ad61a4344d52536b5ec2cd335e7710a0',
        SOURCE / 'PREIMAGE.json': '83220e3ce3f9b10aaf623c40f0d59b5c2894acaad1536ffa9edda35077c2f26a',
        SOURCE / 'TEMPORARY-ROLLBACK-POLICY.json': '9f216c10fc416ef1f922b806b62d974aab9ac16acbb1ffffef0c5ce877ba8547',
        CAPTURE / 'CRON-SCAN.json': '108a1419c9b2f3f785c2015bc0160ffe970e23dc7b89e09c7ae230e1fe366e75'}
ROLE = 'HermesSSMRole'
RA = 'arn:aws:iam::271095146765:role/' + ROLE
IID = 'i-0a305a43215e8cae0'
IA = 'arn:aws:ec2:us-west-2:271095146765:instance/' + IID
ADMIN = 'arn:aws:iam::aws:policy/AdministratorAccess'
CW = 'arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy'
CORE = 'arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore'
POL = 'host-runtime-least-privilege-20260926'
GUARD = 'stage2-retry-temporary-rollback-20260926'
PATHS = ['/usr/bin', '/usr/lib/node_modules', '/usr/lib/node_modules/@anthropic-ai']
HASH_PATHS = ['/etc/sudoers', '/etc/sudoers.d/90-cloud-init-users', '/etc/sudoers.d/ssm-agent-users', '/etc/group', '/etc/gshadow', '/etc/cloud/cloud.cfg']
CALL_SECONDS = 230
WORK_SECONDS = 75
SDK_SECONDS = 65  # 5*(3+6) + (1+2+4+8) = 60 nominal; alarm also covers DNS/credentials.
DIAGNOSTIC_SECONDS = 5
RECOVERY_SECONDS = 140
FOOTER_SECONDS = 10
WAIT_SECONDS = 65
assert WORK_SECONDS + DIAGNOSTIC_SECONDS + RECOVERY_SECONDS + FOOTER_SECONDS == CALL_SECONDS


def verification_config():
    return Config(connect_timeout=3, read_timeout=6,
                  retries={'mode': 'legacy', 'total_max_attempts': 5})


def _token(value):
    return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:/+=-]{1,256}', value) else None


def error_metadata(exc, phase, operation):
    response = exc.response if isinstance(exc, ClientError) else {}
    meta = response.get('ResponseMetadata', {})
    return {'type': _token(type(exc).__name__), 'phase': _token(phase),
            'operation': _token(getattr(exc, 'operation_name', None)) or _token(operation),
            'aws_error_code': _token(response.get('Error', {}).get('Code')),
            'http_status': meta.get('HTTPStatusCode') if type(meta.get('HTTPStatusCode')) is int else None,
            'request_id': _token(meta.get('RequestId')),
            'sdk_retry_attempts': meta.get('RetryAttempts') if type(meta.get('RetryAttempts')) is int else None}


def atomic(path, obj):
    path = Path(path)
    temp = path.with_name(path.name + '.writing')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(obj, stream, indent=2, default=str)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def observed_call(call, *, phase, operation, path):
    try:
        return call()
    except BaseException as exc:
        atomic(path, {'utc': dt.datetime.now(dt.timezone.utc).isoformat(),
                      'error': error_metadata(exc, phase, operation)})
        raise


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def timestamp(value):
    return dt.datetime.fromisoformat(str(value))


def require(value, message='Verification failed'):
    if not value:
        raise AssertionError(message)


def policy_logic(now):
    """Load ONLY pinned function definitions; never execute old module top levels."""
    env = dict(dt=dt, json=json, now=now, ROLE=ROLE, RA=RA, IID=IID, IA=IA,
               ADMIN=ADMIN, CW=CW, CORE=CORE)
    for path, sha in PINS.items():
        require(digest(path) == sha, 'Pinned source drift')
    raw = (SOURCE / 'least-privilege-policy.json').read_bytes()
    require(hashlib.sha256(raw).hexdigest() == EXPECTED, 'Candidate drift')
    prior = json.loads((SOURCE / 'PREIMAGE.json').read_text())
    env.update(policy=json.loads(raw), core=prior['iam']['managed'][CORE]['version']['PolicyVersion']['Document'])
    selected = {'context', 'vectors', 'norm', 'expected_pre'}
    nodes = [n for n in ast.parse((SOURCE / 'retry.py').read_text()).body
             if isinstance(n, ast.FunctionDef) and n.name in selected]
    require({n.name for n in nodes} == selected)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE / 'retry.py'), 'exec'), env)
    return SimpleNamespace(**env), prior


class Clock:
    def mono(self):
        return time.monotonic()

    def now(self):
        return dt.datetime.now(dt.timezone.utc)

    def boot(self):
        return Path('/proc/sys/kernel/random/boot_id').read_text().strip()

    def sleep(self, seconds):
        time.sleep(seconds)

    @contextmanager
    def limit(self, seconds):
        def timeout(signum, frame):
            raise TimeoutError('Bounded deadline')
        require(seconds > 0, 'No remaining time')
        handler = signal.signal(signal.SIGALRM, timeout)
        old, interval = signal.setitimer(signal.ITIMER_REAL, seconds)
        started = self.mono()
        try:
            yield
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, handler)
            if old:
                signal.setitimer(signal.ITIMER_REAL, max(0.000001, old - (self.mono() - started)), interval)


class Live:
    """Created ONLY within the timed, explicitly authorized entry point."""
    def __init__(self):
        self.session = None
        self.clients = {}

    def prepare_sdk(self, service, method, args):
        if service not in self.clients:
            self.clients[service] = self.session.client(service, config=verification_config())
            cfg = self.clients[service].meta.config
            require(cfg.retries == verification_config().retries)
            require((cfg.connect_timeout, cfg.read_timeout) == (3, 6))
        dispatch = getattr(self.clients[service], method)
        return lambda: dispatch(**args)

    def sdk(self, service, method, args):
        return self.prepare_sdk(service, method, args)()

    def local(self, kind, arg=None):
        if kind == 'credential':
            if self.session is None:
                import boto3
                import botocore
                require((boto3.__version__, botocore.__version__) == ('1.42.89', '1.42.89'), 'SDK baseline drift')
                self.session = boto3.Session(region_name='us-west-2')
            require(self.session.get_credentials().method == 'iam-role')
            return 'iam-role'
        if kind == 'owners':
            import stat
            out = []
            for p in PATHS:
                s = os.lstat(p)
                out.append(dict(path=p, uid=s.st_uid, gid=s.st_gid, mode=oct(stat.S_IMODE(s.st_mode)),
                                inode=s.st_ino, device=s.st_dev, type='directory' if stat.S_ISDIR(s.st_mode) else 'other'))
            return out
        commands = {
            'hash': ['sudo', '-n', 'sha256sum', '--', arg] if arg else [],
            'hashes': ['sudo', '-n', 'sha256sum', '--', *HASH_PATHS],
            'groups': ['id', 'ubuntu'], 'sudo': ['sudo', '-n', 'true'],
            'ssm_local_root': ['sudo', '-n', '-u', 'ssm-user', 'sudo', '-n', 'id', '-u'],
            'ssm_agent': ['systemctl', 'is-active', 'snap.amazon-ssm-agent.amazon-ssm-agent.service'],
            'cloudwatch_agent': ['systemctl', 'is-active', 'amazon-cloudwatch-agent.service'],
            'cron_user': ['crontab', '-l'], 'cron_root': ['sudo', '-n', 'crontab', '-l']}
        p = subprocess.run(commands[kind], capture_output=True, timeout=30)
        if kind.startswith('cron_'):
            return {'exit_code': p.returncode, 'sha256': hashlib.sha256(p.stdout).hexdigest()}
        require(p.returncode == 0, 'Local read check failed')
        text = p.stdout.decode().strip()
        if kind == 'hashes':
            rows = [line.split(None, 1) for line in text.splitlines()]
            require(len(rows) == len(HASH_PATHS) and all(len(row) == 2 for row in rows))
            require([row[1] for row in rows] == HASH_PATHS)
            require(all(re.fullmatch('[a-f0-9]{64}', row[0]) for row in rows))
            return {path: sha for sha, path in rows}
        if kind == 'hash':
            require(re.fullmatch('[a-f0-9]{64}', text.split()[0]) is not None)
            return text.split()[0]
        if kind == 'ssm_local_root':
            require(text == '0')
        return text


class Driver:
    def __init__(self, directory, adapter_factory=Live, clock=None, recovery=None, started=None):
        self.clock = clock or Clock()
        self.started = self.clock.mono() if started is None else started
        self.work_end = self.started + WORK_SECONDS
        self.d = Path(directory)
        self.factory = adapter_factory
        self.adapter = None
        self.credential_checked = False
        self.recovery_seam = recovery
        self.phase, self.operation = 'initialization', 'None'
        self.recovering = False
        self.recovery_attempted = False
        self.error_saved = False
        self.initialization_error = None
        self.logic = self.pre = None
        self.state_loaded = False
        self.r = {
            'schema': 'root-custody-stage2-repaired-v1', 'status': 'PREPARING', 'task_complete': False,
            'candidate_sha256': EXPECTED, 'stage': 'preflight', 'unit': 0, 'data': {},
            'mutation_started': False, 'guard_removed': False, 'rollback_triggered': False,
            'completed_phases': [], 'call_number': 0, 'inflight': None}
        try:
            with self.clock.limit(self.work_end - self.clock.mono()):
                if (self.d / 'RUN.json').exists():
                    self.r = json.loads((self.d / 'RUN.json').read_text())
                self.state_loaded = True
                self.logic, self.prior = policy_logic(self.clock.now)
                self.pre = json.loads((self.d / 'PREIMAGE.json').read_text()) if (self.d / 'PREIMAGE.json').exists() else self.prior
        except BaseException as exc:
            self.initialization_error = exc
            self.capture_error(exc)

    def save(self):
        require(self.state_loaded, 'Unreadable state must not be overwritten')
        atomic(self.d / 'RUN.json', self.r)

    def receipt(self, kind, **data):
        row = {'utc': self.clock.now().isoformat(), 'event': kind, **data}
        fd = os.open(self.d / 'PHASES.jsonl', os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, 'w') as f:
            f.write(json.dumps(row, default=str) + '\n')
            f.flush()
            os.fsync(f.fileno())
        fd = os.open(self.d, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return row

    def call(self, service, method, before_dispatch=None, **args):
        self.operation = ''.join(part.title() for part in method.split('_'))
        try:
            with self.clock.limit(min(SDK_SECONDS, self.work_end - self.clock.mono())):
                if self.adapter is None:
                    self.adapter = self.factory()
                if not self.credential_checked:
                    require(self.adapter.local('credential') == 'iam-role')
                    self.credential_checked = True
                # Live preparation resolves the lazy client and bound method within
                # this same SDK/work deadline. Injected offline adapters may already
                # expose a preparation-free sdk seam.
                prepare = getattr(self.adapter, 'prepare_sdk', None)
                dispatch = prepare(service, method, args) if prepare else lambda: self.adapter.sdk(service, method, args)
                if before_dispatch is not None:
                    before_dispatch()
                response = dispatch()
            require(not response.get('IsTruncated'), 'Truncated response')
            return response
        except BaseException as exc:
            # Capture only: persistence runs once under the diagnostic deadline.
            self.capture_error(exc)
            raise

    def local(self, kind, arg=None):
        self.operation = 'local:' + kind
        if self.adapter is None:
            self.adapter = self.factory()
        if kind == 'credential' and self.credential_checked:
            return 'iam-role'
        result = self.adapter.local(kind, arg)
        if kind == 'credential':
            require(result == 'iam-role')
            self.credential_checked = True
        return result

    @staticmethod
    def completed(units, *checks):
        # Fold only assertions/local receipts into their producing unit; never
        # combine SDK operations, waits or mutation-intent boundaries.
        name, action = units[-1]
        def finish():
            action()
            for check in checks:
                check()
        return units[:-1] + [(name, finish)]

    def capture_error(self, exc):
        # Keep the original allowlisted cause even if its separate file cannot land.
        key = 'rollback_error' if self.recovering else 'first_error'
        self.r.setdefault(key, {'utc': self.clock.now().isoformat(),
                               'error': error_metadata(exc, self.phase, self.operation)})
        return self.r[key]

    def persist_error(self, exc):
        # Caller supplies the one bounded diagnostic path. Never str(exc).
        row = self.capture_error(exc)
        name = 'ROLLBACK-ERROR.json' if self.recovering else 'FIRST-ERROR.json'
        if not (self.d / name).exists():
            atomic(self.d / name, row)
        self.error_saved = True

    def same_preimage(self, snap):
        require(all(self.logic.norm(snap[k]) == self.logic.norm(self.pre[k])
                    for k in ['iam', 'ownership_modes', 'preservation']), 'Preimage drift')
        self.identity(snap['caller'])

    def identity(self, ident):
        require(ident['Arn'] == 'arn:aws:sts::271095146765:assumed-role/' + ROLE + '/' + IID)
        require(ident['Account'] == '271095146765')

    def desired(self, with_guard=True):
        return {'attached': [CORE], 'inline': {POL: self.logic.policy, **({GUARD: self.r['guard']} if with_guard else {})}}

    def state(self, snap):
        return self.logic.expected_pre(snap)

    def snapshot_units(self, label):
        # Fixed scoped plan: fresh listings must match the checkpoint's exact
        # expected names before any absent-policy slots can be omitted. The plan
        # length never depends on partially collected data or changes on resume.
        before = self.logic.expected_pre(self.pre)
        if label in ['preflight-snapshot', 'rehearsal-preimage', 'apply-preimage', 'ROLLBACK-READBACK']:
            expected = before
        elif label in ['rehearsal-staged', 'apply-staged']:
            expected = {'attached': before['attached'], 'inline': set(before['inline']) | {POL, GUARD}}
        elif label == 'rollback-before':
            expected = None  # Interrupted mutations require the full scoped inventory.
        else:
            expected = self.desired(not self.r['guard_removed'])
        data = self.r['data'].setdefault(label, {'iam': {'managed': {}, 'inline': {}}, 'preservation': {'sha256': {}}})
        units = []
        def put(key, method, **args):
            def read():
                data['iam'][key] = self.call('iam', method, **args)
                if expected is not None and key == 'attached':
                    require(Counter(v['PolicyArn'] for v in data['iam'][key]['AttachedPolicies']) == Counter(expected['attached']), 'Attachment drift')
                if expected is not None and key == 'inline_names':
                    require(Counter(data['iam'][key]['PolicyNames']) == Counter(list(expected['inline'])), 'Inline name drift')
            units.append((label + ':' + key, read))
        put('role', 'get_role', RoleName=ROLE)
        put('attached', 'list_attached_role_policies', RoleName=ROLE)
        put('inline_names', 'list_role_policies', RoleName=ROLE)
        for arn in sorted(before['attached'] if expected is None else expected['attached']):
            def metadata(arn=arn):
                if arn in [v['PolicyArn'] for v in data['iam']['attached']['AttachedPolicies']]:
                    data['iam']['managed'][arn] = {'metadata': self.call('iam', 'get_policy', PolicyArn=arn)}
            def version(arn=arn):
                if arn in data['iam']['managed']:
                    data['iam']['managed'][arn]['version'] = self.call('iam', 'get_policy_version', PolicyArn=arn,
                        VersionId=data['iam']['managed'][arn]['metadata']['Policy']['DefaultVersionId'])
            units.extend([(label + ':managed_metadata', metadata), (label + ':managed_version', version)])
        for name in sorted(set(before['inline']) | {POL, GUARD} if expected is None else expected['inline']):
            def inline(name=name):
                if name in data['iam']['inline_names']['PolicyNames']:
                    data['iam']['inline'][name] = self.call('iam', 'get_role_policy', RoleName=ROLE, PolicyName=name)
            units.append((label + ':inline', inline))
        put('instance_profiles', 'list_instance_profiles_for_role', RoleName=ROLE)
        def local_snapshot():
            # One aggregate deadline, not eight independent 30-second maxima.
            with self.clock.limit(min(30, self.work_end - self.clock.mono())):
                data['ownership_modes'] = self.local('owners')
                data['preservation']['sha256'] = self.local('hashes')
                require(set(data['preservation']['sha256']) == set(HASH_PATHS))
                data['preservation']['groups'] = self.local('groups')
        units.append((label + ':local', local_snapshot))
        units.append((label + ':caller', lambda: data.__setitem__('caller', self.call('sts', 'get_caller_identity'))))
        def finish():
            data['credential_provider_method'] = self.local('credential')
            names = set(data['iam']['inline_names']['PolicyNames'])
            attached = {v['PolicyArn'] for v in data['iam']['attached']['AttachedPolicies']}
            require(names == set(data['iam']['inline']))
            require(attached == set(data['iam']['managed']))
            self.identity(data['caller'])
            require(data['credential_provider_method'] == 'iam-role')
            data['captured_at_utc'] = self.clock.now().isoformat()
            atomic(self.d / (label + '.json'), data)
        return self.completed(units, finish)

    def check_units(self, label, snapshot=None):
        out = self.r['data'].setdefault(label, {})
        units = []
        def sdk(key, service, method, args, select=lambda r: r):
            units.append((label + ':' + key, lambda: out.__setitem__(key, select(self.call(service, method, **args)))))
        def local_health():
            with self.clock.limit(min(30, self.work_end - self.clock.mono())):
                for kind in ['sudo', 'ssm_local_root', 'ssm_agent', 'cloudwatch_agent']:
                    out[kind] = self.local(kind)
        units.append((label + ':local_health', local_health))
        sdk('ssm_associations', 'ssm', 'list_associations', {'AssociationFilterList': [{'key': 'InstanceId', 'value': IID}]}, lambda r: len(r.get('Associations', [])))
        sdk('ssm_sessions', 'ssm', 'describe_sessions', {'State': 'Active', 'Filters': [{'key': 'Target', 'value': IID}], 'MaxResults': 20}, lambda r: len(r.get('Sessions', [])))
        sdk('ssm_parameters', 'ssm', 'describe_parameters', {'MaxResults': 5}, lambda r: len(r.get('Parameters', [])))
        sdk('ssm_commands', 'ssm', 'list_commands', {'InstanceId': IID, 'MaxResults': 10}, lambda r: [{'CommandId': v['CommandId'], 'Status': v['Status']} for v in r.get('Commands', [])])
        def invocation():
            command = next((v for v in out['ssm_commands'] if v['Status'] == 'Success'), None)
            require(command is not None)
            response = self.call('ssm', 'get_command_invocation', CommandId=command['CommandId'], InstanceId=IID)
            out['ssm_command_invocation'] = {k: response[k] for k in ['CommandId', 'InstanceId', 'Status', 'DocumentName']}
        units.append((label + ':invocation', invocation))
        sdk('ssm_document', 'ssm', 'get_document', {'Name': 'AWS-RunShellScript'}, lambda r: {'name': r['Name'], 'status': r['Status']})
        sdk('ec2_instances', 'ec2', 'describe_instances', {'InstanceIds': [IID]}, lambda r: sum(len(v['Instances']) for v in r['Reservations']))
        sdk('ec2_status', 'ec2', 'describe_instance_status', {'InstanceIds': [IID], 'IncludeAllInstances': True},
            lambda r: [{'InstanceId': v['InstanceId'], 'InstanceState': v['InstanceState']['Name'], 'SystemStatus': v['SystemStatus']['Status'], 'InstanceStatus': v['InstanceStatus']['Status']} for v in r['InstanceStatuses']])
        sdk('ec2_serial', 'ec2', 'get_serial_console_access_status', {}, lambda r: r['SerialConsoleAccessEnabled'])
        sdk('ec2_tags', 'ec2', 'describe_tags', {'Filters': [{'Name': 'resource-id', 'Values': [IID]}]}, lambda r: len(r.get('Tags', [])))
        sdk('ec2_volumes', 'ec2', 'describe_volumes', {'Filters': [{'Name': 'attachment.instance-id', 'Values': [IID]}]}, lambda r: len(r.get('Volumes', [])))
        sdk('s3_bucket_list', 's3', 'list_buckets', {}, lambda r: len(r.get('Buckets', [])))
        sdk('secrets_manager_list', 'secretsmanager', 'list_secrets', {'MaxResults': 5}, lambda r: len(r.get('SecretList', [])))
        sdk('cloudwatch_metric_list', 'cloudwatch', 'list_metrics', {'Namespace': 'CWAgent', 'MetricName': 'mem_used_percent', 'Dimensions': [{'Name': 'InstanceId', 'Value': IID}]}, lambda r: len(r.get('Metrics', [])))
        if snapshot is None:
            snapshot = label + '-snapshot'
            units += self.snapshot_units(snapshot)
        def preservation():
            snap = self.r['data'][snapshot]
            out['preservation_snapshot'] = snapshot
            require(snap['preservation'] == self.pre['preservation'])
            require(snap['ownership_modes'] == self.pre['ownership_modes'])
            require(bool(out['ec2_status']) and all(v['SystemStatus'] == 'ok' and v['InstanceStatus'] == 'ok' for v in out['ec2_status']))
        units = self.completed(units, preservation)
        # Fresh recovery is last, not a cached sample from the start of a long check.
        sdk('connection', 'ssm', 'get_connection_status', {'Target': IID}, lambda r: {k: r[k] for k in ['Target', 'Status']})
        sdk('ssm', 'ssm', 'describe_instance_information', {'Filters': [{'Key': 'InstanceIds', 'Values': [IID]}]},
            lambda r: [{k: v[k] for k in ['InstanceId', 'PingStatus', 'LastPingDateTime', 'AgentVersion']} for v in r['InstanceInformationList']])
        def metric():
            t = self.clock.now()
            response = self.call('cloudwatch', 'get_metric_statistics', Namespace='CWAgent', MetricName='mem_used_percent',
                Dimensions=[{'Name': 'InstanceId', 'Value': IID}], StartTime=t-dt.timedelta(minutes=15), EndTime=t, Period=60, Statistics=['Average'])
            points = sorted(response['Datapoints'], key=lambda v: timestamp(v['Timestamp']))
            require(points)
            out['cloudwatch'] = {'count': len(points), 'latest': points[-1]}
        units.append((label + ':metric', metric))
        def finish():
            require(len(out['ssm']) == 1 and out['ssm'][0]['PingStatus'] == 'Online')
            require(out['connection']['Status'] == 'connected')
            ping = timestamp(out['ssm'][0]['LastPingDateTime'])
            metric_time = timestamp(out['cloudwatch']['latest']['Timestamp'])
            require(0 <= (self.clock.now() - ping).total_seconds() <= 420)
            require(0 <= (self.clock.now() - metric_time).total_seconds() <= 300)
            out['observed_at_utc'] = self.clock.now().isoformat()
            if label in ['phase600', 'fresh_recovery_before_guard_removal', 'final_guard_absent']:
                cut = timestamp(self.r['cutover_utc'])
                require(ping > cut + dt.timedelta(seconds=60) and metric_time > cut + dt.timedelta(seconds=60))
            atomic(self.d / (label + '.json'), out)
            self.receipt('durable_phase_receipt', phase=label, heartbeat_utc=ping, metric_utc=metric_time)
        return self.completed(units, finish)

    def simulation_units(self, label, principal=False, expired=False):
        data = self.r['data'].setdefault(label, {'results': [], 'requests': 0})
        units = []
        for v in self.logic.vectors():
            for offset in range(0, len(v['actions']), 80):
                actions = v['actions'][offset:offset+80]
                def simulate(v=v, actions=actions):
                    at = timestamp(self.r['guard_expiry_utc']) + dt.timedelta(seconds=1) if expired else None
                    args = {'ActionNames': actions, 'ResourceArns': [v['resource']],
                            'ContextEntries': self.logic.context(at=at, **v['context']), 'MaxItems': 1000}
                    args.update({'PolicySourceArn': RA} if principal else {'PolicyInputList': [json.dumps(self.logic.policy), json.dumps(self.logic.core)]})
                    response = self.call('iam', 'simulate_principal_policy' if principal else 'simulate_custom_policy', **args)
                    results = response['EvaluationResults']
                    require(Counter((a, v['resource']) for a in actions) == Counter((r['EvalActionName'], r['EvalResourceName']) for r in results), 'Incomplete simulator output')
                    require(all(r['EvalDecision'] == v['expected'] and not r.get('MissingContextValues') for r in results), 'Simulation mismatch')
                    for result in results:
                        data['results'].append({'action': result['EvalActionName'], 'resource': result['EvalResourceName'],
                            'decision': result['EvalDecision'], 'expected': v['expected'], 'missing_context': result.get('MissingContextValues', []),
                            'context': args['ContextEntries'], 'request_id': response['ResponseMetadata']['RequestId']})
                    data['requests'] += 1
                    self.receipt('simulation_unit_passed', label=label, requests=data['requests'], count=len(data['results']),
                                 request_id=_token(response['ResponseMetadata']['RequestId']), sdk_retry_attempts=response['ResponseMetadata'].get('RetryAttempts'))
                units.append((label + ':vector', simulate))
        require(len(units) == 29)
        def complete():
            decisions = Counter(v['decision'] for v in data['results'])
            require(data['requests'] == 29 and len(data['results']) == 315)
            require(decisions == {'allowed': 54, 'implicitDeny': 261})
            data.update(label=label, principal=principal, count=315, passed=True, allowed_count=54, implicit_deny_count=261,
                        simulated_time_override=(timestamp(self.r['guard_expiry_utc']) + dt.timedelta(seconds=1)).isoformat() if expired else None)
            atomic(self.d / (label + '.json'), data)
        return self.completed(units, complete)

    def cron_units(self, label):
        out = self.r['data'].setdefault(label, {})
        def read():
            with self.clock.limit(min(30, self.work_end - self.clock.mono())):
                for kind in ['cron_user', 'cron_root']:
                    out[kind] = self.local(kind)
        units = [(label + ':local', read)]
        def finish():
            old = json.loads((CAPTURE / 'CRON-SCAN.json').read_text())
            for kind, key in [('cron_user', 'crontab -l'), ('cron_root', 'sudo -n crontab -l')]:
                require(out[kind] == {'exit_code': old[key]['rc'], 'sha256': old[key]['sha256']})
            if label == 'cron_after':
                require(out == self.r['data']['cron_before'])
        return self.completed(units, finish)

    def staged_state(self, label):
        before = self.logic.expected_pre(self.pre)
        require(self.state(self.r['data'][label]) == {'attached': before['attached'],
            'inline': {**before['inline'], POL: self.logic.policy, GUARD: self.r['guard']}})

    def guard_margin(self, first_apply=False):
        if 'guard_expiry_utc' in self.r and not self.r['guard_removed']:
            margin = 1200 if first_apply else RECOVERY_SECONDS + WORK_SECONDS + FOOTER_SECONDS
            require((timestamp(self.r['guard_expiry_utc']) - self.clock.now()).total_seconds() > margin,
                    'Insufficient guard lifetime' if first_apply else 'Guard recovery margin exhausted')

    def mutate(self, method, before_dispatch=None, **args):
        first_apply = self.r['stage'] == 'apply' and not self.r['mutation_started']
        self.guard_margin(first_apply)
        self.local('credential')  # Resolve credentials before fresh effect admission.
        self.guard_margin(first_apply)
        self.r['mutation_started'] = True
        self.save()  # Persist intent before every mutation; ambiguous interruption is never replayed.
        self.guard_margin(first_apply)  # Refuse stale intent before client preparation.
        def admit():
            self.guard_margin(first_apply)
            if before_dispatch is not None:
                before_dispatch()
        return self.call('iam', method, before_dispatch=admit, RoleName=ROLE, **args)

    def stage_units(self):
        return [('stage:candidate', lambda: self.mutate('put_role_policy', PolicyName=POL, PolicyDocument=json.dumps(self.logic.policy))),
                ('stage:guard', lambda: self.mutate('put_role_policy', PolicyName=GUARD, PolicyDocument=json.dumps(self.r['guard'])))]

    def plan(self):
        stage = self.r['stage']
        if stage == 'preflight':
            def bind():
                self.same_preimage(self.r['data']['preflight-snapshot'])
                self.pre = self.r['data']['preflight-snapshot']
                atomic(self.d / 'PREIMAGE.json', self.pre)
                self.r['preimage_sha256'] = digest(self.d / 'PREIMAGE.json')
                raw = (SOURCE / 'least-privilege-policy.json').read_bytes()
                (self.d / 'least-privilege-policy.json').write_bytes(raw)
                os.chmod(self.d / 'least-privilege-policy.json', 0o600)
                require(digest(self.d / 'least-privilege-policy.json') == EXPECTED)
            def context():
                keys = self.call('iam', 'get_context_keys_for_custom_policy', PolicyInputList=[json.dumps(self.logic.policy), json.dumps(self.logic.core)])['ContextKeyNames']
                require(set(keys) <= {v['ContextKeyName'] for v in self.logic.context()})
            def canary():
                response = self.call('iam', 'simulate_principal_policy', PolicySourceArn=RA, ActionNames=['iam:GetRole'], ResourceArns=[RA], ContextEntries=self.logic.context())
                self.exact_allow(response, ['iam:GetRole'])
            return self.completed(self.snapshot_units('preflight-snapshot'), bind) + self.check_units('before', 'preflight-snapshot') + [('preflight:context', context), ('preflight:principal_canary', canary)] + self.simulation_units('CANDIDATE-PREFLIGHT') + self.cron_units('cron_before')
        if stage == 'rehearse':
            def guard():
                self.same_preimage(self.r['data']['rehearsal-preimage'])
                expiry = self.clock.now() + dt.timedelta(minutes=45)
                g = json.loads((SOURCE / 'TEMPORARY-ROLLBACK-POLICY.json').read_text())
                for statement in g['Statement']:
                    statement['Condition']['DateLessThan']['aws:CurrentTime'] = expiry.isoformat()
                self.r.update(guard=g, guard_expiry_utc=expiry.isoformat())
                atomic(self.d / 'TEMPORARY-ROLLBACK-POLICY.json', g)
            def guard_sim():
                response = self.call('iam', 'simulate_custom_policy', PolicyInputList=[json.dumps(self.r['guard'])],
                    ActionNames=['iam:AttachRolePolicy', 'iam:DeleteRolePolicy'], ResourceArns=[RA], ContextEntries=self.logic.context())
                self.exact_allow(response, ['iam:AttachRolePolicy', 'iam:DeleteRolePolicy'])
            return self.snapshot_units('rehearsal-preimage') + [('rehearse:guard', guard), ('rehearse:guard_simulation', guard_sim)] + self.stage_units() + self.completed(self.snapshot_units('rehearsal-staged'), lambda: self.staged_state('rehearsal-staged')) + [('rehearse:rollback', self.rehearsal_recovery)]
        if stage == 'apply':
            def before():
                self.same_preimage(self.r['data']['apply-preimage'])
                require((timestamp(self.r['guard_expiry_utc']) - self.clock.now()).total_seconds() > 1200, 'Insufficient guard lifetime')
            def core_check():
                response = self.call('iam', 'list_attached_role_policies', RoleName=ROLE)
                require(CORE in [v['PolicyArn'] for v in response['AttachedPolicies']])
            def detach():
                self.mutate('detach_role_policy', PolicyArn=ADMIN)
                self.r.update(cutover_utc=self.clock.now().isoformat(), cutover_mono=self.clock.mono(), boot_id=self.clock.boot())
                self.receipt('administrator_access_detached', cutover_utc=self.r['cutover_utc'])
            return (self.snapshot_units('apply-preimage') + self.check_units('fresh_before_apply', 'apply-preimage') + [('apply:binding', before)]
                + self.stage_units() + self.completed(self.snapshot_units('apply-staged'), lambda: self.staged_state('apply-staged'))
                + [('apply:delete_old_inline', lambda: self.mutate('delete_role_policy', PolicyName='ops-readonly-verify')),
                   ('apply:detach_cw', lambda: self.mutate('detach_role_policy', PolicyArn=CW)), ('apply:retain_core', core_check), ('apply:detach_admin', detach)]
                + self.completed(self.snapshot_units('immediate-state'), lambda: require(self.state(self.r['data']['immediate-state']) == self.desired()))
                + self.check_units('immediate', 'immediate-state') + self.simulation_units('PRINCIPAL-PREFINAL-GUARD-EXPIRED-CONTEXT', True, True))
        if stage in ['phase180', 'phase360', 'phase480', 'phase600']:
            def observed():
                elapsed = self.elapsed()
                require(elapsed >= int(stage[5:]))
                self.r['elapsed_since_cutover_seconds'] = elapsed
                self.r['completed_phases'].append(stage)
                if stage == 'phase600':
                    self.r.update(ten_minute_window_complete=True, fresh_natural_ssm_and_metrics_after_cutover=True)
            return ([(stage + ':wait', lambda: self.wait_until(int(stage[5:])))]
                + self.completed(self.snapshot_units(stage + '-state'), lambda: require(self.state(self.r['data'][stage + '-state']) == self.desired()))
                + self.completed(self.check_units(stage, stage + '-state'), observed))
        if stage == 'finalize':
            def gate():
                require(self.r.get('ten_minute_window_complete') and self.elapsed() >= 600)
                require(self.state(self.r['data']['final-guard-state']) == self.desired())
            return self.completed(self.snapshot_units('final-guard-state'), gate) + self.simulation_units('FINAL-PERMISSIONS-GUARD-EXPIRED-CONTEXT', True, True) + self.check_units('fresh_recovery_before_guard_removal') + self.cron_units('cron_after')
        if stage == 'cleanup':
            def permission():
                # Independent approval is checked anew in advance, not inferred from an earlier run.
                response = self.call('iam', 'simulate_principal_policy', PolicySourceArn=RA,
                    ActionNames=['iam:DeleteRolePolicy'], ResourceArns=[RA], ContextEntries=self.logic.context())
                self.exact_allow(response, ['iam:DeleteRolePolicy'])
            def fresh_recovery():
                recent = self.r['data']['fresh_recovery_before_guard_removal']
                require(0 <= (self.clock.now() - timestamp(recent['ssm'][0]['LastPingDateTime'])).total_seconds() <= 420, 'Cleanup heartbeat aged out')
                require(0 <= (self.clock.now() - timestamp(recent['cloudwatch']['latest']['Timestamp'])).total_seconds() <= 300, 'Cleanup metric aged out')
            def remove():
                fresh_recovery()
                # Recheck after intent, credentials and lazy client preparation.
                self.mutate('delete_role_policy', before_dispatch=fresh_recovery, PolicyName=GUARD)
                self.r['guard_removed'] = True
                self.receipt('temporary_rollback_guard_removed')
            return [('cleanup:permission', permission), ('cleanup:remove', remove)] + self.completed(self.snapshot_units('guard-absent-state'), lambda: require(self.state(self.r['data']['guard-absent-state']) == self.desired(False))) + self.simulation_units('FINAL-PRINCIPAL-SIMULATION', True) + self.check_units('final_guard_absent') + self.snapshot_units('POSTIMAGE') + [('cleanup:accept', self.accept)]
        return []

    def exact_allow(self, response, actions):
        results = response['EvaluationResults']
        require(Counter((a, RA) for a in actions) == Counter((v['EvalActionName'], v['EvalResourceName']) for v in results))
        require(all(v['EvalDecision'] == 'allowed' and not v.get('MissingContextValues') for v in results))

    def elapsed(self):
        require(self.clock.boot() == self.r['boot_id'], 'Boot changed; observation cannot resume')
        mono = self.clock.mono() - self.r['cutover_mono']
        wall = (self.clock.now() - timestamp(self.r['cutover_utc'])).total_seconds()
        require(mono >= 0 and abs(mono - wall) <= 5, 'Clock discontinuity')
        return mono

    def wait_until(self, target):
        remaining = max(0, target - self.elapsed())
        seconds = min(WAIT_SECONDS, remaining)
        self.receipt('phase_wait_started', phase=self.r['stage'], foreground_wait_seconds=seconds)
        self.clock.sleep(seconds)
        self.r['repeat_wait'] = self.elapsed() < target
        self.receipt('phase_wait_complete', elapsed_since_cutover_seconds=self.elapsed())

    def blocking_snapshot(self, label):
        self.r['data'].pop(label, None)
        for _, fn in self.snapshot_units(label):
            fn()
        return self.r['data'][label]

    def recovery(self):
        """Same bounded routine for explicit rollback, rehearsal, and any failure.
        This is a 140-second attempt, NOT a guarantee under worst-case AWS faults.
        After guard deletion only independent off-host IAM recovery is trustworthy.
        """
        self.recovering = True
        require(not self.recovery_attempted, 'No repeated recovery loop')
        self.recovery_attempted = True
        self.recovery_end = min(self.started + CALL_SECONDS - FOOTER_SECONDS, self.clock.mono() + RECOVERY_SECONDS)
        # Rollback diagnostics belong INSIDE recovery's 140s, not its footer.
        self.work_end = self.recovery_end - DIAGNOSTIC_SECONDS
        with self.clock.limit(self.work_end - self.clock.mono()):
            if self.recovery_seam:
                return self.recovery_seam(self)
            require(self.logic is not None and self.pre is not None and self.state_loaded,
                    'Initialization incomplete; independent off-host administrator required')
            require(not self.r['guard_removed'], 'Independent off-host administrator required')
            require(self.r.get('preimage_sha256') == digest(self.d / 'PREIMAGE.json'), 'Bound recovery preimage unavailable')
            if self.adapter is None:
                self.adapter = self.factory()
            # Reattach first, before diagnostic reads can consume the recovery reserve.
            self.call('iam', 'attach_role_policy', RoleName=ROLE, PolicyArn=ADMIN)
            self.receipt('rollback_attach_admin_success')
            self.clock.sleep(20)
            before = self.blocking_snapshot('rollback-before')
            state = self.state(before)
            expected = self.logic.expected_pre(self.pre)
            require(set(state['inline']) <= set(expected['inline']) | {POL, GUARD})
            require(set(state['attached']) <= set(expected['attached']))
            for arn in expected['attached']:
                self.call('iam', 'attach_role_policy', RoleName=ROLE, PolicyArn=arn)
            for name, document in expected['inline'].items():
                self.call('iam', 'put_role_policy', RoleName=ROLE, PolicyName=name, PolicyDocument=json.dumps(document))
            for name in state['inline']:
                if name not in expected['inline']:
                    require(name in [POL, GUARD])
                    self.call('iam', 'delete_role_policy', RoleName=ROLE, PolicyName=name)
            post = self.blocking_snapshot('ROLLBACK-READBACK')
            self.same_preimage(post)
            for _, fn in self.check_units('rollback_recovery', 'ROLLBACK-READBACK'):
                fn()
            self.r.update(rollback_exact_preimage_match=True, administrator_access_currently_attached=True,
                          temporary_guard_present=False, candidate_present=False)
            self.receipt('rollback_verified')

    def rehearsal_recovery(self):
        self.recovery()
        self.recovering = False
        self.r.update(mutation_started=False, rollback_command_rehearsal={'passed': True, 'same_routine_as_explicit_rollback': True})

    def accept(self):
        recent = self.r['data']['final_guard_absent']
        require(0 <= (self.clock.now() - timestamp(recent['ssm'][0]['LastPingDateTime'])).total_seconds() <= 420, 'Final heartbeat aged out')
        require(0 <= (self.clock.now() - timestamp(recent['cloudwatch']['latest']['Timestamp'])).total_seconds() <= 300, 'Final metric aged out')
        post = self.r['data']['POSTIMAGE']
        require(self.state(post) == self.desired(False))
        for key in ['role', 'instance_profiles']:
            require(self.logic.norm(post['iam'][key]) == self.logic.norm(self.pre['iam'][key]))
        require(self.logic.norm(post['iam']['managed'][CORE]) == self.logic.norm(self.pre['iam']['managed'][CORE]))
        require(post['preservation'] == self.pre['preservation'])
        require(post['ownership_modes'] == self.pre['ownership_modes'])
        self.r.update(status='PASS', task_complete=True, temporary_guard_present=False, candidate_present=True,
                      administrator_access_currently_attached=False, final_policy_state=self.state(post),
                      exact_changes={'added_inline': [POL], 'deleted_inline': ['ops-readonly-verify'],
                                     'detached_managed': [ADMIN, CW], 'retained_managed': [CORE], 'trust_and_instance_profile_unchanged': True})

    @staticmethod
    def readonly_unit(name):
        # Positive list of existing read/predicate units, not a default-safe
        # blacklist. Unknown/new units and all authorization/effect/wait slots stop.
        label, _, kind = name.partition(':')
        snapshots = {'preflight-snapshot', 'rehearsal-preimage', 'rehearsal-staged',
                     'apply-preimage', 'apply-staged', 'immediate-state',
                     'phase180-state', 'phase360-state', 'phase480-state', 'phase600-state',
                     'final-guard-state', 'fresh_recovery_before_guard_removal-snapshot',
                     'guard-absent-state', 'final_guard_absent-snapshot', 'POSTIMAGE'}
        checks = {'before', 'fresh_before_apply', 'immediate', 'phase180', 'phase360',
                  'phase480', 'phase600', 'fresh_recovery_before_guard_removal', 'final_guard_absent'}
        simulations = {'CANDIDATE-PREFLIGHT', 'PRINCIPAL-PREFINAL-GUARD-EXPIRED-CONTEXT',
                       'FINAL-PERMISSIONS-GUARD-EXPIRED-CONTEXT', 'FINAL-PRINCIPAL-SIMULATION'}
        return ((label in snapshots and kind in {'role', 'attached', 'inline_names',
                    'managed_metadata', 'managed_version', 'inline', 'instance_profiles', 'local', 'caller'})
                or (label in checks and kind in {'local_health', 'ssm_associations', 'ssm_sessions',
                    'ssm_parameters', 'ssm_commands', 'invocation', 'ssm_document', 'ec2_instances',
                    'ec2_status', 'ec2_serial', 'ec2_tags', 'ec2_volumes', 's3_bucket_list',
                    'secrets_manager_list', 'cloudwatch_metric_list', 'connection', 'ssm', 'metric'})
                or (label in simulations and kind == 'vector')
                or name in {'cron_before:local', 'cron_after:local', 'apply:retain_core',
                            'preflight:context', 'preflight:principal_canary', 'rehearse:guard_simulation'})

    def verify_commit(self):
        # Read durable bytes, not just the live object. A receipt/final-save
        # interruption cannot be turned into an implicit successful next unit.
        durable = json.loads((self.d / 'RUN.json').read_text())
        require(durable == json.loads(json.dumps(self.r, default=str)), 'Commit readback drift')
        require(durable['inflight'] is None and not durable.get('first_error'), 'Uncommitted unit')
        rows = [json.loads(x) for x in (self.d / 'PHASES.jsonl').read_text().splitlines()]
        require(len(rows) >= 2 and rows[-1]['event'] == 'bounded_call_complete'
                and rows[-2]['event'] == 'unit_committed', 'Missing durable footer')
        require(rows[-1]['phase'] == rows[-2]['phase'] == self.phase, 'Footer phase drift')
        require((rows[-2]['next_stage'], rows[-2]['next_unit']) ==
                (durable['stage'], durable['unit']), 'Commit cursor drift')

    def advance(self, command='advance', cleanup_authorized=False, *, readonly_units=1,
                admission=None, verified=None):
        require(type(readonly_units) is int and 1 <= readonly_units <= 8, 'Read-only bound 1..8')
        require(readonly_units == 1 or (command == 'advance' and admission is not None
                and verified is not None), 'Grouping requires bound caller admission/verification')
        stage = self.r['stage']
        for index in range(readonly_units):
            code = self._advance_unit(command, cleanup_authorized, admission=admission,
                                      verified=verified, readonly_only=readonly_units > 1)
            if code == 3:  # Deliberate boundary, no unit started and no effect.
                return 0 if index else 2
            if code or self.r['stage'] != stage:
                return code
        return 0

    def _advance_unit(self, command='advance', cleanup_authorized=False, *,
                      admission=None, verified=None, readonly_only=False):
        # Every internal unit uses the ORIGINAL deadline and failure path.
        # No fresh Driver, recovery reserve, work clock or automatic failed retry.
        # Construction and CLI admission share this invocation's original deadline.
        try:
            if self.initialization_error is not None:
                raise self.initialization_error
            self.phase, self.operation = self.r['stage'], 'phase'
            with self.clock.limit(self.work_end - self.clock.mono()):
                require(command in ['advance', 'rollback'])
                require(not self.r['status'].startswith('FAILED') or command == 'rollback', 'Stopped run cannot restart')
                if self.r['stage'] == 'cleanup' and not cleanup_authorized and command != 'rollback':
                    # No SDK/readback without approval. Describe only durable
                    # knowledge; an uncommitted removal must never imply retention.
                    if self.r['inflight']:
                        self.r['status'] = 'GUARD_STATE_UNCERTAIN_INDEPENDENT_CLEANUP_REQUIRED'
                    elif self.r['guard_removed']:
                        self.r['status'] = 'GUARD_REMOVED_INDEPENDENT_CLEANUP_REQUIRED'
                    else:
                        self.r['status'] = 'VERIFIED_GUARD_RETAINED_INDEPENDENT_CLEANUP_REQUIRED'
                    self.save()
                    return 2
                require(self.r['stage'] != 'done' or command == 'rollback', 'Run already complete')
                if command != 'rollback':
                    self.guard_margin(self.r['stage'] == 'apply' and not self.r['mutation_started'])
                if self.r.get('preimage_sha256'):
                    require(digest(self.d / 'PREIMAGE.json') == self.r['preimage_sha256'], 'Bound preimage bytes drift')
                # A previous uncommitted unit is ambiguous, even if read-only. Recover, never replay.
                require(not self.r['inflight'] or command == 'rollback', 'Unfinished prior foreground call')
                if 'cutover_utc' in self.r:
                    self.elapsed()
                if self.r['mutation_started'] and not self.r['guard_removed'] and command != 'rollback':
                    require((timestamp(self.r['guard_expiry_utc']) - self.clock.now()).total_seconds() > RECOVERY_SECONDS + WORK_SECONDS + FOOTER_SECONDS, 'Guard recovery margin exhausted')
                if readonly_only:
                    import copy
                    current = self.r
                    try:
                        self.r = copy.deepcopy(current)
                        name = self.plan()[self.r['unit']][0]
                    finally:
                        self.r = current
                    if not self.readonly_unit(name):
                        return 3
                if admission is not None:
                    self.credential_checked = False
                    admission(self)  # All exceptions use the native recovery path.
                    self.guard_margin(self.r['stage'] == 'apply' and not self.r['mutation_started'])
                self.r['call_number'] += 1
                if command == 'rollback':
                    self.phase = 'rollback'
                    self.r['inflight'] = {'command': 'rollback'}
                    self.save()
                    # Outside normal-work alarm: dedicated bounded recovery below.
                    unit = None
                else:
                    units = self.plan()
                    name, unit = units[self.r['unit']]
                    self.phase = name
                    self.r['inflight'] = {'phase': name, 'unit': self.r['unit']}
                    self.save()
                    self.receipt('unit_started', phase=name, unit=self.r['unit'], call_number=self.r['call_number'])
                    if name != 'rehearse:rollback':
                        unit()
                        if admission is not None:
                            self.receipt('unit_body_complete', phase=name, unit=self.r['unit'],
                                         call_number=self.r['call_number'])
                    else:
                        unit = None
            if command == 'rollback' or self.phase == 'rehearse:rollback':
                # Work alarm has ended; the recovery cap cannot be accidentally nested under it.
                if command == 'rollback':
                    self.recovery()
                    self.r.update(status='FAILED_ROLLED_BACK', task_complete=False)
                else:
                    self.rehearsal_recovery()
            # Commit is still work, not an unbounded gap outside the work alarm.
            # A recovery command instead uses only its remaining recovery allowance.
            with self.clock.limit(min(DIAGNOSTIC_SECONDS, self.work_end - self.clock.mono())):
                if command != 'rollback':
                    if not self.r.pop('repeat_wait', False):
                        self.r['unit'] += 1
                    if self.r['unit'] == len(units):
                        order = ['preflight', 'rehearse', 'apply', 'phase180', 'phase360', 'phase480', 'phase600', 'finalize', 'cleanup', 'done']
                        completed = self.r['stage']
                        self.r.update(stage=order[order.index(completed)+1], unit=0)
                        if completed == 'preflight':
                            self.r['status'] = 'PREFLIGHT_PASS_NO_MUTATION'
                        elif completed == 'rehearse':
                            self.r['status'] = 'ROLLBACK_REHEARSAL_PASS'
                        elif completed == 'apply':
                            self.r['status'] = 'OBSERVING_GUARD_PRESENT'
                            self.r['completed_phases'].append('immediate')
                # Do not clear durable intent until the terminal receipt succeeds.
                self.save()
                self.receipt('unit_committed', phase=self.phase, next_stage=self.r['stage'], next_unit=self.r['unit'])
            # Success footer is work too: a failure leaves the full recovery reserve.
            self.footer(self.work_end, clear_intent=True)
            if readonly_only or verified is not None:
                with self.clock.limit(self.work_end - self.clock.mono()):
                    self.verify_commit()
                    if verified is not None:
                        verified(self)
            return 0
        except BaseException as exc:
            self.r.update(status='FAILED_STOPPED', task_complete=False)
            self.capture_error(exc)
            try:
                diagnostic_end = self.recovery_end if self.recovery_attempted else self.started + WORK_SECONDS + DIAGNOSTIC_SECONDS
                with self.clock.limit(min(DIAGNOSTIC_SECONDS, diagnostic_end - self.clock.mono())):
                    self.persist_error(exc)
                    self.receipt('stopped_on_failure', phase=self.phase, error_type=_token(type(exc).__name__))
                    self.save()
            except BaseException:
                # A broken report must never replace the cause or prevent recovery.
                self.r['error_persistence_failed'] = True
            if self.r['mutation_started'] or 'cutover_utc' in self.r or command == 'rollback':
                self.r['rollback_triggered'] = True
                if self.recovering:
                    # Its diagnostics were already bounded above; never repeat recovery.
                    self.r['status'] = 'FAILED_REQUIRES_OFFHOST_ADMIN'
                else:
                    try:
                        self.recovery()
                        self.r['status'] = 'FAILED_ROLLED_BACK'
                        self.r['inflight'] = None
                    except BaseException as rb:
                        self.r['status'] = 'FAILED_REQUIRES_OFFHOST_ADMIN'
                        self.recovering = True
                        self.capture_error(rb)
                        try:
                            with self.clock.limit(min(DIAGNOSTIC_SECONDS, self.recovery_end - self.clock.mono())):
                                self.persist_error(rb)
                        except BaseException:
                            self.r['rollback_error_persistence_failed'] = True
            try:
                self.footer(self.started + CALL_SECONDS)
            except BaseException:
                # No second recovery/footer loop. Durable intent/failure forbids replay.
                self.r['footer_persistence_failed'] = True
            return 1

    def footer(self, deadline, clear_intent=False):
        with self.clock.limit(min(FOOTER_SECONDS, deadline - self.clock.mono())):
            self.receipt('bounded_call_complete', phase=self.phase, duration_seconds=self.clock.mono()-self.started,
                         work_budget_seconds=WORK_SECONDS, recovery_budget_seconds=RECOVERY_SECONDS)
            inflight = self.r['inflight']
            if clear_intent:
                self.r['inflight'] = None
            try:
                self.save()  # Last fallible operation; no normal-resume state before receipt.
            except BaseException:
                self.r['inflight'] = inflight
                raise


def authorization(path, run_dir, cleanup=False):
    """Operator/reviewer attestations are prerequisites, not self-authenticating grants."""
    auth = json.loads(Path(path).read_text())
    require(auth['explicit_live_authorization'] is True)
    require(auth['candidate_sha256'] == EXPECTED and auth['successor_sha256'] == digest(__file__))
    require(Path(auth['run_directory']).resolve() == run_dir.resolve())
    require(timestamp(auth['expires_utc']) > dt.datetime.now(dt.timezone.utc))
    require(auth['independent_offhost_recovery_confirmed'] is True)
    require(bool(auth['operator_authorization_reference']) and bool(auth['independent_pro_cloud_review']) and bool(auth['independent_astra_review']))
    if cleanup:
        require(auth['guard_cleanup_explicitly_authorized'] is True)
        require(auth['preimage_sha256'] == digest(run_dir / 'PREIMAGE.json'))
        require(auth['cutover_utc'] == json.loads((run_dir / 'RUN.json').read_text())['cutover_utc'])
    return auth


def main(argv=None):
    clock = Clock()
    started = ENTRY_STARTED if __name__ == '__main__' else clock.mono()
    if __name__ == '__main__':
        signal.setitimer(signal.ITIMER_REAL, 0)  # Transfer the same absolute deadline.
    lock = None
    try:
        with clock.limit(started + WORK_SECONDS - clock.mono()):
            parser = argparse.ArgumentParser(description=__doc__)
            parser.add_argument('command', nargs='?', choices=['plan', 'advance', 'rollback', 'status'], default='plan')
            parser.add_argument('--run-dir', type=Path)
            parser.add_argument('--execute-authorized', action='store_true')
            parser.add_argument('--authorization', type=Path)
            parser.add_argument('--cleanup-authorization', type=Path)
            args = parser.parse_args(argv)
            if args.command == 'plan':
                print(json.dumps({'default': 'OFFLINE_PLAN_ONLY_NO_CLIENTS', 'candidate_sha256': EXPECTED,
                    'commands': ['plan', 'status', 'advance', 'rollback'],
                    'stages': ['preflight', 'rehearse', 'apply', 'phase180', 'phase360', 'phase480', 'phase600', 'finalize', 'cleanup'],
                    'one_advance': 'Default one bounded unit with <=1 ordinary SDK method, or <=65-second pure wait',
                    'opt_in_caller': 'single_unit.py --readonly-units 2..8: consecutive read-only same-phase units only; fresh full caller admission and verified durable footer per unit under ONE aggregate budget; never autopilot phases',
                    'budgets_seconds': {'work': WORK_SECONDS, 'sdk_unit': SDK_SECONDS,
                        'diagnostics': DIAGNOSTIC_SECONDS, 'recovery': RECOVERY_SECONDS,
                        'footer': FOOTER_SECONDS, 'total': CALL_SECONDS},
                    'authority': 'Independent Pro/Cloud + Astra exact-digest review and fresh operator authorization required'}, indent=2))
                return 0
            require(args.run_dir is not None and args.run_dir.resolve() != D, 'Use a fresh separate run directory, never completed evidence')
            if args.command == 'status':
                run = json.loads((args.run_dir / 'RUN.json').read_text())
                print(json.dumps({k: run[k] for k in ['status', 'stage', 'unit', 'task_complete', 'inflight']}))
                return 0
            require(args.execute_authorized and args.authorization, 'No authorization; no clients created')
            authorization(args.authorization, args.run_dir)
            cleanup = False
            if args.cleanup_authorization:
                authorization(args.cleanup_authorization, args.run_dir, cleanup=True)
                cleanup = True
            args.run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            require(not (args.run_dir / 'RESULT.json').exists(), 'Completed report directory refused')
            lock = os.open(args.run_dir / '.foreground.lock', os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Driver handles initialization failure without inheriting a work alarm
        # into its recovery. Its deadline still starts at CLI entry, not here.
        driver = Driver(args.run_dir, clock=clock, started=started)
        return driver.advance(args.command, cleanup)
    finally:
        if lock is not None:
            os.close(lock)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        print(json.dumps({'status': 'REFUSED_OR_FAILED', 'error_type': _token(type(exc).__name__)}))
        sys.exit(1)
