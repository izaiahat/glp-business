#!/usr/bin/env python3
"""Post-publication search notifications, never a second publisher.

Uses the main-release lock. Exact terminal deployment and live sitemap binding,
changed indexable HTML only, durable intent before each external write. Unknown
writes refuse replay. A notification receipt proves receipt, not indexing.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

ROOT = Path('/home/ubuntu/business')
SITE = 'https://glpclinicscout.com'
SITEMAP = SITE + '/sitemap.xml'
REPORTS = ROOT/'reports/glp-clinic-scout/seo-machine/search-notifications'
LEDGER = ROOT/'reports/glp-clinic-scout/seo-machine/indexnow-submission-ledger.jsonl'


INSTALLED_SOURCE = Path('/home/ubuntu/business/scripts/glp_release_search_notify.py')


def _under_test_harness():
    """True anywhere inside a pytest process: runtest, collection or import.

    PYTEST_CURRENT_TEST is only set while a test item runs, so it is not
    sufficient on its own; a loaded pytest/_pytest module or a pytest argv
    marks the whole process as a test harness.
    """
    if os.environ.get('PYTEST_CURRENT_TEST') or os.environ.get('PYTEST_VERSION'):
        return True
    return any(name == 'pytest' or name == '_pytest' or name.startswith('_pytest.') for name in tuple(sys.modules))


def require_installed_effect(*, source=None):
    """Refuse live IndexNow/GSC effects before any IO unless this process is
    the installed checkout's module and not a test harness."""
    if _under_test_harness():
        raise RuntimeError('live_search_effect_refused:pytest')
    here = Path(source or __file__).resolve()
    if here != INSTALLED_SOURCE.resolve():
        # Any worktree, CI clone, copied tree or other uninstalled checkout.
        raise RuntimeError('live_search_effect_refused:source_not_installed')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def durable_mkdir(path):
    """Create path and make every newly created directory entry durable."""
    path = Path(path)
    missing = []
    probe = path
    while not probe.exists():
        missing.append(probe)
        probe = probe.parent
    path.mkdir(parents=True, exist_ok=True)
    for created in reversed(missing):
        _fsync_dir(created.parent)
        _fsync_dir(created)


def save(path, value):
    """Crash-durable atomic write: file fsync, rename, then directory fsync.

    Callers rely on the final name surviving a power loss before any external
    request is made, so the containing directory entry is fsynced too.
    """
    path = Path(path)
    durable_mkdir(path.parent)
    raw=(json.dumps(value,sort_keys=True,indent=2)+'\n').encode()
    temp=path.with_name(path.name+'.tmp-'+str(os.getpid()))
    with temp.open('xb') as f:
        f.write(raw); f.flush(); os.fsync(f.fileno())
    os.replace(temp,path)
    _fsync_dir(path.parent)


def current_pointer():
    return json.loads((ROOT/'reports/glp-clinic-scout/seo-machine/current-content-base.json').read_text())


def get(url):
    with urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'GLPClinicScoutSearchNotify/1.0'}),timeout=60) as r:
        if r.status!=200: raise RuntimeError('search_live_readback_failed')
        return r.read()


def live_sitemap():
    return get(SITEMAP)


def post(endpoint,payload):
    require_installed_effect()
    req=urllib.request.Request(endpoint,data=json.dumps(payload).encode(),headers={'Content-Type':'application/json','User-Agent':'GLPClinicScoutSearchNotify/1.0'},method='POST')
    with urllib.request.urlopen(req,timeout=60) as r:
        return r.status,r.read()


def gsc_submit(xml):
    require_installed_effect()
    # Direct canonical SA only: no user-OAuth fallback, mailbox impersonation,
    # new credentials or quarantined historical submit helpers.
    sys.path.insert(0,str(ROOT/'systems/seo-blog-machine/scripts'))
    from gsc_principal import mint_google_token, mint_keyless_signed_jwt, mint_sa_keyfile
    minted=mint_google_token(['https://www.googleapis.com/auth/webmasters'],
                             minters=[mint_keyless_signed_jwt,mint_sa_keyfile])
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    service=build('searchconsole','v1',credentials=Credentials(token=minted['token']),cache_discovery=False)
    service.sitemaps().submit(siteUrl=SITE+'/',feedpath=SITEMAP).execute(num_retries=0)
    rows=service.sitemaps().list(siteUrl=SITE+'/').execute(num_retries=0).get('sitemap',[])
    match=[r for r in rows if r.get('path')==SITEMAP]
    if not match: raise RuntimeError('gsc_sitemap_readback_absent')
    return {'status':'API_EXECUTE_COMPLETED','provider_readback':match,'principal':minted['principal'],
            'credential_mode':minted['mode'],'deprecated_user_oauth':False}


def page_hashes(public):
    return {('/' if p.parent == public else '/'+p.relative_to(public).parent.as_posix()+'/'):sha(p)
            for p in public.rglob('index.html') if p.is_file() and not p.is_symlink()}


def changed_urls(old,new):
    before=page_hashes(old); after=page_hashes(new)
    sitemap=ET.fromstring((new/'sitemap.xml').read_bytes())
    allowed={x.text for x in sitemap.findall('{*}url/{*}loc')}
    robots=(new/'robots.txt').read_text()
    import urllib.robotparser
    policy=urllib.robotparser.RobotFileParser(); policy.parse(robots.splitlines())
    urls=[]
    for route,h in sorted(after.items()):
        route='/' if route=='//' else route
        url=SITE+route
        page=new/route.lstrip('/')/'index.html'
        if before.get(route)!=h and url in allowed and policy.can_fetch('*',url):
            text=page.read_text()
            if not re.search(r'<meta\b[^>]*name=[\"\']robots[\"\'][^>]*content=[\"\'][^\"\']*noindex',text,re.I):
                urls.append(url)
    return urls


def append_rows(ledger, rows):
    durable_mkdir(ledger.parent)
    created = not ledger.exists()
    with ledger.open('a') as f:
        for row in rows: f.write(json.dumps(row,sort_keys=True)+'\n')
        f.flush();os.fsync(f.fileno())
    if created:
        _fsync_dir(ledger.parent)


LIVE_POST, LIVE_GSC_SUBMIT = post, gsc_submit


def submit_deployment(pointer, predecessor, *, root=ROOT, report_root=REPORTS,
                      ledger=LEDGER, poster=post, gsc_submit=gsc_submit):
    if poster is LIVE_POST or gsc_submit is LIVE_GSC_SUBMIT:
        require_installed_effect()
    deployment=pointer.get('deployment_id','')
    if not re.fullmatch(r'[a-zA-Z0-9-]+',deployment): raise RuntimeError('deployment_id_invalid')
    final=pointer['final_receipt']; raw=Path(final['path']).read_bytes();terminal=json.loads(raw)
    if (hashlib.sha256(raw).hexdigest()!=final['sha256'] or terminal.get('status')!='PASS_PRODUCTION_TERMINAL'
        or terminal.get('deployment',{}).get('deployment_id')!=deployment
        or terminal.get('site_head')!=pointer.get('site_head')):
        raise RuntimeError('terminal_deployment_binding')
    if current_pointer()!=pointer: raise RuntimeError('search_pointer_drift')
    expected_predecessor = (pointer.get('supersedes') or {}).get('release_root')
    if not expected_predecessor or Path(predecessor).resolve(strict=True) != Path(expected_predecessor).resolve(strict=True):
        raise RuntimeError('predecessor_binding')
    release=Path(pointer['release_root']); public=release/'public'; xml=(public/'sitemap.xml').read_bytes()
    if xml!=live_sitemap(): raise RuntimeError('live_sitemap_deployment_drift')
    binding={'deployment_id':deployment,'site_head':pointer['site_head'],
             'terminal_receipt':final,'release_root':str(release),'predecessor':str(predecessor),
             'sitemap_sha256':hashlib.sha256(xml).hexdigest()}
    urls=changed_urls(Path(predecessor)/'public',public)
    work=report_root/deployment; final_path=work/'search-receipt.json'
    if final_path.exists():
        value=json.loads(final_path.read_text())
        if value['binding']!=binding or value['indexnow']['submitted_urls']!=urls:
            raise RuntimeError('search_receipt_binding_changed')
        for key in ('indexnow_receipt','gsc_receipt'):
            s=value[key]
            if sha(s['path'])!=s['sha256']:raise RuntimeError('search_child_receipt_drift')
        return value
    for kind in ('indexnow','gsc'):
        child=work/(kind+'.json'); intent=work/(kind+'-intent.json')
        if child.exists():
            existing=json.loads(child.read_text())
            if existing.get('binding')!=binding:raise RuntimeError('search_child_binding_changed')
            continue
        if intent.exists():raise RuntimeError('uncertain_'+kind+'_requires_exact_readback')
        # Intent is durable before request; never blind-retry unknown writes.
        save(intent,{'binding':binding,'urls':urls,'created_at_utc':datetime.now(timezone.utc).isoformat()})
        if kind=='indexnow':
            config=json.loads((release/'data/indexnow.json').read_text())
            loc=urllib.parse.urlsplit(config['key_location'])
            if loc.scheme!='https' or loc.netloc!='glpclinicscout.com' or loc.query or loc.fragment or '..' in loc.path.split('/') or loc.path!='/'+config['key']+'.txt' or config['host']!='glpclinicscout.com':
                raise RuntimeError('indexnow_root_key_scope_invalid')
            keyfile=public/loc.path.lstrip('/')
            if keyfile.read_text().strip()!=config['key']:raise RuntimeError('indexnow_local_key_mismatch')
            posts=[]
            for i in range(0,len(urls),10000):
                batch=urls[i:i+10000]
                status,body=poster('https://api.indexnow.org/indexnow',{'host':config['host'],'key':config['key'],'keyLocation':config['key_location'],'urlList':batch})
                if status not in (200,202):raise RuntimeError('indexnow_rejected:'+str(status))
                posts.append({'http_status':status,'urls':batch,'response_sha256':hashlib.sha256(body).hexdigest()})
            value={'binding':binding,'submitted_urls':urls,'posts':posts,'status':'RECEIVED_NOT_INDEXING_PROOF' if posts else 'NO_CHANGED_URLS'}
            save(child,value)
        else:
            value={'binding':binding,**gsc_submit(xml)};save(child,value)
    index_path=work/'indexnow.json'; gsc_path=work/'gsc.json';index=json.loads(index_path.read_text())
    # The terminal receipt and ledger are downstream of durable effect responses.
    rows=[{'schema':'glp-indexnow-ledger-row-v1','url':url,'release':str(release),
           'deployment_id':deployment,'site_head':pointer['site_head'],'run_id':deployment,
           'submitted_at_utc':datetime.now(timezone.utc).isoformat(),'http_status':p['http_status'],
           'receipt_path':str(index_path),'receipt_sha256':sha(index_path)} for p in index['posts'] for url in p['urls']]
    previous=[json.loads(x) for x in ledger.read_text().splitlines()] if ledger.exists() else []
    present={(r.get('deployment_id'),r.get('url'),r.get('receipt_sha256')) for r in previous}
    append_rows(ledger,[r for r in rows if (r['deployment_id'],r['url'],r['receipt_sha256']) not in present])
    value={'schema':'glp-release-search-receipt-v1','status':'PASS_SEARCH_SUBMITTED','deployment_id':deployment,
           'binding':binding,'indexnow':index,'gsc':json.loads(gsc_path.read_text()),
           'indexnow_receipt':{'path':str(index_path),'sha256':sha(index_path)},
           'gsc_receipt':{'path':str(gsc_path),'sha256':sha(gsc_path)},'ledger':str(ledger),
           'interpretation':'Submission received; crawling, indexing and ranking not proved.'}
    save(final_path,value);return value


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--deployment-id',required=True)
    parser.add_argument('--predecessor',type=Path,required=True)
    args=parser.parse_args(argv)
    from glp_release_main import lock_module
    with lock_module().acquire('main-site-release', owner='search-notifications', root=ROOT):
        pointer=current_pointer()
        if pointer['deployment_id']!=args.deployment_id:raise RuntimeError('not_current_deployment')
        print(json.dumps(submit_deployment(pointer,args.predecessor),sort_keys=True))
    return 0


if __name__=='__main__':raise SystemExit(main())
