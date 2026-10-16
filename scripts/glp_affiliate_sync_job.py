#!/usr/bin/env python3
"""F03 source-only affiliate synchronization: read -> source PR -> exact-head CI -> merge.

Hourly glp_release_main.py is the production publisher and explicitly uses the
shared gated release library. This command never builds or publishes, including
when an old installed cron still supplies --publish. No Sheet writes or tracker visits.
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
    from glp_release_inputs import path_digest
    argv=list(config['base_argv']); ns=config['namespace']
    for i,t in enumerate(argv):
        if t=='--bind':
            destination=argv[i+2]
            if destination==ns:argv[i+1]=str(source)
            else:
                target=report/('bind-'+str(i))
                # Non-source writable mounts are inputs, not an ungoverned
                # cache. Never silently reuse a dirty/preexisting build seed.
                if target.exists() and path_digest(target)!=path_digest(argv[i+1]):
                    raise ValueError('effective_writable_bind_preexisting_drift:'+destination)
                if not target.exists():shutil.copytree(argv[i+1],target,symlinks=True)
                argv[i+1]=str(target)
    # Runtime-only mapping shared with input capture; never persist host paths
    # as the cross-run identity, and never mutate the declared recipe.
    config['_effective_base_argv']=list(argv)
    return argv


def ci(pr,report,receipt_name='CI.json'):
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
    save(report/receipt_name,evidence)
    return evidence


BUILD_AUTHORITY_FILES = (
    'artifacts/ops/build-report.json',
    'artifacts/ops/price-index-manifest.json',
    'artifacts/ops/wave4-deal-ingestion-manifest.json',
)


def rebuild_inputs_complete(root):
    return all((Path(root) / name).is_file() and not (Path(root) / name).is_symlink()
               for name in BUILD_AUTHORITY_FILES)


def build_authority_files(source):
    if not rebuild_inputs_complete(source):
        raise RuntimeError('native_build_rebuild_inputs_missing')
    return [{'file': name, 'sha256': sha(source / name)} for name in BUILD_AUTHORITY_FILES]


def equivalent_main_rebuild_inputs(base):
    """Reuse only native private receipts from an exact source/public/worker rebuild."""
    from glp_deploy_glp34_authority_v1 import file_closure
    from glp_affiliate_release import validate_build_evidence
    base = Path(base)
    try:
        original = read(base.parent / 'PREPARED.json')
    except (OSError, ValueError):
        return {}
    if original.get('schema') != 'glp43-current-prepared-v1':
        return {}
    terminal_path = ROOT / 'reports/glp-clinic-scout/seo-machine' / ('current-release-' + original['site_head']) / 'CURRENT-RECEIPT.json'
    try:
        terminal = read(terminal_path)
        if (terminal.get('status') != 'PASS_PRODUCTION_TERMINAL'
                or terminal['binding']['prepared_manifest_sha256'] != sha(base.parent / 'PREPARED.json')
                or file_closure(base) != original['release']):
            return {}
    except (OSError, ValueError, KeyError):
        return {}
    for report in sorted(base.parents[2].glob('main-*'), reverse=True):
        try:
            intent = read(report / 'publish-intent.json')
            prepared_path = report / 'prepared/PREPARED.json'
            if intent['base']['release_root'] != str(base) or intent['prepared'] != {'path': str(prepared_path), 'sha256': sha(prepared_path)}:
                continue
            rebuilt = read(prepared_path)
            root = prepared_path.parent / 'release'
            if any(rebuilt[k] != original[k] for k in ('site_head', 'public_tree_sha256')):
                continue
            if rebuilt['compiled']['worker_sha256'] != original['compiled']['worker_sha256']:
                continue
            if rebuilt['deploy_directory']['tree_sha256'] != original['deploy_directory']['tree_sha256']:
                continue
            if rebuilt['source']['tracked_files'] != original['source']['tracked_files']:
                continue
            if file_closure(root) != rebuilt['release']:
                continue
            gate_path = prepared_path.parent / 'gate.json'
            if sha(gate_path) != rebuilt['build_receipt']['sha256']:
                continue
            package = read(prepared_path.parent / 'PACKAGE-MANIFEST.json')
            if sha(prepared_path.parent / 'PACKAGE-MANIFEST.json') != rebuilt['package_manifest_sha256']:
                continue
            validate_build_evidence(read(gate_path), package['files'])
            rows = {r['file']: r['sha256'] for r in rebuilt['source']['authority_files']}
            names = BUILD_AUTHORITY_FILES[1:]
            if not all(rows.get(n) == sha(root / n) for n in names):
                continue
            return {n: {'path': str(root / n), 'sha256': rows[n], 'prepared': str(prepared_path),
                        'prepared_sha256': sha(prepared_path)} for n in names}
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return {}


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
      'authority_files':build_authority_files(source),
      'steps':[{'id':'affiliate-census','status':'PASS','evidence':{'raw_held':census}}]}
    save(report/'GATE.json',gate)


def prepare_sync_source(source, report, changed):
    """Bind a source branch to its parent AND complete intended tree.

    A registry-only key collides when an accepted article advances main. On an
    uncertain prior push, authenticate the remote tree/parent and reuse that
    exact commit; never force-push or mint a timestamp-dependent replacement.
    """
    base = run(['git','rev-parse','HEAD'],cwd=source)
    run(['git','add',*changed],cwd=source)
    tree = run(['git','write-tree'],cwd=source)
    branch = 'automation/affiliate-sync-' + hashlib.sha256((base+'\n'+tree).encode()).hexdigest()[:24]
    ref = 'refs/heads/' + branch
    remote = run(['git','ls-remote','--heads','origin',ref],cwd=source)
    if remote:
        rows = [line.split() for line in remote.splitlines()]
        if len(rows)!=1 or len(rows[0])!=2 or rows[0][1]!=ref:
            raise RuntimeError('sync_remote_source_identity_ambiguous')
        head = rows[0][0]
        run(['git','fetch','origin',head],cwd=source,log=report/'source-resume-fetch.log')
        if (run(['git','rev-parse','FETCH_HEAD^{tree}'],cwd=source)!=tree
                or run(['git','show','-s','--format=%P','FETCH_HEAD'],cwd=source)!=base):
            raise RuntimeError('sync_remote_source_identity_drift')
        run(['git','checkout','--detach',head],cwd=source)
        save(report/'source-intent.json',{'branch':branch,'head':head,'base':base,'tree':tree,'reused_remote':True})
    else:
        run(['git','checkout','-b',branch],cwd=source)
        run(['git','commit','-m','Sync authenticated affiliate terms and deny adverse selected relationships'],cwd=source,log=report/'commit.log')
        save(report/'source-intent.json',{'branch':branch,'head':run(['git','rev-parse','HEAD'],cwd=source),'base':base,'tree':tree})
        run(['git','push','-u','origin',branch],cwd=source,log=report/'push.log')
    return branch


def execute(config,report,*,source_ref=None,source_pr=None,publish=False,source_only=True):
    """Default to source production; hourly main must explicitly own publication."""
    from glp_affiliate_registry_sync import sync
    # Resume receipts already bind a PR to an exact head. Reuse the existing
    # read-only CI admission before allocating another clone for that head.
    if source_ref and source_pr:
        # Admission is preliminary: a later fetch/sync failure must not add a
        # retention custody marker. CI.json remains authoritative post-sync.
        resumed=ci(source_pr,report,'CI-admission.non-custody.json')
        if resumed['headRefOid']!=source_ref:raise RuntimeError('source_pr_head_mismatch')
    # Keep the post-sync CI/head check below: sync may produce a different PR.
    source=report/'candidate'
    run(['git','clone','--quiet','--single-branch','git@github.com:'+REPO+'.git',str(source)])
    if source_ref:
        run(['git','fetch','origin',source_ref],cwd=source,log=report/'resume-fetch.log')
        run(['git','checkout','--detach',source_ref],cwd=source)
    outcome=sync(source,report/'registry',True)
    if source_only and not source_ref and not source_pr and outcome['changed']==0:
        result={'status':'PASS_SOURCE_NO_CHANGE','registry_changed':0,'deployed':False,
                'production_publisher':'glp_release_main.py','run_type':os.environ.get('GLP_RUN_TYPE','manual')}
        save(report/'RESULT.json',result);return result
    prior={} if source_only else read(ROOT/'reports/glp-clinic-scout/seo-machine/current-content-base.json')
    prior_root=Path(prior.get('release_root',report))
    surfaces=['data/program-registry.json','src/affiliate-relationship-sync.mjs','src/go-runtime-selection.mjs','scripts/build-staging.mjs','functions/go/program-manifest.js']
    # The registry no-change shortcut describes only affiliate surfaces. An
    # explicit source PR/ref can add accepted guides or other site source while
    # those surfaces are identical; it must traverse exact-head CI/build/release.
    # glp_release_main.py owns the complete-source already-current check.
    if not source_only and not source_ref and not source_pr and outcome['changed']==0 and rebuild_inputs_complete(prior_root) and all((prior_root/p).is_file() and sha(prior_root/p)==sha(source/p) for p in surfaces):
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
        branch=prepare_sync_source(source,report,changed)
        prs=json.loads(run(['gh','pr','list','--repo',REPO,'--head',branch,'--state','all','--json','url,state,headRefOid']))
        if len(prs)>1 or (prs and prs[0]['state']=='CLOSED'):
            raise RuntimeError('sync_source_pr_ambiguous_or_closed')
        if prs and prs[0]['headRefOid']!=run(['git','rev-parse','HEAD'],cwd=source):
            raise RuntimeError('sync_source_pr_head_drift')
        source_pr=prs[0]['url'] if prs else run(['gh','pr','create','--repo',REPO,'--base','main','--head',branch,'--title','Automatic F03 affiliate terms and relationship sync','--body','Exact sole-writer readback; denial-only relationship changes and authenticated-capture terms. No activation or tracker visits. Operator-approved F04/F06 propagation.'])
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
    if source_only:
        result={'status':'PASS_SOURCE_MERGED','source_pr':source_pr,'source_head':head,
                'merge_head':merged['mergeCommit']['oid'],'registry_changed':outcome['changed'],
                'deployed':False,'production_publisher':'glp_release_main.py',
                'run_type':os.environ.get('GLP_RUN_TYPE','manual')}
        save(report/'RESULT.json',result);return result
    from glp_affiliate_release import package_and_publish
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

    Failed, orphaned and no-change runs keep their small records and logs.
    Only clean/pushed clones or no-PR generated-sync-only tracked diffs are
    disposable; the latter are saved as a hash-bound candidate.DIFF.patch.
    Custody/publication markers, release_root pins and newest `keep` runs stay.
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
            elif (run_dir/'RESULT.json').is_file() and read(run_dir/'RESULT.json').get('status') not in {'PASS_ALREADY_CURRENT','PASS_SOURCE_NO_CHANGE','FAIL'}:row['reason']='non_noop_result'
            else:
                git=['git','--no-optional-locks','-c','safe.directory='+str(clone),'-c','core.fsmonitor=false','-C',str(clone)]
                status=subprocess.run(git+['status','--porcelain','--untracked-files=normal'],capture_output=True,text=True,timeout=60)
                local=subprocess.run(git+['log','HEAD','--branches','--not','--remotes','--format=%H'],capture_output=True,text=True,timeout=60)
                untracked=subprocess.run(git+['ls-files','--others','--exclude-standard','-z'],capture_output=True,timeout=60)
                names=subprocess.run(git+['diff','--no-renames','--name-only','-z','HEAD'],capture_output=True,text=True,timeout=60)
                generated={p for p in names.stdout.split('\0') if p}
                allowed={'data/program-registry.json','functions/go/program-manifest.js'}
                if (status.returncode or local.returncode or local.stdout.strip() or untracked.returncode or untracked.stdout
                        or names.returncode or (status.stdout.strip() and (not generated or not generated<=allowed))):
                    row['reason']='dirty_or_unpushed'
                else:
                    diff_argv=git+['diff','--no-ext-diff','--no-textconv','--binary','HEAD']
                    diff=subprocess.check_output(diff_argv,timeout=60)
                    patch=None
                    if diff:
                        path=run_dir/'candidate.DIFF.patch'
                        if path.is_symlink():raise ValueError('aliased_candidate_diff')
                        if path.exists():
                            if path.read_bytes()!=diff:raise ValueError('candidate_diff_conflict')
                        else:
                            with path.open('xb') as stream:
                                stream.write(diff);stream.flush();os.fsync(stream.fileno())
                        if path.read_bytes()!=diff:raise ValueError('candidate_diff_readback_failed')
                        patch={'path':str(path),'sha256':hashlib.sha256(diff).hexdigest(),'paths':sorted(generated)}
                    # Keep a locator: re-clone HEAD plus any verified-archive pointers.
                    head=(clone/'.git/HEAD').read_text().strip() if (clone/'.git/HEAD').is_file() else None
                    refs=(clone/'.git/packed-refs').read_text() if (clone/'.git/packed-refs').is_file() else None
                    locators={str(q.relative_to(clone)):read(q) for q in clone.rglob('*.ARCHIVED.json') if q.is_file() and not q.is_symlink()}
                    commit=subprocess.check_output(git+['rev-parse','HEAD'],text=True,timeout=60).strip()
                    save(run_dir/'candidate.RETIRED.json',{'repository':REPO,'head_ref':head,
                         'head':commit,
                         'packed_refs':refs,'archived_locators':locators,'generated_diff':patch,'reason':'reclonable_unpinned_sync_clone'})
                    du=subprocess.run(['du','-sxB1',str(clone)],capture_output=True,text=True,timeout=120)
                    # The patch/locator must precede unlink; changed bytes, refs,
                    # new custody or an active owner veto this candidate.
                    if (subprocess.check_output(diff_argv,timeout=60)!=diff
                            or subprocess.check_output(git+['status','--porcelain','--untracked-files=normal'],text=True,timeout=60)!=status.stdout
                            or subprocess.check_output(git+['rev-parse','HEAD'],text=True,timeout=60).strip()!=commit
                            or (patch is not None and (run_dir/'candidate.DIFF.patch').read_bytes()!=diff)
                            or subprocess.check_output(git+['log','HEAD','--branches','--not','--remotes','--format=%H'],timeout=60).strip()
                            or any((run_dir/m).exists() for m in CUSTODY_MARKERS)
                            or (active is not None and active(clone))):raise ValueError('clone_changed_before_retirement')
                    shutil.rmtree(clone)
                    row.update(status='RETIRED',reason='reclonable_unpinned_sync_clone',
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
    if a.resume_report:raise ValueError('hourly_main_release_only: use glp_release_main.py')
    if a.provision_from:print(json.dumps(provision(a.provision_from)));return 0
    config=read(a.config)
    root=ROOT/'reports/glp-clinic-scout/affiliate-machine/registry-site-sync';root.mkdir(parents=True,exist_ok=True)
    with (root/'run.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:print(json.dumps({'status':'DEFERRED_BUSY'}));return 0
        # An uncertain prior publication blocks any later effect until exact native readback.
        # Main-release refusals can leave a wrapper intent without any new effect.
        # Reuse its exact terminal/prepared reconciliation; all unknowns still block.
        from glp_release_main import main_pre_effect_refusal
        for intent in root.glob('*/publish-intent.json'):
            if not (intent.parent/'PUBLISH.json').exists() and not main_pre_effect_refusal(intent.parent):
                raise RuntimeError('prior_publication_uncertain:'+str(intent))
        if not a.source_ref and not a.source_pr and not a.resume_report:
            completed={read(p).get('site_head') for p in root.glob('*/PUBLISH.json')
                       if read(p).get('status')=='PASS_PRODUCTION_TERMINAL'}
            pending=[]
            for intent in sorted(root.glob('*/source-intent.json')):
                data=read(intent);pr_path=intent.parent/'PR.json'
                if data['head'] not in completed and not (intent.parent/'MERGE.json').is_file() and pr_path.is_file() and not superseded(data['head']):
                    pending.append((data['head'],read(pr_path)['url']))
            if pending:a.source_ref,a.source_pr=pending[-1]
        # F03 never resumes prepared releases; hourly main owns that effect.
        report=a.output or root/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        report.mkdir(parents=True,exist_ok=False)
        # Legacy installed --publish remains accepted but cannot grant publication.
        try:result=execute(config,report,source_ref=a.source_ref,source_pr=a.source_pr,publish=False,source_only=True)
        except Exception as e:
            save(report/'FAILURE.json',{'status':'FAIL','error':str(e),'run_type':os.environ.get('GLP_RUN_TYPE','manual')});raise
        finally:
            try:
                base=read(ROOT/'reports/glp-clinic-scout/seo-machine/current-content-base.json')
                retire_disposable_clones(root,pins=[Path(base['release_root'])])
            except Exception as e:print(json.dumps({'clone_retention':'SKIPPED','reason':str(e)}),file=sys.stderr)
        print(json.dumps(result));return 0

if __name__=='__main__':raise SystemExit(main())
