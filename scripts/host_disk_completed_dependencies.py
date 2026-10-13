"""Finite completed-CI npm dependencies, proved against exact cached tarballs.

Only explicit policy rows are admitted. Source worktrees remain pinned for every
legacy class. This class collects all *other* pin sources freshly; it never
subtracts a flattened pin union. No age, merge or vendor-name deletion authority.
"""
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tarfile
import time

from host_disk_large_classes import lsof_inactive
from host_disk_cold_classes import signature

BUS = Path('/home/ubuntu/business')
CACHE = Path('/home/ubuntu/.npm/_cacache/content-v2/sha512')
AUTHORITY = 'D3-completed-transient-ci-dependencies-20261013'


def read_bytes(p):
    fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_NOATIME)
    with os.fdopen(fd, 'rb') as f:
        return f.read()


def digest(p):
    return hashlib.sha256(read_bytes(p)).hexdigest()


def command(args):
    r = subprocess.run(args, capture_output=True, text=True, timeout=30)
    if r.returncode:
        raise ValueError('owner Git unavailable')
    return r.stdout.strip()


def owner_identity(owner, entry):
    if owner.parent != BUS/'.worktrees' or owner.resolve() != owner.absolute():
        raise ValueError('finite worktree scope')
    g = ['git', '-c', 'safe.directory='+str(owner), '-c', 'core.fsmonitor=false', '-C', str(owner)]
    if command(g+['rev-parse','HEAD']) != entry['owner_head']:
        raise ValueError('owner head drift')
    if command(g+['ls-files','--','node_modules']):
        raise ValueError('tracked dependency material')
    # Source may intentionally contain accepted build deltas: retain it unchanged.
    if command(g+['status','--porcelain','--','package.json','package-lock.json']):
        raise ValueError('dependency source dirty')


def fresh_pins():
    import host_disk_auto_classes as auto
    import host_disk_cold_classes as cold
    if os.geteuid()==0:
        source=str(Path(__file__).resolve().parent)
        program=(f'import sys,json;sys.path.insert(0,{source!r});'
                 'import host_disk_auto_classes as a;'
                 'print(json.dumps(sorted(map(str,a.external_pins(include_worktrees=False)))))')
        r=subprocess.run(['sudo','-n','-H','-u','ubuntu',sys.executable,'-I','-B','-c',program],
                         capture_output=True,text=True,timeout=180)
        if r.returncode:raise ValueError('incomplete dependency pin inventory')
        data=json.loads(r.stdout)
        if not isinstance(data,list) or any(not isinstance(p,str) or not p.startswith('/') for p in data):
            raise ValueError('invalid dependency pin inventory')
        pins={Path(p) for p in data}
    else:
        pins=auto.external_pins(include_worktrees=False)
    return cold.custody_pins(pins)


def intersects(root, pins):
    return any(root == p or root in p.parents or p in root.parents for p in map(Path,pins))


def inbound_links(root, deadline):
    """No-follow all operational reuse locations; unknown/deadline refuses.

    Skip Git internals and this exact candidate only; inspect links inside other
    dependency environments too (npm link / Python import reuse is custody).
    Candidate-internal npm bins are validated against package.json separately.
    """
    roots = [BUS/'.worktrees', BUS/'worktrees', BUS/'site-system', BUS/'reports',
             Path('/home/ubuntu/.hermes')]
    found = set()
    stack = [p for p in roots if p.exists()]
    while stack:
        if time.monotonic() >= deadline:
            raise TimeoutError('symlink custody inventory')
        p = stack.pop()
        if p.is_symlink():
            target = p.resolve(strict=False)
            if target == root or root in target.parents or target in root.parents:
                found.add(target)
            continue
        if p==root or p.name=='.git':
            continue
        if p.is_dir():
            with os.scandir(p) as es:
                stack.extend(Path(e.path) for e in es if e.is_symlink() or e.is_dir(follow_symlinks=False))
    return found


def check_deadline(deadline):
    if time.monotonic() >= deadline:
        raise TimeoutError('dependency verification deadline')


def safe_relative(name):
    p = PurePosixPath(name)
    if p.is_absolute() or not p.parts or any(x in {'.','..'} for x in p.parts) or str(p) != name:
        raise ValueError('unsafe package member')
    return p


def ownership(root, entry, deadline, cache):
    """Exact every-member content proof; no extraction, network or package code."""
    owner = root.parent
    for p,key in [(owner/'package-lock.json','lock_sha256'),(owner/'package.json','package_sha256'),
                  (root/'.package-lock.json','installed_lock_sha256')]:
        if digest(p) != entry[key]:
            raise ValueError('exact manifest drift')
    lock=json.loads(read_bytes(owner/'package-lock.json'))
    installed=json.loads(read_bytes(root/'.package-lock.json'))
    if lock.get('lockfileVersion') not in (2,3) or installed.get('lockfileVersion') not in (2,3):
        raise ValueError('unsupported npm lock')
    expected={'.package-lock.json':entry['installed_lock_sha256']}
    bins={}
    for name, package in installed['packages'].items():
        check_deadline(deadline)
        safe_relative(name)
        if not name.startswith('node_modules/') or name not in lock['packages']:
            raise ValueError('unowned installed package')
        declared=lock['packages'][name]
        for field in ('version','integrity','resolved'):
            if package.get(field) != declared.get(field):
                raise ValueError('package lock identity')
        if not str(declared.get('resolved','')).startswith('https://registry.npmjs.org/'):
            raise ValueError('nonregistry dependency')
        sri=declared.get('integrity','')
        if not re.fullmatch(r'sha512-[A-Za-z0-9+/]+={0,2}',sri):
            raise ValueError('missing package integrity')
        raw=base64.b64decode(sri[7:],validate=True)
        if len(raw)!=64:raise ValueError('invalid package integrity')
        hexd=raw.hex();path=Path(cache)/hexd[:2]/hexd[2:4]/hexd[4:]
        blob=read_bytes(path)
        if hashlib.sha512(blob).digest()!=raw:raise ValueError('cached tarball integrity')
        import io
        prefix=name[len('node_modules/'):]
        with tarfile.open(fileobj=io.BytesIO(blob),mode='r:gz') as tf:
            namespace=None
            for member in tf:
                check_deadline(deadline)
                raw_name=member.name.rstrip('/')
                if raw_name.startswith('/') or '..' in raw_name.split('/'):
                    raise ValueError('unsafe package tar member')
                relative=safe_relative(str(PurePosixPath(raw_name)))
                if namespace is None:namespace=relative.parts[0]
                if relative.parts[0]!=namespace:raise ValueError('tarball namespace')
                if member.isdir():continue
                if not member.isfile():raise ValueError('nonregular package tar member')
                rel=str(PurePosixPath(*relative.parts[1:]));safe_relative(rel)
                key=prefix+'/'+rel
                f=tf.extractfile(member)
                with f: data=f.read()
                # Database/financial backup material stays protected even when
                # published by a dependency. Vendor credential *code* is admitted
                # only by byte equality with this manifest-integrity tarball.
                suffix=PurePosixPath(rel).suffix.lower()
                if suffix in {'.db','.sqlite','.sqlite3'} or data[:16]==b'SQLite format 3\0' or data[8:12]==b'DUCK':
                    raise ValueError('protected dependency payload')
                hashed=hashlib.sha256(data).hexdigest()
                if key in expected and expected[key]!=hashed:
                    raise ValueError('conflicting duplicate package member')
                expected[key]=hashed
                if rel=='package.json':
                    meta=json.loads(data);declared_bins=meta.get('bin',{})
                    if isinstance(declared_bins,str):declared_bins={meta['name'].split('/')[-1]:declared_bins}
                    for bin_name, target in declared_bins.items():
                        safe_relative(bin_name);safe_relative(target.removeprefix('./'))
                        bin_parent=(PurePosixPath(prefix.rsplit('/node_modules/',1)[0])/'node_modules'
                                    if '/node_modules/' in prefix else PurePosixPath('.'))
                        bins.setdefault(str(bin_parent/'.bin'/bin_name),set()).add(str(PurePosixPath(prefix)/target.removeprefix('./')))
    # npm/pacote renames published .gitignore on extraction, not its bytes.
    for key in list(expected):
        q=PurePosixPath(key)
        if q.name=='.gitignore':
            renamed=str(q.with_name('.npmignore'))
            if renamed not in expected:expected[renamed]=expected.pop(key)
    rows=[];stack=[root];device=root.lstat().st_dev;amount=0;seen=set()
    while stack:
        check_deadline(deadline)
        p=stack.pop();s=p.lstat();rel=str(p.relative_to(root));sig=signature(p)
        if s.st_dev!=device:raise ValueError('cross-device dependency')
        if stat.S_ISDIR(s.st_mode):
            with os.scandir(p) as es:stack.extend(Path(e.path) for e in es)
        elif stat.S_ISREG(s.st_mode):
            if s.st_nlink!=1 or rel not in expected or digest(p)!=expected[rel]:
                raise ValueError('unowned or changed dependency file: '+rel)
            seen.add(rel)
        elif stat.S_ISLNK(s.st_mode):
            if rel not in bins or p.resolve(strict=True) not in {root/b for b in bins[rel]} or root not in p.resolve().parents:
                raise ValueError('unowned dependency symlink: '+rel)
        else:raise ValueError('special dependency file')
        if signature(p)!=sig:raise ValueError('dependency changed during verification')
        rows.append((rel,sig));amount+=s.st_blocks*512
    if seen!=set(expected):raise ValueError('incomplete dependency installation')
    return sorted(rows),amount


def retire(api, entry, *, dry_run, deadline, cache=CACHE, pins=()):
    root=Path(entry.get('path',''))
    row={'rule':'completed_ci_npm_dependencies','path':str(root),'bytes_freed':0}
    try:
        if root.name!='node_modules' or not root.is_absolute() or root.is_symlink() or root.resolve()!=root.absolute():
            raise ValueError('dependency scope')
        owner=root.parent;owner_identity(owner,entry)
        receipt=Path(entry['receipt'])
        if digest(receipt)!=entry['receipt_sha256']:raise ValueError('completion receipt drift')
        proof=json.loads(read_bytes(receipt))
        if (proof.get('status')!='PASS' or not proof.get('finished') or proof.get('head_sha')!=entry['owner_head']
                or proof.get('worktree')!=str(owner)
                or not any(c.get('exit_code')==0 and c.get('command','').startswith('npm ci ') and '--ignore-scripts' in c['command'] for c in proof.get('commands',[]))):
            raise ValueError('completed install authority missing')
        if intersects(root,tuple(fresh_pins())+tuple(pins)) or inbound_links(root,deadline):raise ValueError('live dependency custody')
        if api.path_is_active_privileged(owner) or api.path_is_active_privileged(root):raise ValueError('active or unknown dependency owner')
        before,amount=ownership(root,entry,deadline,cache)
        row.update(before_bytes=amount,after_bytes=amount)
        if not lsof_inactive(owner) or not lsof_inactive(root):raise ValueError('open dependency or source owner')
        owner_identity(owner,entry)
        if digest(receipt)!=entry['receipt_sha256'] or intersects(root,tuple(fresh_pins())+tuple(pins)) or inbound_links(root,deadline):raise ValueError('new dependency custody')
        if api.path_is_active_privileged(owner) or api.path_is_active_privileged(root) or ownership(root,entry,deadline,cache)[0]!=before:
            raise ValueError('new reference or dependency identity drift')
        if dry_run:row.update(status='ELIGIBLE',eligible_bytes=amount)
        elif api.remove_disposable(root,deadline=deadline) and not root.exists():row.update(status='REMOVED',after_bytes=0,bytes_freed=amount)
        else:row.update(status='PARTIAL_UNKNOWN',ok=False)
    except (OSError,ValueError,KeyError,TypeError,AttributeError,TimeoutError,subprocess.SubprocessError,tarfile.TarError) as exc:
        row.update(status='REFUSED',reason=str(exc))
    return row


def run(api, policy, *, dry_run, deadline, pins=()):
    if isinstance(policy,(str,Path)):
        policy=json.loads(read_bytes(Path(policy)))
    section=policy.get('completed_ci_dependencies',[])
    if not isinstance(section,list) or len(section)>32:
        raise ValueError('invalid bounded dependency policy')
    rows=[]
    for entry in section:
        if time.monotonic()>=deadline:break
        if entry.get('authority')!=AUTHORITY:
            rows.append({'rule':'completed_ci_npm_dependencies','status':'REFUSED','bytes_freed':0,'reason':'unsupported release authority'})
            continue
        rows.append(retire(api,entry,dry_run=dry_run,deadline=deadline,pins=pins))
    return rows
