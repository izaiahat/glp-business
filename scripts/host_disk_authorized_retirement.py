#!/usr/bin/env python3
"""Explicit D1b authority: completed archives and superseded site members.

Not a scheduler change. Legacy 30-day R2 authority stays unchanged. Uses the
existing singleton and refreshes canonical/rollback/PR/scheduler custody before
and after each verified remote transfer. Partial recovery archives remain local.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parent))
import host_disk_cold_classes as cold
from host_disk_r2 import BUCKET, PREFIX, Destination, Control, credentials, digest

BUS=Path('/home/ubuntu/business')
SITE=BUS/'site-system/flagship-v2'
BASE=BUS/'reports/glp-clinic-scout/seo-machine/current-content-base.json'
AUTHORITY='D1b-expanded-operator-retirement-20261012'
_CANONICAL_ROOTS=()
_SITE_PIN_CACHE={}


def store_pair(destination, archive, manifest, deadline):
    """Immutable key reconciliation plus full GET, never HEAD/ETag-only proof."""
    if destination.private_check() is not True:raise ValueError('private custody')
    ah=digest(archive);mh=digest(manifest)
    objects=[]
    for path,key,sha,role in ((archive,PREFIX+ah+'.tar.zst',ah,'archive'),
                            (manifest,PREFIX+ah+'-'+mh+'.manifest.json',mh,'manifest')):
        result=destination.readback(key,deadline,missing=True)
        created=result is None
        if created:
            destination.upload(path,key,deadline)
            result=destination.readback(key,deadline)
        if result['sha256']!=sha or result['bytes']!=Path(path).stat().st_size:
            raise ValueError('remote binding mismatch')
        objects.append(dict(result,role=role,created=created))
    if destination.private_check() is not True:raise ValueError('private custody')
    return {'bucket':BUCKET,'private':True,'verified':True,'objects':objects,
            'object_count':2,'object_bytes':sum(o['bytes'] for o in objects),
            'authority':AUTHORITY}


def site_protected(p,pins,root):
    """Only the obsolete seal-name veto is superseded; all named holds remain."""
    p=Path(p);root=Path(root)
    if not isinstance(pins,(tuple,frozenset)):pins=tuple(pins)
    if p!=root and root not in p.parents:return True
    structural={SITE,SITE/'candidates',SITE/'releases',SITE/'work'} if SITE in root.parents else set()
    key=(id(pins),tuple(sorted(map(str,structural))))
    cached=_SITE_PIN_CACHE.get(key)
    if cached is None or cached[0] is not pins:
        if len(_SITE_PIN_CACHE)>32:_SITE_PIN_CACHE.clear()
        cached=(pins,frozenset(str(p) for p in pins if Path(p) not in structural))
        _SITE_PIN_CACHE[key]=cached
    pinset=cached[1]
    if any(str(q) in pinset for q in (p,*p.parents)):return True
    # Strip ONLY seal/sealed prefixes inside this explicitly scoped tree. The
    # baseline predicate still handles databases, backups, finance, R8 and secrets.
    pieces=list(p.parts)
    for i in range(len(root.parts)-1,len(pieces)):
        if pieces[i].startswith(('sealed','seal-')):pieces[i]='retired-'+pieces[i]
    return cold.protected(Path(*pieces),())


def fresh_pins():
    """Read the real registry through its authorized non-root identity."""
    global _CANONICAL_ROOTS
    code="import sys,json;sys.path.insert(0,'/home/ubuntu/business/scripts');import host_disk_auto_classes as a;p=a.pr_pins()|a.worktree_pins()|a.live_release_pins()|a.r8_pins()|a.linear_pins();print(json.dumps(sorted(map(str,p))))"
    cmd=[sys.executable,'-c',code]
    if os.geteuid()==0:cmd=['sudo','-n','-H','-u','ubuntu',*cmd]
    result=subprocess.run(cmd,capture_output=True,text=True,timeout=300,check=True)
    pins={Path(p) for p in json.loads(result.stdout)}
    before=BASE.read_bytes();base=json.loads(before)
    canonical=Control(credentials()).request('GET','/pages/projects/glp-clinic-scout').get('canonical_deployment') or {}
    if (base.get('status')!='PASS' or canonical.get('id')!=base.get('deployment_id')
        or canonical.get('environment')!='production'
        or (canonical.get('latest_stage') or {}).get('status')!='success'
        or BASE.read_bytes()!=before):raise ValueError('canonical/base custody')
    roles=[]
    for record in (base,base.get('supersedes') or {}):
        root=Path(record.get('release_root',''))
        if not root.is_absolute() or not root.is_dir():raise ValueError('production/rollback root missing')
        roles.append(root);pins.add(root);pins.add(root.parent.parent)
    _CANONICAL_ROOTS=tuple(roles)
    from host_disk_large_sweep import references
    gate=BUS/'reports/ops/fable-plan-2-20261009/lanes/D1b/reference-gate'
    gate.mkdir(parents=True,exist_ok=True)
    pins.update(references(gate))
    profiles=Path('/home/ubuntu/.hermes/profiles')
    if profiles.is_dir():
        for profile in profiles.iterdir():
            if profile.is_dir():pins.update(cold.live_symlink_targets(home=profile,max_depth=3))
    return cold.custody_pins(pins)


class SiteAuthority:
    def __init__(self,root,destination):self.root=Path(root);self.destination=destination
    def admit(self,root,days):
        root=Path(root)
        if root!=self.root or days!=0 or root.parent not in tuple(SITE/k for k in ('candidates','releases','work')):
            raise ValueError('site authority scope')
        pins=fresh_pins()
        if any(root==p or root in p.parents or p in root.parents for p in _CANONICAL_ROOTS):
            raise ValueError('current production or rollback custody')
        if root.is_symlink() or root.resolve()!=root.absolute() or site_protected(root,pins,root):
            raise ValueError('current rollback PR scheduler or named custody')
        return {'scope':'superseded_site','authority':AUTHORITY,'root':str(root)}
    def protect(self,p,pins):return site_protected(p,pins,self.root)
    def store(self,archive,manifest,deadline):
        self.admit(self.root,0)
        return store_pair(self.destination,archive,manifest,deadline)


def completed_archive(api,p,destination,report,deadline,dry_run):
    p=Path(p);row={'rule':'authorized_completed_archive_r2','path':str(p),'bytes_freed':0}
    manifest=Path(str(p)+'.manifest.json');verified=Path(str(p)+'.verified.json');ledger=Path(str(p)+'.retirement.jsonl')
    if not all(q.is_file() and not q.is_symlink() for q in (p,manifest,verified,ledger)):
        return dict(row,status='RETAINED_INCOMPLETE_RECOVERY_CONTROLS')
    data=json.loads(manifest.read_text());proof=json.loads(verified.read_text())
    members=data['members'];expected={r['name']:r['sha256'] for r in members}
    retired={}
    for line in ledger.read_text().splitlines():
        record=json.loads(line)
        if record['member'] in retired:raise ValueError('duplicate retirement ledger')
        retired[record['member']]=record['sha256']
    if retired!=expected:return dict(row,status='RETAINED_PARTIAL_RETIREMENT_RECOVERY')
    if proof.get('verified') is not True or digest(p)!=proof['archive_sha256'] or digest(manifest)!=proof['manifest_sha256']:
        raise ValueError('local verification drift')
    pins=fresh_pins()
    source=Path(data['source'])
    if any(site_protected(source/r['name'],pins,source) for r in members) or any(source==q or source in q.parents or q in source.parents for q in _CANONICAL_ROOTS):
        raise ValueError('archive member live or named custody')
    if (p.parent.resolve()!=p.parent.absolute() or p.stat().st_nlink!=1
        or site_protected(p.with_suffix('.ordinary'),pins,p.parent)
        or not cold.inactive(api,p)):raise ValueError('archive live/named custody')
    identity=cold.signature(p)
    controls={q:cold.signature(q) for q in (manifest,verified,ledger)}
    if dry_run:return dict(row,status='ELIGIBLE',eligible_bytes=p.stat().st_blocks*512)
    cold.verify_archive(p,members,report/(digest(p)+'.listing.txt'))
    remote=store_pair(destination,p,manifest,deadline)
    pins=fresh_pins()
    if any(site_protected(source/r['name'],pins,source) for r in members) or any(source==q or source in q.parents or q in source.parents for q in _CANONICAL_ROOTS):
        raise ValueError('new archive member live or named custody')
    if (cold.signature(p)!=identity or any(cold.signature(q)!=s for q,s in controls.items())
        or digest(manifest)!=proof['manifest_sha256']
        or p.parent.resolve()!=p.parent.absolute()
        or site_protected(p.with_suffix('.ordinary'),pins,p.parent)
        or not cold.inactive(api,p)):raise ValueError('new archive custody/drift')
    proof['remote']=remote;cold.write_json(verified,proof)
    amount=p.stat().st_blocks*512;p.unlink()
    return dict(row,status='MIGRATED_R2_VERIFIED',bytes_freed=amount,remote=remote)


def ranked_roots(parent):
    result=subprocess.run(['du','-x','-B1','-d','1',str(parent)],capture_output=True,text=True,timeout=180,check=True)
    rows=[]
    for line in result.stdout.splitlines():
        size,sep,name=line.partition('\t');path=Path(name)
        if sep and size.isdigit() and path.parent==parent:rows.append((int(size),path))
    return [p for size,p in sorted(rows,reverse=True)]


def cleanup_lease(path,inherited=None):
    path=Path(path)
    fd=inherited if inherited is not None else os.open(path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        held=os.fstat(fd);expected=path.lstat()
        if not stat.S_ISREG(held.st_mode) or (held.st_dev,held.st_ino)!=(expected.st_dev,expected.st_ino):
            raise ValueError('singleton lease identity')
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        return fd
    except Exception:
        if inherited is None:os.close(fd)
        raise


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--operator-authority',required=True,choices=[AUTHORITY])
    parser.add_argument('--report',required=True)
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--budget',type=int,default=7200)
    parser.add_argument('--lease-fd',type=int,help='inherited singleton descriptor for preview/apply custody')
    args=parser.parse_args();report=Path(args.report)
    lock_path=Path('/home/ubuntu/.hermes/runstate/host-disk-cleanup.lock')
    lock_fd=cleanup_lease(lock_path,args.lease_fd)
    report.mkdir(parents=True,exist_ok=False)
    sys.path.append(str(BUS/'ops'))
    sys.path.append(str(BUS/'scripts'))
    import host_disk_pruner as api
    destination=Destination.from_environment();deadline=time.monotonic()+args.budget
    rows=[];before=os.statvfs('/')
    def save():
        after=os.statvfs('/')
        cold.write_json(report/'RESULT.json',{'authority':AUTHORITY,'dry_run':not args.apply,
            'before_free_bytes':before.f_bavail*before.f_frsize,'after_free_bytes':after.f_bavail*after.f_frsize,
            'total_bytes':after.f_blocks*after.f_frsize,'rows':rows,
            'attributable_bytes_freed':sum(r.get('bytes_freed',0) for r in rows)})
    save()
    for p in sorted((BUS/'reports/host-disk-prune').rglob('*.tar.zst')):
        if time.monotonic()+60>=deadline:break
        try:rows.append(completed_archive(api,p,destination,report,deadline,not args.apply))
        except Exception as error:rows.append({'rule':'authorized_completed_archive_r2','path':str(p),'bytes_freed':0,'status':'REFUSED','reason':str(error)})
        save()
    for kind in ('candidates','releases','work'):
        parent=SITE/kind
        if not parent.is_dir():continue
        try:roots=ranked_roots(parent)
        except Exception as error:
            rows.append({'rule':'authorized_site_ranking','path':str(parent),'bytes_freed':0,'status':'REFUSED','reason':str(error)});save();continue
        for root in roots:
            fs=os.statvfs('/')
            if args.apply and fs.f_bavail/fs.f_blocks>=0.15:break
            if time.monotonic()+120>=deadline:break
            if root.is_symlink() or not root.is_dir():continue
            authority=SiteAuthority(root,destination);index=0
            while time.monotonic()+120<deadline:
                index+=1
                dest=report/'archives'/f'{kind}-{root.name}-{index}.tar.zst'
                try:
                    pins=fresh_pins()
                    row=cold.cold_archive(api,root,dest,pins=pins,dry_run=not args.apply,deadline=deadline,
                        cold_days=0,remote_authority=authority,max_archive_input_bytes=4*1024**3,
                        min_bytes=1024**2,min_net_bytes=1024**2,reserve_bytes=1024**3)
                    row['rule']='authorized_superseded_site_r2';rows.append(row);save()
                    fs=os.statvfs('/')
                    if args.apply and fs.f_bavail/fs.f_blocks>=0.15:break
                    if not args.apply or not row.get('gross_bytes_freed') or not row.get('deferred_for_headroom_members'):break
                except Exception as error:
                    rows.append({'rule':'authorized_superseded_site_r2','path':str(root),'bytes_freed':0,'status':'REFUSED','reason':str(error)});save();break
    cold.write_json(report/'R2-SUMMARY.json',destination.summary())
    save();print(json.dumps({'report':str(report),'bytes_freed':sum(r.get('bytes_freed',0) for r in rows)}))


if __name__=='__main__':main()
