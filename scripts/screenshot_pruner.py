"""Rule D: retire only cold run images, preserving original text and verified copies."""
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tarfile
import tempfile
import time

import evidence_preserving_pruner as ep

IMAGE_SUFFIXES = {'.png', '.jpg', '.jpeg', '.webp', '.avif', '.gif'}
TEXT_SUFFIXES = {'.txt', '.log', '.md', '.json', '.jsonl', '.csv', '.tsv', '.yaml', '.yml',
                 '.toml', '.xml', '.html', '.css', '.js', '.mjs', '.cjs', '.py', '.sh', '.sha256'}
MANIFEST = 'MANIFEST-DELETED-IMAGES.json'
TEXT_ARCHIVE = 'TEXT-RECORDS.tar.zst'
RULE = 'D_cold_run_images'


class GitUnknown(ValueError):
    pass


def protected(p):
    # D is image retirement, not seal destruction. Naming a QA run 'sealed' is
    # not a live pointer. Financial/secret/state/release paths still stay intact.
    return ep.protected(p,archive=True)


def reference_files(config, deadline):
    # Copied current*.json fixtures in historical reports are narrative, not
    # consumers. Keep explicit required pointers and genuinely named live/deploy
    # pointers, including report-resident ones outside copied artifact/fixture trees.
    reports=Path(config['business'])/'reports'
    required=set(map(Path,config['required_reference_files']))
    result=[]
    for p in ep.reference_files(config,deadline):
        p=Path(p)
        if p in required or reports not in p.parents:
            result.append(p); continue
        relative=p.relative_to(reports)
        copied=any(n in {'artifacts','_proof-tests','fixtures','test-results'}
                   or n.startswith(('isolated_','isolated-','fixture-')) for n in relative.parts)
        live=p.name.startswith(('live-release','live-deploy','current-release','current-deploy')) or p.name=='deploy-config.json'
        if live and not copied: result.append(p)
    return result


def git_owners(p):
    owners=[]
    for a in Path(p).parents:
        marker=a/'.git'
        if marker.is_dir() and not marker.is_symlink() and not any(marker.iterdir()):
            continue  # Exact empty placeholder, as in the normal pruner.
        if marker.exists() or marker.is_symlink():
            if marker.is_symlink(): raise GitUnknown('git_marker_symlink:'+str(a))
            owners.append(a)
    return owners


def git_query(owner, deadline, path=None):
    timeout=min(10,deadline-time.monotonic())
    if timeout<=0: raise ep.ScanBudget()
    # Full NUL output, not api.run's 4000-character tail. Root cron reads
    # ubuntu-owned indexes through an exact ephemeral safe.directory binding.
    env={k:v for k,v in os.environ.items() if not k.startswith('GIT_')}
    command=['git','-c','safe.directory='+str(owner),'-c','core.fsmonitor=false',
             '-C',str(owner),'--literal-pathspecs','ls-files','--cached','-z']
    if path is not None: command+=['--',str(Path(path).relative_to(owner))]
    r=subprocess.run(command,cwd='/',env=env,capture_output=True,timeout=timeout)
    if r.returncode or len(r.stdout)>64*1024*1024:
        raise GitUnknown('git_probe_failed:'+str(owner))
    return {os.fsdecode(n) for n in r.stdout.split(b'\0') if n}


def untracked_images(root, images, deadline):
    indexes={}; parent_owners={}; allowed=[]; tracked=[]
    for r in images:
        p=root/r['path']; veto=False
        if p.parent not in parent_owners: parent_owners[p.parent]=git_owners(p)
        for owner in parent_owners[p.parent]:
            if owner not in indexes: indexes[owner]=git_query(owner,deadline)
            if str(p.relative_to(owner)) in indexes[owner]: veto=True
        (tracked if veto else allowed).append(r)
    return allowed,tracked


def git_image_veto(p, deadline):
    return any(str(p.relative_to(owner)) in git_query(owner,deadline,p)
               for owner in git_owners(p))


def stable(s):
    # Link counts/ctime change when another approved name of the inode is unlinked.
    return [s.st_dev, s.st_ino, s.st_mode, s.st_uid, s.st_gid, s.st_size, s.st_mtime_ns]


def scan(root, deadline, *, cutoff=None, generated=False):
    """Image mtimes determine run age; any recent image still vetoes the run."""
    images, texts, records = [], [], []
    device = root.lstat().st_dev
    stack = [root]
    while stack:
        if time.monotonic() >= deadline: raise ep.ScanBudget()
        p = stack.pop()
        s = p.lstat()
        if p.parent == root and generated and p.name in {MANIFEST, TEXT_ARCHIVE}: continue
        if s.st_dev != device: raise ValueError('cross_filesystem')
        if protected(p): continue  # Preserve sensitive sibling/subtree; not a run-wide veto.
        if stat.S_ISDIR(s.st_mode):
            with os.scandir(p) as it: stack.extend(Path(e.path) for e in it)
            continue
        if not stat.S_ISREG(s.st_mode): raise ValueError('symlink_or_special_file')
        if cutoff is not None and p.suffix.lower() in IMAGE_SUFFIXES and s.st_mtime >= cutoff:
            raise ep.YoungTree()
        if ep.database_magic(p): continue  # Database content is never an image effect.
        record = {'path':str(p.relative_to(root)), 'identity':stable(s), 'size':s.st_size,
                  'mtime':s.st_mtime, 'mtime_ns':s.st_mtime_ns,
                  'blocks':s.st_blocks*512, 'nlink':s.st_nlink}
        records.append(record)
        if p.suffix.lower() in IMAGE_SUFFIXES: images.append(record)
        elif p.suffix.lower() in TEXT_SUFFIXES: texts.append(record)
    return sorted(images,key=lambda r:r['path']), sorted(texts,key=lambda r:r['path']), records


def archive_text(root, texts, deadline, staging=None):
    """Make a verified compressed copy, never unlink any non-image original."""
    target = root/TEXT_ARCHIVE
    if target.is_symlink(): raise ValueError('ambiguous_text_archive')
    staging=Path(staging or ep.SCRATCH)
    staging.mkdir(parents=True,exist_ok=True)
    scratch=tempfile.TemporaryDirectory(prefix='rule-D-text-',dir=staging)
    tmp=Path(scratch.name)/'text.tar.zst'
    try:
        with tmp.open('xb') as out:
            proc = subprocess.Popen(['zstd','-q','-3','-T1','-c'], stdin=subprocess.PIPE,
                                    stdout=out, stderr=subprocess.DEVNULL)
            try:
                with tarfile.open(fileobj=proc.stdin,mode='w|',format=tarfile.PAX_FORMAT,
                                  dereference=True) as tf:
                    for r in texts:
                        if time.monotonic() >= deadline: raise ep.ScanBudget()
                        p=root/r['path']
                        if stable(p.lstat()) != r['identity']: raise ValueError('text_changed')
                        tf.add(p,arcname=r['path'],recursive=False)
                proc.stdin.close()
                if proc.wait(timeout=max(.1,deadline-time.monotonic())): raise ValueError('text_compression_failed')
                out.flush(); os.fsync(out.fileno())
            finally:
                if proc.poll() is None: proc.kill(); proc.wait()
        expected=[]
        for r in texts:
            p=root/r['path']; s=p.lstat()
            if stable(s)!=r['identity']: raise ValueError('text_changed')
            expected.append({'name':r['path'],'type':'file','mode':stat.S_IMODE(s.st_mode),
                             'uid':s.st_uid,'gid':s.st_gid,'mtime_ns':s.st_mtime_ns,
                             'size':s.st_size,'sha256':ep.sha(p,deadline),'xattrs':{}})
        verification=ep.verify_archive(tmp,expected,deadline)
        digest=ep.sha(tmp,deadline)
        os.replace(tmp,target)
        return {'path':TEXT_ARCHIVE,'sha256':digest,'verification':verification}
    finally:
        # Staging cleanup is outside the run; never unlink a non-image run file.
        scratch.cleanup()


def verify_manifest(path, expected):
    if path.is_symlink(): raise ValueError('manifest_symlink')
    parsed=json.loads(path.read_text())
    if parsed!=expected: raise ValueError('manifest_roundtrip_mismatch')
    names=[r['path'] for r in parsed['images']]
    if len(names)!=len(set(names)): raise ValueError('duplicate_image_path')
    for r in parsed['images']:
        p=Path(r['path'])
        if p.is_absolute() or '..' in p.parts or p.suffix.lower() not in IMAGE_SUFFIXES:
            raise ValueError('manifest_path_refused')
        if len(r['sha256'])!=64: raise ValueError('manifest_checksum_invalid')
    return parsed


def reclaimable(images):
    """Count allocated blocks once, only if every hard-link name is in this set."""
    inodes={}
    for r in images:
        key=tuple(r['identity'][:2])
        v=inodes.setdefault(key,{'names':0,'nlink':r['nlink'],'blocks':r['blocks'],'size':r['size']})
        v['names']+=1
        if v['nlink']!=r['nlink'] or v['blocks']!=r['blocks']: raise ValueError('inode_changed')
    return {'eligible_bytes':sum(v['blocks'] for v in inodes.values() if v['names']==v['nlink']),
            'unique_image_inodes':len(inodes), 'unique_image_logical_bytes':sum(v['size'] for v in inodes.values()),
            'externally_linked_inodes':sum(v['names']<v['nlink'] for v in inodes.values())}


def ledger_events(path, manifest, *, repair=False):
    if not path.exists(): return {}
    if path.is_symlink(): raise ValueError('ledger_symlink')
    raw=path.read_bytes(); end=raw.rfind(b'\n')+1
    if end<len(raw) and repair:
        fd=os.open(path,os.O_WRONLY|os.O_NOFOLLOW)
        try: os.ftruncate(fd,end); os.fsync(fd)
        finally: os.close(fd)
    expected={r['path'] for r in manifest['images']}
    events={}
    for line in raw[:end].splitlines():
        event=json.loads(line)
        if event['path'] not in expected or event['phase'] not in {'prepared','deleted','missing_unknown'}:
            raise ValueError('ledger_event_refused')
        events[event['path']]=event
    return events


def append_event(path, event):
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_APPEND|os.O_NOFOLLOW,0o600)
    try:
        with os.fdopen(fd,'ab',closefd=False) as f:
            f.write(json.dumps(event,sort_keys=True).encode()+b'\n'); f.flush(); os.fsync(fd)
    finally: os.close(fd)


def prune_run(api, root, pins, *, dry_run, deadline, journal_dir, now=None):
    root=Path(root); now=time.time() if now is None else now
    row={'rule':RULE,'path':str(root),'ok':True,'bytes_freed':0}
    journal=Path(journal_dir)/(hashlib.sha256(str(root).encode()).hexdigest()+'.json')
    ledger=journal.with_suffix('.events.jsonl')
    before=__import__('shutil').disk_usage(root).free
    try:
        if root.is_symlink() or root.resolve()!=root.absolute() or protected(root):
            row['status']='GATED_PROTECTED'; return row
        if pins.references(root):
            row['status']='GATED_REFERENCE_CHANGED' if hasattr(pins,'last_changed_source') else 'GATED_REFERENCED'
            if hasattr(pins,'last_changed_source'): row['reference_source']=pins.last_changed_source
            return row
        # Git is probed for each image's containing repositories after scan;
        # .git inside this run must not veto unrelated untracked captures.
        if api.path_is_active_privileged(root):
            row['status']='GATED_ACTIVE_PROCESS'; return row
        resumed=journal.exists()
        if resumed:
            if journal.is_symlink(): raise ValueError('journal_symlink')
            state=json.loads(journal.read_text())
            if state['root']!=str(root) or state['root_identity']!=stable(root.lstat())[:5]:
                raise ValueError('journal_root_changed')
            current,_,records=scan(root,deadline,cutoff=now-30*86400,generated=True)
            current,tracked=untracked_images(root,current,deadline)
            row['tracked_image_files_kept']=len(tracked)
            allowed={r['path']:r for r in state['records']}
            for r in records:
                if r['path'] not in allowed or r['identity']!=allowed[r['path']]['identity']:
                    raise ValueError('run_changed_after_manifest')
            for r in state['records']:
                if Path(r['path']).suffix.lower() not in IMAGE_SUFFIXES and not (root/r['path']).is_file():
                    raise ValueError('non_image_original_missing')
            if state['status'] not in {'preparing','manifest_prepared'}:
                manifest=verify_manifest(root/MANIFEST,state['manifest'])
                if state.get('status')=='complete':
                    if current: raise ValueError('images_appeared_after_completion')
                    archive=root/manifest['text_archive']['path']
                    if archive.is_symlink() or ep.sha(archive,deadline)!=manifest['text_archive']['sha256']:
                        raise ValueError('text_archive_changed')
                    row.update(status='ALREADY_COMPLETE',manifest_verified=True,text_archive_verified=True)
                    return row
            elif {r['path'] for r in records}!=set(allowed):
                raise ValueError('original_missing_during_prepare')
            if dry_run:
                row.update(reclaimable(current),status='ELIGIBLE',image_files=len(current),image_inventory=current)
                return row
        else:
            if (root/MANIFEST).exists() or (root/TEXT_ARCHIVE).exists(): raise ValueError('manifest_without_journal')
            images,texts,records=scan(root,deadline,cutoff=now-30*86400)
            if not images: row['status']='NO_IMAGES'; return row
            images,tracked=untracked_images(root,images,deadline)
            row['tracked_image_files_kept']=len(tracked)
            if not images:
                row['status']='GATED_TRACKED_OR_UNKNOWN_GIT'; return row
            row.update(reclaimable(images),image_files=len(images))
            if dry_run:
                row.update(status='ELIGIBLE',image_inventory=images); return row
            hashes={}
            for r in images:
                p=root/r['path']; signature=ep.sig(p.lstat())
                key=tuple(r['identity'][:2])
                if key not in hashes: hashes[key]=ep.sha(p,deadline)
                if ep.sig(p.lstat())!=signature: raise ValueError('image_changed_during_hash')
                r['sha256']=hashes[key]
            state={'root':str(root),'root_identity':stable(root.lstat())[:5],
                   'records':records,'status':'preparing','images':images,'texts':texts,
                   'created_epoch':now,'deleted':[],'bytes_freed':0}
            ep.atomic_json(journal,state)
        if state['status']=='preparing':
            text_archive=archive_text(root,state['texts'],deadline,staging=journal.parent)
            state['manifest']={'schema':'deleted-run-images-v1','root':str(root),'cold_days':30,
                               'created_epoch':state['created_epoch'],'images':state.pop('images'),
                               'text_archive':text_archive}
            state.pop('texts',None)
            state['status']='manifest_prepared'
            ep.atomic_json(journal,state)
        manifest=state['manifest']; images=manifest['images']
        if state['status']=='manifest_prepared':
            if not (root/MANIFEST).exists(): ep.atomic_json(root/MANIFEST,manifest)
            verify_manifest(root/MANIFEST,manifest)
            state['status']='verified'; ep.atomic_json(journal,state)
        # Revalidate the complete source snapshot after hashing/compression and before effects.
        current,_,records=scan(root,deadline,cutoff=now-30*86400,generated=True)
        expected={r['path']:r['identity'] for r in state['records'] if r['path'] not in state['deleted']}
        actual={r['path']:r['identity'] for r in records}
        # Missing images after a crash reconcile to the verified manifest; new/changed names never do.
        if any(n not in expected or expected[n]!=ident for n,ident in actual.items()):
            raise ValueError('run_changed_before_delete')
        verify_manifest(root/MANIFEST,manifest)
        archive=root/manifest['text_archive']['path']
        if archive.is_symlink() or ep.sha(archive,deadline)!=manifest['text_archive']['sha256']:
            raise ValueError('text_archive_changed')
        if pins.references(root): raise ValueError('reference_changed_before_delete')
        if any(git_image_veto(root/r['path'],deadline) for r in images
               if (root/r['path']).exists()) or api.path_is_active_privileged(root):
            raise ValueError('git_or_process_became_active')
        events=ledger_events(ledger,manifest,repair=True)
        deleted=set(state['deleted']) | {n for n,e in events.items() if e['phase'] in {'deleted','missing_unknown'}}
        if any(not (root/r['path']).is_file() for r in state['records']
               if Path(r['path']).suffix.lower() not in IMAGE_SUFFIXES):
            raise ValueError('non_image_original_missing')
        for r in images:
            if time.monotonic()>=deadline: raise ep.ScanBudget()
            p=root/r['path']
            if r['path'] in deleted: continue
            if not p.exists():
                # Ambiguous unlink: reconcile without inventing attributed freed blocks.
                append_event(ledger,{'path':r['path'],'phase':'missing_unknown','bytes_freed':0})
                deleted.add(r['path']); continue
            if p.is_symlink() or p.resolve()!=p.absolute() or stable(p.lstat())!=r['identity'] or protected(p):
                raise ValueError('image_changed_before_unlink')
            signature=ep.sig(p.lstat())
            if ep.sha(p,deadline)!=r['sha256'] or ep.sig(p.lstat())!=signature:
                raise ValueError('image_hash_changed_before_unlink')
            # Fresh all-UID process/fd and reference checks for each effect.
            if git_image_veto(p,deadline) or pins.references(root) or api.path_is_active_privileged(root):
                raise ValueError('git_reference_or_process_became_active')
            s=p.lstat()
            if stable(s)!=r['identity'] or p.resolve()!=p.absolute(): raise ValueError('image_identity_changed')
            append_event(ledger,{'path':r['path'],'phase':'prepared','bytes_freed':0})
            p.unlink()
            freed=s.st_blocks*512 if s.st_nlink==1 else 0
            append_event(ledger,{'path':r['path'],'phase':'deleted','bytes_freed':freed})
            deleted.add(r['path']); row['bytes_freed']+=freed
        verify_manifest(root/MANIFEST,manifest)
        if any((root/r['path']).exists() for r in images): raise ValueError('image_deletion_incomplete')
        state['status']='complete'; state['deleted']=sorted(deleted)
        state['bytes_freed']=sum(e.get('bytes_freed',0) for e in ledger_events(ledger,manifest).values())
        ep.atomic_json(journal,state)
        row.update(status='IMAGES_DELETED',image_files=len(images),manifest=str(root/MANIFEST),
                   manifest_verified=True,text_archive_verified=True,
                   run_df_free_delta_bytes=__import__('shutil').disk_usage(root).free-before)
    except GitUnknown as exc:
        row.update(status='GATED_TRACKED_OR_UNKNOWN_GIT',reason=str(exc)[:200])
    except ep.YoungTree:
        row['status']='GATED_YOUNG_FILE'
    except ep.ScanBudget:
        row.update(status='TIME_BUDGET_PARTIAL',partial=True)
    except (OSError,ValueError,KeyError,subprocess.TimeoutExpired) as exc:
        row.update(status='GATED_INVALID_OR_CHANGED',reason=str(exc)[:200])
    return row


def run_units(api, units, pins, *, dry_run, deadline, journal_dir, progress=None):
    rows=[]; inodes={}; files=0; partial=False
    for root in units:
        if time.monotonic()>=deadline:
            partial=True; break
        row=prune_run(api,root,pins,dry_run=dry_run,deadline=deadline,journal_dir=journal_dir)
        inventory=row.pop('image_inventory',[])
        for r in inventory:
            key=tuple(r['identity'][:2]); files+=1
            v=inodes.setdefault(key,{'names':0,'nlink':r['nlink'],'blocks':r['blocks'],'size':r['size']})
            v['names']+=1
            if v['nlink']!=r['nlink'] or v['blocks']!=r['blocks']:
                partial=True
        if 'eligible_bytes' in row: row['run_exclusive_eligible_bytes']=row.pop('eligible_bytes')
        partial=partial or row.get('partial',False) or row.get('status')=='GATED_REFERENCE_CHANGED'
        rows.append(row)
        if progress: progress(rows)
    summary={'rule':RULE+'_summary','ok':True,'bytes_freed':0,'partial':partial,
             'status':'TIME_BUDGET_PARTIAL' if partial else 'COMPLETE',
             'runs_evaluated':len(rows),'image_files':files,
             'unique_image_inodes':len(inodes),
             'unique_image_logical_bytes':sum(v['size'] for v in inodes.values()),
             'externally_linked_inodes':sum(v['names']<v['nlink'] for v in inodes.values()),
             'eligible_bytes':sum(v['blocks'] for v in inodes.values() if v['names']==v['nlink']) if not partial else 0,
             'reclaim_basis':'unique allocated image inodes with ALL link names eligible; gross before manifests/text copies'}
    rows.append(summary)
    return rows


def rule_contract(policy):
    d=policy['rule_D']
    scope={k:d[k] for k in ('image_roots','cold_days','image_extensions','approval','qa_protection_exception')}
    scope['protected_roots']=policy['protected_roots']
    source_dir=Path(__file__).parent
    implementations=[Path(ep.__file__),Path(__file__),source_dir/'host_disk_pruner.py',
                     source_dir/'pruner_process_reference_guard.py',source_dir/'bounded_pruner.py']
    scope['implementation_sha256']={p.name:hashlib.sha256(p.read_bytes()).hexdigest()
                                  for p in implementations}
    return hashlib.sha256(json.dumps(scope,sort_keys=True).encode()).hexdigest()


def read_dry_run_receipt(receipt, expected_sha256, deadline):
    """Authenticate and decode ONE bounded regular-file buffer, never reopen it."""
    limit=32*1024*1024
    fd=os.open(receipt,os.O_RDONLY|os.O_NOFOLLOW|os.O_NOATIME|os.O_NONBLOCK)
    def identity(s):
        return (s.st_dev,s.st_ino,s.st_mode,s.st_uid,s.st_gid,s.st_size,
                s.st_mtime_ns,s.st_ctime_ns,s.st_nlink)
    try:
        before=os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size>limit:
            raise ValueError('D_verified_dry_run_required')
        with os.fdopen(fd,'rb',closefd=False) as stream:
            chunks=[]; size=0
            while True:
                if time.monotonic()>=deadline: raise ep.ScanBudget()
                chunk=stream.read(min(1024*1024,limit+1-size))
                if not chunk: break
                size+=len(chunk)
                if size>limit: raise ValueError('D_verified_dry_run_required')
                chunks.append(chunk)
        raw=b''.join(chunks)
        if (identity(os.fstat(fd))!=identity(before)
                or identity(receipt.lstat())!=identity(before)
                or len(raw)!=before.st_size
                or hashlib.sha256(raw).hexdigest()!=expected_sha256):
            raise ValueError('D_verified_dry_run_required')
    finally:
        os.close(fd)
    data=json.loads(raw)
    if time.monotonic()>=deadline: raise ep.ScanBudget()
    if (not isinstance(data,dict) or not isinstance(data.get('rows'),list)
            or any(not isinstance(row,dict) for row in data['rows'])):
        raise ValueError('D_dry_run_incomplete_or_stale')
    return data


def run_rules(api, config, *, dry_run, deadline, progress=None):
    """Production dispatch: fixed roots, bound approval, dry-first, all guards fail closed."""
    import shutil
    before=shutil.disk_usage(ep.BUS).free
    started=time.monotonic()
    try:
        policy_path=ep.BUS/'ops/disk-retention-policy.json'
        policy=json.loads(policy_path.read_text())
        d=policy.get('rule_D')
        if not d:
            return [{'rule':RULE+'_summary','status':'NOT_CONFIGURED','ok':True,'partial':False,'bytes_freed':0}]
        flag=ep.BUS/'site-system/flagship-v2'
        roots={flag/'qa',flag/'candidates'}
        if (policy.get('schema')!='disk-retention-v1' or set(map(Path,d['image_roots']))!=roots
                or d['cold_days']!=30 or set(d['image_extensions'])!=IMAGE_SUFFIXES
                or Path(d['qa_protection_exception'])!=flag/'qa'
                or str(flag/'qa') not in policy['protected_roots']):
            raise ValueError('D_policy_scope_refused')
        approval=ep.BUS/'ops/linear/approvals/OPERATOR-DECISION-20261001-qa-screenshot-deletion.md'
        if (d['approval']['path']!=str(approval) or approval.is_symlink()
                or ep.sha(approval,deadline)!=d['approval']['sha256']
                or d['approval']['sha256']!='3ac87b3d008a214b11df4fa540817435d6962e82a5887076c2ab9486cf390c3b'):
            raise ValueError('D_operator_approval_pin_refused')
        contract=rule_contract(policy)
        if not dry_run:
            proof=d.get('dry_run_receipt') or {}
            receipt=Path(proof.get('path','/missing'))
            allowed={ep.BUS/'reports/ops/host-disk-pruner-screenshots-20261001',
                     ep.BUS/'reports/ops/host-disk-pruner-screenshots-v2-20261001'}
            if (not any(p in receipt.parents for p in allowed) or receipt.is_symlink()
                    or receipt.resolve()!=receipt.absolute()):
                raise ValueError('D_verified_dry_run_required')
            data=read_dry_run_receipt(receipt,proof.get('sha256'),deadline)
            summaries=[r for r in data['rows'] if r['rule']==RULE+'_summary']
            if (data.get('dry_run') is not True or len(summaries)!=1
                    or summaries[0].get('status')!='COMPLETE'
                    or summaries[0].get('ok') is not True
                    or summaries[0].get('partial') is not False
                    or summaries[0].get('contract_sha256')!=contract):
                raise ValueError('D_dry_run_incomplete_or_stale')
        reference_config=dict(config)
        reference_config['reference_roots']=list(dict.fromkeys(config['reference_roots']+
            [str(ep.BUS/'scripts'),str(flag/'scripts')]))
        sources=reference_files(reference_config,min(deadline,time.monotonic()+1200))
        sources.append(policy_path)
        # Maintained top-level configuration and installed host cron/service definitions.
        for p in flag.iterdir():
            if p.is_file() and not p.is_symlink() and ep.reference_source_allowed(p): sources.append(p)
        service=Path('/home/ubuntu/.config/systemd/user/host-disk-cleanup.service')
        if service.is_file(): sources.append(service)
        pins=ep.ReferenceIndex(sources,ep.BUS,rule_D_qa_root=flag/'qa')
        units=[]
        infrastructure={'baselines','fixtures','tests','ws-v','scripts','quarantine'}
        for root in sorted(roots):
            if root.is_symlink() or root.resolve()!=root.absolute() or not root.is_dir():
                raise ValueError('D_root_unknown_or_symlink')
            with os.scandir(root) as it:
                units.extend(Path(e.path) for e in it if e.is_dir(follow_symlinks=False)
                             and not e.name.startswith('.') and e.name not in infrastructure)
        units.sort()
        rows=run_units(api,units,pins,dry_run=dry_run,deadline=deadline,
                       journal_dir=ep.STATE.parent/'host-pruner-images',progress=progress)
        rows[-1].update(contract_sha256=contract,root_free_before_bytes=before,
                       root_free_after_bytes=shutil.disk_usage(ep.BUS).free,
                       runtime_seconds=round(time.monotonic()-started,3),reference_sources=len(pins.sources))
        return rows
    except (OSError,ValueError,KeyError,ep.ScanBudget) as exc:
        return [{'rule':RULE+'_summary','status':'GATED_POLICY_OR_REFERENCE_INVENTORY',
                 'ok':False,'partial':True,'bytes_freed':0,'eligible_bytes':0,'reason':str(exc)[:250]}]
