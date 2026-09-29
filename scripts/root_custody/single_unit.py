"""Exact parent-bound one-unit caller. No looping, recovery or source edits."""
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import subprocess
import sys

BASE = Path(__file__).resolve().parent
ROOT = Path('/home/ubuntu/business')
RUN = ROOT / 'reports/ops/glp320-custody-live-preflight-20260927/run'
AUTH = BASE / 'AUTHORIZATION.json'
SRC = ROOT / 'reports/ops/root-custody-dispatch-boundary-fix-20260926/phased.py'
PYTHON = '/home/ubuntu/.hermes/hermes-agent/venv/bin/python3'
AUTH_SHA = '9c5c9f16171362af7c7bf9e18bbf6f9551978faf0ee8f5c98466fc08a5eb2ee3'
SRC_SHA = '3931c9d50602137d887f3a5fd3dfe0e3a14ee434eb15e627e49c9679a3e4c7f7'
U = int(sys.argv[1])
REC = BASE / f'caller-unit-{U:02d}.json'
record = {'unit': U, 'started_utc': dt.datetime.now(dt.timezone.utc).isoformat(), 'driver_invoked': False}
step = 'initial'
def now(): return dt.datetime.now(dt.timezone.utc)
def sha(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def load(p): return json.loads(Path(p).read_text())
def require(x, name):
    if not x: raise AssertionError(name)
def persist():
    tmp = REC.with_suffix('.writing')
    with tmp.open('x') as f:
        json.dump(record, f, indent=2, default=str); f.write('\n'); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, REC)
    fd = os.open(BASE, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)
def bindings():
    require(sha(AUTH) == AUTH_SHA and sha(SRC) == SRC_SHA, 'auth_or_source_drift')
    a = load(AUTH)
    require(dt.datetime.fromisoformat(a['issued_utc']) <= now() < dt.datetime.fromisoformat(a['expires_utc']), 'auth_window')
    require((dt.datetime.fromisoformat(a['expires_utc'])-now()).total_seconds() > 240, 'full_bounded_call_must_fit_authority')
    require(a['allowed_stage']=='rehearse' and U in a['allowed_units'] and 0 <= U <= 32, 'scope')
    require(a['run_directory']==str(RUN) and a['guard_cleanup_explicitly_authorized'] is False, 'scope_binding')
    require(a['explicit_live_authorization'] and a['independent_offhost_recovery_confirmed'], 'authority')
    pins = load(RUN.parent/'initial-admission.json')['pins']
    require(len(pins)==7 and all(sha(p['path'])==p['sha256'] for p in pins), 'seven_pins')
    require(sha(RUN/'PREIMAGE.json')==a['preimage_sha256'], 'preimage_binding')
    require(sha(RUN/'least-privilege-policy.json')==a['candidate_sha256'], 'run_candidate_binding')
    r = load(RUN/'RUN.json')
    require((r['stage'],r['unit'],r['call_number'])==('rehearse',U,63+U), 'exact_cursor')
    require(r['status']=='PREFLIGHT_PASS_NO_MUTATION' and r['inflight'] is None, 'status_or_inflight')
    require(not r.get('first_error') and not r['rollback_triggered'] and not r['guard_removed'] and 'cutover_utc' not in r, 'failure_or_cutover')
    require(r['mutation_started'] == (U>=16), 'mutation_intent_state')
    require(not (RUN/'RESULT.json').exists() and not (BASE/'RESULT.json').exists(), 'already_finished')
    rows = [json.loads(x) for x in (RUN/'PHASES.jsonl').read_text().splitlines()]
    require(rows[-1]['event']=='bounded_call_complete' and rows[-2]['event']=='unit_committed', 'prior_footer')
    require(rows[-1]['phase']==rows[-2]['phase'] and (rows[-2]['next_stage'],rows[-2]['next_unit'])==('rehearse',U), 'prior_commit')
    require(sum(x['event']=='unit_started' for x in rows)==63+U and sum(x['event']=='unit_committed' for x in rows)==63+U and sum(x['event']=='bounded_call_complete' for x in rows)==63+U, 'receipt_counts')
    if U==0:
        require(sha(RUN/'RUN.json')==a['initial_run_sha256'] and sha(RUN/'PHASES.jsonl')==a['initial_phases_sha256'], 'initial_state_binding')
    else:
        prev=load(BASE/f'caller-unit-{U-1:02d}.json')
        require(prev['outcome']=='COMMITTED' and prev['returncode']==0, 'prior_caller_failed')
        require(prev['after_run_sha256']==sha(RUN/'RUN.json') and prev['after_phases_sha256']==sha(RUN/'PHASES.jsonl'), 'prior_caller_binding')
    return a,r,rows,pins

def census():
    exclude=set(); pid=os.getpid()
    while pid>1:
        exclude.add(pid)
        try: pid=int(Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()[1])
        except FileNotFoundError: break
    hits=[]; count=0
    pattern=re.compile(r'root-custody|phased\.py|curated[^\x00 ]*(?:sync|import)|(?:sync|import)[^\x00 ]*curated|weekly[^\x00 ]*refresh|refresh[^\x00 ]*weekly|price[_-]index|pin[_-]release|custody.*(?:apply|advance)',re.I)
    for d in Path('/proc').iterdir():
        if not d.name.isdigit() or int(d.name) in exclude: continue
        try: raw=(d/'cmdline').read_bytes()
        except FileNotFoundError: continue
        count+=1
        if raw and pattern.search(raw.decode(errors='replace')):
            hits.append({'pid':int(d.name),'cmdline_sha256':hashlib.sha256(raw).hexdigest()})
    require(not hits, 'conflicting_registered_process')
    return {'checked_utc':now().isoformat(),'processes_read':count,'conflicts':hits,'scope':'registered custody/import/price-index/weekly/pin-release commands; parent owns exclusive window'}

try:
    require(not REC.exists(), 'no_replay_after_receipt')
    lock=(BASE/'.caller.lock').open('a'); fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    step='bindings'; auth,r,rows,pins=bindings()
    record.update(authorization_sha256=sha(AUTH),source_sha256=sha(SRC),pins=pins,before_run_sha256=sha(RUN/'RUN.json'),before_phases_sha256=sha(RUN/'PHASES.jsonl'),prior_footer=rows[-1])
    step='process_census'; record['census_before']=census()
    p=runpy.run_path(str(SRC)); clock=p['Clock']()
    step='fresh_readonly_admission'
    with clock.limit(90):
        logic,_=p['policy_logic'](now); live=p['Live'](); pre=load(RUN/'PREIMAGE.json')
        require(live.local('credential')=='iam-role','credential')
        def read(s,m,**kw):
            with clock.limit(65): out=live.sdk(s,m,kw)
            require(not out.get('IsTruncated') and not out.get('NextToken'),'truncated_read')
            return out
        ident=read('sts','get_caller_identity')
        checker=object.__new__(p['Driver']); checker.logic=logic; checker.pre=pre; checker.r=r; checker.clock=clock
        checker.identity(ident)
        expected=logic.expected_pre(pre)
        if U>=16: expected['inline'][p['POL']]=logic.policy
        if U>=17: expected['inline'][p['GUARD']]=r['guard']
        iam={'role':read('iam','get_role',RoleName=p['ROLE']), 'attached':read('iam','list_attached_role_policies',RoleName=p['ROLE']), 'inline_names':read('iam','list_role_policies',RoleName=p['ROLE']), 'managed':{},'inline':{}}
        require(sorted(x['PolicyArn'] for x in iam['attached']['AttachedPolicies'])==expected['attached'],'attached_drift')
        require(sorted(iam['inline_names']['PolicyNames'])==sorted(expected['inline']),'phase_inline_names')
        for arn in expected['attached']:
            meta=read('iam','get_policy',PolicyArn=arn)
            iam['managed'][arn]={'metadata':meta,'version':read('iam','get_policy_version',PolicyArn=arn,VersionId=meta['Policy']['DefaultVersionId'])}
        for name in expected['inline']: iam['inline'][name]=read('iam','get_role_policy',RoleName=p['ROLE'],PolicyName=name)
        iam['instance_profiles']=read('iam','list_instance_profiles_for_role',RoleName=p['ROLE'])
        snap={'iam':iam,'caller':ident,'ownership_modes':live.local('owners'),'preservation':{'sha256':live.local('hashes'),'groups':live.local('groups')}}
        require(logic.expected_pre(snap)==expected,'registered_expected_state')
        if U>=17:
            checker.r=dict(r,data={'admission':snap}); checker.staged_state('admission'); checker.r=r
        # Remove ONLY the expected intentional additions in a private memory copy;
        # compare all retained role, trust, profile and policy metadata to preimage.
        normalized=json.loads(json.dumps(snap,default=str))
        for name in (p['POL'],p['GUARD']):
            if name not in pre['iam']['inline'] and name in normalized['iam']['inline']:
                del normalized['iam']['inline'][name]; normalized['iam']['inline_names']['PolicyNames'].remove(name)
        checker.same_preimage(normalized)
        checker.guard_margin()
        for kind,val in [('sudo',''),('ssm_local_root','0'),('ssm_agent','active'),('cloudwatch_agent','active')]: require(live.local(kind)==val,kind)
        cron=load(p['CAPTURE']/'CRON-SCAN.json')
        for kind,key in [('cron_user','crontab -l'),('cron_root','sudo -n crontab -l')]: require(live.local(kind)=={'exit_code':cron[key]['rc'],'sha256':cron[key]['sha256']},kind)
        status=read('ec2','describe_instance_status',InstanceIds=[p['IID']],IncludeAllInstances=True)['InstanceStatuses']
        require(len(status)==1 and status[0]['SystemStatus']['Status']=='ok' and status[0]['InstanceStatus']['Status']=='ok' and status[0]['InstanceState']['Name']=='running','ec2_health')
        connection=read('ssm','get_connection_status',Target=p['IID'])
        info=read('ssm','describe_instance_information',Filters=[{'Key':'InstanceIds','Values':[p['IID']]}])['InstanceInformationList']
        require(connection['Status']=='connected' and connection['Target']==p['IID'] and len(info)==1 and info[0]['PingStatus']=='Online','ssm_health')
        ping=info[0]['LastPingDateTime']
        points=read('cloudwatch','get_metric_statistics',Namespace='CWAgent',MetricName='mem_used_percent',Dimensions=[{'Name':'InstanceId','Value':p['IID']}],StartTime=now()-dt.timedelta(minutes=15),EndTime=now(),Period=60,Statistics=['Average'])['Datapoints']
        require(points,'metric_missing'); metric=max(x['Timestamp'] for x in points)
        response=read('ssm','describe_sessions',State='Active',Filters=[{'key':'Target','value':p['IID']}],MaxResults=100)
        recovery=auth['observed_recovery']; sessions=[s for s in response['Sessions'] if s['SessionId']==recovery['session_id']]
        require(len(sessions)==1,'exact_recovery_missing'); s=sessions[0]
        require(s['Status']=='Connected' and s['Target']==recovery['target'] and s['Owner']=='arn:aws:iam::271095146765:root' and s['StartDate'].isoformat()==recovery['started_at'],'exact_recovery_identity')
        require(0<=(now()-ping).total_seconds()<=420 and 0<=(now()-metric).total_seconds()<=300,'fresh_health_ages')
        record['admission']={'checked_utc':now().isoformat(),'phase_appropriate_iam_pass':True,'preservation_pass':True,'agents_ec2_ssm_health_pass':True,'heartbeat_utc':ping.isoformat(),'metric_utc':metric.isoformat(),'heartbeat_age_seconds':(now()-ping).total_seconds(),'metric_age_seconds':(now()-metric).total_seconds(),'exact_recovery_connected':True,'session_id':recovery['session_id'],'session_request_id':response['ResponseMetadata']['RequestId'],'root_testimony_message':recovery['operator_message_id']}
    step='final_admission'; record['census_dispatch']=census(); bindings()
    require(record['before_run_sha256']==sha(RUN/'RUN.json') and record['before_phases_sha256']==sha(RUN/'PHASES.jsonl'),'admission_state_changed')
    record.update(outcome='ADMITTED_INTENT',driver_invoked=True,dispatch_utc=now().isoformat()); persist()
    step='registered_foreground_call'
    result=subprocess.run([PYTHON,'-B',str(SRC),'advance','--run-dir',str(RUN),'--execute-authorized','--authorization',str(AUTH)],capture_output=True,text=True)
    after=load(RUN/'RUN.json'); tail=[json.loads(x) for x in (RUN/'PHASES.jsonl').read_text().splitlines()]
    record.update(returncode=result.returncode,finished_utc=now().isoformat(),stdout=result.stdout,stderr_sha256=hashlib.sha256(result.stderr.encode()).hexdigest(),after={k:after.get(k) for k in ['status','stage','unit','call_number','mutation_started','inflight','rollback_triggered','first_error','rollback_error','guard_removed','rollback_exact_preimage_match','temporary_guard_present','candidate_present','cutover_utc']},after_run_sha256=sha(RUN/'RUN.json'),after_phases_sha256=sha(RUN/'PHASES.jsonl'),phases_tail=tail[-3:],outcome='RETURNED_UNVERIFIED'); persist()
    step='receipt_verification'
    require(result.returncode==0 and after['inflight'] is None and not after.get('first_error') and not after['rollback_triggered'],'native_failure_STOP')
    require((after['stage'],after['unit'],after['call_number'])==(('apply',0,96) if U==32 else ('rehearse',U+1,64+U)),'post_cursor')
    require(tail[-1]['event']=='bounded_call_complete' and tail[-2]['event']=='unit_committed' and tail[-1]['phase']==tail[-2]['phase'],'post_footer')
    require(not after['guard_removed'] and 'cutover_utc' not in after,'prohibited_cutover')
    require(sha(SRC)==SRC_SHA and sha(AUTH)==AUTH_SHA and all(sha(x['path'])==x['sha256'] for x in pins),'post_bindings')
    if U==32: require(after['status']=='ROLLBACK_REHEARSAL_PASS' and after['rollback_exact_preimage_match'] and not after['mutation_started'] and not after['candidate_present'] and not after['temporary_guard_present'],'terminal_rehearsal')
    record['outcome']='COMMITTED'; persist()
    print(json.dumps({k:record[k] for k in ['unit','outcome','returncode','admission','after','phases_tail']},indent=2))
except BaseException as e:
    record.update(outcome='STOP_REFUSAL_OR_FAILURE',failed_step=step,error_type=type(e).__name__,failure_utc=now().isoformat())
    if isinstance(e,AssertionError): record['gate']=str(e)
    if not REC.exists() or record.get('dispatch_utc'): persist()
    print(json.dumps(record,indent=2,default=str)); sys.exit(1)
