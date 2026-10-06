#!/usr/bin/env python3
"""Bounded, evidence-preserving host maintenance (operator rules A/B/C).

Archive originals are never removed until every member has round-tripped through
zstd and TAR with matching SHA256, names, type, ownership, mode and mtime. Large
archives resume between independent zstd frames in one ordinary TAR stream.
"""
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bounded_pruner import ScanBudget, discovery, tree_stats

BUS = Path('/home/ubuntu/business')
POLICY = BUS/'ops/host-pruner-evidence-policy.json'
STATE = Path('/home/ubuntu/.hermes/runstate/host-pruner-evidence.json')
SCRATCH = Path('/home/ubuntu/.hermes/cache/scratch')
GIB = 1024**3
REGENERABLE = {'node_modules','__pycache__','.venv','venv','.cache','test-results'}
FORBIDDEN = {'secrets','state','sessions','.enzyme','.git','.ssh','credentials',
             'financial','financial-records','finance','payments','claude-1000','hermes-results',
             'browser-profiles','Default','Local State','Preferences','Cookies','Login Data','History',
             'rook-live-cf2726c','rook-file-live-00d03f4'}


def protected(p, *, archive=False):
    p=Path(p)
    stop=next((i for i,n in enumerate(p.parts) if n in REGENERABLE),len(p.parts))
    financial_parts=p.parts if archive else p.parts[:stop]
    financial=any(any(token in n.lower() for token in
                  ('reconciliation','payout','invoice','ledger','revenue','transaction','stripe','financial'))
                  for n in financial_parts)
    return (financial or any(n in FORBIDDEN
                or n.startswith(('tmux-','.org.chromium.','chrome-profile',
                'chromium-profile','browser-profile','cdp-','financial','.env'))
                or (not archive and n.startswith(('sealed','seal-','d2-input-seal')))
                for n in p.parts)
            or p.suffix in {'.db','.sqlite','.sqlite3'}
            or any(x in p.name for x in ('.db-wal','.db-shm','.sqlite-wal','.sqlite-shm'))
            or (archive and 'releases' in p.parts))


def sig(s):
    return [s.st_dev,s.st_ino,s.st_mode,s.st_uid,s.st_gid,s.st_size,s.st_mtime_ns,s.st_ctime_ns,s.st_nlink]


def hash_stream(stream,deadline):
    h=hashlib.sha256()
    while True:
        if time.monotonic()>=deadline: raise ScanBudget()
        data=stream.read(1024*1024)
        if not data: return h.hexdigest()
        h.update(data)


def sha(p,deadline):
    fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW|os.O_NOATIME)
    with os.fdopen(fd,'rb') as f: return hash_stream(f,deadline)


def atomic_json(path,data):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+'.tmp-'+str(os.getpid()))
    with tmp.open('w') as f:
        json.dump(data,f,sort_keys=True); f.write('\n'); f.flush(); os.fsync(f.fileno())
    os.replace(tmp,path)


def git_veto(api,p):
    owners=[]
    for a in p.parents:
        marker=a/'.git'
        if marker.is_dir() and not marker.is_symlink() and not any(marker.iterdir()):
            continue  # Empty placeholder has no HEAD/index and cannot track files.
        if marker.exists() or marker.is_symlink(): owners.append(a)
    if not owners: return False,None
    owner=owners[0]
    r=api.run(['git','-c','safe.directory='+str(owner),'-c','core.fsmonitor=false',
               '-C',str(owner),'ls-files','--',str(p.relative_to(owner))],cwd=Path('/'),timeout=3)
    return r['returncode']!=0 or bool(r['stdout_tail'].strip()),owner


def database_magic(p):
    try:
        s=p.lstat()
        if not stat.S_ISREG(s.st_mode) or s.st_size<16: return False
        fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW|os.O_NOATIME)
        with os.fdopen(fd,'rb') as f: header=f.read(16)
        return header==b'SQLite format 3\0' or header[8:12]==b'DUCK'
    except OSError: return True


def regenerable(api,root,state,*,dry_run,deadline,protected_roots=()):
    exclusions=tuple(map(Path,protected_roots))
    def blocked(p): return protected(p) or any(p==r or r in p.parents for r in exclusions)
    # Close every discovery FD before the all-UID reference veto. Our own
    # scandir FDs must not make all caches in an ancestor worktree look active.
    rows=[]; cursor=state.setdefault('A_cursor',{})
    pending=state.setdefault('A_pending',[])
    scan_deadline=min(deadline,time.monotonic()+max(.1,(deadline-time.monotonic())*.45))
    walker=discovery(root,cursor,deadline=scan_deadline,skip=blocked)
    try:
        item=next(walker)
        while True:
            p,is_dir=item; descend=is_dir
            if is_dir and p.name in REGENERABLE:
                descend=False
                if str(p) not in pending: pending.append(str(p))
            item=walker.send(descend)
    except StopIteration: state.pop('A_cursor',None)
    except ScanBudget: pass
    finally: walker.close()
    for name in list(pending):
        if time.monotonic()>=deadline: break
        p=Path(name)
        if not p.exists() or blocked(p): pending.remove(name); continue
        identity=api.path_identity(p); veto,owner=git_veto(api,p)
        if not veto and not api.path_is_active_privileged(owner or p):
            try:
                newest,amount=tree_stats(p,cutoff=time.time()-72*3600,deadline=deadline,
                                        protected=lambda x:blocked(x) or database_magic(x),allow_symlinks=True)
            except ScanBudget: break
            if newest<time.time()-72*3600:
                valid,reason=api.revalidate_candidate(p,identity,require_directory=True)
                if valid and not dry_run: valid=api.remove_disposable(p,deadline=deadline)
                rows.append({'rule':'A_regenerable','path':str(p),'status':'ELIGIBLE' if dry_run and valid else ('REMOVED' if valid else 'REFUSED'),
                             'ok':True,'eligible_bytes':amount if valid and dry_run else 0,
                             'bytes_freed':amount if valid and not dry_run else 0})
        pending.remove(name)
    if not pending: state.pop('A_pending',None)
    if state.get('A_cursor') or pending:
        rows.append({'rule':'A_regenerable','status':'TIME_BUDGET_PARTIAL','ok':True,'bytes_freed':0})
    return rows


def dedupe(roots,state,*,dry_run,deadline,protected_roots=()):
    import hardlink_dedup
    exclusions=tuple(map(Path,protected_roots))
    result=hardlink_dedup.deduplicate(list(map(str,roots)),dry_run=dry_run,
                                     deadline=deadline,scan_state=state,max_seen=500000,min_size=4096,
                                     extra_protected=lambda p:protected(p) or any(p==r or r in p.parents for r in exclusions) or database_magic(p),require_old_parent=False)
    result['rule']='B_readonly_evidence_dedupe'
    if result.get('status')=='TIME_BUDGET_PARTIAL' and not result.get('errors'): result['ok']=True
    return result


class ReferenceIndex:
    def consumer_text(self,p):
        text=p.read_text(errors='replace')
        if p == self.business/'ops/email_flows/scheduler-heartbeat.json':
            # engine.append_scheduler_heartbeat emits only these nine telemetry
            # fields. Keep the source in inventory; unknown shapes remain raw
            # consumers so drift, path references and identity guards still veto.
            try:
                pairs=json.loads(text,object_pairs_hook=lambda items: items)
                if isinstance(pairs,list) and all(isinstance(item,tuple) for item in pairs):
                    data=dict(pairs)
                    keys={'monitor','ran_at','upstream_ok','auth_valid','items_seen',
                          'sent_count','draft_count','cadence_s','secret_printed'}
                    if (len(pairs)==len(keys) and set(data)==keys
                            and data['monitor']=='shared_email_flow_scheduler'
                            and type(data['ran_at']) is str
                            and data['ran_at'].endswith('Z')
                            and type(data['upstream_ok']) is bool
                            and data['auth_valid'] is True and data['secret_printed'] is False
                            and type(data['cadence_s']) is int and data['cadence_s']==1800
                            and all(type(data[k]) is int and data[k]>=0
                                    for k in ('items_seen','sent_count','draft_count'))):
                        from datetime import datetime
                        instant=datetime.fromisoformat(data['ran_at'][:-1]+'+00:00')
                        if instant.isoformat().replace('+00:00','Z')==data['ran_at']:
                            return '{"monitor":"shared_email_flow_scheduler"}'
            except (ValueError,TypeError):
                pass
        if p==Path('/home/ubuntu/.hermes/cron/jobs.json'):
            data=json.loads(text)
            jobs=data.get('jobs',[]) if isinstance(data,dict) else data
            return json.dumps([{'script':j.get('script'),'workdir':j.get('workdir'),'prompt':j.get('prompt')} for j in jobs],sort_keys=True)
        return text

    def __init__(self,files,business,*,rule_D_qa_root=None):
        self.business=Path(business); self.sources=[]
        self.rule_D_qa_root=Path(rule_D_qa_root) if rule_D_qa_root is not None else None
        for name in sorted(set(map(str,files))):
            p=Path(name)
            if self.rule_D_qa_root is not None and p.suffix.lower() in {'.md','.txt','.log','.csv','.tsv','.jsonl'}:
                continue  # D references are consumers/configs, never narrative text.
            if p.is_symlink() or not p.is_file() or p.stat().st_size>32*1024*1024:
                raise ValueError('reference_source_unreadable:'+str(p))
            self.sources.append((p,hashlib.sha256(self.consumer_text(p).encode()).hexdigest(),sig(p.stat())))
        self.texts=[self.consumer_text(p) for p,_,_ in self.sources]
        self.ancestor_pins=[]
        def pins(value,source):
            if isinstance(value,dict):
                for key,item in value.items():
                    if key in {'protected_roots','release_root','current_root','live_root','deploy_root','active_roots'}:
                        for path in item if isinstance(item,list) else [item]:
                            if isinstance(path,str):
                                pin=Path(path) if path.startswith('/') else self.business/path
                                # D alone projects the broad QA protection out of
                                # this key. Live pins and literal run references remain.
                                if (key=='protected_roots' and pin==self.rule_D_qa_root
                                        and source==self.business/'ops/disk-retention-policy.json'): continue
                                self.ancestor_pins.append(pin)
                    pins(item,source)
            elif isinstance(value,list):
                for item in value: pins(item,source)
        for (source,_,_),text in zip(self.sources,self.texts):
            try: pins(json.loads(text),source)
            except ValueError: pass
    def references(self,p):
        # Refuse both ancestor/descendant pins and literal basenames. False
        # positives preserve evidence; unknown/mutated reference input gates all.
        p=Path(p)
        if hasattr(self,'last_changed_source'): del self.last_changed_source
        for source,digest,signature in self.sources:
            if p in source.parents: return True
            if not source.exists() or source.is_symlink():
                self.last_changed_source=str(source); return True
            if sig(source.stat()) != signature:
                current_digest = hashlib.sha256(self.consumer_text(source).encode()).hexdigest()
                if current_digest != digest:
                    self.last_changed_source = str(source)
                    return True
        forms={str(p)}
        try: forms.add(str(p.relative_to(self.business)))
        except ValueError: pass
        if self.rule_D_qa_root is not None:
            # D must recognize scripts/configs using a project-relative run
            # name as well as fully qualified live/deploy pointers.
            forms.add(p.name)
            try: forms.add(str(p.relative_to(self.rule_D_qa_root.parent)))
            except ValueError: pass
        for text in self.texts:
            if any(x in text for x in forms): return True
        return any(pin==p or pin in p.parents or p in pin.parents for pin in self.ancestor_pins)


def reference_source_allowed(p):
    p=Path(p)
    if p.suffix=='.md' or '/ops/linear/' in str(p) or '/ops/gpt6pro-relay/waves/' in str(p): return False
    if p.suffix in {'.py','.js','.mjs','.sh'}: return True
    name=p.name.lower()
    # Health-manager output is volatile telemetry, not a consumer config. Its
    # incident/count/timestamp updates must not invalidate every cold archive.
    if name=='scheduled-job-health-state.json': return False
    return p.suffix in {'.json','.yaml','.yml','.toml'} and (
        any(word in name for word in ('manifest','pin','oracle','policy','schedule'))
        or name.startswith(('current','live-release','live-deploy')) or name=='deploy-config.json')


def reference_files(config,deadline):
    data_dirs={'assets','images','screenshots','videos','attachments','dist','build','public','static','snapshots','captures','artifacts'}
    result=[]
    for root in config['reference_roots']:
        root=Path(root)
        if not root.is_dir(): raise ValueError('reference_root_missing')
        for d,dirs,files in os.walk(root,followlinks=False):
            if time.monotonic()>=deadline: raise ScanBudget('reference_walk:'+str(Path(d))+' selected='+str(len(result)))
            dirs[:]=[n for n in dirs if n not in REGENERABLE and n not in data_dirs and n not in {'.git','.enzyme','secrets','state','linear','waves'}
                      and not (Path(d)/n).is_symlink()]
            for n in files:
                p=Path(d)/n
                if reference_source_allowed(p): result.append(p)
    for name in config['required_reference_files']:
        if not Path(name).is_file(): raise ValueError('required_reference_missing')
        result.append(Path(name))
    # All current/live/pin/deploy pointers, but not the full historical corpus.
    for root in config['archive_roots']:
        root=Path(root)
        for d,dirs,files in os.walk(root,followlinks=False):
            if time.monotonic()>=deadline: raise ScanBudget('reference_walk:'+str(Path(d))+' selected='+str(len(result)))
            dirs[:]=[n for n in dirs if n not in REGENERABLE and n not in data_dirs and not (Path(d)/n).is_symlink()
                      and not protected(Path(d)/n,archive=True)]
            for n in files:
                if n.endswith(('.json','.toml','.yaml','.yml')) and (
                    n.startswith(('current','live-release','live-deploy')) or n=='deploy-config.json'):
                    result.append(Path(d)/n)
    jobs=Path('/home/ubuntu/.hermes/cron/jobs.json')
    if jobs.is_file(): result.append(jobs)
    else: raise ValueError('cron_script_consumers_unknown')
    for name in config['required_reference_files']:
        data=json.loads(Path(name).read_text())
        live=data.get('release_root')
        if live:
            candidate=Path(live)/'wrangler.toml'
            if candidate.is_file(): result.append(candidate)
    return result


class YoungTree(ValueError):
    pass


def inventory(root,deadline,*,content=False,cutoff=None):
    rows=[]; stack=[root]; newest=0; seen={}; links={}; logical=0
    while stack:
        if time.monotonic()>=deadline: raise ScanBudget()
        p=stack.pop()
        if protected(p,archive=True): raise ValueError('protected_descendant')
        s=p.lstat(); rel='.' if p==root else str(p.relative_to(root))
        if cutoff is not None and not stat.S_ISDIR(s.st_mode) and s.st_mtime>=cutoff:
            raise YoungTree()
        if s.st_dev!=root.stat().st_dev: raise ValueError('cross_filesystem')
        if not (stat.S_ISREG(s.st_mode) or stat.S_ISDIR(s.st_mode) or stat.S_ISLNK(s.st_mode)):
            raise ValueError('special_file')
        attrs={n:base64.b64encode(os.getxattr(p,n,follow_symlinks=False)).decode()
               for n in os.listxattr(p,follow_symlinks=False)}
        row={'name':rel,'signature':sig(s),'mode':stat.S_IMODE(s.st_mode),'uid':s.st_uid,
             'gid':s.st_gid,'mtime_ns':s.st_mtime_ns,'size':s.st_size,'xattrs':attrs}
        if stat.S_ISDIR(s.st_mode):
            row['type']='dir'
            with os.scandir(p) as it: stack.extend(Path(e.path) for e in it)
        elif stat.S_ISLNK(s.st_mode):
            row.update(type='symlink',link=os.readlink(p)); newest=max(newest,s.st_mtime)
        else:
            if database_magic(p): raise ValueError('protected_database_magic')
            row['type']='file'; newest=max(newest,s.st_mtime); logical+=s.st_size
            key=(s.st_dev,s.st_ino); seen[key]=(s.st_blocks*512,s.st_nlink); links[key]=links.get(key,0)+1
            if content:
                row['sha256']=sha(p,deadline)
                if sig(p.lstat())!=row['signature']: raise ValueError('changed_during_hash')
        rows.append(row)
    amount=sum(size for key,(size,nlink) in seen.items() if links[key]==nlink)
    rows.sort(key=lambda r:(len(Path(r['name']).parts),r['name']))
    return rows,newest,amount,logical


def estimate_archive(root,entries,logical,allocated,deadline):
    # Actual -19/--long compression of up to 8 MiB from the largest files.
    sample=bytearray()
    for row in sorted(entries,key=lambda r:r['size'],reverse=True):
        if row['type']!='file' or not row['size']: continue
        fd=os.open(root/row['name'],os.O_RDONLY|os.O_NOFOLLOW|os.O_NOATIME)
        with os.fdopen(fd,'rb') as f: sample.extend(f.read(min(1024*1024,8*1024*1024-len(sample))))
        if len(sample)>=8*1024*1024: break
    if not sample: return 0,0
    timeout=max(.1,min(30,deadline-time.monotonic()))
    r=subprocess.run(['zstd','-q','-19','--long','-T2','-c'],input=sample,capture_output=True,timeout=timeout)
    if r.returncode: raise ValueError('compression_probe_failed')
    ratio=min(1.02,max(.002,len(r.stdout)/len(sample)))
    estimate=int(logical*ratio*1.20)+len(entries)*512+16384
    return max(0,allocated-estimate),estimate


def verify_archive(path,expected,deadline):
    by_name={r['name']:r for r in expected}; seen=set(); content_hashes=[]; verified_files={}
    process=subprocess.Popen(['zstd','-q','-d','-c',str(path)],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
    try:
        with tarfile.open(fileobj=process.stdout,mode='r|') as tf:
            for m in tf:
                if time.monotonic()>=deadline: raise ScanBudget('archive_verification:'+str(path))
                name=m.name.removeprefix('./').rstrip('/') or '.'
                if name not in by_name or name in seen: raise ValueError('archive_member_listing_mismatch')
                row=by_name[name]; seen.add(name)
                kind='file' if m.isfile() or m.islnk() else ('dir' if m.isdir() else ('symlink' if m.issym() else 'unsupported'))
                if kind!=row['type'] or m.mode!=row['mode'] or m.uid!=row['uid'] or m.gid!=row['gid']:
                    raise ValueError('archive_metadata_mismatch')
                if abs(float(m.mtime)-row['mtime_ns']/1e9)>0.000001: raise ValueError('archive_mtime_mismatch')
                for attr,encoded in row.get('xattrs',{}).items():
                    if attr.startswith('system.posix_acl_'):
                        key='SCHILY.acl.'+attr.removeprefix('system.posix_acl_')
                        if key not in m.pax_headers: raise ValueError('archive_acl_missing')
                    elif attr.startswith('user.'):
                        value=m.pax_headers.get('SCHILY.xattr.'+attr)
                        if value is None or value.encode('utf-8','surrogateescape')!=base64.b64decode(encoded):
                            raise ValueError('archive_xattr_mismatch')
                if kind=='file':
                    if m.islnk():
                        target=m.linkname.removeprefix('./')
                        if target not in verified_files: raise ValueError('unresolved_archive_hardlink')
                        size,digest=verified_files[target]
                    else:
                        digest=hash_stream(tf.extractfile(m),deadline); size=m.size
                    if size!=row['size'] or digest!=row['sha256']: raise ValueError('archive_content_mismatch')
                    verified_files[name]=(size,digest); content_hashes.append((name,digest))
                elif kind=='symlink' and m.linkname!=row['link']: raise ValueError('archive_symlink_mismatch')
        process.stdout.close()
        if process.wait(timeout=max(.1,deadline-time.monotonic())): raise ValueError('zstd_roundtrip_failed')
        if seen!=set(by_name): raise ValueError('archive_missing_members')
        return {'full_listing':True,'full_content_roundtrip':True,'members':len(seen),
                'content_manifest_sha256':hashlib.sha256(json.dumps(sorted(content_hashes)).encode()).hexdigest()}
    finally:
        if process.poll() is None: process.kill(); process.wait()


def archive_candidates(config,*,deadline,progress=None):
    """Select disjoint deepest cold units with one no-follow metadata walk.

    Selection stops at depth four; summaries still cover EVERY descendant so a
    newer file below that depth cannot authorize an unsafe whole-tree archive.
    Only >=50 MiB units are selected/descended for archival. This discovery is
    advisory: archive_tree repeats the complete content/protection inventory.
    """
    max_depth=min(4,int(config.get('archive_max_depth',4)))
    minimum=max(50*1024*1024,int(config.get('archive_min_bytes',50*1024*1024)))
    cutoff=time.time()-30*86400; candidates=[]
    def sealed(p):
        return p.name.startswith(('sealed','seal-','d2-input-seal'))
    def walk(p,depth,device,inside_seal=False):
        if time.monotonic()>=deadline: raise ScanBudget('archive_discovery:'+str(p))
        s=p.lstat()
        if s.st_dev!=device or protected(p,archive=True): return 0,0,False,[]
        if not stat.S_ISDIR(s.st_mode):
            valid=stat.S_ISREG(s.st_mode) or stat.S_ISLNK(s.st_mode)
            return s.st_blocks*512,s.st_mtime,valid,[]
        if (p.name.startswith('.') or (p/'MANIFEST.json').exists()
                or (p/'MANIFEST-DELETED-IMAGES.json').exists()): return 0,0,False,[]
        total=0; newest=0; valid=True; selected=[]
        with os.scandir(p) as it: children=[Path(e.path) for e in it]
        for child in children:
            try:
                amount,mtime,safe,units=walk(child,depth+1,device,inside_seal or sealed(p))
            except OSError:
                valid=False; continue
            total+=amount; newest=max(newest,mtime); valid=valid and safe
            selected.extend(units)
        # A sealed tree is indivisible; selection cannot reach its descendants.
        if selected: return total,newest,valid,selected
        if (1<=depth<=max_depth and not inside_seal and not p.name.startswith('.')
                and total>=minimum and valid and newest<cutoff):
            selected=[p]
        return total,newest,valid,selected
    for name in config['archive_roots']:
        root=Path(name); before=len(candidates)
        if root.is_dir() and not root.is_symlink() and not protected(root,archive=True):
            _,_,_,units=walk(root,0,root.stat().st_dev)
            candidates.extend(units)
        if progress: progress(root,len(candidates)-before)
    return candidates


def archive_tree(api,root,pins,*,dry_run,deadline,state_dir=None,min_bytes=0):
    root=Path(root); row={'rule':'C_cold_evidence_archive','path':str(root),'ok':True,'bytes_freed':0}
    if (protected(root,archive=True) or root.is_symlink() or root.resolve()!=root.absolute()
            or (root/'MANIFEST.json').is_file()
            or any((p/'MANIFEST-DELETED-IMAGES.json').exists() for p in (root,*root.parents))):
        return dict(row,status='GATED_PROTECTED')
    if pins.references(root):
        if hasattr(pins,'last_changed_source'):
            return dict(row,status='GATED_REFERENCE_CHANGED',changed_reference_source=pins.last_changed_source)
        return dict(row,status='GATED_REFERENCED')
    if api.path_is_active_privileged(root): return dict(row,status='GATED_ACTIVE_OR_UNKNOWN')
    veto,_=git_veto(api,root)
    if veto: return dict(row,status='GATED_TRACKED_OR_UNKNOWN_GIT')
    key=hashlib.sha256(str(root).encode()).hexdigest()[:24]
    state_dir=Path(state_dir or root.parent/'.host-pruner-archive-state')
    journal_path=state_dir/(key+'.json')
    archive=root.with_name(root.name+'.tar.zst')
    partial=root.with_name('.'+root.name+'.tar.zst.partial')
    try:
        current,newest,amount,logical=inventory(root,deadline,content=False,cutoff=time.time()-30*86400)
        if newest>=time.time()-30*86400: return dict(row,status='GATED_YOUNG_FILE')
        if amount<min_bytes:
            return dict(row,status='GATED_SHARED_OR_SMALL_UNIT',original_bytes=amount)
        if dry_run:
            eligible,estimate=estimate_archive(root,current,logical,amount,deadline)
            return dict(row,status='ELIGIBLE',eligible_bytes=eligible,original_bytes=amount,
                        estimated_archive_bytes=estimate,estimate_method='8MiB_actual_zstd19_long_sample_20pct_margin',members=len(current))
        # Converting a fully externally hardlinked copy would consume new blocks
        # rather than reclaim them. Keep that copy as-is; B owns shared evidence.
        if amount==0:
            return dict(row,status='GATED_EXTERNALLY_SHARED',original_bytes=amount)
        state_dir.mkdir(exist_ok=True)
        if archive.exists(): return dict(row,status='GATED_ARCHIVE_NAME_EXISTS')
        if journal_path.exists():
            journal=json.loads(journal_path.read_text())
            entries=journal['entries']
            if [r['signature'] for r in current]!=[r['signature'] for r in entries]:
                return dict(row,status='GATED_SOURCE_CHANGED_RESUME')
            if partial.is_symlink(): return dict(row,status='GATED_PARTIAL_SYMLINK')
            if not partial.exists() or partial.stat().st_size<journal['committed_bytes']:
                return dict(row,status='GATED_PARTIAL_MISSING_OR_TRUNCATED')
            with partial.open('r+b') as f: f.truncate(journal['committed_bytes'])
        else:
            if partial.exists(): return dict(row,status='GATED_ORPHAN_PARTIAL')
            if shutil.disk_usage(root).free<logical+GIB: return dict(row,status='GATED_HEADROOM')
            entries,_,amount,logical=inventory(root,deadline,content=True)
            journal={'root':str(root),'archive':str(archive),'entries':entries,'next':0,
                     'committed_bytes':0,'footer':False,'original_bytes':amount,'logical_bytes':logical}
            fd=os.open(partial,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600); os.close(fd)
            atomic_json(journal_path,journal)
        while journal['next']<len(entries):
            if time.monotonic()+45>=deadline: return dict(row,status='ARCHIVE_BUILD_PARTIAL',members_done=journal['next'],members=len(entries))
            start=journal['next']; stop=start; volume=0
            while stop<len(entries) and (volume<128*1024*1024 or stop==start):
                volume+=entries[stop]['size'] if entries[stop]['type']=='file' else 512; stop+=1
            with tempfile.TemporaryDirectory(prefix='host-pruner-tar-',dir=SCRATCH) as tmp:
                tarpath=Path(tmp)/'chunk.tar'
                names=Path(tmp)/'members.list'
                for entry in entries[start:stop]:
                    if sig((root/entry['name']).lstat())!=entry['signature']:
                        raise ValueError('source_changed_before_chunk')
                names.write_bytes(b'\0'.join(os.fsencode(e['name']) for e in entries[start:stop])+b'\0')
                r=subprocess.run(['tar','--format=pax','--xattrs','--acls','--selinux','--numeric-owner',
                                  '--no-recursion','-C',str(root),'--null','-T',str(names),'-cf',str(tarpath)],
                                 stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                                 timeout=max(.1,deadline-time.monotonic()-30))
                if r.returncode: raise ValueError('tar_chunk_failed_or_source_changed')
                length=0
                with tarfile.open(tarpath,'r:') as tf:
                    for member in tf: length=tf.offset
                with tarpath.open('r+b') as f: f.truncate(length)
                chunk=Path(tmp)/'chunk.zst'
                with chunk.open('wb') as out:
                    r=subprocess.run(['zstd','-q','-19','--long','-T2','-c',str(tarpath)],stdout=out,
                                     stderr=subprocess.DEVNULL,timeout=max(.1,deadline-time.monotonic()-30))
                if r.returncode: raise ValueError('compression_failed')
                with partial.open('ab') as out,chunk.open('rb') as src:
                    shutil.copyfileobj(src,out,1024*1024); out.flush(); os.fsync(out.fileno())
                journal.update(next=stop,committed_bytes=partial.stat().st_size)
                atomic_json(journal_path,journal)
        if not journal['footer']:
            footer=subprocess.check_output(['zstd','-q','-19','--long','-c'],input=b'\0'*10240,timeout=10)
            with partial.open('ab') as f: f.write(footer); f.flush(); os.fsync(f.fileno())
            journal.update(footer=True,committed_bytes=partial.stat().st_size); atomic_json(journal_path,journal)
        if time.monotonic()+30>=deadline: return dict(row,status='ARCHIVE_VERIFY_PENDING')
        verification=verify_archive(partial,entries,deadline-15)
        final,newest,_,_=inventory(root,deadline,content=False)
        if final!=[{k:v for k,v in r.items() if k!='sha256'} for r in entries]:
            raise ValueError('source_changed_after_archive')
        if pins.references(root) or git_veto(api,root)[0] or api.path_is_active_privileged(root):
            return dict(row,status='GATED_REFERENCE_BEFORE_COMMIT')
        archive_sha=sha(partial,deadline)
        if partial.stat().st_blocks*512>=amount:
            # This verified staging copy would not reclaim space. Preserve every
            # original; do not leave a second full-size generated copy behind.
            staged_bytes=partial.stat().st_blocks*512
            partial.unlink(); journal_path.unlink()
            return dict(row,status='GATED_NO_ACTUAL_SAVINGS',original_bytes=amount,
                        staged_bytes_removed=staged_bytes,verification=verification)
        pointer={'schema':'evidence-archive-v1','original_path':str(root),'archive':str(archive),
                 'sha256':archive_sha,'verification':verification,'members':len(entries),
                 'compression':'zstd -19 --long; concatenated zstd frames, ordinary single TAR stream',
                 'original_allocated_bytes':amount}
        pointerdir=root.with_name('.'+root.name+'.archive-pointer')
        retired=root.with_name('.'+root.name+'.archive-retiring')
        if pointerdir.exists() or retired.exists(): return dict(row,status='GATED_TRANSACTION_COLLISION')
        pointerdir.mkdir(mode=0o755)
        atomic_json(pointerdir/'MANIFEST.json',pointer)
        (pointerdir/'README.md').write_text(f'# Archived evidence\n\nOriginal paths and bytes are preserved in `{archive.name}`.\n\nSHA256: `{archive_sha}`\n\nRestore in an empty directory with:\n```sh\nzstd -d -c {archive.name!r} | tar -xpf -\n```\n\nFull content, listing and metadata round-trip verification is recorded in MANIFEST.json.\n')
        os.rename(partial,archive)
        atomic_json(archive.with_name(archive.name+'.inventory.json'),{'pointer':pointer,'entries':entries})
        # Journal before each namespace change; incomplete retirement is resumed.
        journal.update(stage='COMMITTING',retired=str(retired),pointerdir=str(pointerdir),pointer=pointer)
        atomic_json(journal_path,journal)
        os.rename(root,retired); os.rename(pointerdir,root)
        journal.update(stage='RETIRING'); atomic_json(journal_path,journal)
        removed=api.remove_disposable(retired,deadline=deadline)
        if not removed: return dict(row,status='RETIREMENT_PENDING',archive=str(archive),verification=verification)
        overhead=archive.stat().st_blocks*512+archive.with_name(archive.name+'.inventory.json').stat().st_blocks*512+8192
        journal_path.unlink()
        return dict(row,status='ARCHIVED',archive=str(archive),bytes_freed=max(0,amount-overhead),
                    original_bytes=amount,archive_bytes=archive.stat().st_blocks*512,verification=verification)
    except YoungTree:
        return dict(row,status='GATED_YOUNG_FILE')
    except ScanBudget:
        return dict(row,status='TIME_BUDGET_PARTIAL')
    except subprocess.TimeoutExpired:
        return dict(row,status='ARCHIVE_BUILD_PARTIAL')
    except (OSError,ValueError,tarfile.TarError) as exc:
        return dict(row,status='GATED_ERROR',error_type=type(exc).__name__,reason=str(exc)[:180])


def recover_retirements(api,state_dir,allowed,deadline,pins=None):
    rows=[]
    for file in Path(state_dir).glob('*.json'):
        if time.monotonic()>=deadline: break
        j=json.loads(file.read_text())
        if j.get('stage') not in {'COMMITTING','RETIRING'}: continue
        root=Path(j['root']); retired=Path(j['retired']); pointerdir=Path(j['pointerdir']); archive=Path(j['archive'])
        if (not any(base in root.parents and 1<=len(root.relative_to(base).parts)<=4 for base in allowed)
                or protected(root,archive=True) or root.resolve()!=root.absolute()
                or retired!=root.with_name('.'+root.name+'.archive-retiring')):
            raise ValueError('invalid_retirement_journal')
        if not archive.is_file() or sha(archive,deadline)!=j['pointer']['sha256']: continue
        if not retired.exists() and root.is_dir() and not (root/'MANIFEST.json').exists():
            if (pins is None or pins.references(root) or git_veto(api,root)[0]
                    or api.path_is_active_privileged(root)): continue
            current,_,_,_=inventory(root,deadline,content=True)
            if current!=j['entries']: continue
            os.rename(root,retired)
        if not root.exists() and pointerdir.is_dir(): os.rename(pointerdir,root)
        if not (root/'MANIFEST.json').is_file(): continue
        remaining=0
        if retired.exists():
            _,remaining=tree_stats(retired,deadline=deadline)
            if api.path_is_active_privileged(retired) or not api.remove_disposable(retired,deadline=deadline): continue
        file.unlink()
        rows.append({'rule':'C_cold_evidence_archive','path':str(root),'ok':True,'status':'RETIREMENT_RESUMED','bytes_freed':remaining})
    return rows


def delete_run_images(api, root, pins, **kwargs):
    from screenshot_pruner import prune_run
    return prune_run(api, root, pins, **kwargs)


def run(api,config,state,*,dry_run,deadline,full_pass=False,progress_path=None,archive_only=False,images_only=False):
    rows=[]; start=time.monotonic()
    archive_progress={'roots_discovered':[], 'roots_evaluated':[], 'candidates':0, 'evaluated':0}
    def save(stage):
        if not dry_run: atomic_json(STATE,state)
        if progress_path:
            atomic_json(progress_path, {'stage':stage, 'rows':rows, 'archive_progress':archive_progress,
                        'bytes_freed':sum(r.get('bytes_freed',0) for r in rows),
                        'runtime_seconds':time.monotonic()-start})
    d_partial=False
    if not archive_only:
        from screenshot_pruner import run_rules as run_D
        save('D_reference_inventory_and_run_images')
        def d_progress(d_rows):
            if progress_path:
                atomic_json(progress_path,{'stage':'D_cold_run_images','rows':d_rows,
                    'runtime_seconds':time.monotonic()-start})
        rows.extend(run_D(api,config,dry_run=dry_run,deadline=deadline,progress=d_progress))
        d_partial=any(r.get('partial',False) or not r.get('ok',True) for r in rows)
        save('D_cold_run_images_complete' if not d_partial else 'D_partial_or_gated')
        if images_only:
            state['full_pass_complete']=not d_partial
            save('complete' if not d_partial else 'partial')
            return rows
    try:
        retention=json.loads((BUS/'ops/disk-retention-policy.json').read_text())
        protected_roots=list(retention['protected_roots'])
    except (OSError,ValueError,KeyError):
        # Unknown retention protection cannot authorize A/B effects.
        protected_roots=[str(BUS)]
    protected_roots.extend(r['path'] for r in rows if r.get('rule')=='D_cold_run_images'
                           and (Path(r['path'])/'MANIFEST-DELETED-IMAGES.json').exists())
    bstate={} if dry_run else state.setdefault('B_scan',{})
    if not archive_only:
        save('A_regenerable')
        while True:
            rows.extend(regenerable(api,Path(config['business']),state,dry_run=dry_run,
                        deadline=min(deadline,time.monotonic()+config['A_budget_seconds']),protected_roots=protected_roots))
            save('A_regenerable')
            if not full_pass or time.monotonic()>=deadline or not (state.get('A_cursor') or state.get('A_pending')): break
        save('B_readonly_evidence_dedupe')
        while True:
            result=dedupe(config['dedupe_roots'],bstate,dry_run=dry_run,
                          deadline=min(deadline,time.monotonic()+config['B_budget_seconds']),protected_roots=protected_roots)
            rows.append(result); save('B_readonly_evidence_dedupe')
            if not full_pass or time.monotonic()>=deadline or result.get('status')!='TIME_BUDGET_PARTIAL': break
    save('C_reference_inventory')
    try:
        reference_started=time.monotonic()
        pins=ReferenceIndex(reference_files(config,min(deadline,time.monotonic()+1200)),config['business'])
        save('C_reference_inventory_complete')
    except (OSError,ValueError,ScanBudget) as exc:
        rows.append({'rule':'C_cold_evidence_archive','ok':True,'status':'GATED_REFERENCE_INVENTORY_INCOMPLETE',
                     'reason':str(exc)[:300],'reference_runtime_seconds':round(time.monotonic()-reference_started,3),'bytes_freed':0})
        save('C_reference_inventory_gated')
        return rows
    state_dir=STATE.parent/'host-pruner-archives'
    roots=list(map(Path,config['archive_roots']))
    contract=['depth-v1',config.get('archive_max_depth',4),config.get('archive_min_bytes',50*1024*1024),list(map(str,roots))]
    if dry_run or state.get('C_contract')!=contract:
        state.pop('C_done',None); state.pop('C_sizes',None)
        state['C_contract']=contract
    if not dry_run:
        state_dir.mkdir(exist_ok=True)
        rows.extend(recover_retirements(api,state_dir,set(roots),deadline,pins))
    def discovered(root,count):
        archive_progress['roots_discovered'].append(str(root))
        archive_progress['candidates']+=count
        save('C_candidate_discovery')
    try:
        candidates=archive_candidates(config,deadline=deadline,progress=discovered)
    except (ScanBudget,OSError) as exc:
        state['full_pass_complete']=False
        rows.append({'rule':'C_cold_evidence_archive','status':'TIME_BUDGET_PARTIAL',
                     'ok':True,'bytes_freed':0,'reason':str(exc)[:180]})
        save('partial'); return rows
    # Complete manageable trees first, then advance larger resumable archives.
    estimates=state.setdefault('C_sizes',{})
    candidates.sort(key=lambda p:(estimates.get(str(p),0)>4*GIB,-estimates.get(str(p),0),str(p)))
    # Legacy C_done is historical path-only telemetry, not fresh admission.
    # Held candidates can change between runs; successfully retired source
    # trees naturally disappear from discovery. Preserve the old ledger but
    # derive completion solely from guards evaluated during THIS invocation.
    done=[]
    pending_statuses={'TIME_BUDGET_PARTIAL','ARCHIVE_BUILD_PARTIAL','ARCHIVE_VERIFY_PENDING','RETIREMENT_PENDING','GATED_REFERENCE_CHANGED'}
    save('C_cold_evidence_archive')
    for p in candidates:
        if str(p) in done: continue
        if time.monotonic()+45>=deadline: break
        result=archive_tree(api,p,pins,dry_run=dry_run,deadline=deadline,state_dir=state_dir,
                            min_bytes=max(50*1024*1024,int(config.get('archive_min_bytes',50*1024*1024))))
        if result.get('original_bytes'): estimates[str(p)]=result['original_bytes']
        rows.append(result)
        if result.get('status') not in pending_statuses:
            done.append(str(p))
        archive_progress['evaluated']+=1
        archive_progress['roots_evaluated']=[str(root) for root in roots
                if all(str(p) in done for p in candidates if root in p.parents)]
        save('C_cold_evidence_archive')
    archive_progress['roots_evaluated']=[str(root) for root in roots
            if all(str(p) in done for p in candidates if root in p.parents)]
    state['full_pass_complete']=not (d_partial or (not archive_only and (state.get('A_cursor') or state.get('A_pending') or bstate.get('cursors')))
            or any(str(p) not in done for p in candidates))
    save('complete' if state['full_pass_complete'] else 'partial')
    rows.append({'rule':'evidence_preserving_summary','ok':True,'runtime_seconds':round(time.monotonic()-start,3),
                 'bytes_freed':0,'partial':not state['full_pass_complete'], 'archive_progress':archive_progress})
    return rows


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(); parser.add_argument('--dry-run',action='store_true')
    parser.add_argument('--report',required=True); parser.add_argument('--max-seconds',type=int,default=1050)
    parser.add_argument('--full-pass',action='store_true')
    parser.add_argument('--archive-only',action='store_true',help='Read-only C inventory (requires --dry-run)')
    parser.add_argument('--images-only',action='store_true',help='Read-only D inventory (requires --dry-run)')
    args=parser.parse_args(); os.nice(10)
    if args.archive_only and not args.dry_run: parser.error('--archive-only requires --dry-run')
    if args.images_only and (not args.dry_run or args.archive_only): parser.error('--images-only requires --dry-run and excludes --archive-only')
    import host_disk_pruner as api
    config=json.loads(POLICY.read_text())
    expected_dedupe={BUS/'reports',*(BUS/'site-system/flagship-v2'/n for n in ('qa','candidates','work','releases'))}
    expected_archive={BUS/'reports'/n for n in ('ops','glp-clinic-scout','boring-ai')} | {BUS/'site-system/flagship-v2'/n for n in ('qa','candidates','work')}
    if (config.get('schema')!='host-pruner-evidence-v1' or config.get('archive_cold_days')!=30
            or Path(config.get('business',''))!=BUS
            or set(map(Path,config['dedupe_roots']))!=expected_dedupe
            or set(map(Path,config['archive_roots']))!=expected_archive):
        raise SystemExit('evidence_policy_scope_refused')
    target=Path(args.report)
    allowed_outputs=[BUS/'reports/host-disk-prune',BUS/'reports/ops/host-disk-pruner-optimize-20260930',
                     BUS/'reports/ops/host-disk-pruner-archive-depth-20261001',
                     BUS/'reports/ops/host-disk-pruner-screenshots-20261001',
                     BUS/'reports/ops/host-disk-pruner-screenshots-v2-20261001']
    if (target.resolve()!=target.absolute() or not any(p in target.parents for p in allowed_outputs)):
        raise SystemExit('report_output_scope_refused')
    # Exact scoped roots only; policy cannot expand privileged deletion elsewhere.
    assert config['business']==str(BUS)
    assert all(Path(p)==BUS/'reports' or BUS/'reports' in Path(p).parents or
               Path(p).parent==BUS/'site-system/flagship-v2' for p in config['dedupe_roots'])
    assert all(Path(p) in {BUS/'reports/ops',BUS/'reports/glp-clinic-scout',BUS/'reports/boring-ai',
                          BUS/'site-system/flagship-v2/qa',BUS/'site-system/flagship-v2/candidates',
                          BUS/'site-system/flagship-v2/work'} for p in config['archive_roots'])
    state={}
    if STATE.exists(): state=json.loads(STATE.read_text())
    if args.full_pass and state.get('full_pass_complete'): state={}
    limit=365*86400 if args.full_pass else min(1050,args.max_seconds)
    rows=run(api,config,state,dry_run=args.dry_run,deadline=time.monotonic()+limit,
             full_pass=args.full_pass,progress_path=target.with_name('progress.json'),archive_only=args.archive_only,images_only=args.images_only)
    if not args.dry_run: atomic_json(STATE,state)
    atomic_json(args.report,{'dry_run':args.dry_run,'rows':rows,'bytes_freed':sum(r.get('bytes_freed',0) for r in rows),
                            'eligible_bytes':sum(r.get('eligible_bytes',0) for r in rows)})
    print(json.dumps({'report':args.report,'bytes_freed':sum(r.get('bytes_freed',0) for r in rows),
                      'eligible_bytes':sum(r.get('eligible_bytes',0) for r in rows)}))
    # The detached controller must never mislabel a partial pass as completed.
    raise SystemExit(0 if state.get('full_pass_complete') and all(r.get('ok',True) for r in rows) else 2)
