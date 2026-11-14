"""Explicit regenerable-only host rules. Evidence/state/profile roots are never swept."""
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time

POLICY = Path('/home/ubuntu/business/ops/host-pruner-safe-regenerable-policy.json')
LIVE_WORKTREES = {'rook-live-cf2726c', 'rook-file-live-00d03f4'}
HARD_NAMES = {'reports', 'evidence', '.enzyme', 'secrets', 'state', 'sessions',
              'browser-profiles', 'claude-1000', 'hermes-results', 'financial',
              'financial-records', 'finance', 'payments', '.ssh', 'credentials', 'auth.json', 'id_rsa',
              'Default', 'Local State', 'Preferences', 'Cookies', 'Login Data', 'History'}


def hard_path(p):
    p=Path(p)
    return (any(n in HARD_NAMES or n.startswith(('sealed-', 'tmux-', '.org.chromium.',
                        'com.google.Chrome.', 'chrome-profile', 'cdp-', 'financial', '.env')) for n in p.parts)
            or any(n in LIVE_WORKTREES or n.startswith(('chromium-profile', 'browser-profile', 'sealed')) for n in p.parts)
            or p.suffix in {'.db','.sqlite','.sqlite3'}
            or any(s in p.name for s in ('.db-wal','.db-shm','.sqlite-wal','.sqlite-shm')))


def disposable_stats(p,cutoff,deadline,allow_git=False):
    """One no-follow pass checks ALL descendant ages and hard exclusions."""
    stack=[Path(p)]; inodes={}
    while stack:
        if time.monotonic()>=deadline: raise TimeoutError('scan_budget')
        x=stack.pop()
        if hard_path(x) or (x.name=='.git' and not allow_git): return False,0,'protected_content'
        try:
            s=x.lstat()
            if stat.S_ISREG(s.st_mode) and s.st_size>=16:
                try:
                    fd=os.open(x,os.O_RDONLY|os.O_NOFOLLOW|os.O_NOATIME)
                except PermissionError:
                    fd=os.open(x,os.O_RDONLY|os.O_NOFOLLOW)
                with os.fdopen(fd,'rb') as stream: header=stream.read(16)
                if header==b'SQLite format 3\x00' or header[8:12]==b'DUCK': return False,0,'database_magic'
            if s.st_mtime>=cutoff: return False,0,'young_descendant'
            key=(s.st_dev,s.st_ino)
            record=inodes.setdefault(key,[s.st_blocks*512,s.st_nlink,0,stat.S_ISDIR(s.st_mode)])
            record[2]+=1
            if stat.S_ISDIR(s.st_mode):
                with os.scandir(x) as entries:
                    for e in entries:
                        if time.monotonic()>=deadline: raise TimeoutError('scan_budget')
                        stack.append(Path(e.path))
        except OSError: return False,0,'unreadable_or_racing'
    amount=sum(blocks for blocks,links,count,is_dir in inodes.values() if is_dir or count>=links)
    return True,amount,'ok'


def checkpoint(api,state,dry_run):
    if not dry_run: api.write_maintenance_state(state)


def prune_cache_files(api,rule,state,*,dry_run,time_budget):
    """Only blob/HTTP cache leaves; never partially dismantle uv packages.

    Young shard-directory mtimes do not make every old blob young. Select old
    regular files by allocated size, then retain the fresh per-file all-UID veto.
    Hardlinked files are left intact: removing one name cannot reclaim blocks.
    """
    root=Path(rule['root']); started=time.monotonic(); deadline=started+time_budget
    cutoff=time.time()-max(168,rule['age_hours'])*3600; rows=[]; candidates=[]
    if (hard_path(root) or root.is_symlink() or root.resolve()!=root.absolute()
            or not root.is_dir()): return rows
    # Refuse any tracked cache content; an untracked fixture/cache may have a
    # git ancestor without making its disposable files tracked.
    if any((p/'.git').exists() for p in [root,*root.parents]):
        tracked=git_read(api,root,['ls-files','--','.'])
        if tracked['returncode'] or tracked['stdout_tail'].strip(): return rows
    if api.path_is_active_privileged(root):
        return [{'class':'package_cache','root':str(root),'ok':True,
                 'status':'GATED_ACTIVE_OR_UNKNOWN_ROOT','bytes_freed':0}]
    stack=[root]; partial=False
    while stack:
        if time.monotonic()>=deadline: partial=True; break
        parent=stack.pop()
        if hard_path(parent) or (parent/'.git').exists(): continue
        try:
            with os.scandir(parent) as entries:
                for e in entries:
                    if time.monotonic()>=deadline: partial=True; break
                    p=Path(e.path)
                    if e.is_symlink() or hard_path(p): continue
                    s=e.stat(follow_symlinks=False)
                    if stat.S_ISDIR(s.st_mode): stack.append(p)
                    elif (stat.S_ISREG(s.st_mode) and s.st_nlink==1
                          and max(s.st_mtime,s.st_atime)<cutoff):
                        candidates.append((s.st_blocks*512,p,api.path_identity(p)))
        except OSError: continue
    candidates.sort(key=lambda row:row[0],reverse=True)
    for _,p,identity in candidates:
        if time.monotonic()>=deadline: partial=True; break
        try:
            ok,amount,reason=disposable_stats(p,cutoff,deadline)
            if not ok: continue
            valid,reason=api.revalidate_candidate(p,identity,require_directory=False)
            if valid and not dry_run:
                valid=api.remove_disposable(p,deadline=deadline)
            rows.append({'class':'package_cache','path':str(p),'ok':True,
                         'status':('ELIGIBLE' if dry_run else 'REMOVED') if valid else 'REFUSED',
                         'reason':reason,'file_age_gating':True,
                         'bytes_freed':amount if valid and not dry_run else 0,
                         'eligible_bytes':amount if valid and dry_run else 0})
        except TimeoutError: partial=True; break
    # Each pass rescans remaining names; no positional cursor can skip siblings.
    rows.append({'class':'package_cache','root':str(root),'ok':True,
                 'status':'TIME_BUDGET_PARTIAL' if partial else 'COMPLETE',
                 'runtime_seconds':round(time.monotonic()-started,3),'bytes_freed':0})
    return rows


def prune_disposables(api,rule,state,*,dry_run,time_budget=20):
    if rule.get('file_age_gating'):
        return prune_cache_files(api,rule,state,dry_run=dry_run,time_budget=time_budget)
    root=Path(rule['root']); kind=rule['class']; started=time.monotonic()
    deadline=started+time_budget; rows=[]
    processes=api.process_inventory()
    if hard_path(root) or root.is_symlink() or root.resolve()!=root.absolute() or not root.is_dir(): return rows
    key=kind+':'+str(root)
    cursor=state.setdefault('safe_rule_cursors',{}).setdefault(key,{'done':[]})
    done=set(cursor['done']); sizes=state.setdefault('safe_size_estimates',{})
    children=[Path(e.path) for e in os.scandir(root) if not e.is_symlink()]
    newest=set()
    if rule.get('keep_newest'):
        groups={}
        for p in children:
            if not p.is_dir(): continue
            family='all' if rule['keep_newest']=='all' else re.sub(r'[-_]\d.*$','',p.name)
            if family not in groups or p.lstat().st_mtime>groups[family].lstat().st_mtime: groups[family]=p
        newest=set(groups.values())
    children.sort(key=lambda p:sizes.get(str(p),0),reverse=True)
    cutoff=time.time()-rule['age_hours']*3600
    partial=False
    for p in children:
        if time.monotonic()>=deadline: partial=True; break
        if p.name in done: continue
        if p in newest or hard_path(p): cursor['done'].append(p.name); continue
        if rule.get('name_prefix') and not p.name.startswith(rule['name_prefix']): continue
        if rule.get('name_prefixes') and not p.name.startswith(tuple(rule['name_prefixes'])): continue
        if rule.get('rotated_only') and not (p.name.endswith(('.gz','.xz','.old')) or re.search(r'\.\d+$',p.name) or p.name.startswith('atop_')): continue
        identity=api.path_identity(p)
        if any((a/'.git').exists() for a in p.parents):
            tracked=git_read(api,p.parent,['ls-files','--',p.name])
            if tracked['returncode'] or tracked['stdout_tail'].strip():
                cursor['done'].append(p.name)
                continue
        try:
            ok,amount,reason=disposable_stats(p,cutoff,deadline)
            if ok:
                sizes[str(p)]=amount
                if api.path_is_active(p,processes): reason='active_reference'; ok=False
            if ok:
                valid,reason=api.revalidate_candidate(p,identity,require_directory=p.is_dir())
                if valid and not dry_run:
                    valid=api.remove_disposable(p,deadline=deadline,as_root=rule.get('privileged',False))
                rows.append({'class':kind,'path':str(p),'ok':True,'status':'REMOVED' if valid and not dry_run else ('ELIGIBLE' if valid else 'REFUSED'),
                             'reason':reason if not valid else 'age_and_all_uid_reference_gate_passed',
                             'bytes_freed':amount if valid and not dry_run else 0,'eligible_bytes':amount if valid and dry_run else 0})
                if not valid and time.monotonic()>=deadline: partial=True; break
        except TimeoutError: partial=True; break
        cursor['done'].append(p.name); checkpoint(api,state,dry_run)
    if not partial: state['safe_rule_cursors'].pop(key,None)
    checkpoint(api,state,dry_run)
    rows.append({'class':kind,'root':str(root),'ok':True,'status':'TIME_BUDGET_PARTIAL' if partial else 'COMPLETE',
                 'runtime_seconds':round(time.monotonic()-started,3),'bytes_freed':0})
    return rows


def git_read(api,p,args,timeout=8):
    return api.run(['git','-c','core.fsmonitor=false','-c','core.hooksPath=/dev/null','-C',str(p),*args],
                   cwd=p,timeout=timeout,env={'GIT_OPTIONAL_LOCKS':'0'})


def worktree_proof(api,p,require_linked=False):
    p=Path(p)
    if hard_path(p) or p.is_symlink() or p.resolve()!=p.absolute() or not (p/'.git').exists():
        return False,'protected_or_not_git'
    if require_linked and not (p/'.git').is_file(): return False,'primary_repository'
    from disk_retention import live_references,load_policy,BUS
    references=live_references()
    try: relative=str(p.relative_to(BUS))
    except ValueError: relative=str(p)
    if str(p) in references or relative in references: return False,'live_brief_reference'
    guarded=load_policy().get('protected_roots',[])
    try:
        pointer=json.loads((BUS/'reports/glp-clinic-scout/seo-machine/current-content-base.json').read_text())
        if pointer.get('release_root'): guarded.append(pointer['release_root'])
    except (OSError,ValueError): pass
    if any(api.is_under(p,Path(g)) or api.is_under(Path(g),p) for g in guarded): return False,'existing_policy_gate'
    if (api.path_is_active(p,api.process_inventory()) or api.path_is_active_privileged(p)):
        return False,'active_or_unknown_all_uid_reference'
    status=git_read(api,p,['status','--porcelain','--untracked-files=all'])
    if status['returncode'] or status['stdout_tail'].strip(): return False,'dirty_or_unknown'
    ahead=git_read(api,p,['rev-list','--max-count=1','HEAD','--not','--remotes'])
    if ahead['returncode'] or ahead['stdout_tail'].strip(): return False,'unpushed_or_no_remote_proof'
    trees=git_read(api,p,['worktree','list','--porcelain'])
    registered=[l.removeprefix('worktree ') for l in trees['stdout_tail'].splitlines() if l.startswith('worktree ')]
    if trees['returncode'] or str(p) not in registered: return False,'not_registered'
    return True,'clean_and_reachable_from_remote_ref'


def prune_git_worktrees(api,state,parents,*,dry_run,time_budget=30):
    started=time.monotonic(); deadline=started+time_budget; rows=[]
    cursor=state.setdefault('git_safe_cursor',{'done':[]}); done=set(cursor['done'])
    sizes=state.setdefault('safe_size_estimates',{})
    roots=[]
    for parent in map(Path,parents):
        if not parent.is_dir() or parent.is_symlink(): continue
        try:
            with os.scandir(parent) as entries:
                for e in entries:
                    p=Path(e.path)
                    try:
                        if e.is_dir(follow_symlinks=False) and not hard_path(p) and (p/'.git').exists(): roots.append(p)
                    except OSError: continue
        except OSError: continue
    roots=sorted(set(roots),key=lambda p:sizes.get(str(p),0),reverse=True)
    partial=False
    unreadable=False
    for p in roots:
        if time.monotonic()>=deadline: partial=True; break
        if str(p) in done: continue
        # Whole worktrees contain tracked content: operator decision only.
        # Clean/remote-reachable proves recoverability, not deletion authority.
        targets=[(p/'node_modules','inactive_node_modules',336)]
        targets += [(p/n,'build_cache',168) for n in ('.next/cache','.turbo','.vite','.parcel-cache')]
        root_unreadable=False
        for target,kind,hours in targets:
            if kind=='stale_git_worktree' and not (p/'.git').is_file(): continue
            try:
                if not target.exists() or hard_path(target) or target.lstat().st_mtime>time.time()-hours*3600: continue
            except OSError as error:
                unreadable=root_unreadable=True
                rows.append({'class':kind,'path':str(target),'ok':False,
                             'status':'GATED_UNREADABLE_TARGET','error_type':type(error).__name__,
                             'bytes_freed':0})
                continue
            try:
                ok,amount,reason=disposable_stats(target,time.time()-hours*3600,deadline,allow_git=kind=='stale_git_worktree')
            except TimeoutError: partial=True; break
            if not ok: continue
            if kind in {'inactive_node_modules','build_cache'}:
                tracked=git_read(api,p,['ls-files','--',str(target.relative_to(p))])
                if tracked['returncode'] or tracked['stdout_tail'].strip(): continue
            identity=api.path_identity(target)
            ok,reason=worktree_proof(api,p,require_linked=kind=='stale_git_worktree')
            if not ok: continue
            if not dry_run:
                ok,reason=worktree_proof(api,p,require_linked=kind=='stale_git_worktree')
                if not ok or api.path_identity(target)!=identity: continue
                if kind=='stale_git_worktree':
                    result=git_read(api,p,['worktree','remove',str(p)],timeout=max(0.1,deadline-time.monotonic()))
                    ok=result['returncode']==0 and not p.exists()
                else: ok=api.remove_disposable(target,deadline=deadline)
            rows.append({'class':kind,'path':str(target),'ok':True,'status':'ELIGIBLE' if dry_run else ('REMOVED' if ok else 'REFUSED'),
                         'bytes_freed':amount if ok and not dry_run else 0,'eligible_bytes':amount if dry_run else 0})
            checkpoint(api,state,dry_run)
        if partial: break
        if not root_unreadable: cursor['done'].append(str(p))
        checkpoint(api,state,dry_run)
    if not partial and not unreadable: state.pop('git_safe_cursor',None)
    checkpoint(api,state,dry_run)
    rows.append({'class':'safe_git_worktrees','ok':not unreadable,'status':'TIME_BUDGET_PARTIAL' if partial else ('INCOMPLETE' if unreadable else 'COMPLETE'),
                 'runtime_seconds':round(time.monotonic()-started,3),'bytes_freed':0})
    return rows


def container_commands(age_hours):
    if age_hours<24: raise ValueError('container_retention_too_short')
    return [['sudo','-n','docker','image','prune','--force','--filter',f'until={age_hours}h'],
            ['sudo','-n','docker','builder','prune','--force','--filter',f'until={age_hours}h']]


def docker_reclaimed(text):
    match=re.search(r'Total reclaimed space:\s*([\d.]+)\s*([kMGT]?B)',text)
    if not match: return None
    scale={'B':1,'kB':1000,'MB':1000**2,'GB':1000**3,'TB':1000**4}
    return int(float(match[1])*scale[match[2]])


def prune_containers(api,state,*,dry_run,deadline):
    available=api.run(['sudo','-n','docker','info','--format','{{.ServerVersion}}'],cwd=Path('/'),timeout=min(5,max(.1,deadline-time.monotonic())))
    if available['returncode']: return [{'class':'container_cache','ok':True,'status':'GATED_ENGINE_UNAVAILABLE','bytes_freed':0}]
    rows=[]
    for command in container_commands(720):
        if time.monotonic()>=deadline: break
        key=' '.join(command[3:5])
        if dry_run:
            rows.append({'class':'container_cache','native_rule':key,'ok':True,'status':'DRY_RUN','age_hours':720,'bytes_freed':0}); continue
        result=api.run(command,cwd=Path('/'),timeout=min(15,max(.1,deadline-time.monotonic())))
        amount=docker_reclaimed(result['stdout_tail'])
        rows.append({'class':'container_cache','native_rule':key,'ok':True,
                     'status':'COMPLETE' if result['returncode']==0 else 'PARTIAL_OR_REFUSED',
                     'bytes_freed':amount or 0,'attribution_known':amount is not None,
                     'age_hours':720,'live_reference_gate':'Docker engine only unreferenced/dangling image/build cache; no volumes or containers'})
        checkpoint(api,state,dry_run)
    return rows


def run_rules(api,*,dry_run,maintenance_state,now,policy_path=POLICY):
    # Retired report traversal cursor is no longer an executable cleanup task.
    maintenance_state.pop('artifact_scan',None)
    started=time.monotonic(); deadline=started+min(140,api.remaining_budget(140))
    try:
        config=json.loads(Path(policy_path).read_text())
        if config.get('schema')!='safe-regenerable-v1': raise ValueError('policy_schema')
    except (OSError,ValueError): return [{'ok':False,'class':'safe_rules','status':'POLICY_UNAVAILABLE','bytes_freed':0}]
    sizes=maintenance_state.setdefault('safe_size_estimates',{})
    for p,amount in config.get('size_estimates',{}).items(): sizes.setdefault(p,amount)
    rules=sorted(config['rules'],key=lambda r:sum(amount for p,amount in sizes.items()
                 if p==r['root'] or p.startswith(r['root'].rstrip('/')+'/')),reverse=True)
    rows=[]
    for rule in rules:
        if time.monotonic()>=deadline: break
        rows.extend(prune_disposables(api,rule,maintenance_state,dry_run=dry_run,time_budget=min(20,deadline-time.monotonic())))
    if time.monotonic()<deadline:
        rows.extend(prune_git_worktrees(api,maintenance_state,config['git_parents'],dry_run=dry_run,time_budget=min(30,deadline-time.monotonic())))
    if time.monotonic()<deadline:
        rows.extend(prune_containers(api,maintenance_state,dry_run=dry_run,deadline=min(deadline,time.monotonic()+25)))
    rows.append({'class':'safe_regenerable_summary','ok':True,'runtime_seconds':round(time.monotonic()-started,3),
                 'bytes_freed':0,'retention_policy':str(policy_path),'reports_and_evidence_untouched':True,
                 'sealed_and_existing_policy_gates_preserved':True})
    return rows
