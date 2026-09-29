"""Focused synthetic acceptance; no SDK installed/constructed, no host probes.
One test per named acceptance check. Policy/retry/F1-F3 evidence is carried by
AST identity, not rerunning the predecessor 315-expectation/30-test matrix.
"""
import ast
import copy
from contextlib import contextmanager, ExitStack
import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'scripts/root_custody'
EVIDENCE = {}


def forbidden(*a, **kw):
    raise AssertionError('No SDK constructors, sockets, processes or live adapter')


# Import declaration-only config/errors fakes, never the installed AWS SDK.
config = ModuleType('botocore.config'); config.Config = forbidden
errors = ModuleType('botocore.exceptions')
errors.ClientError = type('ClientError', (Exception,), {})
aws = ModuleType('boto3'); aws.Session = aws.client = forbidden
core = ModuleType('botocore'); session = ModuleType('botocore.session')
session.Session = session.get_session = forbidden
stubs = {'botocore': core, 'botocore.config': config, 'botocore.exceptions': errors,
         'botocore.session': session, 'boto3': aws}


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


with patch.dict(sys.modules, stubs):
    p = module('glp320_phased', SOURCE/'phased.py')
    with patch.dict(sys.modules, {'phased': p}):
        caller = module('glp320_caller', SOURCE/'single_unit.py')


def predecessor_advance():
    """Undo only additive callback/group seams; authenticate exact predecessor AST.
    This avoids curating a second source/runner or requiring Git history in CI.
    """
    tree=ast.parse((SOURCE/'phased.py').read_text())
    cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='Driver')
    fn=copy.deepcopy(next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='_advance_unit'))
    class RemoveAdditions(ast.NodeTransformer):
        def visit_If(self,node):
            if ast.unparse(node.test) in ['readonly_only','admission is not None','readonly_only or verified is not None']:
                return None
            return self.generic_visit(node)
    fn=RemoveAdditions().visit(fn); fn.name='advance'
    fn.args.kwonlyargs=[]; fn.args.kw_defaults=[]
    assert hashlib.sha256(ast.dump(fn,include_attributes=False).encode()).hexdigest() == '02391ce8885d10e4f2b381b446c67c4adbc9d7a2732fc7a79541df3947f5e067'
    env=dict(vars(p)); exec(compile(ast.fix_missing_locations(ast.Module(body=[fn],type_ignores=[])),'authenticated-predecessor-advance','exec'),env)
    return env['advance']


class Clock:
    def __init__(self):
        self.t = 1000.0
        self.epoch = dt.datetime(2030, 1, 1, tzinfo=dt.timezone.utc)
        self.limits = []
        self.caps = []
    def mono(self): return self.t
    def now(self): return self.epoch + dt.timedelta(seconds=self.t-1000)
    def boot(self): return 'synthetic-boot'
    def sleep(self, n): self.charge(n)
    def charge(self, n):
        end = self.t+n
        if self.limits and end >= min(self.limits):
            self.t = min(self.limits)
            raise TimeoutError('PRIVATE_ERROR_MUST_NOT_PERSIST')
        self.t = end
    @contextmanager
    def limit(self, n):
        if n <= 0: raise TimeoutError('exhausted')
        end = self.t+n
        self.caps.append(n); self.limits.append(end)
        try: yield
        finally: self.limits.remove(end)


def norm(value):
    if isinstance(value, dict):
        return {k: norm(v) for k,v in value.items() if k not in ('ResponseMetadata','RoleLastUsed')}
    if isinstance(value, list): return sorted([norm(v) for v in value], key=lambda v: json.dumps(v, sort_keys=True, default=str))
    if isinstance(value, dt.datetime): return str(value)
    return value


def expected(pre):
    return {'attached': sorted(x['PolicyArn'] for x in pre['iam']['attached']['AttachedPolicies']),
            'inline': {k: v['PolicyDocument'] for k,v in pre['iam']['inline'].items()}}


def fixture_pre():
    return {'iam': {'role': {'Role': {'RoleName': p.ROLE, 'Trust': 'synthetic'}},
            'attached': {'AttachedPolicies': [{'PolicyArn': arn} for arn in [p.ADMIN,p.CW,p.CORE]]},
            'inline_names': {'PolicyNames': ['ops-readonly-verify']},
            'inline': {'ops-readonly-verify': {'PolicyDocument': {'fixture':'old'}}},
            'managed': {arn: {'metadata': {'Policy': {'DefaultVersionId':'v1'}},
                                'version': {'PolicyVersion': {'Document': {'fixture':arn}}}} for arn in [p.ADMIN,p.CW,p.CORE]},
            'instance_profiles': {'InstanceProfiles': [{'fixture':'profile'}]}},
            'caller': {'Arn':'arn:aws:sts::271095146765:assumed-role/'+p.ROLE+'/'+p.IID, 'Account':'271095146765'},
            'ownership_modes': [{'fixture':'owners'}],
            'preservation': {'sha256': {k:'0'*64 for k in p.HASH_PATHS}, 'groups':'synthetic-groups'}}


class Adapter:
    def __init__(self, clock, pre, trace):
        self.clock, self.pre, self.trace = clock, pre, trace
        self.attached = copy.deepcopy(pre['iam']['attached']['AttachedPolicies'])
        self.inline = copy.deepcopy(pre['iam']['inline'])
        self.cost = .01
        self.delays = {}
        self.bad_recovery = False
        self.ping_age = self.metric_age = 0
    def delay(self, key): self.clock.charge(self.delays.get(key, self.cost))
    def local(self, kind, arg=None):
        self.trace.append(('local',kind)); self.delay(kind)
        if kind == 'credential': return 'iam-role'
        if kind == 'owners': return copy.deepcopy(self.pre['ownership_modes'])
        if kind == 'hashes': return copy.deepcopy(self.pre['preservation']['sha256'])
        if kind == 'groups': return self.pre['preservation']['groups']
        if kind.startswith('cron_'): return {'exit_code':0,'sha256':'fixture-cron'}
        return {'sudo':'','ssm_local_root':'0','ssm_agent':'active','cloudwatch_agent':'active'}[kind]
    def prepare_sdk(self, service, method, args):
        self.delay('prepare'); return lambda: self.sdk(service,method,args)
    def sdk(self, service, method, args):
        self.trace.append(('sdk',service,method)); self.delay(method)
        if method == 'get_role': return copy.deepcopy(self.pre['iam']['role'])
        if method == 'list_attached_role_policies': return {'AttachedPolicies':copy.deepcopy(self.attached)}
        if method == 'list_role_policies': return {'PolicyNames':list(self.inline)}
        if method == 'get_role_policy': return copy.deepcopy(self.inline[args['PolicyName']])
        if method == 'get_policy': return copy.deepcopy(self.pre['iam']['managed'][args['PolicyArn']]['metadata'])
        if method == 'get_policy_version': return copy.deepcopy(self.pre['iam']['managed'][args['PolicyArn']]['version'])
        if method == 'list_instance_profiles_for_role': return copy.deepcopy(self.pre['iam']['instance_profiles'])
        if method == 'get_caller_identity': return copy.deepcopy(self.pre['caller'])
        if method == 'put_role_policy': self.inline[args['PolicyName']]={'PolicyDocument':json.loads(args['PolicyDocument'])}; return {}
        if method == 'delete_role_policy': del self.inline[args['PolicyName']]; return {}
        if method == 'detach_role_policy': self.attached=[x for x in self.attached if x['PolicyArn']!=args['PolicyArn']]; return {}
        if method == 'list_commands': return {'Commands':[{'CommandId':'fixture','Status':'Success'}]}
        if method == 'get_command_invocation': return dict(CommandId='fixture',InstanceId=p.IID,Status='Success',DocumentName='fixture')
        if method == 'get_document': return {'Name':'fixture','Status':'Active'}
        if method == 'describe_instances': return {'Reservations':[{'Instances':[{}]}]}
        if method == 'describe_instance_status': return {'InstanceStatuses':[dict(InstanceId=p.IID,InstanceState={'Name':'running'},SystemStatus={'Status':'ok'},InstanceStatus={'Status':'ok'})]}
        if method == 'get_serial_console_access_status': return {'SerialConsoleAccessEnabled':True}
        if method == 'get_connection_status': return {'Target':p.IID,'Status':'connected'}
        if method == 'describe_instance_information': return {'InstanceInformationList':[dict(InstanceId=p.IID,PingStatus='Online',LastPingDateTime=self.clock.now()-dt.timedelta(seconds=self.ping_age),AgentVersion='fixture')]}
        if method == 'get_metric_statistics': return {'Datapoints':[{'Timestamp':self.clock.now()-dt.timedelta(seconds=self.metric_age),'Average':1}]}
        if method == 'describe_sessions': return {'Sessions':[{'SessionId':'fixture-recovery','Status':'Disconnected' if self.bad_recovery else 'Connected','Target':p.IID,'Owner':'arn:aws:iam::271095146765:root','StartDate':self.clock.epoch}], 'ResponseMetadata':{'RequestId':'fixture'}}
        if method in ['simulate_principal_policy','simulate_custom_policy']:
            # Only the unchanged cleanup permission/canary, never broad policy vectors.
            assert len(args['ActionNames']) <= 2
            return {'EvaluationResults':[dict(EvalActionName=a,EvalResourceName=p.RA,EvalDecision='allowed') for a in args['ActionNames']]}
        empty = {'list_associations':'Associations','describe_parameters':'Parameters','describe_tags':'Tags',
                 'describe_volumes':'Volumes','list_buckets':'Buckets','list_secrets':'SecretList','list_metrics':'Metrics'}
        if method in empty: return {empty[method]:[]}
        raise AssertionError(method)


class Harness:
    def __init__(self, directory, stack):
        self.d = directory/'run'; self.d.mkdir()
        self.base = directory/'caller'; self.base.mkdir()
        self.clock = Clock(); self.trace=[]; self.pre=fixture_pre()
        self.logic=SimpleNamespace(norm=norm,expected_pre=expected,policy={'fixture':'candidate'},core={'fixture':'core'},context=lambda **kw:[],vectors=lambda:[])
        self.adapter=Adapter(self.clock,self.pre,self.trace)
        self.recoveries=0
        self.recovery_fail=False
        stack.enter_context(patch.object(p,'policy_logic',lambda now:(self.logic,copy.deepcopy(self.pre))))
        stack.enter_context(patch.object(p,'CAPTURE',directory))
        stack.enter_context(patch.object(p,'SOURCE',directory))
        p.atomic(directory/'TEMPORARY-ROLLBACK-POLICY.json',{'Statement':[{'Condition':{'DateLessThan':{'aws:CurrentTime':'fixture'}}}]})
        p.atomic(directory/'CRON-SCAN.json',{k:{'rc':0,'sha256':'fixture-cron'} for k in ['crontab -l','sudo -n crontab -l']})
        p.atomic(self.d/'PREIMAGE.json',self.pre)
        (self.d/'least-privilege-policy.json').write_text('fixture-only')
        self.driver=self.make()
        self.driver.r.update(stage='rehearse',status='PREFLIGHT_PASS_NO_MUTATION',preimage_sha256=p.digest(self.d/'PREIMAGE.json'))
        self.driver.save()
        self.seed()
    def recover(self,d):
        self.recoveries+=1
        if self.recovery_fail or d.r['guard_removed']: raise RuntimeError('offhost')
        self.clock.charge(.2)
        d.r.update(temporary_guard_present=False,candidate_present=False,rollback_exact_preimage_match=True)
    def make(self):
        return p.Driver(self.d,adapter_factory=lambda:self.adapter,clock=self.clock,recovery=self.recover)
    def seed(self):
        # One synthetic prior committed unit, not a fabricated historical receipt.
        d=self.driver; d.r['call_number']=1; d.save()
        d.receipt('unit_started',phase='synthetic-prior',unit=0,call_number=1)
        d.receipt('unit_committed',phase='synthetic-prior',next_stage=d.r['stage'],next_unit=d.r['unit'])
        d.phase='synthetic-prior'; d.footer(d.work_end)
    def bind_caller(self, maximum=8):
        d=self.driver
        auth=dict(explicit_live_authorization=True,candidate_sha256=p.EXPECTED,successor_sha256=p.digest(p.__file__),
                  caller_sha256=p.digest(caller.__file__),run_directory=str(self.d),issued_utc=self.clock.now().isoformat(),
                  expires_utc=(self.clock.now()+dt.timedelta(days=4000)).isoformat(),independent_offhost_recovery_confirmed=True,
                  operator_authorization_reference='synthetic-only',independent_pro_cloud_review='synthetic-only',independent_astra_review='synthetic-only',
                  guard_cleanup_explicitly_authorized=False,allowed_stage=d.r['stage'],allowed_units=list(range(500)),max_readonly_units=maximum,
                  preimage_sha256=p.digest(self.d/'PREIMAGE.json'),initial_status=d.r['status'],initial_unit=d.r['unit'],
                  initial_call_number=d.r['call_number'],initial_run_sha256=p.digest(self.d/'RUN.json'),initial_phases_sha256=p.digest(self.d/'PHASES.jsonl'),
                  observed_recovery={'session_id':'fixture-recovery','target':p.IID,'started_at':self.clock.epoch.isoformat()})
        auth_path=self.base/'AUTHORIZATION.json'; p.atomic(auth_path,auth)
        pins=[]
        for n in range(7):
            file=self.base/f'fixture-pin-{n}'; file.write_text(f'nonsecret-fixture-{n}')
            pins.append({'path':str(file),'sha256':p.digest(file)})
        p.atomic(self.d.parent/'initial-admission.json',{'pins':pins})
        args=SimpleNamespace(run_dir=self.d,authorization=auth_path,authorization_sha256=p.digest(auth_path),source_sha256=p.digest(p.__file__),caller_sha256=p.digest(caller.__file__),readonly_units=maximum,cleanup_authorization=None,cleanup_authorization_sha256=None)
        # Candidate digest is fixed source policy identity, fixture file is not that policy.
        original_sha=caller.sha
        self._sha=lambda path:p.EXPECTED if Path(path)==self.d/'least-privilege-policy.json' else original_sha(path)
        return caller.Caller(args)
    def callbacks(self):
        def admission(d): self.trace.append(('admission',d.r['unit'])); self.clock.charge(.1)
        def verified(d):
            self.trace.append(('verified',d.r['unit']))
            recovered=self.make()
            assert recovered.r == json.loads(json.dumps(d.r,default=str))
        return admission,verified
    def step(self,n=1,admission=None,verified=None,cleanup=False):
        self.driver=self.make()
        a,v=self.callbacks()
        return self.driver.advance(cleanup_authorized=cleanup,readonly_units=n,admission=admission or a,verified=verified or v)


class Acceptance(unittest.TestCase):
    def setUp(self):
        self.stack=ExitStack(); self.addCleanup(self.stack.close)
        self.temp=self.stack.enter_context(tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')))
        self.root=Path(self.temp)
        for target in [(p.Live,'__init__'),(socket,'socket'),(subprocess,'Popen')]:
            self.stack.enter_context(patch.object(*target,side_effect=forbidden))
        self.stack.enter_context(patch.dict(sys.modules,stubs))
        self.h=Harness(self.root,self.stack)

    def test_unchanged_protected_ast_and_numeric_contract(self):
        tree=ast.parse((SOURCE/'phased.py').read_text())
        cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='Driver')
        pins={'call':'739fe237b2cc52d594f0616d6fedf46a82cb83b629b69b5a7f30f23373966942',
              'snapshot_units':'465fe1a1448f3d3730e6f9b6b968f6f7b8bb0093d83e86fad72f505810526914',
              'check_units':'60dd60e8b55cb26f951b3bc96f443f8779adb3c42f20436146c585404672c445',
              'simulation_units':'4549929335ab85ebc956bbbbf77d767547645a4e2a010d60bb7273b36c61c1f9',
              'guard_margin':'626bfaa214f48bbfc64d7ade794257c490dceb57ddb5a97f30fce2fc7da01e81',
              'mutate':'4fc7012d430251386ede3f9650e1ff74618d70a65c751c0229e646be7f4ef49d',
              'plan':'667dea8488fb51748f82cab217f09d3a50cee206b3a4d50134bb6f5f47f3f09a',
              'recovery':'41bfcbca32162f0bd7829422df4d5e850d54c9f6657e0626b4abb5cae0a35a68',
              'accept':'bcd97411a17b325edf0a3d1952564c9fdd1e3724c07595ec1fc70f1dee951b51',
              'footer':'4ac6e42e2f3b3ef5ce3b2b3e3229df61a02176ba49e0c8918bfb1248177dab41'}
        for node in cls.body:
            if isinstance(node,ast.FunctionDef) and node.name in pins:
                self.assertEqual(hashlib.sha256(ast.dump(node,include_attributes=False).encode()).hexdigest(),pins[node.name])
        self.assertEqual((p.WORK_SECONDS,p.SDK_SECONDS,p.DIAGNOSTIC_SECONDS,p.RECOVERY_SECONDS,p.FOOTER_SECONDS,p.CALL_SECONDS,p.WAIT_SECONDS),(75,65,5,140,10,230,65))
        predecessor_advance()
        EVIDENCE['carried_evidence']={'protected_methods':sorted(pins),'predecessor_sha256':'3931c9d50602137d887f3a5fd3dfe0e3a14ee434eb15e627e49c9679a3e4c7f7','unchanged_ast':True,'old_matrix_rerun':False}

    def test_trace_parity_and_default_bound(self):
        h=self.h
        # Actual snapshot SDK/local/predicate closures with durable per-unit records.
        before={x.name:x.read_bytes() for x in h.d.iterdir() if x.is_file()}
        self.assertEqual(h.step(8),0)
        grouped=copy.deepcopy(h.trace); grouped_state=copy.deepcopy(h.driver.r)
        rows=[json.loads(x) for x in (h.d/'PHASES.jsonl').read_text().splitlines()]
        self.assertEqual(sum(x['event']=='unit_started' for x in rows),9)
        self.assertEqual(h.driver.r['unit'],8)
        for path in h.d.iterdir():
            if path.is_file(): path.unlink()
        for name,raw in before.items(): (h.d/name).write_bytes(raw)
        h.clock=Clock(); h.adapter.clock=h.clock; h.trace.clear()
        for _ in range(8): self.assertEqual(h.step(),0)
        self.assertEqual(h.trace,grouped)
        for key in ['stage','unit','call_number','inflight','data']:
            self.assertEqual(h.driver.r[key],grouped_state[key])
        # Execute the authenticated PREDECESSOR advance too, not merely new vs new.
        for path in h.d.iterdir():
            if path.is_file(): path.unlink()
        for name,raw in before.items(): (h.d/name).write_bytes(raw)
        h.clock=Clock(); h.adapter.clock=h.clock; h.trace.clear()
        legacy=predecessor_advance()
        for _ in range(8):
            d=h.make(); self.assertEqual(legacy(d),0)
        core_trace=lambda rows:[x for x in rows if x[0] not in ['admission','verified']]
        self.assertEqual(core_trace(h.trace),core_trace(grouped))
        # The remaining snapshot chunk includes local reads and completed predicates.
        for _ in range(5):
            d=h.make(); self.assertEqual(legacy(d),0)
        legacy_snapshot=copy.deepcopy(d.r['data']['rehearsal-preimage'])
        for path in h.d.iterdir():
            if path.is_file(): path.unlink()
        for name,raw in before.items(): (h.d/name).write_bytes(raw)
        h.clock=Clock(); h.adapter.clock=h.clock; h.trace.clear()
        self.assertEqual(h.step(8),0); self.assertEqual(h.step(8),0)
        self.assertEqual(h.driver.r['unit'],13)
        self.assertEqual(norm(h.driver.r['data']['rehearsal-preimage']['iam']),norm(legacy_snapshot['iam']))
        for bad in [0,9,-1,True]:
            with self.assertRaises(AssertionError): h.driver.advance(readonly_units=bad)
        EVIDENCE['trace_parity']={'units':13,'default_one_preserved':True,'group_calls':2,'authenticated_predecessor_advance':True,'actual_snapshot_bodies_and_predicates':True}

    def test_phase_mutation_wait_approval_barriers(self):
        h=self.h
        for name in ['stage:candidate','stage:guard','rehearse:rollback','rehearse:guard','apply:binding',
                     'apply:delete_old_inline','apply:detach_cw','apply:detach_admin','phase180:wait',
                     'cleanup:permission','cleanup:remove','cleanup:accept','unknown:read']:
            with self.subTest(name=name):
                d=h.make(); d.plan=lambda:[('rehearsal-preimage:role',lambda:d.call('iam','get_role')), (name,forbidden)]
                a,v=h.callbacks()
                self.assertEqual(d.advance(readonly_units=8,admission=a,verified=v),0)
                self.assertEqual(d.r['unit'],1)
                # Reset only synthetic fixture, not a live retry.
                d.r.update(unit=0,call_number=1); d.save()
        d=h.make(); d.plan=lambda:[('rehearsal-preimage:role',lambda:d.call('iam','get_role'))]
        a,v=h.callbacks(); self.assertEqual(d.advance(readonly_units=8,admission=a,verified=v),0)
        self.assertEqual((d.r['stage'],d.r['unit']),('apply',0))
        d=h.make(); d.r.update(stage='cleanup',unit=0); d.save()
        self.assertEqual(d.advance(readonly_units=8,admission=forbidden,verified=forbidden),2)
        self.assertEqual(h.recoveries,0)
        EVIDENCE['barriers']='effects, guard creation, rollback, waits, binding, permission, removal, final acceptance, unknown, phase'

    def test_interruption_reconstruction_and_single_recovery(self):
        h=self.h
        initial={x.name:x.read_bytes() for x in h.d.iterdir() if x.is_file()}
        cases=[]
        for point in ['intent','sdk_return','commit_receipt','final_save','verified']:
            for path in h.d.iterdir():
                if path.is_file(): path.unlink()
            for name,raw in initial.items(): (h.d/name).write_bytes(raw)
            d=h.make(); d.r['mutation_started']=True; d.r['guard_expiry_utc']=(h.clock.now()+dt.timedelta(seconds=2000)).isoformat(); d.save()
            native_save=d.save; native_receipt=d.receipt; native_sdk=h.adapter.sdk
            fired=[False]
            def trip():
                if not fired[0]: fired[0]=True; raise OSError('PRIVATE_ERROR_MUST_NOT_PERSIST')
            def save():
                if point=='intent' and d.r['inflight'] and d.r['unit']==0: trip()
                if point=='final_save' and d.r['inflight'] is None: trip()
                return native_save()
            def receipt(kind,**kw):
                out=native_receipt(kind,**kw)
                if kind=='unit_committed' and point=='commit_receipt': trip()
                return out
            def sdk(*a,**kw):
                out=native_sdk(*a,**kw)
                if point=='sdk_return': trip()
                return out
            def verified(d): trip() if point=='verified' else None
            d.save=save; d.receipt=receipt
            count=h.recoveries
            with patch.object(h.adapter,'sdk',sdk):
                self.assertEqual(d.advance(readonly_units=8,admission=lambda d:None,verified=verified),1)
            self.assertEqual(h.recoveries,count+1)
            self.assertLessEqual(d.r['unit'],1)
            reconstructed=h.make()
            self.assertTrue(reconstructed.r['status'].startswith('FAILED'))
            before_calls=len(h.trace)
            self.assertEqual(reconstructed.advance(),1)
            self.assertFalse(any(x[0]=='sdk' for x in h.trace[before_calls:]))
            self.assertNotIn('PRIVATE_ERROR_MUST_NOT_PERSIST',(h.d/'RUN.json').read_text())
            cases.append(point)
        EVIDENCE['interruptions']=cases

    def test_fresh_caller_admission_and_post_deletion_disposition(self):
        h=self.h; c=h.bind_caller()
        with patch.object(caller,'sha',h._sha),patch.object(caller,'census',lambda:{'synthetic':True}):
            self.assertEqual(h.step(8,c.admission,c.verified),0)
        self.assertEqual(len(list(h.base.glob('caller-unit-*.json'))),8)
        self.assertEqual(sum(x==('sdk','ssm','describe_sessions') for x in h.trace),8)
        self.assertEqual(sum(x==('local','credential') for x in h.trace),8)
        # A changed exact recovery session on the NEXT unit must not use cache.
        h.adapter.bad_recovery=True
        with patch.object(caller,'sha',h._sha),patch.object(caller,'census',lambda:{'synthetic':True}):
            self.assertEqual(h.step(8,c.admission,c.verified),1)
        self.assertEqual(h.driver.r['unit'],8)
        for removed in [False,True]:
            d=h.make(); d.r.update(status='OBSERVING_GUARD_PRESENT',first_error=None,mutation_started=True,
                                  guard_removed=removed,inflight=None)
            d.r.pop('first_error'); d.save()
            count=h.recoveries
            self.assertEqual(d.advance(readonly_units=8,admission=lambda d:(_ for _ in ()).throw(AssertionError('admission')),verified=lambda d:None),1)
            self.assertEqual(h.recoveries,count+1)
            self.assertEqual(d.r['status'],'FAILED_REQUIRES_OFFHOST_ADMIN' if removed else 'FAILED_ROLLED_BACK')
        EVIDENCE['fresh_admission']={'per_unit_sessions':8,'per_unit_credentials':8,'cached_recovery_refused':True,'after_deletion':'OFFHOST_REQUIRED'}

    def test_aggregate_budget_and_changed_seam_margins(self):
        h=self.h
        initial={x.name:x.read_bytes() for x in h.d.iterdir() if x.is_file()}
        outcomes={}
        for seam in ['credential','prepare','admission','intent','sdk','diagnostic','footer']:
            for path in h.d.iterdir():
                if path.is_file(): path.unlink()
            for name,raw in initial.items(): (h.d/name).write_bytes(raw)
            h.clock=Clock(); h.adapter.clock=h.clock; h.adapter.delays={}
            d=h.make(); d.r['mutation_started']=True; d.r['guard_expiry_utc']=(h.clock.now()+dt.timedelta(seconds=2000)).isoformat(); d.save()
            original_save=d.save; original_receipt=d.receipt; original_persist=d.persist_error
            if seam in ['credential','prepare']: h.adapter.delays[seam]=40
            if seam=='sdk':
                h.adapter.delays['get_role']=40
                h.adapter.delays['list_attached_role_policies']=40
            def admission(d):
                if seam=='admission': h.clock.charge(40)
                if seam=='diagnostic': raise ValueError('PRIVATE_ERROR_MUST_NOT_PERSIST')
                d.local('credential')
            def save():
                if seam=='intent' and d.r['inflight'] and not d.r['status'].startswith('FAILED'): h.clock.charge(40)
                original_save()
            def receipt(kind,**kw):
                if seam=='footer' and kind=='bounded_call_complete': h.clock.charge(40)
                return original_receipt(kind,**kw)
            def persist(exc):
                if seam=='diagnostic': h.clock.charge(6)
                return original_persist(exc)
            d.save=save; d.receipt=receipt; d.persist_error=persist
            count=h.recoveries; start=h.clock.mono()
            self.assertEqual(d.advance(readonly_units=8,admission=admission,verified=lambda d:None),1,seam)
            self.assertEqual(h.recoveries,count+1)
            self.assertLessEqual(h.clock.mono()-start,230)
            self.assertLessEqual(d.r['unit'],1)
            outcomes[seam]={'elapsed':h.clock.mono()-start,'committed_cursor':d.r['unit']}
        # Unchanged final effect predicates, using actual mutate and lazy preparation.
        for first,margin in [(True,1200),(False,225)]:
            for delay in [0,2]:
                h.clock=Clock(); h.adapter.clock=h.clock; h.adapter.delays={'prepare':delay}
                d=h.make(); d.r.update(stage='apply',mutation_started=not first,guard_removed=False,
                                      guard_expiry_utc=(h.clock.now()+dt.timedelta(seconds=margin+1)).isoformat())
                before=len(h.trace)
                if delay:
                    with self.assertRaises(AssertionError): d.mutate('put_role_policy',PolicyName=p.POL,PolicyDocument='{}')
                    self.assertFalse(any(x==('sdk','iam','put_role_policy') for x in h.trace[before:]))
                else: d.mutate('put_role_policy',PolicyName=p.POL,PolicyDocument='{}')
        EVIDENCE['aggregate_budget']=outcomes
        # Actual cleanup deletion closure and final acceptance age checks, paired
        # zero/crossed lazy-preparation controls; no protected dispatch ever runs.
        for age_kind,limit in [('ping',420),('metric',300)]:
            for delay in [0,2]:
                h.clock=Clock(); h.adapter.clock=h.clock; h.adapter.delays={'prepare':delay}
                d=h.make(); d.r.update(stage='cleanup',unit=1,guard={'fixture':'guard'},guard_removed=False,mutation_started=True,
                                      guard_expiry_utc=(h.clock.now()+dt.timedelta(seconds=2000)).isoformat())
                recent={'ssm':[{'LastPingDateTime':h.clock.now()-dt.timedelta(seconds=limit-1 if age_kind=='ping' else 0)}],
                        'cloudwatch':{'latest':{'Timestamp':h.clock.now()-dt.timedelta(seconds=limit-1 if age_kind=='metric' else 0)}}}
                d.r['data']['fresh_recovery_before_guard_removal']=recent
                h.adapter.inline[p.GUARD]={'PolicyDocument':{}}
                with patch.object(p.Driver,'simulation_units',lambda *a,**kw:[]):
                    remove=d.plan()[1][1]
                before=len(h.trace)
                if delay:
                    with self.assertRaises(AssertionError): remove()
                    self.assertFalse(any(x==('sdk','iam','delete_role_policy') for x in h.trace[before:]))
                else: remove()
                d.r['data']['final_guard_absent']=recent
                # Acceptance refuses aged health before touching the postimage.
                h.clock.charge(limit+1)
                with self.assertRaisesRegex(AssertionError,'Final .* aged out'): d.accept()
        EVIDENCE['freshness_controls']={'cleanup_lazy_preparation':{'heartbeat_limit':420,'metric_limit':300},'final_acceptance_aged_health_refused':True}

    def test_dynamic_complete_path_and_timing(self):
        h=self.h
        # Keep actual plan/cursor/wait/check/mutation bodies. Replace ONLY the
        # unchanged broad simulator with 29 priced no-policy slots (not 315 tests).
        def simulations(d,label,principal=False,expired=False):
            return [(label+':vector',lambda:h.clock.charge(.05)) for _ in range(29)]
        h.driver.r.update(stage='apply',unit=0,status='ROLLBACK_REHEARSAL_PASS',guard={'fixture':'guard'},
                          guard_expiry_utc=(h.clock.epoch+dt.timedelta(seconds=2700)).isoformat())
        h.driver.r['data']['cron_before']={k:{'exit_code':0,'sha256':'fixture-cron'} for k in ['cron_user','cron_root']}
        h.driver.save()
        # Guard age includes a synthetic 324s rehearsal plus 120s handoff;
        # never renew the epoch to cutover. External call gap is charged separately.
        h.clock.charge(444)
        calls=units=waits=0; lengths=[]; deletion=None; cleanup_delay=False
        a,v=h.callbacks()
        def admission(d): h.clock.charge(1.9); a(d)
        with patch.object(p.Driver,'simulation_units',simulations):
            while h.driver.r['stage']!='done' and calls<150:
                d=h.make(); h.driver=d
                plan=d.plan(); name=plan[d.r['unit']][0]
                # plan building populates empty caches; publish only fixture setup,
                # normal production rebuild happens inside timed entry.
                d.save()
                if d.r['stage']=='cleanup':
                    lengths.append(len(plan))
                    if not cleanup_delay: h.clock.charge(60); cleanup_delay=True
                n=8 if p.Driver.readonly_unit(name) else 1
                before=d.r['call_number']
                rc=d.advance(cleanup_authorized=True,readonly_units=n,admission=admission,verified=v)
                self.assertEqual(rc,0,(name,d.r.get('first_error'),d.r.get('rollback_error')))
                calls+=1; units+=d.r['call_number']-before
                if name.endswith(':wait'): waits+=1
                if d.r['guard_removed'] and deletion is None: deletion=(h.clock.mono()-1000,units)
                h.clock.charge(6.83690565625)
        self.assertEqual(h.driver.r['stage'],'done')
        self.assertEqual(set(lengths),{80,77})
        self.assertEqual(units-deletion[1],75)
        self.assertGreaterEqual(waits,4)
        self.assertLess(deletion[0]+225,2700)
        self.assertEqual(h.driver.r['guard_expiry_utc'],(h.clock.epoch+dt.timedelta(seconds=2700)).isoformat())
        EVIDENCE['synthetic_lifecycle']={'external_calls':calls,'units':units,'wait_calls':waits,'cleanup_lengths':sorted(set(lengths)),
            'rehearsal_and_handoff_seconds':444,'independent_cleanup_approval_seconds':60,'external_gap_seconds':6.83690565625,
            'guard_deletion_epoch_seconds':deletion[0],'completion_epoch_seconds':h.clock.mono()-1000,'post_deletion_units':75,
            'guard_headroom_after_225_reserve':2700-deletion[0]-225,'AWS_latency_guarantee':False,'broad_policy_matrix_executed':False}


if __name__=='__main__':
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Acceptance))
    print(json.dumps({'passed':result.wasSuccessful(),'tests':result.testsRun,'evidence':EVIDENCE},indent=2))
    sys.exit(not result.wasSuccessful())
