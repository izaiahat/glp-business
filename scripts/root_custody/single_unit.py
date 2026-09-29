"""Phase-limited foreground caller; default one, opt-in <=8 read-only units.
Successor of the retained rehearsal caller, NOT a new authorization. Admission
and receipts share Driver's original aggregate work/recovery deadlines.
"""
import time
import signal
ENTRY_STARTED = time.monotonic()
if __name__ == '__main__':
    def _entry_timeout(signum, frame):
        raise TimeoutError('Bounded caller initialization')
    signal.signal(signal.SIGALRM, _entry_timeout)
    signal.setitimer(signal.ITIMER_REAL, 75)
import argparse
import copy
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import phased as p


def now(): return dt.datetime.now(dt.timezone.utc)
def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def load(path): return json.loads(Path(path).read_text())
def require(value, name): p.require(value, name)


def census():
    exclude=set(); pid=os.getpid()
    while pid>1:
        exclude.add(pid)
        try: pid=int(Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()[1])
        except FileNotFoundError: break
    hits=[]; count=0
    pattern=re.compile(r'root-custody|root_custody|phased\.py|curated[^\x00 ]*(?:sync|import)|(?:sync|import)[^\x00 ]*curated|weekly[^\x00 ]*refresh|refresh[^\x00 ]*weekly|price[_-]index|pin[_-]release|custody.*(?:apply|advance)',re.I)
    for d in Path('/proc').iterdir():
        if not d.name.isdigit() or int(d.name) in exclude: continue
        try: raw=(d/'cmdline').read_bytes()
        except FileNotFoundError: continue
        count+=1
        if raw and pattern.search(raw.decode(errors='replace')):
            hits.append({'pid':int(d.name),'cmdline_sha256':hashlib.sha256(raw).hexdigest()})
    require(not hits, 'conflicting_registered_process')
    return {'checked_utc':now().isoformat(),'processes_read':count,'conflicts':hits,'scope':'registered custody/import/price-index/weekly/pin-release commands; parent owns exclusive window'}


def expected_state(driver):
    """Project only already committed intentional IAM changes in the actual plan."""
    r = driver.r
    expected = copy.deepcopy(driver.logic.expected_pre(driver.pre))
    stage = r['stage']
    if stage in ['rehearse', 'apply']:
        current = driver.r
        try:
            driver.r = copy.deepcopy(current)
            completed = [name for name, _ in driver.plan()][:r['unit']]
        finally:
            driver.r = current
        if 'stage:candidate' in completed: expected['inline'][p.POL] = driver.logic.policy
        if 'stage:guard' in completed: expected['inline'][p.GUARD] = r['guard']
        if 'apply:delete_old_inline' in completed: del expected['inline']['ops-readonly-verify']
        if 'apply:detach_cw' in completed: expected['attached'].remove(p.CW)
        if 'apply:detach_admin' in completed: expected['attached'].remove(p.ADMIN)
    else:
        require(stage in ['phase180', 'phase360', 'phase480', 'phase600', 'finalize', 'cleanup'], 'phase_scope')
        expected = driver.desired(not r['guard_removed'])
    return expected


class Caller:
    def __init__(self, args):
        self.args = args
        self.run, self.base = args.run_dir.resolve(), args.authorization.resolve().parent
        self.record = None
        self.rec = None

    def bindings(self, driver):
        a, r = self.args, driver.r
        require(sha(a.authorization) == a.authorization_sha256 and sha(p.__file__) == a.source_sha256
                and sha(__file__) == a.caller_sha256, 'auth_or_source_drift')
        auth = p.authorization(a.authorization, self.run)
        require(auth['caller_sha256'] == a.caller_sha256, 'caller_binding')
        t = driver.clock.now()
        require(p.timestamp(auth['issued_utc']) <= t < p.timestamp(auth['expires_utc']), 'auth_window')
        require((p.timestamp(auth['expires_utc']) - t).total_seconds() > 240, 'full_bounded_call_must_fit_authority')
        require(auth['allowed_stage'] == r['stage'] and r['unit'] in auth['allowed_units'], 'exact_phase_scope')
        require(type(auth['max_readonly_units']) is int and 1 <= a.readonly_units <= auth['max_readonly_units'] <= 8, 'group_authority')
        require(auth['successor_sha256'] == a.source_sha256, 'source_binding')
        require(not (self.run/'RESULT.json').exists() and not (self.base/'RESULT.json').exists(), 'already_finished')
        pins = load(self.run.parent/'initial-admission.json')['pins']
        require(len(pins) == 7 and all(sha(x['path']) == x['sha256'] for x in pins), 'seven_pins')
        p.policy_logic(driver.clock.now)  # Recheck, never cache source/policy admission.
        require(sha(self.run/'PREIMAGE.json') == auth['preimage_sha256'], 'preimage_binding')
        require(sha(self.run/'least-privilege-policy.json') == auth['candidate_sha256'], 'run_candidate_binding')
        require(load(self.run/'RUN.json') == json.loads(json.dumps(r, default=str)), 'prior_state_drift')
        require(r['inflight'] is None and not r.get('first_error') and not r['rollback_triggered'], 'failed_or_ambiguous')
        require(r['status'] == auth['initial_status'], 'status_binding')
        initial = auth['initial_unit']
        require(r['unit'] >= initial and r['call_number'] >= auth['initial_call_number'], 'exact_cursor')
        rows = [json.loads(x) for x in (self.run/'PHASES.jsonl').read_text().splitlines()]
        require(rows[-1]['event'] == 'bounded_call_complete' and rows[-2]['event'] == 'unit_committed', 'prior_footer')
        require(rows[-1]['phase'] == rows[-2]['phase'] and
                (rows[-2]['next_stage'], rows[-2]['next_unit']) == (r['stage'], r['unit']), 'prior_commit')
        require(all(sum(x['event'] == kind for x in rows) == r['call_number']
                    for kind in ['unit_started', 'unit_committed', 'bounded_call_complete']), 'receipt_counts')
        if r['call_number'] == auth['initial_call_number']:
            require(r['unit'] == initial, 'initial_cursor')
            require(sha(self.run/'RUN.json') == auth['initial_run_sha256'] and
                    sha(self.run/'PHASES.jsonl') == auth['initial_phases_sha256'], 'initial_state_binding')
        else:
            prev = load(self.base/f"caller-unit-{r['call_number']-1:04d}.json")
            require(prev['outcome'] == 'COMMITTED' and prev['returncode'] == 0, 'prior_caller_failed')
            require(prev['after_run_sha256'] == sha(self.run/'RUN.json') and
                    prev['after_phases_sha256'] == sha(self.run/'PHASES.jsonl'), 'prior_caller_binding')
        if r['stage'] == 'cleanup':
            require(a.cleanup_authorization is not None and a.cleanup_authorization_sha256 is not None, 'separate_cleanup_authority')
            require(sha(a.cleanup_authorization) == a.cleanup_authorization_sha256, 'cleanup_auth_drift')
            p.authorization(a.cleanup_authorization, self.run, cleanup=True)
        else:
            require(auth['guard_cleanup_explicitly_authorized'] is False, 'cleanup_separate')
        return auth, pins

    def admission(self, driver):
        self.record = None
        self.rec = None
        t = driver.clock.now
        admission_started = t().isoformat()
        auth, pins = self.bindings(driver)
        r, pre, logic = driver.r, driver.pre, driver.logic
        rec = self.base/f"caller-unit-{r['call_number']:04d}.json"
        require(not rec.exists(), 'no_replay_after_receipt')
        self.rec = rec
        self.record = {'unit': r['unit'], 'stage': r['stage'], 'started_utc': admission_started,
                       'foreground_started_mono': driver.started,
                       'authorization_sha256': self.args.authorization_sha256, 'source_sha256': self.args.source_sha256,
                       'caller_sha256': self.args.caller_sha256, 'pins': pins,
                       'before_run_sha256': sha(self.run/'RUN.json'), 'before_phases_sha256': sha(self.run/'PHASES.jsonl'),
                       'census_before': census()}
        require(driver.local('credential') == 'iam-role', 'credential')
        def read(service, method, **kw):
            out = driver.call(service, method, **kw)
            require(not out.get('NextToken'), 'truncated_read')
            return out
        ident = read('sts', 'get_caller_identity'); driver.identity(ident)
        expected = expected_state(driver)
        iam = {'role': read('iam','get_role',RoleName=p.ROLE),
               'attached': read('iam','list_attached_role_policies',RoleName=p.ROLE),
               'inline_names': read('iam','list_role_policies',RoleName=p.ROLE), 'managed': {}, 'inline': {}}
        require(sorted(x['PolicyArn'] for x in iam['attached']['AttachedPolicies']) == sorted(expected['attached']), 'attached_drift')
        require(sorted(iam['inline_names']['PolicyNames']) == sorted(expected['inline']), 'phase_inline_names')
        for arn in expected['attached']:
            meta = read('iam','get_policy',PolicyArn=arn)
            iam['managed'][arn] = {'metadata': meta, 'version': read('iam','get_policy_version',PolicyArn=arn,VersionId=meta['Policy']['DefaultVersionId'])}
        for name in expected['inline']: iam['inline'][name] = read('iam','get_role_policy',RoleName=p.ROLE,PolicyName=name)
        iam['instance_profiles'] = read('iam','list_instance_profiles_for_role',RoleName=p.ROLE)
        snap = {'iam': iam, 'caller': ident, 'ownership_modes': driver.local('owners'),
                'preservation': {'sha256': driver.local('hashes'), 'groups': driver.local('groups')}}
        require(logic.expected_pre(snap) == expected, 'registered_expected_state')
        # All retained role/trust/profile/managed-policy bytes still compare.
        # Substitute ONLY intentional removals proven absent by exact name checks.
        normalized = copy.deepcopy(snap)
        for name in (p.POL, p.GUARD):
            if name not in pre['iam']['inline'] and name in normalized['iam']['inline']:
                del normalized['iam']['inline'][name]
                normalized['iam']['inline_names']['PolicyNames'].remove(name)
        for name in set(pre['iam']['inline']) - set(expected['inline']):
            normalized['iam']['inline'][name] = copy.deepcopy(pre['iam']['inline'][name])
            normalized['iam']['inline_names']['PolicyNames'].append(name)
        for arn in set(logic.expected_pre(pre)['attached']) - set(expected['attached']):
            normalized['iam']['managed'][arn] = copy.deepcopy(pre['iam']['managed'][arn])
            normalized['iam']['attached']['AttachedPolicies'].extend(copy.deepcopy(
                [x for x in pre['iam']['attached']['AttachedPolicies'] if x['PolicyArn'] == arn]))
        driver.same_preimage(normalized)
        driver.guard_margin()
        for kind, val in [('sudo',''),('ssm_local_root','0'),('ssm_agent','active'),('cloudwatch_agent','active')]:
            require(driver.local(kind) == val, kind)
        cron = load(p.CAPTURE/'CRON-SCAN.json')
        for kind, key in [('cron_user','crontab -l'),('cron_root','sudo -n crontab -l')]:
            require(driver.local(kind) == {'exit_code':cron[key]['rc'],'sha256':cron[key]['sha256']}, kind)
        status = read('ec2','describe_instance_status',InstanceIds=[p.IID],IncludeAllInstances=True)['InstanceStatuses']
        require(len(status)==1 and status[0]['SystemStatus']['Status']=='ok' and status[0]['InstanceStatus']['Status']=='ok' and status[0]['InstanceState']['Name']=='running','ec2_health')
        connection = read('ssm','get_connection_status',Target=p.IID)
        info = read('ssm','describe_instance_information',Filters=[{'Key':'InstanceIds','Values':[p.IID]}])['InstanceInformationList']
        require(connection['Status']=='connected' and connection['Target']==p.IID and len(info)==1 and info[0]['PingStatus']=='Online','ssm_health')
        ping = info[0]['LastPingDateTime']
        points = read('cloudwatch','get_metric_statistics',Namespace='CWAgent',MetricName='mem_used_percent',Dimensions=[{'Name':'InstanceId','Value':p.IID}],StartTime=t()-dt.timedelta(minutes=15),EndTime=t(),Period=60,Statistics=['Average'])['Datapoints']
        require(points, 'metric_missing'); metric = max(x['Timestamp'] for x in points)
        response = read('ssm','describe_sessions',State='Active',Filters=[{'key':'Target','value':p.IID}],MaxResults=100)
        recovery = auth['observed_recovery']; sessions = [s for s in response['Sessions'] if s['SessionId']==recovery['session_id']]
        require(len(sessions)==1,'exact_recovery_missing'); s=sessions[0]
        require(s['Status']=='Connected' and s['Target']==recovery['target']==p.IID and s['Owner']=='arn:aws:iam::271095146765:root' and s['StartDate'].isoformat()==recovery['started_at'],'exact_recovery_identity')
        require(0 <= (t()-ping).total_seconds() <= 420 and 0 <= (t()-metric).total_seconds() <= 300, 'fresh_health_ages')
        self.record['admission'] = {'checked_utc': t().isoformat(), 'heartbeat_utc': ping.isoformat(), 'metric_utc': metric.isoformat(), 'exact_recovery_connected': True}
        self.record['census_dispatch'] = census()
        self.bindings(driver)
        require(self.record['before_run_sha256']==sha(self.run/'RUN.json') and self.record['before_phases_sha256']==sha(self.run/'PHASES.jsonl'),'admission_state_changed')
        self.record.update(outcome='ADMITTED_INTENT', dispatch_utc=t().isoformat())
        p.atomic(self.rec, self.record)

    def verified(self, driver):
        # Native verify_commit already proved cursor, cleared intent and footer.
        require(sha(self.args.authorization) == self.args.authorization_sha256
                and sha(p.__file__) == self.args.source_sha256 and sha(__file__) == self.args.caller_sha256
                and all(sha(x['path']) == x['sha256'] for x in self.record['pins']), 'post_bindings')
        self.record.update(returncode=0, finished_utc=driver.clock.now().isoformat(),
                           after_run_sha256=sha(self.run/'RUN.json'), after_phases_sha256=sha(self.run/'PHASES.jsonl'),
                           outcome='COMMITTED')
        p.atomic(self.rec, self.record)
        require(load(self.rec) == self.record, 'caller_receipt_readback')


def main(argv=None):
    clock = p.Clock()
    started = ENTRY_STARTED if __name__ == '__main__' else clock.mono()
    if __name__ == '__main__': signal.setitimer(signal.ITIMER_REAL, 0)
    lock = caller_lock = None
    try:
        with clock.limit(started + p.WORK_SECONDS - clock.mono()):
            parser = argparse.ArgumentParser(description=__doc__)
            parser.add_argument('--run-dir', type=Path, required=True)
            parser.add_argument('--authorization', type=Path, required=True)
            parser.add_argument('--authorization-sha256', required=True)
            parser.add_argument('--source-sha256', required=True)
            parser.add_argument('--caller-sha256', required=True)
            parser.add_argument('--cleanup-authorization', type=Path)
            parser.add_argument('--cleanup-authorization-sha256')
            parser.add_argument('--readonly-units', type=int, choices=range(1,9), default=1)
            parser.add_argument('--execute-authorized', action='store_true')
            args = parser.parse_args(argv)
            require(args.execute_authorized, 'no_effect_authority')
            require(args.run_dir.is_dir() and args.run_dir.resolve() != Path(__file__).resolve().parent, 'existing_fresh_run_required')
            caller = Caller(args)
            caller_lock = os.open(caller.base/'.caller.lock', os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(caller_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            lock = os.open(args.run_dir/'.foreground.lock', os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        driver = p.Driver(args.run_dir, clock=clock, started=started)
        return driver.advance(cleanup_authorized=args.cleanup_authorization is not None,
                              readonly_units=args.readonly_units, admission=caller.admission, verified=caller.verified)
    finally:
        if lock is not None: os.close(lock)
        if caller_lock is not None: os.close(caller_lock)


if __name__ == '__main__':
    try: sys.exit(main())
    except Exception as exc:
        print(json.dumps({'status':'REFUSED_OR_FAILED','error_type':p._token(type(exc).__name__)}))
        sys.exit(1)
