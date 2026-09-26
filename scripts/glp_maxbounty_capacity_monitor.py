#!/usr/bin/env python3
"""Read-only MaxBounty account capacity observer. Never an application resumer."""
from __future__ import annotations
import json
import re
from contextlib import contextmanager
from pathlib import Path

BUSINESS = Path('/home/ubuntu/business')
APPLICATION_QUEUE = 'wuDGU2Xg1LvwXuk6'
TARGET_ID = '14449'
TARGET_QUEUE_ID = 'm2b-maxbounty-14449'
DETAIL_URL = 'https://affiliates.maxbounty.com/campaign/14449'

class Refusal(Exception):
    """Only fixed internal codes are permitted; external errors are never copied."""

def select_row(rows):
    if not isinstance(rows,list) or len(rows)!=1:
        raise Refusal('QUEUE_EXACT_TARGET_NOT_UNIQUE')
    row=rows[0]
    if not isinstance(row,dict) or any(row.get(k)!=v for k,v in {
        'network':'maxbounty','program_id':TARGET_ID,'queue_id':TARGET_QUEUE_ID,'status':'BLOCKED'
    }.items()):
        raise Refusal('QUEUE_IDENTITY_OR_STATE_MISMATCH')
    if re.search(r'fella',str(row.get('program_name','')),re.I):
        raise Refusal('EXCLUDED_PROVIDER')
    try:
        payload=json.loads(row['payload']) if isinstance(row['payload'],str) else row['payload']
        program=payload['program']
        if not isinstance(program,dict) or program.get('program_id')!=TARGET_ID or program.get('detail_url')!=DETAIL_URL:
            raise ValueError
    except (KeyError,TypeError,ValueError):
        raise Refusal('PROGRAM_PROJECTION_IDENTITY_MISMATCH') from None
    return {'queue_id':TARGET_QUEUE_ID,'program_id':TARGET_ID,'detail_url':DETAIL_URL}


def atomic_json(path, value):
    import os
    import tempfile
    path=Path(path)
    if path.is_symlink():raise Refusal('STATE_UNSAFE')
    fd,name=tempfile.mkstemp(prefix='.'+path.name+'.',dir=path.parent)
    try:
        with os.fdopen(fd,'w') as out:
            json.dump(value,out,sort_keys=True);out.write('\n');out.flush();os.fsync(out.fileno())
        os.replace(name,path)
        dfd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(dfd)
        finally:os.close(dfd)
    finally:
        if os.path.exists(name):os.unlink(name)


class DiscardOutput:
    def write(self,value):return len(value)
    def flush(self):pass


def run_once(path, read, probe):
    import contextlib
    import hashlib
    from datetime import datetime,timezone
    path=Path(path)
    if path.is_symlink():raise Refusal('STATE_UNSAFE')
    previous={}
    if path.exists():
        try:
            previous=json.loads(path.read_text())
            if previous.get('schema')!='glp.maxbounty.capacity-monitor.v1':raise ValueError
        except (ValueError,TypeError,AttributeError):raise Refusal('STATE_INVALID') from None
    result={'schema':'glp.maxbounty.capacity-monitor.v1','job_id':'89cd7b766dba',
            'queue_id':TARGET_QUEUE_ID,'program_id':TARGET_ID,'detail_url':DETAIL_URL,
            'capacity':'UNKNOWN','provider_pending_requests':None,
            'provider_pending_requests_evidence':'NOT_OBSERVED',
            'local_queue_count':None,'status':'BLOCKED_READ','reason':None,
            'observed_at_utc':datetime.now(timezone.utc).isoformat(),
            'successful_reads':previous.get('successful_reads',0),
            'effects':{'queue_writes':0,'applications':0,'sheet_calls':0,'tracking_clicks':0,'tracking_mints':0}}
    try:
        with contextlib.redirect_stdout(DiscardOutput()),contextlib.redirect_stderr(DiscardOutput()):
            target=select_row(read())
            evidence=probe(target)
        classification=evidence.get('classification')
        if evidence.get('campaign_api_status') not in {200,404} or classification not in {
            'provider_capacity_blocked','application_pending','already_approved','apply_needed','application_denied','terminal_provider_unavailable'}:
            raise Refusal('CAMPAIGN_EVIDENCE_UNCONFIRMED')
        digest=evidence.get('campaign_api_body_sha256')
        if not isinstance(digest,str) or not re.fullmatch('[0-9a-f]{64}',digest):
            raise Refusal('CAMPAIGN_EVIDENCE_UNCONFIRMED')
        blocked=evidence.get('capacity_marker_seen') is True and evidence['campaign_api_status']==200
        boundary=evidence.get('network_boundary') or {}
        result['network_boundary']={k:boundary[k] for k in ['campaign_gets','authentication_posts','page_view_posts','requests_blocked_before_network'] if type(boundary.get(k)) is int and boundary[k]>=0}
        result.update(status='OBSERVED',capacity='BLOCKED' if blocked else 'UNKNOWN',
                      classification=classification,campaign_api_status=evidence['campaign_api_status'],
                      campaign_api_body_sha256=digest,capacity_marker_seen=blocked,
                      successful_reads=result['successful_reads']+1)
    except Refusal as exc:
        result['reason']=exc.args[0]
    except Exception:
        result['reason']='OBSERVER_ERROR'
    stable={k:result.get(k) for k in ['status','reason','capacity','classification','campaign_api_status','provider_pending_requests','queue_id','program_id']}
    signature=hashlib.sha256(json.dumps(stable,sort_keys=True).encode()).hexdigest()
    changed=signature!=previous.get('semantic_signature')
    result.update(semantic_signature=signature,changed=changed,
                  notification_evidence='LOCAL_STDOUT_INTENT_NOT_DELIVERY')
    atomic_json(path,result)
    if changed:
        print('📊 **MaxBounty Capacity** — read-only observation\n\n'
              + ('Provider capacity: **'+result['capacity']+'**; pending-request count: **UNKNOWN**.'
                 if result['status']=='OBSERVED' else 'Read blocked: **'+result['reason']+'**; capacity: **UNKNOWN**.')
              + '\n→ no applications, queue or Sheet changes\n-# state: '+str(path))
    return result


class RequestGuard:
    """Pre-network blocking, not retrospective permission checking."""
    def __init__(self):
        self.campaign_gets=0;self.auth_posts=0;self.pageview_posts=0;self.blocked=0
    def allow(self,method,url):
        from urllib.parse import urlsplit
        u=urlsplit(url)
        if u.scheme!='https' or u.netloc not in {'affiliates.maxbounty.com','api.maxbounty.com'} or u.username or u.password:
            return False
        if re.search(r'tracking|requestApproval|/go/|/save|/mint|/upload|/delete|/update',u.path,re.I):return False
        if u.netloc=='affiliates.maxbounty.com':
            if u.path.startswith('/campaign/') and url!=DETAIL_URL:return False
            return method in {'GET','HEAD'}
        if u.path.startswith('/affiliates/api/campaign/'):
            if url!='https://api.maxbounty.com/affiliates/api/campaign/'+TARGET_ID or method!='GET' or self.campaign_gets:return False
            self.campaign_gets+=1;return True
        if method in {'GET','HEAD','OPTIONS'}:return True
        if method=='POST' and u.path=='/affiliates/api/authenticationApp' and not u.query and self.auth_posts<1:
            self.auth_posts+=1;return True
        if method=='POST' and u.path=='/affiliates/api/affiliate/pageView' and not u.query and self.pageview_posts<3:
            self.pageview_posts+=1;return True
        return False
    def route(self,route):
        if self.allow(route.request.method,route.request.url):route.continue_()
        else:self.blocked+=1;route.abort('blockedbyclient')


def read_queue(dt=None):
    from urllib.parse import urlencode
    if dt is None:
        import sys
        sys.path.insert(0,str(BUSINESS/'ops'))
        import n8n_dt as dt
    filt={'type':'and','filters':[{'columnName':'queue_id','condition':'eq','value':TARGET_QUEUE_ID}]}
    data=dt._native_http('GET','/data-tables/'+APPLICATION_QUEUE+'/rows?'+urlencode({'limit':2,'filter':json.dumps(filt,separators=(',',':'))}),timeout_s=30)
    if not isinstance(data,dict) or data.get('nextCursor') or not isinstance(data.get('data'),list):
        raise Refusal('QUEUE_READ_INCOMPLETE')
    return data['data']


def probe_with_session(target,session,mb):
    if target!={'queue_id':TARGET_QUEUE_ID,'program_id':TARGET_ID,'detail_url':DETAIL_URL}:
        raise Refusal('PROGRAM_PROJECTION_IDENTITY_MISMATCH')
    if getattr(session,'email',None)!='ops@glpclinicscout.com':
        raise Refusal('ACCOUNT_ALIAS_MISMATCH')
    page=session.driver.page
    guard=RequestGuard()
    page.context.route('**/*',guard.route)
    page.context.set_offline(False)
    login=mb.login_with_dom_settle(session)
    if login.get('outcome') not in {'session_reused','session_reused_after_dom_settle'}:
        raise Refusal('LOGIN_WALL_OR_CHALLENGE')
    state,_unused_control=mb._open_campaign_detail(session.driver,{'program_id':TARGET_ID,'detail_url':DETAIL_URL})
    if any(state.get(k)!=v for k,v in {'program_id':TARGET_ID,'detail_url':DETAIL_URL,'observed_url':DETAIL_URL}.items()):
        raise Refusal('CAMPAIGN_IDENTITY_MISMATCH')
    return {'campaign_api_status':state.get('campaign_api_status'),
            'classification':state.get('classification'),
            'campaign_api_body_sha256':state.get('campaign_api_body_sha256'),
            'capacity_marker_seen':bool(mb._provider_capacity_markers(state.get('campaign_api_relationship') or {})),
            'network_boundary':{'campaign_gets':guard.campaign_gets,'authentication_posts':guard.auth_posts,
                                'page_view_posts':guard.pageview_posts,'requests_blocked_before_network':guard.blocked}}


def assert_profile_idle(profile,proc=Path('/proc')):
    import os
    profile=Path(profile)
    if not profile.is_dir() or profile.resolve()!=profile:raise Refusal('PROFILE_UNSAFE')
    if any(os.path.lexists(profile/n) for n in ['SingletonLock','SingletonSocket','SingletonCookie']):
        raise Refusal('PROFILE_ALREADY_OWNED')
    needle=('--user-data-dir='+str(profile)).encode()
    for process in Path(proc).iterdir():
        if not process.name.isdigit():continue
        try:args=(process/'cmdline').read_bytes().split(b'\0')
        except FileNotFoundError:continue
        except PermissionError:raise Refusal('PROFILE_OWNER_UNKNOWN') from None
        if needle in args:raise Refusal('PROFILE_ALREADY_OWNED')


def verify_pins(path):
    import hashlib
    try:
        data=json.loads(Path(path).read_text())
        if not data['files']:raise ValueError
        for name,expected in data['files'].items():
            target=Path(name)
            if target.is_symlink() or hashlib.sha256(target.read_bytes()).hexdigest()!=expected:
                raise ValueError
    except (OSError,ValueError,TypeError,KeyError):raise Refusal('SOURCE_PIN_MISMATCH') from None


def restricted_classes(pl):
    class Driver(pl.PlaywrightDriver):
        def start(self):
            from playwright.sync_api import sync_playwright
            self._pw=sync_playwright().start()
            # Existing M2b SafeDriver contract: no evasion hooks or fallback profile.
            self._ctx=self._pw.chromium.launch_persistent_context(
                str(self.profile_dir),channel='chrome',headless=True,offline=True,viewport={'width':1440,'height':1000})
            self.page=self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
            self.page.set_default_timeout(20000)
            if self._ctx.service_workers:raise Refusal('SERVICE_WORKER_BOUNDARY_UNKNOWN')
            return self
        def fill(self,*args,**kwargs):raise Refusal('LOGIN_WALL_OR_CHALLENGE')
        def click(self,*args,**kwargs):raise Refusal('LOGIN_WALL_OR_CHALLENGE')
    class Session(pl.PortalSession):
        def capture(self,*args,**kwargs):return {}
        def _claim_login_attempt(self):raise Refusal('LOGIN_WALL_OR_CHALLENGE')
        def _solve_captcha_if_present(self):raise Refusal('LOGIN_WALL_OR_CHALLENGE')
        def _complete_email_2fa(self,*args):raise Refusal('LOGIN_WALL_OR_CHALLENGE')
    return Driver,Session


class NullReceipts:
    def record(self,*args,**kwargs):return None


def session_leases():
    """Hold the original owner/profile refusals for the whole fresh tool session."""
    from contextlib import ExitStack
    import fcntl
    profile=Path('/home/ubuntu/.hermes/secrets/affiliate-marketplaces/maxbounty/browser-profile')
    lease_paths=[BUSINESS/'reports/glp-clinic-scout/affiliate-machine/applications/.application-machine.lock',
                 BUSINESS/'reports/ops/orchestration-restore-20260907/PLAN/MARKETPLACE-ACCESS/M2b/MAXBOUNTY-LIVE/owner.lock']
    stack=ExitStack()
    try:
        for lease in lease_paths:
            if lease.is_symlink() or not lease.is_file():raise Refusal('SESSION_LEASE_MISSING')
            handle=stack.enter_context(lease.open('r+'))
            try:fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise Refusal('SESSION_LEASE_BUSY') from None
        assert_profile_idle(profile)
        return stack
    except BaseException:
        stack.close()
        raise


def capacity_lease_path():
    return BUSINESS/'reports/glp-clinic-scout/affiliate-machine/applications/.maxbounty-capacity-lease.json'


def _lease_number(value):
    import math
    return type(value) in {int, float} and math.isfinite(value) and value > 0


def _lease_token(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def _read_capacity_lease(path):
    import os
    import stat
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError:
        raise Refusal('CAPACITY_LEASE_INVALID') from None
    try:
        with os.fdopen(fd) as src:
            if not stat.S_ISREG(os.fstat(src.fileno()).st_mode):
                raise ValueError
            lease = json.load(src)
        if (not isinstance(lease, dict) or lease.get('schema') != 'glp.maxbounty.capacity-lease.v1'
                or lease.get('status') not in {'active', 'released'}
                or not _lease_token(lease.get('owner_token'))
                or not all(_lease_number(lease.get(k)) for k in ('acquired_at', 'renewed_at', 'expires_at'))
                or not lease['acquired_at'] <= lease['renewed_at'] < lease['expires_at']
                or not isinstance(lease.get('takeovers'), list)):
            raise ValueError
        fields = {'schema', 'status', 'owner_token', 'acquired_at', 'renewed_at', 'expires_at', 'takeovers'}
        if lease['status'] == 'released':
            fields.add('released_at')
            if not _lease_number(lease.get('released_at')) or lease['released_at'] != lease['renewed_at']:
                raise ValueError
        if 'controller' in lease:
            fields.add('controller')
            anchor = lease['controller']
            if (not isinstance(anchor, dict) or set(anchor) != {'revision', 'status', 'sha256'}
                    or type(anchor['revision']) is not int or anchor['revision'] < 1
                    or anchor['status'] not in {'ready', 'in_progress', 'closed'}
                    or not _lease_token(anchor['sha256'])):
                raise ValueError
        if set(lease) != fields:
            raise ValueError
        for event in lease['takeovers']:
            if (not isinstance(event, dict) or set(event) != {
                    'previous_owner_sha256', 'owner_sha256', 'expired_at', 'taken_over_at'}
                    or not all(_lease_token(event[k]) for k in ('previous_owner_sha256', 'owner_sha256'))
                    or not all(_lease_number(event[k]) for k in ('expired_at', 'taken_over_at'))
                    or not event['expired_at'] < event['taken_over_at'] <= lease['acquired_at']):
                raise ValueError
        return lease
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        raise Refusal('CAPACITY_LEASE_INVALID') from None


def _capacity_lease_update(owner_token, ttl_seconds, *, release=False):
    """Only call while both original, stable-inode session locks are held."""
    import secrets
    import time
    path = capacity_lease_path()
    lease = _read_capacity_lease(path)
    now = time.time()
    if lease is not None and now < lease['renewed_at']:
        raise Refusal('CAPACITY_LEASE_CLOCK_REGRESSED')
    if owner_token is None:
        takeovers = lease['takeovers'] if lease is not None else []
        if lease is not None and lease['status'] == 'active' and now <= lease['expires_at']:
            raise Refusal('CAPACITY_LEASE_BUSY')
        owner_token = secrets.token_hex(32)
        if lease is not None and lease['status'] == 'active':
            import hashlib
            takeovers.append({
                'previous_owner_sha256': hashlib.sha256(lease['owner_token'].encode()).hexdigest(),
                'owner_sha256': hashlib.sha256(owner_token.encode()).hexdigest(),
                'expired_at': lease['expires_at'], 'taken_over_at': now})
        lease = {'schema': 'glp.maxbounty.capacity-lease.v1', 'status': 'active',
                 'owner_token': owner_token, 'acquired_at': now,
                 'renewed_at': now, 'expires_at': now + ttl_seconds, 'takeovers': takeovers}
    else:
        if lease is None:
            raise Refusal('CAPACITY_LEASE_MISSING')
        if not secrets.compare_digest(lease['owner_token'], owner_token):
            raise Refusal('CAPACITY_LEASE_WRONG_OWNER')
        if lease['status'] != 'active':
            raise Refusal('CAPACITY_LEASE_RELEASED')
        if now >= lease['expires_at']:
            raise Refusal('CAPACITY_LEASE_EXPIRED')
        lease.update(renewed_at=now, expires_at=now + ttl_seconds)
    if release:
        lease.update(status='released', released_at=now)
    atomic_json(path, lease)
    return owner_token


@contextmanager
def capacity_lease_stage(*, owner_token=None, final=False, ttl_seconds=300):
    """Start or resume one stage; persist custody across interpreter exit.

    Retain the returned token privately for the next stage. A normal final
    stage releases custody; errors leave it reserved until expiry. The original
    kernel locks protect the *whole* stage, never the replaceable JSON inode.
    """
    if (not _lease_number(ttl_seconds) or ttl_seconds > 900 or type(final) is not bool
            or (owner_token is not None and not _lease_token(owner_token))):
        raise Refusal('CAPACITY_LEASE_ARGUMENT_INVALID')
    with session_leases():
        token = _capacity_lease_update(owner_token, ttl_seconds)
        yield token
        _capacity_lease_update(token, ttl_seconds, release=final)


def controller_state_path():
    return capacity_lease_path().with_name('.maxbounty-capacity-controller.json')


def _controller_lease(owner_token):
    """Read-only custody check, including before every browser boundary.

    Caller holds both stable-inode locks for the entire stage. The private
    lease is the trust anchor, not a hash stored alongside untrusted state.
    """
    import secrets
    import time
    lease = _read_capacity_lease(capacity_lease_path())
    if lease is None:
        raise Refusal('CAPACITY_LEASE_MISSING')
    if not secrets.compare_digest(lease['owner_token'], owner_token):
        raise Refusal('CAPACITY_LEASE_WRONG_OWNER')
    if lease['status'] != 'active':
        raise Refusal('CAPACITY_LEASE_RELEASED')
    now = time.time()
    if now < lease['renewed_at']:
        raise Refusal('CAPACITY_LEASE_CLOCK_REGRESSED')
    if now >= lease['expires_at']:
        raise Refusal('CAPACITY_LEASE_EXPIRED')
    return lease


def _controller_digest(value):
    import hashlib
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _read_controller(anchor, binding):
    import hashlib
    import os
    import stat
    try:
        fd = os.open(controller_state_path(), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as src:
            info = os.fstat(src.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > 65536):
                raise ValueError
            raw = src.read(65537)
        if hashlib.sha256(raw).hexdigest() != anchor['sha256']:
            raise ValueError
        state = json.loads(raw)
        if (not isinstance(state, dict) or set(state) != {'binding', 'revision', 'controller'}
                or state['binding'] != binding or state['revision'] != anchor['revision']):
            raise ValueError
        return state['controller']
    except (OSError, ValueError, TypeError, KeyError):
        raise Refusal('CONTROLLER_STATE_INVALID') from None


def _fresh_source_identity(pins):
    import hashlib
    verify_pins(pins)
    pinned = json.loads(Path(pins).read_text())['files']
    sources = [Path(__file__).resolve(), Path(__file__).with_name('glp_maxbounty_fresh_probe.py').resolve()]
    if any(str(source) not in pinned for source in sources):
        raise Refusal('FRESH_PRODUCER_SOURCE_PIN_MISSING')
    return _controller_digest({p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sources})


@contextmanager
def fresh_browser_stage(*, pins, cdp, drain_events, switch_tab, owner_token=None,
                        final=False, ttl_seconds=300, target=None):
    """Lease-bound prepare / vault-tool interval / poll / final-probe stage.

    Yield (token, controller). Carry the token privately, not the Python object.
    Only a successful pre-probe stage is resumable. Before any browser I/O an
    in_progress anchor burns the old checkpoint: crashes and uncertain effects
    cannot replay it. No credentials or raw payloads are checkpointed. The final
    stage closes the owned context before releasing. Standalone cron is held.
    """
    import hashlib
    from scripts.glp_maxbounty_fresh_probe import FreshCapacityProbe, TARGET
    if target is not None and target != TARGET:
        raise Refusal('PROGRAM_PROJECTION_IDENTITY_MISMATCH')
    source_sha256 = _fresh_source_identity(pins)
    with capacity_lease_stage(owner_token=owner_token, final=final, ttl_seconds=ttl_seconds) as token:
        lease = _controller_lease(token)
        binding = {'schema': 'glp.maxbounty.controller.v1',
                   'owner_sha256': hashlib.sha256(token.encode()).hexdigest(),
                   'request_sha256': _controller_digest(TARGET), 'source_sha256': source_sha256}
        anchor = lease.get('controller')
        state = None
        if owner_token is not None:
            if anchor is None or anchor['status'] != 'ready':
                raise Refusal('CONTROLLER_STAGE_NOT_RESUMABLE')
            state = _read_controller(anchor, binding)
        revision = anchor['revision'] + 1 if anchor else 1
        lease['controller'] = {'revision': revision, 'status': 'in_progress', 'sha256': '0' * 64}
        atomic_json(capacity_lease_path(), lease)

        active = True

        def check_custody():
            if not active:
                raise Refusal('CONTROLLER_STAGE_FENCED')
            current = _controller_lease(token)
            if current.get('controller') != lease['controller']:
                raise Refusal('CONTROLLER_STAGE_FENCED')

        producer = FreshCapacityProbe(cdp=cdp, drain_events=drain_events, switch_tab=switch_tab)
        producer.check_custody = check_custody
        try:
            if state is None:
                producer.begin()
            else:
                producer.restore(state)
            yield token, producer
            check_custody()
            if producer.fault:
                raise Refusal(producer.fault)  # run_once may have caught the failure
            if final:
                producer.close()
                checkpoint = {'closed': True}  # no retained campaign body/projection
            else:
                checkpoint = producer.checkpoint()
            state = {'binding': binding, 'revision': revision, 'controller': checkpoint}
            check_custody()
            atomic_json(controller_state_path(), state)
            # Anchor producer bytes, never a possibly tampered postwrite reread.
            # Old checkpoints cannot rewind budgets under this lease anchor.
            digest = hashlib.sha256((json.dumps(state, sort_keys=True) + '\n').encode()).hexdigest()
            check_custody()
            lease['controller'] = {'revision': revision, 'status': 'closed' if final else 'ready', 'sha256': digest}
            atomic_json(capacity_lease_path(), lease)
        finally:
            active = False  # escaped Python references never outlive lock custody


def fresh_browser_session(*, pins, cdp, drain_events, switch_tab):
    """Single-process compatibility entry; staged callers use fresh_browser_stage.

    Both entries require pins and the same durable lease. Browser/heavy
    admission and actual vault tools remain the agent runner's prerequisites.
    """
    _fresh_source_identity(pins)
    from scripts.glp_maxbounty_fresh_probe import FreshCapacityProbe
    from contextlib import ExitStack
    leases=ExitStack()
    try:
        leases.enter_context(capacity_lease_stage(final=True))
        producer=leases.enter_context(FreshCapacityProbe(cdp=cdp, drain_events=drain_events, switch_tab=switch_tab))
        return leases,producer
    except BaseException:
        leases.close()
        raise


def live_probe(target):
    # The standalone shell cannot invoke vault tools or own a named AI browser.
    # An authorized agent can use fresh_browser_stage; registration stays held.
    # Never launch the retired persistent-cookie transport as a fallback.
    if target!={'queue_id':TARGET_QUEUE_ID,'program_id':TARGET_ID,'detail_url':DETAIL_URL}:
        raise Refusal('PROGRAM_PROJECTION_IDENTITY_MISMATCH')
    with capacity_lease_stage(final=True):
        raise Refusal('FRESH_VAULT_STAGES_NOT_INTEGRATED')


def main():
    import argparse
    import fcntl
    import os
    import signal
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pins',type=Path,default=Path(__file__).with_suffix('.pins.json'))
    parser.add_argument('--check-offline',action='store_true')
    args=parser.parse_args()
    try:
        verify_pins(args.pins)
        if args.check_offline:
            print('PASS_OFFLINE_SOURCE_PINS_NO_NETWORK');return 0
        state=BUSINESS/'ops/state/glp-maxbounty-capacity-monitor.json'
        state.parent.mkdir(parents=True,exist_ok=True)
        fd=os.open(str(state)+'.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            def timeout(_signum,_frame):raise Refusal('BOUNDED_READ_TIMEOUT')
            signal.signal(signal.SIGALRM,timeout);signal.alarm(150)
            try:result=run_once(state,read_queue,live_probe)
            finally:signal.alarm(0)
        return 0 if result['status']=='OBSERVED' else 2
    except Exception:
        print('MaxBounty capacity monitor: local preflight/state failure; no success claim.')
        return 2


if __name__=='__main__':
    raise SystemExit(main())
