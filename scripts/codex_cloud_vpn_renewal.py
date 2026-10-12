#!/usr/bin/env python3
"""Scoped Tailscale OAuth/90-day key API, with secret-safe durable staging.

Key values live only in private 0600 pending files and process memory. Public
state retains metadata and phase. Never retry an ambiguous create request.
"""
from __future__ import annotations
import argparse,datetime as dt,fcntl,json,os,stat,urllib.error,urllib.parse,urllib.request
from pathlib import Path
ROOT=Path('/home/ubuntu/business')
SECURE=Path('/home/ubuntu/.hermes/secrets/codex-cloud')
CLIENT=SECURE/'tailscale-oauth.json'
STATE=ROOT/'state/codex-cloud-vpn-renewal.json'
CONFIG=ROOT/'ops/codex-cloud/tailscale-renewal.json'
TAG='tag:codex-cloud'
LIFETIME=90*86400


def atomic(path,body,private=False):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    tmp=path.with_name('.'+path.name+'.'+os.urandom(8).hex())
    fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    os.fchmod(fd,0o600)
    try:
        with os.fdopen(fd,'w') as f:
            json.dump(body,f,indent=2);f.write('\n');f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        tmp.unlink(missing_ok=True)


def load(path, fallback):
    return json.loads(path.read_text()) if path.exists() else fallback


def private_read(path):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    try:
        s=os.fstat(fd)
        if not stat.S_ISREG(s.st_mode) or s.st_uid!=os.getuid() or stat.S_IMODE(s.st_mode)!=0o600 or s.st_nlink!=1:
            raise RuntimeError('private_file_mode_or_owner')
        with os.fdopen(fd,'r') as stream:
            fd=None
            return json.load(stream)
    finally:
        if fd is not None: os.close(fd)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        raise RuntimeError('api_redirect_refused')


class API:
    def __init__(self,client=CLIENT):
        self.opener=urllib.request.build_opener(NoRedirect())
        secret=private_read(client)
        assert secret['scope']=='auth_keys' and secret['tags']==[TAG]
        body=urllib.parse.urlencode({'grant_type':'client_credentials','client_id':secret['client_id'],
                                    'client_secret':secret['client_secret'],'scope':'auth_keys'}).encode()
        req=urllib.request.Request('https://api.tailscale.com/api/v2/oauth/token',data=body,method='POST')
        with self.opener.open(req,timeout=30) as resp: result=json.load(resp)
        self.token=result['access_token']
        self.client_id=secret['client_id']
    def request(self,method,path,body=None):
        req=urllib.request.Request('https://api.tailscale.com/api/v2/tailnet/-/'+path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={'Authorization':'Bearer '+self.token,'Content-Type':'application/json'},method=method)
        with self.opener.open(req,timeout=30) as resp:
            raw=resp.read();return json.loads(raw) if raw else {'http':resp.status}
    def metadata(self,key_id):
        d=self.request('GET','keys/'+urllib.parse.quote(key_id,safe=''))
        d.pop('key',None)
        return d
    def mint(self,description):
        return self.request('POST','keys',{'capabilities':{'devices':{'create':{
            'reusable':True,'ephemeral':True,'preauthorized':True,'tags':[TAG]}}},
            'expirySeconds':LIFETIME,'description':description})
    def revoke(self,key_id):return self.request('DELETE','keys/'+urllib.parse.quote(key_id,safe=''))


def due(expires,now):
    return dt.datetime.fromisoformat(expires.replace('Z','+00:00'))-now < dt.timedelta(days=14)


def mint_due(config,state_path=STATE,api=None,now=None,secure=SECURE):
    api=api or API();now=now or dt.datetime.now(dt.timezone.utc)
    state=json.loads(state_path.read_text()) if state_path.exists() else {'schema':1,'environments':{}}
    outcomes=[]
    for env in config['environments']:
        assert env['tags']==[TAG]
        name=env['name'];prior=state['environments'].get(name)
        if prior and prior['phase'] not in ('OLD_REVOKED',):
            if prior['phase']=='MINT_INTENT': raise RuntimeError('ambiguous_mint_requires_metadata_reconcile')
            outcomes.append({k:prior[k] for k in ('name','phase','key_id','expires')});continue
        old=prior['key_id'] if prior else env['key_id']
        meta=api.metadata(old)
        create=meta['capabilities']['devices']['create']
        if create['tags']!=[TAG] or not all(create.get(k) for k in ('reusable','ephemeral','preauthorized')):
            raise RuntimeError('existing_key_scope_mismatch')
        expiry=meta['expires']
        if not due(expiry,now):
            outcomes.append({'name':name,'phase':'NOT_DUE','key_id':old,'expires':expiry});continue
        pending=secure/('pending-'+name+'.json')
        if pending.exists(): raise RuntimeError('owned_pending_secret_exists_reconcile')
        description='R1'+('Account1' if 'account1' in name else 'Account2')+now.strftime('%Y%m%dT%H%M%SZ')
        row={'name':name,'account':env['account'],'environment_id':env['environment_id'],
             'old_key_id':old,'old_expires':expiry,'description':description,'phase':'MINT_INTENT'}
        state['environments'][name]=row;atomic(state_path,state)
        minted=api.mint(description)
        key=minted['key'];key_id=minted['id'];metadata=api.metadata(key_id)
        caps=metadata['capabilities']['devices']['create']
        assert caps['tags']==[TAG] and all(caps.get(k) for k in ('reusable','ephemeral','preauthorized'))
        created=dt.datetime.fromisoformat(metadata['created'].replace('Z','+00:00'))
        ends=dt.datetime.fromisoformat(metadata['expires'].replace('Z','+00:00'))
        assert LIFETIME-5 <= (ends-created).total_seconds() <= LIFETIME+5
        atomic(pending,{'key':key,'key_id':key_id,'environment_id':env['environment_id']},private=True)
        row.update(phase='MINTED',key_id=key_id,expires=metadata['expires'],created=metadata['created'],
                   tags=[TAG],lifetime_days=90,secret_path=str(pending))
        key=None;minted=None;atomic(state_path,state)
        outcomes.append({k:row[k] for k in ('name','phase','key_id','expires','lifetime_days')})
    return {'status':'MINT_PHASE_COMPLETE','environments':outcomes,'acl_changed':False,'secret_printed':False}


def browser(env, action):
    import gpt6pro_relay as relay
    import uuid
    relay.use_account(relay.account_of(relay.load_config(),env['account']))
    relay.BU_ENV['BU_NAME']='codex-vpn-'+env['name']+'-'+uuid.uuid4().hex[:10]
    source=Path(__file__).with_name('codex_cloud_vpn_browser.py')
    prefix='import pathlib,json,sys\n'
    prefix+='sys.path.insert(0,'+repr(str(Path(__file__).parent))+')\nimport codex_cloud_vpn_renewal as renewal\n'
    prefix+='env='+repr(env)+'\nstate=json.loads(renewal.STATE.read_text())\nrow=state["environments"][env["name"]]\n'
    prefix+='exec(pathlib.Path('+repr(str(source))+').read_text(),globals())\n'
    if action=='usage':
        prefix+='print(json.dumps({"usage":check_usage(env)}))\n'
    elif action=='save':
        prefix+='save_vpn(env,row,renewal,state)\nprint(json.dumps({"phase":row["phase"]}))\n'
    elif action=='publish':
        prefix+='publish(env,row,renewal,state)\nprint(json.dumps({"phase":row["phase"]}))\n'
    else:
        raise ValueError('unknown_browser_action')
    try:
        result=relay.bu(prefix,timeout=180)
        return json.loads(str(result).strip().splitlines()[-1])
    except BaseException as error:
        import re
        labels=re.findall(r'provider_publish_refused_after_one_retry|provider_publish_refused|browser_state_timeout|exact_environment_resource_not_observed|approved_host_settings_missing_do_not_infer_repair|new_owned_editor_not_created|browser_session_identity_challenge|account_identity_mismatch|publication_uncertain_no_replay',str(error))
        reason=labels[-1] if labels else type(error).__name__
        raise ValueError('browser_'+action+'_failed:'+reason) from None


def parse_proof(body, env, nonce):
    import re
    if re.search(r'tskey-|Bearer |PRIVATE KEY',body,re.I):
        raise ValueError('unsafe_proof_body_no_credentials')
    start,end=body.find('{'),body.rfind('}')
    value=json.loads(body[start:end+1])
    if set(value)!={'schema','nonce','environment_id','new_cloud_task','task_id','task_url','allowed','denied'} or set(value.get('allowed',{}))!={'command_exit','http','schema','generated_at'} or set(value.get('denied',{}))!={'port','command_exit','http'}:
        raise ValueError('proof_fields_not_allowlisted')
    if value.get('schema')!='codex-cloud-vpn-proof-v1' or value.get('nonce')!=nonce or value.get('environment_id')!=env['environment_id']:
        raise ValueError('cloud_proof_binding_mismatch')
    import uuid
    if value.get('new_cloud_task') is not True or not value.get('task_id') or not value.get('task_url','').startswith('https://chatgpt.com/'):
        raise ValueError('new_cloud_task_custody_missing')
    try: uuid.UUID(value['task_id'])
    except (ValueError,TypeError,AttributeError): raise ValueError('cloud_task_id_invalid') from None
    if value['task_id'] not in value['task_url']:
        raise ValueError('cloud_task_url_id_mismatch')
    url=urllib.parse.urlsplit(value['task_url'])
    if url.hostname!='chatgpt.com' or url.username or url.password or url.port not in (None,443):
        raise ValueError('cloud_task_url_not_allowed')
    value['task_url']=urllib.parse.urlunsplit((url.scheme,url.netloc,url.path,'',''))
    allowed,denied=value['allowed'],value['denied']
    try:
        generated=dt.datetime.fromisoformat(allowed['generated_at'].replace('Z','+00:00'))
        if generated.tzinfo is None: raise ValueError('timestamp_timezone_required')
    except (ValueError,TypeError,AttributeError): raise ValueError('cloud_timestamp_invalid') from None
    if type(allowed.get('command_exit')) is not int or allowed.get('command_exit')!=0 or allowed.get('http')!=200 or allowed.get('schema')!='codex-evidence-v1' or not allowed.get('generated_at'):
        raise ValueError('allowed_cloud_get_not_proved')
    if type(denied.get('command_exit')) is not int or not 0 <= denied['command_exit'] <= 255 or denied.get('port')!=8443 or denied.get('http') not in (403,503):
        raise ValueError('denied_positive_control_not_proved')
    return value


def proof(env, row, state):
    import dot_dispatch as dots
    import uuid
    dot='rook' if env['name'].endswith('1') else 'finch'
    if not row.get('native_ticket'):
        nonce='r1-vpn-'+uuid.uuid4().hex[:20]
        brief=(f"Operator-authorized R1 recurring VPN renewal proof. Owner R1. Skills: codex-cloud-tailnet-pilot, credential-boundaries. "
          f"Discover and start exactly ONE NEW Codex Cloud task in {env['name']} / {env['environment_id']}, current published version, GPT-6.1 Sol / Light. "
          "Included allowance was just verified positive in the exact account; check again if your native tool exposes it. No credits, resets, local/connected-Mac substitute, repository/host edits, credential reads, or other tasks. "
          "In the NEW Cloud VM run proxy-respecting curl --max-time 12 GET http://hermes-01.tail9e25df.ts.net:18909/v1/fleet-status.json (parse schema/generated_at only), and ONE denied HEAD curl --max-time 12 -I https://hermes-01.tail9e25df.ts.net:8443/ . "
          f"Print nonce {nonce} in actual terminal output. Reply RESULT with a JSON object schema codex-cloud-vpn-proof-v1, nonce, environment_id, new_cloud_task true, task_id, task_url, "
          "allowed:{command_exit,http,schema,generated_at}, denied:{port:8443,command_exit,http}. Raw exits/statuses only, no invented data; <1600 chars. Or BLOCKED with exact provider message/reset time. Stop after two requests. Evidence is not approval.")
        row.update(phase='PROOF_DISPATCH_INTENT',proof_nonce=nonce)
        atomic(STATE,state)
        try:
            sent=dots.dispatch(dot,'GLP-359',brief,root=ROOT/'reports/ops/dot-tickets',parent='fable-plan-2-20261009',owner='R1')
        except ValueError as error:
            if str(error) in ('one_open_ticket_per_dot_reconcile_existing','brief_exceeds_discord_limit','subscription_binding_or_ttl'):
                row.update(phase='PUBLISHED',last_blocker=str(error));atomic(STATE,state)
            raise
        row.update(phase='PROOF_PENDING',native_ticket=sent['ticket'],native_request_id=sent['request_id'])
        atomic(STATE,state)
    directory=ROOT/'reports/ops/dot-tickets'/row['native_ticket']
    ticket=json.loads((directory/'TICKET.json').read_text())
    if ticket.get('dot')!=dot or ticket.get('task_owner')!='R1' or ticket.get('request_id')!=row['native_request_id'] or ticket.get('parent')!='fable-plan-2-20261009':
        raise ValueError('native_ticket_binding_mismatch')
    if ticket['state'] not in ('complete','blocked'):
        return None
    from dot_lane import terminal
    result=terminal(directory,ticket)
    if result['outcome']!='result':
        raise ValueError('native_cloud_proof_blocked_no_old_key_revoke')
    value=parse_proof(result['text'],env,row['proof_nonce'])
    row.update(phase='CLOUD_PROVED',cloud_proof=value,native_reply_id=ticket['reply_id'])
    atomic(STATE,state)
    return value


def run(config, state_path=STATE, api=None):
    state=load(state_path,{'schema':1,'environments':{}})
    api=api or API()
    current=json.loads(json.dumps(config))
    for env in current['environments']:
        row=state['environments'].get(env['name'],{})
        if row.get('phase')=='OLD_REVOKED':
            env.update(key_id=row['key_id'],expires=row['expires'])
    minted=mint_due(current,state_path,api)
    state=load(state_path,{'schema':1,'environments':{}})
    outcomes=[]
    for env in current['environments']:
        row=state['environments'].get(env['name'])
        if not row or row.get('phase')=='OLD_REVOKED':
            outcomes.append({'name':env['name'],'status':'NOT_DUE'})
            continue
        try:
            if row['phase']=='MINTED':
                browser(env,'save')
                state=load(state_path,{});row=state['environments'][env['name']]
            if row['phase'] in ('VPN_SAVED','PUBLISH_INTENT','PUBLISH_FAILED'):
                browser(env,'publish')
                state=load(state_path,{});row=state['environments'][env['name']]
            if row['phase'] in ('PUBLISHED','PROOF_PENDING'):
                if not row.get('native_ticket'):
                    row['usage']=browser(env,'usage')['usage'];atomic(state_path,state)
                if proof(env,row,state) is None:
                    outcomes.append({'name':env['name'],'status':'PROOF_PENDING','ticket':row['native_ticket']})
                    continue
            if row['phase']=='CLOUD_PROVED':
                api.metadata(row['key_id'])
                row['phase']='REVOKE_INTENT';atomic(state_path,state)
                api.revoke(row['old_key_id'])
                row['phase']='OLD_REVOKED';atomic(state_path,state)
                private=Path(row['secret_path'])
                private.unlink(missing_ok=True)
            if row['phase']=='REVOKE_INTENT':
                raise ValueError('old_key_revoke_uncertain_inspect_exact_target_no_retry')
            if row['phase'] not in ('OLD_REVOKED','CLOUD_PROVED'):
                raise ValueError('unsupported_or_uncertain_phase_no_replay')
            outcomes.append({'name':env['name'],'status':row['phase'],'key_id':row['key_id'],'expires':row['expires']})
        except BaseException as error:
            failure={'name':env['name'],'status':'FAILED','phase':row.get('phase'),'error_class':type(error).__name__}
            import re
            reason=str(error)
            if re.fullmatch(r'[A-Za-z0-9_:-]{1,160}',reason) and not re.search(r'tskey|Bearer',reason,re.I): failure['reason']=reason
            outcomes.append(failure)
            alert_failure(failure)
    active={name:{key:row[key] for key in ('name','account','environment_id','key_id','expires','tags')} for name,row in state['environments'].items() if row.get('phase')=='OLD_REVOKED'}
    if active:
        atomic(ROOT/'state/codex-cloud-active-key-registry.json',{'schema':1,'environments':active})
    return {'status':'FAILED' if any(row['status']=='FAILED' for row in outcomes) else 'PASS_OR_PENDING','environments':outcomes,'acl_changed':False,'secret_printed':False}


def alert_failure(failure):
    import codex_cloud_key_renewal as notify
    path=ROOT/'state/codex-cloud-vpn-alerts.json';state=load(path,{})
    key=dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d')+':'+failure['name']+':'+str(failure['phase'])
    message='⚠️ **Codex VPN auto-renew** — '+failure['name']+' failed at '+str(failure['phase'])+' ('+failure['error_class']+'). Old key retained unless confirmed revoked. Inspect state/codex-cloud-vpn-renewal.json; no credential in this alert.'
    notify.notify(state,path,notify.Discord(),notify.OPS_ALERTS,key,message)


def reconcile_no_task(account):
    report=ROOT/'reports/ops/fable-plan-2-20261009/lanes/R1'/('CLOUD-PROOF-'+account+'-UI.txt')
    state=load(STATE,{})
    row=state['environments']['hermes-evidence-account'+account]
    body=report.read_text()
    if row['phase']!='PROOF_INTENT' or row.get('native_ticket') or row.get('proof_nonce') not in body or 'No task was started, no HTTP requests were run' not in body:
        raise ValueError('no_task_reconciliation_not_authenticated')
    import hashlib
    row.update(phase='PUBLISHED',setup_probe={'nonce':row.pop('proof_nonce'),'outcome':'setup_chat_has_no_new_task_tool','receipt':str(report),'sha256':hashlib.sha256(body.encode()).hexdigest()})
    atomic(STATE,state)
    return {'status':'PUBLISHED_NO_NATIVE_TASK_SENT','name':row['name']}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',type=Path,default=CONFIG)
    group=p.add_mutually_exclusive_group()
    group.add_argument('--mint',action='store_true')
    group.add_argument('--run',action='store_true')
    group.add_argument('--reconcile-setup-no-task',choices=['1','2'])
    a=p.parse_args()
    lock=STATE.with_suffix('.lock');lock.parent.mkdir(parents=True,exist_ok=True)
    with lock.open('a') as f:
        fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:
            if a.reconcile_setup_no_task:
                out=reconcile_no_task(a.reconcile_setup_no_task)
            else:
                config=json.loads(a.config.read_text())
                out=mint_due(config) if a.mint else run(config)
            print(json.dumps(out));return 1 if out.get('status')=='FAILED' else 0
        except BaseException as error:
            failure={'name':'renewal-job','status':'FAILED','phase':'preflight','error_class':type(error).__name__}
            try: alert_failure(failure)
            except BaseException: pass
            print(json.dumps({**failure,'secret_printed':False}));return 1
if __name__=='__main__':raise SystemExit(main())
