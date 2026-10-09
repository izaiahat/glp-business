#!/usr/bin/env python3
"""Explicit-target maintenance classes; never scheduled/import-time effects.

Callers supply a complete, live protection/reference inventory and disjoint targets.
No scheduler adoption while the older singleton is running. Evidence is archived,
not shared with writable source inodes. Fail closed on every process/identity gate.
"""
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tarfile
import time

PROTECTED = {'.git','secrets','credentials','browser-profiles','hermes-results',
             'state','sessions','financial','payments','Default','Local State',
             'Preferences','Cookies','Login Data','History'}

PROTECTED.update({'.enzyme','.ssh','.aws','.gnupg','.azure','.config','financial-records','finance',
                  'claude-1000','rook-live-cf2726c','rook-file-live-00d03f4'})

# Sealed release toolchains (site-system/toolchains/**) are deploy custody, never cache.
PROTECTED.add('toolchains')

LIVE_HOME=Path('/home/ubuntu/.hermes')

def live_symlink_targets(home=None,max_depth=3):
    """All live Hermes symlink targets are custody roots, including external skills.

    Never follow directory symlinks while discovering. Permission/scan failures
    propagate: incomplete live-target inventory cannot authorize any effects.
    """
    home=Path(home or LIVE_HOME);stack=[(home,0)];targets=set()
    while stack:
        p,depth=stack.pop()
        if p.is_symlink():
            targets.add(p.resolve(strict=False));continue
        if depth>=max_depth or not p.is_dir():continue
        with os.scandir(p) as es:
            stack.extend((Path(e.path),depth+1) for e in es)
    return targets

def custody_pins(pins):
    return tuple(map(Path,pins))+tuple(live_symlink_targets())

_PIN_CACHE={}

def protected(p, pins=()):
    p=Path(p)
    # Compile a reference set once, not thousands of pin comparisons per inode.
    key=id(pins)
    cached=_PIN_CACHE.get(key)
    if cached is None or cached[0] is not pins:
        cached=(pins,frozenset(map(str,pins))); _PIN_CACHE[key]=cached
    pinset=cached[1]
    referenced=any(str(a) in pinset for a in [p,*p.parents])
    return (referenced or
            p.suffix.lower() in {'.db','.sqlite','.sqlite3','.duckdb','.db-wal','.db-shm','.db-journal','.sqlite-wal','.sqlite-shm','.sqlite-journal','.sqlite3-wal','.sqlite3-shm','.zst','.tar','.tgz','.gz'} or
            any(n in PROTECTED or n.startswith(('.env','tmux-','.org.chromium.',
                'browser-profile','chrome-profile','chromium-profile','sealed','seal-'))
                or any(t in n.lower() for t in ('backup','recovery','credential','secret','reconciliation','payout',
                    'invoice','ledger','revenue','transaction','stripe','financial'))
                for n in p.parts))

def signature(p):
    s=p.lstat()
    return [s.st_dev,s.st_ino,s.st_mode,s.st_size,s.st_mtime_ns,s.st_ctime_ns,s.st_nlink,s.st_uid,s.st_gid]

def sha_stream(f):
    h=hashlib.sha256()
    for data in iter(lambda:f.read(1024*1024),b''): h.update(data)
    return h.hexdigest()

def sha(p):
    fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW|os.O_NOATIME)
    with os.fdopen(fd,'rb') as f: return sha_stream(f)

def write_json(p,d):
    p=Path(p); q=p.with_name(p.name+'.tmp')
    with q.open('w') as f:
        f.write(json.dumps(d,sort_keys=True,indent=2)+'\n');f.flush();os.fsync(f.fileno())
    os.replace(q,p)
    fd=os.open(p.parent,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)

def dirty_worktree(p):
    for a in [p,*p.parents]:
        marker=a/'.git'
        if marker.is_dir() and not marker.is_symlink() and not any(marker.iterdir()):
            continue  # Match the maintained pruner: empty placeholder is not a worktree.
        if marker.exists() or marker.is_symlink():
            try:
                r=subprocess.run(['git','-c','safe.directory='+str(a),'-c','core.fsmonitor=false',
                    '-C',str(a),'status','--porcelain','--untracked-files=normal'],
                    capture_output=True,text=True,timeout=20)
                return r.returncode!=0 or bool(r.stdout.strip())
            except (OSError,subprocess.TimeoutExpired): return True
    return False

def inactive(api,p):
    return not api.path_is_active_privileged(p)

def inventory(root,pins,cutoff,deadline,*,hashes):
    """Select only cold files; no directory traversal through symlinks/protection."""
    root=Path(root); rows=[]; skipped=[]; stack=[root]; device=root.lstat().st_dev
    while stack:
        if time.monotonic()>deadline: raise TimeoutError('inventory')
        p=stack.pop()
        if protected(p,pins): skipped.append(str(p)); continue
        s=p.lstat()
        if s.st_dev!=device: raise ValueError('cross-device')
        if stat.S_ISDIR(s.st_mode):
            # Nested checkout ownership, not merely the ancestor of the unit.
            if (p/'.git').exists() and dirty_worktree(p):
                skipped.append(str(p)); continue
            with os.scandir(p) as es: stack.extend(Path(e.path) for e in es)
            continue
        if not stat.S_ISREG(s.st_mode) or max(s.st_mtime,s.st_ctime)>cutoff:
            skipped.append(str(p)); continue
        # Detect extensionless SQLite databases without changing atime.
        fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW|os.O_NOATIME)
        with os.fdopen(fd,'rb') as f:
            header=f.read(16)
            if header==b'SQLite format 3\0' or header[8:12]==b'DUCK': skipped.append(str(p)); continue
            f.seek(0); digest=sha_stream(f) if hashes else None
        sig=signature(p)
        if sig!=[s.st_dev,s.st_ino,s.st_mode,s.st_size,s.st_mtime_ns,s.st_ctime_ns,s.st_nlink,s.st_uid,s.st_gid]:
            raise ValueError('changed during inventory')
        rows.append({'name':str(p.relative_to(root)),'signature':sig,
                     'sha256':digest,'allocated_bytes':s.st_blocks*512})
    return sorted(rows,key=lambda r:r['name']),skipped

def node_modules(api,targets,*,pins=(),dry_run=True,deadline):
    """Delete only fully old dependency trees, never a dirty checkout or open file."""
    from host_disk_large_classes import snapshot,lsof_inactive
    out=[]; cutoff=time.time()-3*86400; pins=custody_pins(pins)
    for target in targets:
        p=Path(target); row={'rule':'cold_node_modules','path':str(p),'bytes_freed':0}
        try:
            if p.name!='node_modules' or p.is_symlink() or p.resolve()!=p.absolute() or protected(p,pins):
                raise ValueError('scope/protection')
            if dirty_worktree(p) or not inactive(api,p): raise ValueError('dirty/active')
            before,amount=snapshot(p,deadline)
            from host_disk_payload_guard import reject_database_payloads
            reject_database_payloads(p,before)
            if any(max(r[5],r[6])/1e9>cutoff for r in before): raise ValueError('young descendant')
            fresh_pins=custody_pins(pins)
            if any(protected(p/r[0],fresh_pins) for r in before): raise ValueError('new live symlink custody')
            if not lsof_inactive(p) or not inactive(api,p) or snapshot(p,deadline)[0]!=before:
                raise ValueError('reference/identity')
            row['eligible_bytes']=amount
            if dry_run: row['status']='ELIGIBLE'
            elif api.remove_disposable(p,deadline=deadline) and not p.exists():
                row.update(status='REMOVED',bytes_freed=amount)
            else: row['status']='PARTIAL_UNKNOWN'
        except (OSError,ValueError,TimeoutError) as e: row.update(status='REFUSED',reason=str(e))
        out.append(row)
    return out

def idle_scratch(api,targets,*,pins=(),dry_run=True,deadline):
    """Only immediate scratch entries or glp-ultracode-nowrite tmp roots >24h idle."""
    from host_disk_large_classes import snapshot,lsof_inactive
    out=[]; cutoff=time.time()-86400; pins=custody_pins(pins)
    for target in targets:
        p=Path(target); row={'rule':'idle_scratch_tmp','path':str(p),'bytes_freed':0}
        try:
            allowed=(p.parent==Path('/home/ubuntu/.hermes/cache/scratch') or
                     (p.parent==Path('/tmp') and p.name.startswith('glp-ultracode-browser-nowrite-')))
            if not allowed or protected(p,pins) or p.is_symlink() or p.resolve()!=p.absolute():raise ValueError('scope/protection')
            # Directory atime moves whenever any scanner (including this pruner)
            # lists it; only regular-file atime is a use signal.
            if max(p.lstat().st_mtime,p.lstat().st_ctime,p.lstat().st_atime if p.is_file() else 0)>cutoff:raise ValueError('young/used')
            if dirty_worktree(p) or not inactive(api,p):raise ValueError('dirty/active')
            before,amount=snapshot(p,deadline)
            from host_disk_payload_guard import reject_database_payloads
            reject_database_payloads(p,before)
            for r in before:
                q=p/r[0];s=q.lstat()
                used=s.st_atime if stat.S_ISREG(s.st_mode) else 0
                if protected(q,pins) or max(used,s.st_mtime,s.st_ctime)>cutoff:raise ValueError('protected/young descendant')
            fresh_pins=custody_pins(pins)
            if any(protected(p/r[0],fresh_pins) for r in before): raise ValueError('new live symlink custody')
            if not lsof_inactive(p) or not inactive(api,p) or snapshot(p,deadline)[0]!=before:raise ValueError('reference/identity')
            if dry_run:row.update(status='ELIGIBLE',eligible_bytes=amount)
            elif api.remove_disposable(p,deadline=deadline) and not p.exists():row.update(status='REMOVED',bytes_freed=amount)
            else:row['status']='PARTIAL_UNKNOWN'
        except (OSError,ValueError,TimeoutError) as e:row.update(status='REFUSED',reason=str(e))
        out.append(row)
    return out

def verified_duplicates(api,targets,manifest,*,pins=(),dry_run=True,deadline):
    """Exact cold read-only duplicate files; retain one independent copy+locator.

    No mutable-source hardlinks. File identities, SHA256 and live references are
    rechecked before removal; unknown ownership/age/database cases stay.
    """
    groups={}; rows=[]; cutoff=time.time()-7*86400; pins=custody_pins(pins)
    for target in targets:
        p=Path(target)
        if time.monotonic()>deadline:raise TimeoutError('duplicates')
        if p.is_symlink() or protected(p,pins) or dirty_worktree(p):continue
        s=p.lstat()
        if not stat.S_ISREG(s.st_mode) or s.st_mode & 0o222 or s.st_nlink!=1 or max(s.st_mtime,s.st_ctime)>cutoff:continue
        fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW|os.O_NOATIME)
        with os.fdopen(fd,'rb') as f:
            header=f.read(16)
            if header==b'SQLite format 3\0' or header[8:12]==b'DUCK':continue
        sig=signature(p); digest=sha(p); key=(s.st_size,digest)
        if key not in groups:groups[key]=(p,sig);continue
        keep,keep_sig=groups[key]
        row={'rule':'verified_cold_duplicate','path':str(p),'retained_copy':str(keep),
             'sha256':digest,'bytes_freed':0}
        if (protected(p,custody_pins(pins)) or not inactive(api,p) or not inactive(api,keep) or signature(p)!=sig
            or signature(keep)!=keep_sig or sha(keep)!=digest or sha(p)!=digest):
            rows.append(dict(row,status='REFUSED'));continue
        if dry_run:row.update(status='ELIGIBLE',eligible_bytes=s.st_blocks*512)
        else:
            # Intent manifest is durable before the unlink, not merely a summary.
            write_json(manifest,rows+[dict(row,status='VERIFIED_REMOVAL_INTENT')])
            p.unlink();row.update(status='REMOVED',bytes_freed=s.st_blocks*512)
        rows.append(row)
    if not dry_run:write_json(manifest,rows)
    return rows

def verify_archive(archive,rows,listing):
    """Independent decompression: exact member set plus every SHA256 and metadata."""
    expected={r['name']:r for r in rows}; seen=set(); names=[]
    proc=subprocess.Popen(['zstd','-q','-d','-c',str(archive)],stdout=subprocess.PIPE)
    try:
        with tarfile.open(fileobj=proc.stdout,mode='r|') as tf:
            for m in tf:
                if m.name not in expected or m.name in seen or not m.isfile(): raise ValueError('member set/type')
                r=expected[m.name]; sig=r['signature']
                if (m.size,m.mode,m.uid,m.gid)!=(sig[3],stat.S_IMODE(sig[2]),sig[7],sig[8]): raise ValueError('metadata')
                if abs(m.mtime-sig[4]/1e9)>0.000001: raise ValueError('mtime')
                with tf.extractfile(m) as f:
                    if sha_stream(f)!=r['sha256']: raise ValueError('member hash')
                seen.add(m.name); names.append(m.name)
        # Drain padding/concatenated frames so decompressor status is meaningful.
        for _ in iter(lambda:proc.stdout.read(1024*1024),b''): pass
        proc.stdout.close()
        if proc.wait(timeout=30)!=0 or seen!=set(expected): raise ValueError('incomplete archive')
        Path(listing).write_text('\n'.join(names)+'\n')
    finally:
        if proc.poll() is None:
            proc.stdout.close(); proc.wait(timeout=30)

def cold_archive(api,root,destination,*,pins=(),dry_run=True,deadline,cold_days=7,min_bytes=0,reserve_bytes=0):
    """Compress explicit cold members, preserve protected leaves and directory skeletons."""
    root=Path(root); dest=Path(destination); pins=custody_pins(pins)
    row={'rule':'cold_evidence_archive','path':str(root),'bytes_freed':0}
    if root.is_symlink() or root.resolve()!=root.absolute() or protected(root,pins):
        return dict(row,status='REFUSED',reason='scope/protection')
    if dirty_worktree(root) or not inactive(api,root):
        return dict(row,status='REFUSED',reason='dirty/active')
    rows,skipped=inventory(root,pins,time.time()-cold_days*86400,deadline,hashes=False)
    # Reclaim a shared inode only when every original link is in this selection.
    links={};blocks={}
    for r in rows:
        key=tuple(r['signature'][:2]);links[key]=links.get(key,0)+1;blocks[key]=r
    amount=sum(r['allocated_bytes'] for key,r in blocks.items() if links[key]>=r['signature'][6])
    retained_shared=[r for r in rows if links[tuple(r['signature'][:2])]<r['signature'][6]]
    skipped.extend(str(root/r['name']) for r in retained_shared)
    rows=[r for r in rows if links[tuple(r['signature'][:2])]>=r['signature'][6]]
    row.update(eligible_bytes=amount,members=len(rows),protected_or_young=len(skipped))
    if dry_run or not rows: return dict(row,status='ELIGIBLE' if rows else 'EMPTY')
    if amount<min_bytes:return dict(row,status='BELOW_RECLAIM_FLOOR')
    if reserve_bytes:
        fs=os.statvfs(root)
        worst=sum(r['signature'][3]+65536 for r in rows)
        if fs.f_bavail*fs.f_frsize<worst+reserve_bytes:return dict(row,status='INSUFFICIENT_HEADROOM')
    for r in rows:
        if time.monotonic()>deadline:raise TimeoutError('hash deadline')
        p=root/r['name']
        if signature(p)!=r['signature']:raise ValueError('source changed before hashing')
        r['sha256']=sha(p)
        if signature(p)!=r['signature']:raise ValueError('source changed while hashing')
    if dest.exists(): raise ValueError('archive destination already exists')
    dest.parent.mkdir(parents=True,exist_ok=True)
    manifest=dest.with_suffix(dest.suffix+'.manifest.json')
    listing=dest.with_suffix(dest.suffix+'.listing.txt')
    ledger=dest.with_suffix(dest.suffix+'.retirement.jsonl')
    write_json(manifest,{'source':str(root),'members':rows,'retained':skipped,'cold_days':cold_days})
    tmp=dest.with_suffix(dest.suffix+'.partial')
    with tmp.open('xb') as output:
        proc=subprocess.Popen(['zstd','-q','-3','--long=27','-T1','-c'],stdin=subprocess.PIPE,stdout=output)
        try:
            with tarfile.open(fileobj=proc.stdin,mode='w|',format=tarfile.PAX_FORMAT,dereference=True) as tf:
                for r in rows:
                    p=root/r['name']
                    if signature(p)!=r['signature']: raise ValueError('source changed during build')
                    info=tf.gettarinfo(str(p),arcname=r['name'])
                    fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW|os.O_NOATIME)
                    with os.fdopen(fd,'rb') as f: tf.addfile(info,f)
            proc.stdin.close()
            if proc.wait(timeout=max(1,deadline-time.monotonic()))!=0: raise ValueError('compression failed')
            output.flush(); os.fsync(output.fileno())
        finally:
            if proc.poll() is None: proc.stdin.close(); proc.wait(timeout=30)
    if min_bytes and amount-(tmp.stat().st_blocks*512+manifest.stat().st_blocks*512+len(rows)*2048)<min_bytes:
        identity=signature(tmp)
        if not protected(tmp,custody_pins(pins)) and inactive(api,tmp) and signature(tmp)==identity:
            tmp.unlink()
            return dict(row,status='GATED_NO_NET_SAVINGS',bytes_freed=-manifest.stat().st_blocks*512)
        return dict(row,status='GATED_TEMP_ACTIVE_OR_UNKNOWN',bytes_freed=0)
    os.replace(tmp,dest)
    verify_archive(dest,rows,listing)
    archive_sha=sha(dest)
    # All identities checked before first effect; check again per unlink below.
    fresh_pins=custody_pins(pins)
    if any(protected(root/r['name'],fresh_pins) for r in rows): raise ValueError('new live symlink custody')
    if not inactive(api,root) or dirty_worktree(root): raise ValueError('new active/dirty reference')
    if any(signature(root/r['name'])!=r['signature'] for r in rows): raise ValueError('source changed before retire')
    verify={'archive_sha256':archive_sha,'manifest_sha256':sha(manifest),
            'listing_sha256':sha(listing),'verified_members':len(rows),'verified':True}
    write_json(dest.with_suffix(dest.suffix+'.verified.json'),verify)
    freed=0; own_unlinks={}
    with ledger.open('x') as f:
        for r in rows:
            if time.monotonic()>deadline: raise TimeoutError('retirement')
            p=root/r['name'];current=signature(p);expected=r['signature'];key=tuple(expected[:2])
            own=own_unlinks.get(key,0)
            # Our earlier hardlink unlink changes nlink/ctime only. Any other
            # drift still vetoes; verify exact bytes when ctime legitimately moves.
            if (current[:5]!=expected[:5] or current[7:]!=expected[7:] or current[6]!=expected[6]-own
                or (not own and current[5]!=expected[5]) or (own and sha(p)!=r['sha256'])):
                raise ValueError('changed before unlink')
            p.unlink();own_unlinks[key]=own+1
            n=r['allocated_bytes'] if current[6]==1 else 0; freed+=n
            f.write(json.dumps({'source':str(p),'member':r['name'],'sha256':r['sha256'],'freed_bytes':n})+'\n')
        f.flush(); os.fsync(f.fileno())
    overhead=sum(x.stat().st_blocks*512 for x in [dest,manifest,listing,ledger,dest.with_suffix(dest.suffix+'.verified.json')])
    row.update(status='ARCHIVED_VERIFIED_RETIRED',gross_bytes_freed=freed,overhead_bytes=overhead,
               bytes_freed=freed-overhead,archive=str(dest),manifest=str(manifest),**verify)
    return row
