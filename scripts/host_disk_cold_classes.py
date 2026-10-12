#!/usr/bin/env python3
"""Explicit-target maintenance classes; never scheduled/import-time effects.

Callers supply a complete, live protection/reference inventory and disjoint targets.
No scheduler adoption while the older singleton is running. Evidence is archived,
not shared with writable source inodes. Fail closed on every process/identity gate.
"""
import hashlib
import json
import os
import re
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

def existing_pins(pins):
    """Drop stale narrative/administrative paths; unreadable custody fails closed.

    Keep dangling symlinks themselves. Resolved live symlink targets are added
    separately and remain protected regardless of current target availability.
    """
    live = set()
    for p in map(Path, pins):
        try:
            p.lstat()
        except FileNotFoundError:
            continue
        live.add(p)
    return live


def custody_pins(pins):
    from host_disk_custody import collect
    live=existing_pins(set(pins)|collect())
    # Resolve current alias chains again at every gate, not from cached prose.
    live.update(p.resolve(strict=False) for p in tuple(live))
    return tuple(live)+tuple(live_symlink_targets())

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
    return (referenced or p.name.endswith('.tar.zst.partial') or
            p.suffix.lower() in {'.db','.sqlite','.sqlite3','.duckdb','.db-wal','.db-shm','.db-journal','.sqlite-wal','.sqlite-shm','.sqlite-journal','.sqlite3-wal','.sqlite3-shm','.zst','.tar','.tgz','.gz'} or
            any(re.search(r'(^|[^a-z0-9])r8([^a-z0-9]|$)',n.lower()) or n in PROTECTED or n.startswith(('.env','tmux-','.org.chromium.',
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

def inventory(root,pins,cutoff,deadline,*,hashes,protection=None):
    """Select only cold files; no directory traversal through symlinks/protection."""
    root=Path(root); rows=[]; skipped=[]; stack=[root]; device=root.lstat().st_dev
    protect=protection or protected
    while stack:
        if time.monotonic()>deadline: raise TimeoutError('inventory')
        p=stack.pop()
        if protect(p,pins): skipped.append(str(p)); continue
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

def discard_staging(api, owned):
    """Only this transaction's new ordinary inodes, never evidence/custody.

    Report-root narrative pins intentionally cover evidence, not our exclusive
    uncommitted output. Live symlink custody and independent process checks still
    veto rollback. Call only before a retirement ledger exists.
    """
    runtime_pins=custody_pins(())
    for p, identity in owned.items():
        if not p.exists():
            continue
        current = signature(p)
        live = live_symlink_targets()
        if (current[:3] != identity[:3] or current[6] != 1
                or not stat.S_ISREG(current[2])
                or any(p == q or q in p.parents or p in q.parents for q in live)
                or any(p == q or q in p.parents for q in runtime_pins)
                or not inactive(api, p)):
            raise ValueError('staging rollback custody/identity refused')
        if signature(p) != current:
            raise ValueError('staging rollback changed')
        p.unlink()


def cleanup_abandoned_archives(api, prune_root, *, source_roots, dry_run, deadline):
    """Recover pre-retirement staging only; manifests prove originals untouched.

    This runs under the controller singleton. Crash leftovers lacking a complete
    original-identity/hash proof, or ANY retirement/verification custody, stay.
    """
    result=[]
    for partial in sorted(Path(prune_root).glob('*/large/archives/*.tar.zst.partial')):
        if time.monotonic()>deadline: break
        dest=Path(str(partial).removesuffix('.partial'))
        manifest=Path(str(dest)+'.manifest.json')
        row={'rule':'abandoned_archive_staging','path':str(partial),'bytes_freed':0,'ok':True}
        try:
            siblings=[dest,Path(str(dest)+'.retirement.jsonl'),Path(str(dest)+'.verified.json')]
            if any(p.exists() or p.is_symlink() for p in siblings):
                raise ValueError('retirement/recovery custody')
            if partial.is_symlink() or manifest.is_symlink() or partial.parent.resolve()!=partial.parent.absolute():
                raise ValueError('staging symlink custody')
            before={p:signature(p) for p in (partial,manifest)}
            data=json.loads(manifest.read_text());root=Path(data['source']);members=data['members']
            if not root.is_absolute() or not any(root==q or q in root.parents for q in map(Path,source_roots)):
                raise ValueError('unadmitted source')
            if root.resolve()!=root.absolute() or not members or dirty_worktree(root) or not inactive(api,root):
                raise ValueError('source active/dirty/symlink/unknown')
            seen=set()
            for member in members:
                name=Path(member['name']);p=root/name
                if name.is_absolute() or '..' in name.parts or str(name) in seen:
                    raise ValueError('invalid/duplicate member')
                seen.add(str(name))
                if time.monotonic()>deadline:raise TimeoutError('abandoned source proof deadline')
                if p.resolve()!=p.absolute() or signature(p)!=member['signature'] or sha(p)!=member['sha256']:
                    raise ValueError('original missing/changed')
                if signature(p)!=member['signature']:raise ValueError('original changed during hash')
            if any(signature(p)!=identity for p,identity in before.items()):
                raise ValueError('staging changed')
            owned=dict(before)
            listing=Path(str(dest)+'.listing.txt')
            if listing.exists():owned[listing]=signature(listing)
            amount=sum(p.stat().st_blocks*512 for p in owned)
            if dry_run:
                row.update(status='ELIGIBLE_ABANDONED_STAGING',eligible_bytes=amount)
            else:
                discard_staging(api,owned)
                row.update(status='ABANDONED_STAGING_REMOVED',bytes_freed=amount)
        except (OSError,ValueError,KeyError,TypeError,TimeoutError) as exc:
            row.update(status='REFUSED',reason=str(exc))
        result.append(row)
    return result


def cold_archive(api,root,destination,*,pins=(),dry_run=True,deadline,cold_days=7,min_bytes=0,reserve_bytes=0,min_net_bytes=None,archive_destination=None,max_archive_input_bytes=None,remote_authority=None):
    """Compress explicit cold members, preserve protected leaves and directory skeletons."""
    root=Path(root); dest=Path(destination); pins=custody_pins(pins)
    protect=protected
    authority=None
    if remote_authority is not None:
        authority=remote_authority.admit(root,cold_days)
        if not isinstance(authority,dict) or authority.get('scope')!='superseded_site':
            raise ValueError('explicit site retirement authority missing')
        if archive_destination is not None:raise ValueError('ambiguous remote authority')
        archive_destination=remote_authority
        protect=remote_authority.protect
    if archive_destination is not None and cold_days<30 and authority is None:
        raise ValueError('remote archive requires 30d cold scope')
    row={'rule':'cold_evidence_archive','path':str(root),'bytes_freed':0}
    if root.is_symlink() or root.resolve()!=root.absolute() or protect(root,pins):
        return dict(row,status='REFUSED',reason='scope/protection')
    if dirty_worktree(root) or not inactive(api,root):
        return dict(row,status='REFUSED',reason='dirty/active')
    rows,skipped=inventory(root,pins,time.time()-cold_days*86400,deadline,hashes=False,protection=protect)
    # Reclaim a shared inode only when every original link is in this selection.
    links={};blocks={}
    for r in rows:
        key=tuple(r['signature'][:2]);links[key]=links.get(key,0)+1;blocks[key]=r
    amount=sum(r['allocated_bytes'] for key,r in blocks.items() if links[key]>=r['signature'][6])
    retained_shared=[r for r in rows if links[tuple(r['signature'][:2])]<r['signature'][6]]
    skipped.extend(str(root/r['name']) for r in retained_shared)
    rows=[r for r in rows if links[tuple(r['signature'][:2])]>=r['signature'][6]]
    if max_archive_input_bytes is not None:
        row['eligible_bytes_total']=amount
        groups={}
        for r in rows:groups.setdefault(tuple(r['signature'][:2]),[]).append(r)
        selected=[];deferred=[];used=0
        for group in sorted(groups.values(),key=lambda group:min(r['name'] for r in group)):
            cost=sum(r['signature'][3]+65536 for r in group)
            if used+cost>max_archive_input_bytes:deferred.extend(group)
            else:selected.extend(group);used+=cost
        rows=selected
        skipped.extend(str(root/r['name']) for r in deferred)
        row['deferred_for_headroom_members']=len(deferred)
        selected_keys={tuple(r['signature'][:2]) for r in rows}
        amount=sum(blocks[key]['allocated_bytes'] for key in selected_keys)
    row.update(eligible_bytes=amount,members=len(rows),protected_or_young=len(skipped))
    if not rows:return dict(row,status='INSUFFICIENT_HEADROOM' if row.get('deferred_for_headroom_members') else 'EMPTY')
    if dry_run:return dict(row,status='ELIGIBLE')
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
    tmp=dest.with_suffix(dest.suffix+'.partial')
    verified=dest.with_suffix(dest.suffix+'.verified.json')
    outputs=[tmp,dest,manifest,listing,verified,ledger]
    if any(p.exists() or p.is_symlink() for p in outputs):
        raise ValueError('archive transaction already exists')
    if dest.parent.resolve()!=dest.parent.absolute():
        raise ValueError('archive destination symlink custody')
    owned={}; committed=False
    try:
        with manifest.open('x') as f:
            owned[manifest]=signature(manifest)
            json.dump({'source':str(root),'members':rows,'retained':skipped,'cold_days':cold_days},f)
            f.flush(); os.fsync(f.fileno())
        with tmp.open('xb') as output:
            owned[tmp]=signature(tmp)
            proc=subprocess.Popen(['zstd','-q','-3','--long=27','-T1','-c'],stdin=subprocess.PIPE,stdout=output)
            try:
                with tarfile.open(fileobj=proc.stdin,mode='w|',format=tarfile.PAX_FORMAT,dereference=True) as tf:
                    for r in rows:
                        if time.monotonic()>deadline: raise TimeoutError('build deadline')
                        p=root/r['name']
                        if signature(p)!=r['signature']: raise ValueError('source changed during build')
                        info=tf.gettarinfo(str(p),arcname=r['name'])
                        fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW|os.O_NOATIME)
                        with os.fdopen(fd,'rb') as f: tf.addfile(info,f)
                proc.stdin.close()
                if proc.wait(timeout=max(1,deadline-time.monotonic()))!=0: raise ValueError('compression failed')
                output.flush(); os.fsync(output.fileno())
            finally:
                # Drain the compressor naturally; never kill a live archive writer.
                if not proc.stdin.closed: proc.stdin.close()
                proc.wait()
        net_floor=min_bytes if min_net_bytes is None else min_net_bytes
        local_net=amount-(tmp.stat().st_blocks*512+manifest.stat().st_blocks*512+len(rows)*2048)
        if net_floor and local_net<net_floor and archive_destination is None:
            return dict(row,status='GATED_NO_NET_SAVINGS',bytes_freed=0)
        with listing.open('x') as f:
            owned[listing]=signature(listing)
        verify_archive(tmp,rows,listing)
        archive_sha=sha(tmp)
        # Verification is complete before publishing the archive or retiring originals.
        fresh_pins=custody_pins(pins)
        if any(protect(root/r['name'],fresh_pins) for r in rows): raise ValueError('new live symlink custody')
        if not inactive(api,root) or dirty_worktree(root): raise ValueError('new active/dirty reference')
        if any(signature(root/r['name'])!=r['signature'] for r in rows): raise ValueError('source changed before retire')
        verify={'archive_sha256':archive_sha,'manifest_sha256':sha(manifest),
                'listing_sha256':sha(listing),'verified_members':len(rows),'verified':True}
        with verified.open('x') as f:
            owned[verified]=signature(verified)
            json.dump(verify,f);f.flush();os.fsync(f.fileno())
        os.replace(tmp,dest);owned.pop(tmp);owned[dest]=signature(dest)
        fd=os.open(dest.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
        remote=None
        if archive_destination is not None:
            try:
                receipt=archive_destination.store(dest,manifest,deadline)
                if receipt.get('verified') is not True or receipt.get('private') is not True:
                    raise ValueError('unverified remote destination')
                if len(receipt.get('objects',[]))!=2 or any(r.get('verified') is not True for r in receipt['objects']):
                    raise ValueError('remote object readback proof missing')
                expected={'archive':(archive_sha,dest.stat().st_size),'manifest':(verify['manifest_sha256'],manifest.stat().st_size)}
                objects={r['role']:r for r in receipt['objects']}
                if set(objects)!=set(expected) or any((objects[k]['sha256'],objects[k]['bytes'])!=v for k,v in expected.items()):
                    raise ValueError('remote archive/manifest binding mismatch')
                remote=receipt
            except Exception as exc:
                row['remote_fallback']={'error_type':type(exc).__name__,'local_verified':True}
            if sha(dest)!=archive_sha or sha(manifest)!=verify['manifest_sha256']:
                raise ValueError('archive/manifest drift after remote attempt')
            # Upload/readback can take minutes: refresh every source/reference gate.
            fresh_pins=custody_pins(pins)
            scopes={root}
            for member in rows:
                scopes.update(p for p in (root/member['name']).parents if p==root or root in p.parents)
            if any(p.is_symlink() or p.resolve()!=p.absolute() for p in scopes):
                raise ValueError('source symlink/scope drift after upload')
            if any(protect(root/r['name'],fresh_pins) for r in rows):
                raise ValueError('new custody after remote attempt')
            if not inactive(api,root) or dirty_worktree(root):
                raise ValueError('new active/dirty after remote attempt')
            if any(signature(root/r['name'])!=r['signature'] for r in rows):
                raise ValueError('source drift after remote attempt')
            if authority is not None:
                remote_authority.admit(root,cold_days)
                if not remote:
                    return dict(row,status='REMOTE_REQUIRED_ORIGINALS_RETAINED')
            if remote:
                verify['remote']=remote
                write_json(verified,verify)
                owned[verified]=signature(verified)
        if remote and net_floor:
            controls=sum(p.stat().st_blocks*512 for p in (manifest,listing,verified))+len(rows)*2048
            if amount-controls<net_floor:
                return dict(row,status='GATED_NO_NET_SAVINGS',bytes_freed=0,remote=remote)
        freed=0; own_unlinks={}
        if not remote and net_floor and local_net<net_floor:
            return dict(row,status='GATED_NO_NET_SAVINGS',bytes_freed=0)
        if time.monotonic()>deadline: raise TimeoutError('pre-retirement deadline')
        with ledger.open('x') as f:
            for r in rows:
                if time.monotonic()>deadline: raise TimeoutError('retirement')
                p=root/r['name'];current=signature(p);expected=r['signature'];key=tuple(expected[:2])
                own=own_unlinks.get(key,0)
                # Only exact self-caused link-count changes may relax ctime.
                if (current[:5]!=expected[:5] or current[7:]!=expected[7:] or current[6]!=expected[6]-own
                    or (not own and current[5]!=expected[5]) or (own and sha(p)!=r['sha256'])):
                    raise ValueError('changed before unlink')
                p.unlink();own_unlinks[key]=own+1
                n=r['allocated_bytes'] if current[6]==1 else 0; freed+=n
                f.write(json.dumps({'source':str(p),'member':r['name'],'sha256':r['sha256'],'freed_bytes':n})+'\n')
                f.flush();os.fsync(f.fileno())
        if remote:
            fresh=custody_pins(())
            held=(signature(dest)!=owned[dest] or any(dest==q or q in dest.parents for q in fresh) or not inactive(api,dest))
            if not held:dest.unlink()
            row.update(rule='cold_evidence_r2_archive',remote=remote,local_archive_retained=held)
        overhead=sum(x.stat().st_blocks*512 for x in [dest,manifest,listing,ledger,verified] if x.exists())
        row.update(status=(('ARCHIVED_R2_VERIFIED_RETIRED_LOCAL_HELD' if row.get('local_archive_retained') else 'ARCHIVED_R2_VERIFIED_RETIRED') if remote else 'ARCHIVED_VERIFIED_RETIRED'),gross_bytes_freed=freed,overhead_bytes=overhead,
                   bytes_freed=freed-overhead,archive=str(dest),manifest=str(manifest),**verify)
        committed=True
        return row
    finally:
        # Even an empty retirement ledger retains recovery custody. Never roll
        # back a partially retired source, including on timeout/ENOSPC/signals.
        if not committed and not ledger.exists():
            discard_staging(api,owned)
