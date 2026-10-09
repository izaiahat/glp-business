#!/usr/bin/env python3
"""Registered F04/F06 affiliate synchronization: sole-writer read -> source -> CI -> native release.

A singleton and durable publication intent prevent overlap or ambiguous retries.
All source changes go through an exact-head GitHub PR. No Sheet writes or tracker visits.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT=Path('/home/ubuntu/business')
REPO='izaiahat/glp-site-candidate'
CONFIG=ROOT/'state/affiliate-terms-runtime/config.json'


def read(p):return json.loads(Path(p).read_bytes())
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def save(p,v):
    from affiliate_terms_fanout import atomic,encoded
    atomic(Path(p),encoded(v))
def run(argv,*,cwd=ROOT,log=None):
    if log:
        with Path(log).open('wb') as f:
            p=subprocess.run(argv,cwd=cwd,stdout=f,stderr=subprocess.STDOUT)
        if p.returncode:raise RuntimeError('command_failed:'+str(log))
        return ''
    return subprocess.check_output(argv,cwd=cwd,text=True).strip()


def namespace(config,source,report):
    argv=list(config['base_argv']); ns=config['namespace']
    for i,t in enumerate(argv):
        if t=='--bind':
            destination=argv[i+2]
            if destination==ns:argv[i+1]=str(source)
            else:
                target=report/('bind-'+str(i))
                if not target.exists():shutil.copytree(argv[i+1],target,symlinks=True)
                argv[i+1]=str(target)
    return argv


def ci(pr,report):
    # PR creation and Actions registration are asynchronous. Poll read-only
    # metadata until checks exist; never mistake that short gap for CI failure.
    for attempt in range(60):
        registered=json.loads(run(['gh','pr','view',pr,'--repo',REPO,'--json','headRefOid,statusCheckRollup']))
        if registered.get('statusCheckRollup'):break
        if attempt==59:raise RuntimeError('ci_registration_timeout')
        time.sleep(2)
    # gh's watch is read-only; an exit/timeout never means merge.
    run(['gh','pr','checks',pr,'--repo',REPO,'--watch','--interval','10'],log=report/'ci-watch.log')
    evidence=json.loads(run(['gh','pr','view',pr,'--repo',REPO,'--json','url,state,headRefOid,mergeCommit,statusCheckRollup']))
    checks=evidence.get('statusCheckRollup') or []
    if not checks or any(c.get('status')!='COMPLETED' or c.get('conclusion')!='SUCCESS' for c in checks):raise RuntimeError('exact_ci_nonpass')
    save(report/'CI.json',evidence)
    return evidence


def build_gate(source,report,config,head):
    from glp_affiliate_release import pin,SCHEMA
    from glp_deploy_glp34_authority_v1 import package_tree
    argv=namespace(config,source,report)
    run(argv+['npm','run','build'],log=report/'build.log')
    tree,files=package_tree(source/'public')
    save(report/'BUILD.json',{'head':head,'exit_code':0,'files':files})
    run(argv+['node','--test','tests/affiliate-relationship-sync.test.mjs'],log=report/'design-test.log')
    # Independent raw-source oracle plus effective policy denials, scanning ALL
    # HTML and nested lazy-card JSON, not just a review-page sample.
    code="""import{readFileSync,readdirSync,writeFileSync}from'node:fs';import{join,relative}from'node:path';
import{rawHeldInputs,rawHeldOutputCensus,assertRawHeldOutput}from'./src/served-affiliate-identity.mjs';
import{loadGoPromotionAuthority}from'./src/go-runtime-selection.mjs';import{GO_RUNTIME_MANIFEST}from'./functions/go/program-manifest.js';
const inputs=rawHeldInputs(process.cwd()),authority=loadGoPromotionAuthority(process.cwd());
for(const[id,p]of Object.entries(authority.programs))if(p.status==='WITHHELD'&&!inputs.held.some(r=>r.id===id))inputs.held.push({id,sources:['effective current authority']});
const docs=[];function walk(p){for(const d of readdirSync(p,{withFileTypes:true})){const f=join(p,d.name);if(d.isDirectory())walk(f);else if(/\\.(html|json)$/.test(f))docs.push({file:relative('public',f),text:readFileSync(f,'utf8')})}}walk('public');
const census=assertRawHeldOutput(rawHeldOutputCensus(docs,inputs,GO_RUNTIME_MANIFEST.programs));
writeFileSync('artifacts/ops/affiliate-sync-census.json',JSON.stringify(census));"""
    run(argv+['node','--input-type=module','-e',code],log=report/'hold-census.log')
    census=read(source/'artifacts/ops/affiliate-sync-census.json')
    from glp_affiliate_absence_audit import audit
    absence = audit(source, read(report/'registry/registry-sync.json'), read(report/'registry/sheet-snapshot.json'))
    save(report/'ADVERSE-ABSENCE.json', absence)
    design={'head':head,'exit_code':0,'checks':['affiliate-relationship-sync','raw-held-output','native-build-validators'],'raw_held':census,
            'unmapped_adverse_evidence':{'path':str(report/'ADVERSE-ABSENCE.json'),'sha256':sha(report/'ADVERSE-ABSENCE.json')}}
    save(report/'DESIGN.json',design)
    if package_tree(source/'public')!=(tree,files):raise RuntimeError('build_output_changed_during_checks')
    names=run(['git','ls-files','-z'],cwd=source).rstrip('\0').split('\0')
    source_rows=[[p,sha(source/p)] for p in names]
    diff=subprocess.check_output(['git','diff','--no-ext-diff','--binary'],cwd=source)
    changed=run(['git','diff','--name-only'],cwd=source).splitlines()
    gate={'schema':SCHEMA,'status':'PASS','scope':'operator-approved-affiliate-sync','head':head,'release_authorized':False,
      'evidence':{'build':pin(report/'BUILD.json'),'ci':pin(report/'CI.json'),'design':pin(report/'DESIGN.json')},
      'output_before':{'sha256':tree,'files':files},'output_after':{'sha256':tree,'files':files},
      'built_source_sha256':hashlib.sha256(json.dumps(source_rows,ensure_ascii=False,separators=(',',':')).encode()).hexdigest(),
      'generated_diff_sha256':hashlib.sha256(diff).hexdigest(),'generated_diff_paths':changed,
      'authority_files':[{'file':'artifacts/ops/build-report.json','sha256':sha(source/'artifacts/ops/build-report.json')}],
      'steps':[{'id':'affiliate-census','status':'PASS','evidence':{'raw_held':census}}]}
    save(report/'GATE.json',gate)


def execute(config,report,*,source_ref=None,source_pr=None,publish=False):
    from glp_affiliate_registry_sync import sync
    from glp_affiliate_release import package_and_publish
    source=report/'candidate'
    run(['git','clone','--quiet','--single-branch','git@github.com:'+REPO+'.git',str(source)])
    if source_ref:
        run(['git','fetch','origin',source_ref],cwd=source,log=report/'resume-fetch.log')
        run(['git','checkout','--detach',source_ref],cwd=source)
    outcome=sync(source,report/'registry',True)
    prior=read(ROOT/'reports/glp-clinic-scout/seo-machine/current-content-base.json')
    prior_root=Path(prior['release_root'])
    surfaces=['data/program-registry.json','src/affiliate-relationship-sync.mjs','src/go-runtime-selection.mjs','scripts/build-staging.mjs','functions/go/program-manifest.js']
    if outcome['changed']==0 and all((prior_root/p).is_file() and sha(prior_root/p)==sha(source/p) for p in surfaces):
        from glp_affiliate_absence_audit import audit
        absence=audit(source,outcome,read(report/'registry/sheet-snapshot.json'),public=prior_root/'public')
        save(report/'ADVERSE-ABSENCE.json',absence)
        result={'status':'PASS_ALREADY_CURRENT','registry_changed':0,'deployment_id':prior['deployment_id'],'run_type':os.environ.get('GLP_RUN_TYPE','manual'),
                'unmapped_adverse':absence['remaining_unmapped'],'evidenced_no_site_presence':absence['evidenced_no_live_offer_presence'],'unaccounted':absence['unaccounted']}
        save(report/'RESULT.json',result);return result
    if outcome['changed']:
        argv=namespace(config,source,report)
        run(argv+['node','scripts/regenerate-go-runtime-manifest.mjs','--current-holds-only'],log=report/'regenerate.log')
        changed=run(['git','diff','--name-only'],cwd=source).splitlines()
        if not set(changed)<= {'data/program-registry.json','functions/go/program-manifest.js'}:raise RuntimeError('sync_source_scope_expanded')
        branch='automation/affiliate-sync-'+sha(source/'data/program-registry.json')[:16]
        run(['git','checkout','-b',branch],cwd=source)
        run(['git','add',*changed],cwd=source)
        run(['git','commit','-m','Sync authenticated affiliate terms and deny adverse selected relationships'],cwd=source,log=report/'commit.log')
        # Deterministic branch identity makes an uncertain push inspectable.
        save(report/'source-intent.json',{'branch':branch,'head':run(['git','rev-parse','HEAD'],cwd=source)})
        run(['git','push','-u','origin',branch],cwd=source,log=report/'push.log')
        source_pr=run(['gh','pr','create','--repo',REPO,'--base','main','--head',branch,'--title','Automatic F03 affiliate terms and relationship sync','--body','Exact sole-writer readback; denial-only relationship changes and authenticated-capture terms. No activation or tracker visits. Operator-approved F04/F06 propagation.'])
        save(report/'PR.json',{'url':source_pr})
    if not source_pr:raise RuntimeError('merged_exact_head_pr_required')
    evidence=ci(source_pr,report)
    head=run(['git','rev-parse','HEAD'],cwd=source)
    if evidence['headRefOid']!=head:raise RuntimeError('source_pr_head_mismatch')
    if evidence['state']!='MERGED':
        run(['gh','pr','merge',source_pr,'--repo',REPO,'--merge','--match-head-commit',head],log=report/'merge.log')
    merged=json.loads(run(['gh','pr','view',source_pr,'--repo',REPO,'--json','state,mergeCommit,headRefOid']))
    if merged['state']!='MERGED' or merged['headRefOid']!=head:raise RuntimeError('merge_readback_failed')
    run(['git','fetch','origin','main'],cwd=source,log=report/'fetch.log')
    if run(['git','rev-parse','origin/main^{tree}'],cwd=source)!=run(['git','rev-parse','HEAD^{tree}'],cwd=source):raise RuntimeError('main_advanced_rebase_required')
    save(report/'MERGE.json',merged)
    save(report/'CI.json',{**evidence,**merged})
    lock=[sys.executable,'-B',str(ROOT/'scripts/candidate_build_lock.py')]
    state=json.loads(run(lock+['status']))
    if state['status']!='FREE':raise RuntimeError('build_slot_busy:'+state.get('holder','unknown'))
    seat='affiliate-sync-'+head[:12]
    run(lock+['acquire','--seat',seat,'--reason','F04/F06 source-bound affiliate sync native build and release'])
    try:
        build_gate(source,report,config,head)
        result=package_and_publish(source,report,seat,publish=publish)
        result.update(run_type=os.environ.get('GLP_RUN_TYPE','manual'),source_pr=source_pr)
        save(report/'RESULT.json',result)
        return result
    finally:run(lock+['release','--seat',seat])


def provision(config_path):
    """Retain authentic build inputs outside the 72-hour scratch pruning tree."""
    config=read(config_path);target=CONFIG.parent
    target.mkdir(parents=True,exist_ok=True)
    if CONFIG.exists():raise ValueError('runtime_config_exists_inspect_before_reprovision')
    argv=list(config['base_argv']);bindings=[]
    for i,t in enumerate(argv):
        if t not in ('--ro-bind','--bind'):continue
        source=Path(argv[i+1])
        if argv[i+2]==config['namespace']:continue
        if not str(source).startswith('/home/ubuntu/.hermes/cache/scratch/'):
            continue
        destination=target/'inputs'/str(i)
        destination.parent.mkdir(parents=True,exist_ok=True)
        if source.is_dir():shutil.copytree(source,destination,symlinks=True)
        else:shutil.copy2(source,destination)
        argv[i+1]=str(destination)
        bindings.append({'source':str(source),'retained':str(destination)})
    config['base_argv']=argv;config['provisioned_from']={'path':str(config_path),'sha256':sha(config_path)}
    config['retained_bindings']=bindings
    save(CONFIG,config)
    return {'status':'PROVISIONED','config':str(CONFIG),'bindings':len(bindings)}


def resume_prepared(report, config, publish):
    """Manual registered continuation after a no-publication preparation run."""
    from glp_affiliate_registry_sync import sync
    from glp_affiliate_release import package_and_publish
    source=report/'candidate';gate=read(report/'GATE.json')
    if (report/'publish-intent.json').exists():
        raise ValueError('publication_intent_exists_inspect_exact_native_target')
    fresh=report/'manual-installed-readback'
    sync_result=sync(source,fresh,apply=False)
    if sync_result['changed']:raise ValueError('current_sheet_changed_rebuild_required')
    original=read(report/'CI.json')
    ci(original['url'],report)
    lock=[sys.executable,'-B',str(ROOT/'scripts/candidate_build_lock.py')]
    seat='affiliate-sync-'+gate['head'][:12]
    run(lock+['acquire','--seat',seat,'--reason','Installed manual F04/F06 continuation of verified prepared bytes'])
    try:
        result=package_and_publish(source,report,seat,publish=publish)
        save(report/'MANUAL-INSTALLED-RESULT.json',{**result,'run_type':'manual_registered_entry',
             'fresh_sheet_diff':len(sync_result['changes']),'source_pr':original['url']})
        return result
    finally:run(lock+['release','--seat',seat])


# Run-dir markers that bind a clone to source/publication custody; never retired.
CUSTODY_MARKERS=('source-intent.json','PR.json','GATE.json','publish-intent.json','PUBLISH.json',
                 'MERGE.json','CI.json','prepared','PREPARED.json','manual-installed-readback','MANUAL-INSTALLED-RESULT.json')
DISPOSABLE_KEEP=3


def retire_disposable_clones(root,*,keep=DISPOSABLE_KEEP,pins=(),active=None):
    """Bound hourly regrowth: each run clones the ~0.5 GiB site repo into <run>/candidate.

    No-change (PASS_ALREADY_CURRENT) and FAIL runs keep their small result/failure JSON
    and logs; only the re-clonable candidate checkout is removed, and only when it is
    clean, has no unpushed commits, carries no custody marker, is not pinned (e.g. the
    current content base release_root) and is not among the newest `keep` runs.
    """
    root=Path(root);pins=tuple(Path(p) for p in pins);rows=[]
    runs=sorted(p for p in root.iterdir() if p.is_dir() and not p.is_symlink() and len(p.name)==16 and p.name.endswith('Z'))
    for run_dir in runs[:-keep] if keep else runs:
        clone=run_dir/'candidate';row={'run':run_dir.name,'status':'KEPT'}
        if not clone.is_dir() or clone.is_symlink():continue
        try:
            if any((run_dir/m).exists() for m in CUSTODY_MARKERS):row['reason']='custody_marker'
            elif any(p==run_dir or run_dir in p.parents for p in pins):row['reason']='pinned'
            elif active is not None and active(clone):row['reason']='active_reference'
            elif not any((run_dir/f).is_file() for f in ('RESULT.json','FAILURE.json')):row['reason']='no_terminal_record'
            elif (run_dir/'RESULT.json').is_file() and read(run_dir/'RESULT.json').get('status')!='PASS_ALREADY_CURRENT':row['reason']='non_noop_result'
            else:
                git=['git','--no-optional-locks','-c','safe.directory='+str(clone),'-c','core.fsmonitor=false','-C',str(clone)]
                status=subprocess.run(git+['status','--porcelain','--untracked-files=normal'],capture_output=True,text=True,timeout=60)
                local=subprocess.run(git+['log','--branches','--not','--remotes','--format=%H'],capture_output=True,text=True,timeout=60)
                broken=(clone/'.git/objects.ARCHIVED.json').is_file()  # objects already archived by a verified reclaim
                if (status.returncode or status.stdout.strip() or local.returncode or local.stdout.strip()) and not broken:
                    row['reason']='dirty_or_unpushed'
                else:
                    # Keep a locator: re-clone HEAD plus any verified-archive pointers.
                    head=(clone/'.git/HEAD').read_text().strip() if (clone/'.git/HEAD').is_file() else None
                    refs=(clone/'.git/packed-refs').read_text() if (clone/'.git/packed-refs').is_file() else None
                    locators={str(q.relative_to(clone)):read(q) for q in clone.rglob('*.ARCHIVED.json') if q.is_file() and not q.is_symlink()}
                    save(run_dir/'candidate.RETIRED.json',{'repository':REPO,'head_ref':head,
                         'head':subprocess.run(git+['rev-parse','HEAD'],capture_output=True,text=True).stdout.strip() or None,
                         'packed_refs':refs,'archived_locators':locators,'reason':'reclonable_noop_or_failed_clone'})
                    du=subprocess.run(['du','-sxB1',str(clone)],capture_output=True,text=True,timeout=120)
                    shutil.rmtree(clone)
                    row.update(status='RETIRED',reason='reclonable_noop_or_failed_clone',
                               bytes_freed=int(du.stdout.split()[0]) if du.returncode==0 else 0)
        except (OSError,ValueError,subprocess.SubprocessError) as e:row.update(status='ERROR_KEPT',reason=str(e))
        rows.append(row)
    return rows


def superseded(head,*,gh=None):
    """A pending source head already contained in the published pointer head is done.

    Never rebuild (and so never re-publish) an ancestor of what production serves;
    the main-release job may have shipped it inside a later merge.
    """
    try:current=read(ROOT/'reports/glp-clinic-scout/seo-machine/current-content-base.json').get('site_head')
    except (OSError,ValueError):return False
    if not current or current==head:return current==head
    gh=gh or (lambda path:json.loads(run(['gh','api',path])))
    try:status=gh(f'repos/{REPO}/compare/{head}...{current}').get('status')
    except Exception:return False
    return status in ('ahead','identical')


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--config',type=Path,default=CONFIG)
    p.add_argument('--resume-report',type=Path)
    p.add_argument('--provision-from',type=Path)
    p.add_argument('--output',type=Path);p.add_argument('--source-ref');p.add_argument('--source-pr');p.add_argument('--publish',action='store_true')
    a=p.parse_args()
    if a.provision_from:print(json.dumps(provision(a.provision_from)));return 0
    config=read(a.config)
    root=ROOT/'reports/glp-clinic-scout/affiliate-machine/registry-site-sync';root.mkdir(parents=True,exist_ok=True)
    with (root/'run.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:print(json.dumps({'status':'DEFERRED_BUSY'}));return 0
        # An uncertain prior publication blocks any later effect until exact native readback.
        for intent in root.glob('*/publish-intent.json'):
            if not (intent.parent/'PUBLISH.json').exists():raise RuntimeError('prior_publication_uncertain:'+str(intent))
        if not a.source_ref and not a.source_pr and not a.resume_report:
            completed={read(p).get('site_head') for p in root.glob('*/PUBLISH.json')
                       if read(p).get('status')=='PASS_PRODUCTION_TERMINAL'}
            pending=[]
            for intent in sorted(root.glob('*/source-intent.json')):
                data=read(intent);pr_path=intent.parent/'PR.json'
                if data['head'] not in completed and pr_path.is_file() and not superseded(data['head']):
                    pending.append((data['head'],read(pr_path)['url']))
            if pending:a.source_ref,a.source_pr=pending[-1]
        if a.resume_report:
            print(json.dumps(resume_prepared(a.resume_report.resolve(),config,a.publish)));return 0
        report=a.output or root/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        report.mkdir(parents=True,exist_ok=False)
        try:result=execute(config,report,source_ref=a.source_ref,source_pr=a.source_pr,publish=a.publish)
        except Exception as e:
            save(report/'FAILURE.json',{'status':'FAIL','error':str(e),'run_type':os.environ.get('GLP_RUN_TYPE','manual')});raise
        finally:
            try:
                base=read(ROOT/'reports/glp-clinic-scout/seo-machine/current-content-base.json')
                retire_disposable_clones(root,pins=[Path(base['release_root'])])
            except Exception as e:print(json.dumps({'clone_retention':'SKIPPED','reason':str(e)}),file=sys.stderr)
        print(json.dumps(result));return 0

if __name__=='__main__':raise SystemExit(main())
