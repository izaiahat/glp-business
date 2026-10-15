#!/usr/bin/env python3
"""Bounded automatic large-class sweep under the existing cleanup singleton."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
BUS=Path('/home/ubuntu/business')
sys.path.insert(0,str(Path(__file__).resolve().parent))
import host_disk_cold_classes as m
import host_disk_pruner as api


def references(report):
    root=BUS/'reports/ops/glp347-pallas-20261007'
    stack=[root];refs=set();source_hashes={};source_identities={}
    while stack:
        p=stack.pop();identity=m.signature(p);source_identities[str(p)]=identity
        if p.is_symlink():refs.add(p.resolve());continue
        if p.is_dir():
            if p.name in {'.git','node_modules','assets','public','dist','browser-profiles'}:continue
            with os.scandir(p) as es:stack.extend(Path(e.path) for e in es)
            if m.signature(p)!=identity:raise ValueError('reference directory changed during scan')
            continue
        if p.suffix not in {'.json','.md','.py','.log','.jsonl','.mjs','.sh','.txt','.yaml','.yml','.toml','.csv'}:continue
        source_hashes[str(p)]=m.sha(p)
        pattern=r'(?:/home/ubuntu/(?:business/|\.hermes/cache/scratch/)|/tmp/|(?:reports|site-system|\.worktrees)/)[^\s\"<>\x60,;\)\]\}]+'
        for s in re.findall(pattern,p.read_text(errors='replace')):
            s=s.rstrip("'/:.");q=Path(s) if s.startswith('/') else BUS/s
            refs.add(q)
            if q.is_symlink():refs.add(q.resolve(strict=False))
        if m.signature(p)!=identity:raise ValueError('reference file changed during scan')
    # Bare roots name no artifact. Scratch and /tmp roots previously pinned every
    # child (300/300 idle entries refused); children stay pinned when named.
    layout={BUS,BUS/'reports',BUS/'reports/ops',BUS/'reports/glp-clinic-scout',
            BUS/'site-system',BUS/'site-system/flagship-v2',BUS/'.worktrees',
            Path('/home/ubuntu/.hermes/cache/scratch'),Path('/tmp'),
            BUS/'reports/glp-clinic-scout/affiliate-machine',
            BUS/'reports/glp-clinic-scout/affiliate-machine/registry-site-sync'}
    pins=refs-layout
    policy_path=BUS/'ops/disk-retention-policy.json'
    source_identities[str(policy_path)]=m.signature(policy_path)
    policy=json.loads(policy_path.read_text())
    pins.update(Path(p) for p in policy['protected_roots'])
    pins.update([root,BUS/'site-system/flagship-v2/releases',BUS/'reports/disk-reclaim-20261008-r2',report])
    pins.update(m.live_symlink_targets())
    from host_disk_relay_custody import collect as relay_custody
    bindings=relay_custody(bus=BUS)
    pins.update(bindings)
    source_identities.update(bindings.identities)
    m.write_json(report/'references.json',{'pins':sorted(map(str,pins)),
        'reference_source_sha256':source_hashes,'reference_source_identity':source_identities,
        'generic_layout_roots_retained_not_retired':sorted(map(str,layout))})
    return tuple(sorted(pins))


def discover(root,pins,deadline,names=None):
    stack=[root];out=[]
    while stack:
        if time.monotonic()>deadline:raise TimeoutError('discovery')
        p=stack.pop()
        if p.is_symlink() or m.protected(p,pins):continue
        if names and p.name in names:out.append(p);continue
        with os.scandir(p) as es:
            stack.extend(Path(e.path) for e in es if e.is_dir(follow_symlinks=False) and e.name!='.git')
    return out


def partial_archive_effect(dest):
    ledger=dest.with_suffix(dest.suffix+'.retirement.jsonl')
    verified=dest.with_suffix(dest.suffix+'.verified.json')
    if not ledger.exists() or not verified.exists():return 0,0
    gross=sum(json.loads(line).get('freed_bytes',0) for line in ledger.read_text().splitlines())
    paths=[dest,ledger,verified,dest.with_suffix(dest.suffix+'.manifest.json'),
           dest.with_suffix(dest.suffix+'.listing.txt')]
    overhead=sum(p.stat().st_blocks*512 for p in paths if p.exists())
    return gross-overhead,gross


def load_external_pins(path,max_age=3600):
    """Open PR/Linear/live-release/R8/worktree/PREPARED pins collected by the
    unprivileged controller. Missing, stale or invalid input disables new classes."""
    data=json.loads(Path(path).read_text())
    if data.get('schema')!='host-disk-external-pins-v1' or time.time()-data['collected_at']>max_age:
        raise ValueError('stale external pin inventory')
    return m.existing_pins(data['pins'])


def auto_classes(guarded,report,pins,external,*,dry_run,deadline):
    import host_disk_auto_classes as auto
    pins=tuple(sorted(set(pins)|set(external)))
    rows=auto.registry_clones(guarded,m,pins=pins,dry_run=dry_run)
    rows.extend(auto.stale_tmp(guarded,m,pins=pins,deadline=deadline,dry_run=dry_run))
    import host_disk_report_caches
    rows.extend(host_disk_report_caches.run(guarded,m,BUS/'reports',pins=pins,
        deadline=deadline,dry_run=dry_run))
    nodes=discover(BUS/'reports',pins,deadline,names={'node_modules'})
    rows.extend(auto.rebuildable_node_modules(guarded,m,nodes,pins=pins,deadline=deadline,dry_run=dry_run))
    return pins,rows


def rank_reports(roots,deadline):
    """Large trees can take >10s to stat; record failures, never silently drop."""
    ranking=[];refusals=[]
    for root in roots:
        row={'rule':'cold_report_ranking','path':str(root),'bytes_freed':0}
        remaining=deadline-time.monotonic()
        if remaining<1:
            refusals.append(dict(row,status='INCOMPLETE',reason='budget'))
            continue
        try:
            result=subprocess.run(['du','-x','-B1','-s',str(root)],capture_output=True,text=True,
                                  timeout=min(120,remaining))
            if result.returncode!=0:raise ValueError('du incomplete')
            ranking.append((int(result.stdout.split()[0]),root))
        except (OSError,ValueError,IndexError,subprocess.TimeoutExpired) as error:
            refusals.append(dict(row,status='INCOMPLETE',reason=type(error).__name__))
    return sorted(ranking,reverse=True),refusals


def cold_report_roots(pins, *, pressure):
    """Enroll publisher evidence under pressure, retaining all member/live gates.

    The old dated-ops-only selector silently ignored the 60GiB publisher tree.
    This only proposes archival; inactive/dirty/cold/DB/R8/custody and verified
    manifest/roundtrip checks still decide whether each original may retire.
    """
    roots=[]
    pins=m.custody_pins(pins)
    bases=sorted((BUS/'reports').iterdir()) if pressure else [BUS/'reports/ops']
    for base in bases:
        if base.name=='host-disk-prune':continue
        if not base.is_dir() or base.is_symlink():continue
        if base.name not in {'ops','glp-clinic-scout'}:
            if not m.protected(base,pins):roots.append(base)
            continue
        if not base.exists(): continue
        for p in sorted(base.iterdir()):
            if p.is_symlink() or not p.is_dir() or m.protected(p,pins): continue
            if base.name=='ops' and not pressure and not re.search(r'20\d{6}',p.name): continue
            roots.append(p)
    return roots


def idle_targets(scratch, tmp=Path('/tmp'), *, include_declared=True):
    """Discover explicit tmp classes; a prefix alone never authorizes removal."""
    targets=list(scratch.iterdir())
    prefixes=('glp-ultracode-browser-nowrite-',)
    if include_declared:prefixes+=('aeo-declared-',)
    for prefix in prefixes:
        targets.extend(tmp.glob(prefix+'*'))
    return targets


def run(report,*,dry_run=False,budget=1800,external_pins=None,completed_ci_only=False):
    if completed_ci_only and not dry_run:raise ValueError('scoped completed-CI entry is dry-run only')
    report=Path(report);report.mkdir(parents=True,exist_ok=True)
    deadline=time.monotonic()+budget
    d=os.statvfs('/');before=d.f_bavail*d.f_frsize
    pressure=before/(d.f_blocks*d.f_frsize)<0.15
    rows=[];r2info=None
    try:
        pins=references(report)
    except (OSError,ValueError,RuntimeError,KeyError,subprocess.SubprocessError) as error:
        # Reference refusal is terminal and precedes every effect. Preserve the
        # original exception/exit status while exposing its actual source to the
        # registered controller, rather than leaving an absent report/empty rows.
        source=getattr(error,'changed_source',None)
        row={'rule':'reference_inventory','status':'GATED_REFERENCE_INVENTORY',
             'ok':False,'partial':True,'bytes_freed':0,'reason':str(error),
             'error_type':type(error).__name__,'changed_source':source}
        d=os.statvfs('/')
        m.write_json(report/'large-classes.json',{'stage':'reference_inventory_refused',
            'pressure_trigger':pressure,'threshold_free_fraction':0.15,
            'before_free_bytes':before,'after_free_bytes':d.f_bavail*d.f_frsize,
            'df_delta_bytes':d.f_bavail*d.f_frsize-before,'rows':[row],'r2':None,
            'reference_veto_source':source,'attributable_bytes_freed':0})
        raise
    from host_disk_reference_guard import ReferenceGuardAPI
    reference_data=json.loads((report/'references.json').read_text())
    guarded=ReferenceGuardAPI(api,reference_data['reference_source_identity'])
    def save(stage):
        d=os.statvfs('/')
        m.write_json(report/'large-classes.json',{'stage':stage,'pressure_trigger':pressure,
            'threshold_free_fraction':0.15,'before_free_bytes':before,
            'after_free_bytes':d.f_bavail*d.f_frsize,'df_delta_bytes':d.f_bavail*d.f_frsize-before,
            'rows':rows,'r2':r2info,'reference_veto_source':guarded.last_reference_veto,
            'attributable_bytes_freed':sum(r.get('bytes_freed',0) for r in rows)})
    from host_disk_completed_dependencies import run as completed_dependencies
    # Candidate-source qualification has a dry-only finite-class selector; the
    # natural registered unit retains normal singleton/source-install custody.
    dependency_policy=(Path(__file__).resolve().parent.parent/'ops/disk-retention-policy.json'
                       if completed_ci_only else BUS/'ops/disk-retention-policy.json')
    save('completed_ci_dependencies')
    rows.extend(completed_dependencies(guarded,dependency_policy,pins=pins,
                                      dry_run=dry_run,deadline=deadline))
    if completed_ci_only:
        save('terminal_completed_ci_dry_run')
        return rows
    external_ok=not external_pins
    if external_pins:
        save('auto_classes')
        try:
            external=load_external_pins(external_pins)
            save('abandoned_archive_staging')
            rows.extend(m.cleanup_abandoned_archives(guarded,BUS/'reports/host-disk-prune',
                source_roots=(BUS/'reports',BUS/'site-system/flagship-v2/archives',BUS/'site-system/archives'),
                dry_run=dry_run,deadline=deadline))
            save('auto_classes')
            pins,auto_rows=auto_classes(guarded,report,pins,external,dry_run=dry_run,deadline=deadline)
            rows.extend(auto_rows);external_ok=True
        except (OSError,ValueError,KeyError,RuntimeError,TimeoutError) as e:
            rows.append({'rule':'auto_classes','status':'GATED_REFERENCE_INVENTORY','reason':str(e),'bytes_freed':0,'ok':False})
    save('discover_nodes');nodes=[]
    for root in [BUS/'reports',BUS/'site-system/flagship-v2/candidates',
                 BUS/'site-system/flagship-v2/work',BUS/'.worktrees']:
        nodes.extend(discover(root,pins,deadline,names={'node_modules'}))
    rows.extend(m.node_modules(guarded,nodes,pins=pins,dry_run=dry_run,deadline=deadline))
    save('idle_scratch')
    scratch=Path('/home/ubuntu/.hermes/cache/scratch')
    targets=idle_targets(scratch,include_declared=bool(external_pins) and external_ok)
    rows.extend(m.idle_scratch(guarded,targets,pins=pins,dry_run=dry_run,deadline=deadline))
    save('cold_evidence')
    class Unavailable:
        def store(self,*args):raise RuntimeError('R2 destination unavailable')
    remote=Unavailable();r2info={'bucket':'glp-cold-evidence','status':'DRY_RUN_NO_CONTACT'}
    if external_ok and not dry_run:
        try:
            from host_disk_r2 import Destination
            remote=Destination.from_environment()
            r2info['status']='CONFIGURED_PRIVATE'
        except Exception as error:
            r2info.update(status='UNAVAILABLE_LOCAL_FALLBACK',error_type=type(error).__name__)

    # Reserve site archive time before the unbounded report backlog.
    # Custody/age/net-savings gates and the overall scheduled cap are unchanged.
    if external_pins and external_ok and time.monotonic()<deadline:
        import host_disk_auto_classes as auto
        save('site_cold_archive')
        site_deadline=min(deadline,time.monotonic()+budget/3)
        rows.extend(auto.site_cold_archive(guarded,m,report,pins=pins,deadline=site_deadline,dry_run=dry_run,archive_destination=remote))
    roots=cold_report_roots(pins,pressure=pressure)
    estimates,ranking_refusals=rank_reports(roots,deadline)
    rows.extend(ranking_refusals)
    if external_ok and time.monotonic()<deadline:
        from host_disk_r2 import migrate_archives
        save('r2_existing_archives')
        rows.extend(migrate_archives(guarded,m,BUS/'reports/host-disk-prune',remote,
                    pins=pins,deadline=deadline,dry_run=dry_run))
    for amount,p in estimates:
        if not external_ok:break
        if amount<1024**2 or time.monotonic()+60>deadline:continue
        name=p.relative_to(BUS/'reports').as_posix().replace('/','--')
        for cold_days,destination in [(30,remote),(7,None)]:
            index=0
            while time.monotonic()+60<deadline:
                index+=1
                dest=report/'archives'/(name+f'-{cold_days}d-{index}.tar.zst')
                fs=os.statvfs(p);available=fs.f_bavail*fs.f_frsize
                cap=min(8*1024**3,max(0,available-1024**3))
                try:
                    result=m.cold_archive(guarded,p,dest,pins=pins,dry_run=dry_run,deadline=deadline,
                        cold_days=cold_days,archive_destination=destination,max_archive_input_bytes=cap,
                        min_bytes=1024**2 if cold_days==30 else 128*1024**2,
                        min_net_bytes=1024**2 if pressure or cold_days==30 else None,reserve_bytes=1024**3)
                    rows.append(result)
                except Exception as error:
                    effect,gross=partial_archive_effect(dest)
                    rows.append({'rule':'cold_evidence_archive','path':str(p),'archive':str(dest),
                        'status':'ERROR_PRESERVED_OR_PARTIAL','reason':str(error),'bytes_freed':effect,
                        'gross_bytes_freed':gross,'ok':False})
                    break
                save('cold_evidence')
                if dry_run or result.get('gross_bytes_freed',0)<=0 or not result.get('deferred_for_headroom_members'):break

    duplicate_targets=[]
    for _,root in sorted(estimates,reverse=True)[:10]:
        if time.monotonic()>deadline:break
        try:
            members,_=m.inventory(root,pins,time.time()-7*86400,deadline,hashes=False)
            duplicate_targets.extend(root/r['name'] for r in members if not r['signature'][2]&0o222)
        except (OSError,ValueError,TimeoutError):continue
    if time.monotonic()<deadline:
        rows.extend(m.verified_duplicates(guarded,duplicate_targets,report/'duplicate-manifest.json',
            pins=pins,dry_run=dry_run,deadline=deadline))
    if external_ok and not dry_run and hasattr(remote,'summary'):
        try:r2info=remote.summary()
        except Exception as error:r2info.update(status='OBJECT_COUNTS_UNKNOWN',error_type=type(error).__name__)
    save('complete');return rows

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--report',required=True)
    parser.add_argument('--dry-run',action='store_true')
    parser.add_argument('--budget',type=int,default=1800)
    parser.add_argument('--external-pins')
    parser.add_argument('--completed-ci-only',action='store_true',help='finite completed dependencies; requires --dry-run')
    args=parser.parse_args()
    result=run(args.report,dry_run=args.dry_run,budget=args.budget,external_pins=args.external_pins,
               completed_ci_only=args.completed_ci_only)
    raise SystemExit(1 if any(not r.get('ok',True) for r in result) else 0)
