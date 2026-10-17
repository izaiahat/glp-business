"""Retention custody for relay jobs/ledgers and live issue descriptions."""
import json, os, re, stat, time
from contextlib import contextmanager
from pathlib import Path
BUS = Path('/home/ubuntu/business')
PATHS = re.compile(r'(?:/home/ubuntu/|/tmp/|(?:reports|site-system|ops|lanes|\.worktrees)/)[^\s\"<>\x60,;\)\]\}]+')

def strings(value):
    if isinstance(value,str): yield value
    elif isinstance(value,dict):
        for key,item in value.items():
            yield str(key)
            yield from strings(item)
    elif isinstance(value,list):
        for item in value: yield from strings(item)

def text_pins(text,bus,*,explicit=False):
    pins=set()
    literal=(not any(char.isspace() for char in text) or text.endswith(('.json','.md','.txt','.png','.jpg','.yml','.yaml','.toml')))
    if literal and not any(char in text for char in '\n\r\x00;:') and text.startswith(('/home/ubuntu/','/tmp/','reports/','site-system/','ops/','lanes/','.worktrees/')):
        value=text.rstrip("'/:.")
        if len(value)>4096 or any(len(part)>255 for part in Path(value).parts): raise ValueError('invalid literal custody path')
        pins.add(Path(value) if value.startswith('/') else bus/value)
    for name in PATHS.findall(text):
        name=name.rstrip("'/:.")
        pins.add(Path(name) if name.startswith('/') else bus/name)
    # Narrative layout/receiving metadata names no artifact. Drop only the
    # existing finite scaffold roots, never an arbitrary ancestor or a policy,
    # reservation/proof or explicit non-layout subtree hold.
    layout={Path('/'),Path('/home/ubuntu'),Path('/home/ubuntu/.hermes'),
            Path('/home/ubuntu/.hermes/cache'),Path('/home/ubuntu/.hermes/cache/scratch'),
            Path('/tmp'),bus,bus/'.worktrees',bus/'worktrees',bus/'reports',
            bus/'reports/ops',bus/'reports/glp-clinic-scout',bus/'reports/host-disk-prune',
            bus/'reports/glp-clinic-scout/affiliate-machine',
            bus/'reports/glp-clinic-scout/affiliate-machine/registry-site-sync',
            bus/'site-system',bus/'site-system/flagship-v2',
            *(bus/'site-system/flagship-v2'/name for name in ('candidates','qa','work','releases'))}
    return pins if explicit else pins-layout


def explicit_pins(value,bus):
    """Typed policy/proof fields are holds even when they name a layout root."""
    pins=set()
    if isinstance(value,dict):
        for key,item in value.items():
            if key in {'protected_roots','release_root','current_root','live_root',
                       'deploy_root','active_roots','proof','proof_path','target_evidence','terminal_unsent'}:
                for text in strings(item): pins.update(text_pins(text,bus,explicit=True))
            pins.update(explicit_pins(item,bus))
    elif isinstance(value,list):
        for item in value: pins.update(explicit_pins(item,bus))
    return pins


def evidence(value):
    if isinstance(value,dict):
        for key,item in value.items():
            if key=='target_evidence': yield item
            yield from evidence(item)
    elif isinstance(value,list):
        for item in value: yield from evidence(item)

def bound_targets(value,bus):
    import hashlib
    if isinstance(value,dict):
        path=value.get('path') or value.get('file')
        expected=value.get('sha256')
        if isinstance(path,str) and isinstance(expected,str) and re.fullmatch(r'[0-9a-fA-F]{64}',expected):
            target=Path(path) if path.startswith('/') else bus/path
            if target.is_symlink() or not target.is_file(): raise ValueError('missing/aliased hash-bound evidence')
            before=signature(target)
            with target.open('rb') as stream:
                digest=hashlib.file_digest(stream,'sha256').hexdigest()
            if signature(target)!=before or digest.lower()!=expected.lower(): raise ValueError('hash-bound evidence changed')
        for item in value.values(): bound_targets(item,bus)
    elif isinstance(value,list):
        for item in value: bound_targets(item,bus)

class BindingPins(set):
    def __init__(self,values=(),identities=None):
        super().__init__(values);self.identities=identities or {}

def fields(s):
    return [s.st_dev,s.st_ino,s.st_mode,s.st_size,s.st_mtime_ns,s.st_ctime_ns,s.st_nlink,s.st_uid,s.st_gid]

def signature(path): return fields(Path(path).lstat())


def reference_name(name,namespace):
    if namespace=='lane': return name=='RESULT.json'
    if namespace=='relay':
        return name=='JOB.json' or ('ledger' in name.lower() and name.endswith(('.json','.jsonl','.ndjson')))
    raise ValueError('unknown discovery namespace')


def discovery_signature(path,namespace,observed=None):
    """Bind discovery membership, not unrelated atomic output writes.

    Directory inode/type/owner/mode and discoverable child membership remain
    custody. Directory size/timestamps/link count also change for route receipts,
    logs and Git lock files, which do not add a reference source. Actual sources
    and directory aliases keep their complete no-follow inode signatures.
    Collection uses os.walk's observed names, not a later listing that could
    bless a newly arrived but unread RESULT/JOB/ledger.
    """
    path=Path(path);before=signature(path)
    if not stat.S_ISDIR(before[2]): raise ValueError('aliased/invalid discovery directory')
    stable=lambda value:[value[i] for i in (0,1,2,7,8)]
    if observed is None:
        with os.scandir(path) as entries:
            names=[entry.name for entry in entries
                   if entry.is_dir() or reference_name(entry.name,namespace)]
    else:
        dirs,files=observed
        names=list(dirs)+[name for name in files if reference_name(name,namespace)]
    members=[]
    for name in sorted(names):
        value=signature(path/name)
        # Each real directory gets its own inventory. Its payload churn is not
        # parent membership drift; aliases retain their complete identity.
        # Regular source bytes are separately bound after their complete read;
        # keep membership/type here so diagnostics identify that exact source.
        identity=(stable(value) if stat.S_ISDIR(value[2]) else
                  value if stat.S_ISLNK(value[2]) else [stat.S_IFMT(value[2])])
        members.append([name,identity])
    if stable(signature(path))!=stable(before): raise ValueError('discovery directory replaced during scan')
    return {'discovery_namespace':namespace,'identity':stable(before),'members':members}


def matches(path,expected):
    """Absence is a bound observation, not permission to skip new namespaces."""
    try:
        if isinstance(expected,dict):
            return discovery_signature(path,expected['discovery_namespace'])==expected
        return signature(path)==expected
    except FileNotFoundError: return expected is None
    except (OSError,ValueError): return False


def lane_sources(root,identities,pins,namespaces):
    """Complete no-follow discovery, including empty dirs and absent root.

    Internal directory aliases (e.g. pytest-current) are bound, not followed:
    their canonical target is covered by the complete real namespace walks.
    Unknown external aliases remain a refusal rather than an omitted subtree.
    """
    try: identities[str(root)]=signature(root)
    except FileNotFoundError:
        identities[str(root)]=None
        return []
    if root.is_symlink() or not root.is_dir(): raise ValueError('aliased/invalid lane namespace')
    sources=[]
    def failed(error): raise error
    for directory,dirs,files in os.walk(root,followlinks=False,onerror=failed):
        identities[str(directory)]=discovery_signature(directory,'lane',(dirs,files))
        for name in dirs:
            link=Path(directory)/name
            if not link.is_symlink(): continue
            identities[str(link)]=signature(link)
            target=link.resolve(strict=True)
            if not target.is_dir() or not any(target==n or n in target.parents for n in namespaces):
                raise ValueError('external lane namespace alias')
            pins.update((link,target))
        if 'RESULT.json' in files: sources.append(Path(directory)/'RESULT.json')
    return sources


def newest_site_pins(bus,identities):
    """Q2: retain three newest creation-ordered roots per site class, fail closed.

    GNU stat exposes Linux birth time; mtime is not creation order. This is
    deny-only shared custody for both ordinary C and the registered large sweep.
    """
    import subprocess
    pins=set()
    for kind in ('candidates','qa','releases','work'):
        parent=bus/'site-system/flagship-v2'/kind
        try: identities[str(parent)]=signature(parent)
        except FileNotFoundError:
            identities[str(parent)]=None;continue
        if parent.is_symlink() or not parent.is_dir(): raise ValueError('aliased site class')
        roots=sorted(p for p in parent.iterdir() if p.is_dir() and not p.is_symlink())
        if not roots: continue
        if len(roots)>10000: raise ValueError('site creation inventory exceeds bound')
        result=subprocess.run(['stat','--printf','%w\\0%n\\0','--',*map(str,roots)],
                              capture_output=True,text=True,timeout=30,check=True)
        parts=result.stdout.split('\0')
        if len(parts)!=2*len(roots)+1 or parts[-1]!='': raise ValueError('incomplete site birth inventory')
        ordered=[]
        for i,root in enumerate(roots):
            birth,name=parts[2*i:2*i+2]
            if not birth or birth=='-' or name!=str(root): raise ValueError('site birth order unavailable')
            ordered.append((birth,root))
        pins.update(root for _,root in sorted(ordered)[-3:])
    return pins


@contextmanager
def collector_timing(timings,name):
    counter=timings.setdefault('collector.'+name,{'calls':0,'seconds':0.0,'failures':0})
    counter['calls']+=1
    started=time.monotonic()
    try: yield
    except BaseException:
        counter['failures']+=1
        raise
    finally: counter['seconds']+=time.monotonic()-started


def collect(*,bus=BUS,reader=None,timings=None):
    timings={} if timings is None else timings
    bus=Path(bus)
    relay=bus/'ops/gpt6pro-relay'
    pins={relay,bus/'reports/ops/system-resume-20261006'}
    identities={}
    with collector_timing(timings,'newest_site'):
        pins.update(newest_site_pins(bus,identities))
    with collector_timing(timings,'relay'):
        if relay.exists():
            if relay.is_symlink(): raise ValueError('relay custody root alias')
            def failed(error): raise error
            for directory,dirs,files in os.walk(relay,followlinks=False,onerror=failed):
                identities[str(directory)]=discovery_signature(directory,'relay',(dirs,files))
                for name in dirs:
                    p=Path(directory)/name
                    if p.is_symlink(): pins.update((p,p.resolve(strict=False)))
                for name in files:
                    if not reference_name(name,'relay'): continue
                    path=Path(directory)/name
                    if path.is_symlink(): raise ValueError('aliased relay reference source')
                    before=path.stat()
                    if not stat.S_ISREG(before.st_mode) or before.st_size>64*1024**2: raise ValueError('unreadable/oversized relay source')
                    values=[]
                    with path.open() as stream:
                        if path.suffix=='.json': values=[json.load(stream)]
                        else:
                            for line in stream:
                                if line.strip(): values.append(json.loads(line))
                    after=path.stat()
                    identity=lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
                    if identity(before)!=identity(after): raise ValueError('relay source changed during read')
                    pins.add(path);identities[str(path)]=fields(after)
                    for text in strings(values): pins.update(text_pins(text,bus))
                    pins.update(explicit_pins(values,bus))
        else:
            identities[str(relay)]=None
    with collector_timing(timings,'lanes'):
        sources=[]
        namespaces=(bus/'lanes',bus/'reports/ops/fable-plan-2-20261009/lanes')
        for lanes in namespaces:
            sources.extend(lane_sources(lanes,identities,pins,namespaces))
    with collector_timing(timings,'transitive'):
        pending=sources+[Path(p) for p in pins if Path(p).suffix=='.json' and Path(p).is_file()]
        visited=set()
        while pending:
            path=pending.pop()
            if path in visited: continue
            visited.add(path)
            if len(visited)>10000: raise ValueError('reference closure exceeds bound')
            if path.is_symlink(): raise ValueError('aliased proof/lane reference source')
            before=path.stat()
            if before.st_size>64*1024**2: raise ValueError('oversized proof/lane source')
            with path.open() as stream: value=json.load(stream)
            after=path.stat()
            if (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns): raise ValueError('proof/lane source drift')
            pins.add(path);identities[str(path)]=fields(after)
            discovered=set().union(*(text_pins(text,bus) for text in strings(value)))
            pins.update(discovered)
            pins.update(explicit_pins(value,bus))
            for item in evidence(value): bound_targets(item,bus)
            transitive=set().union(*(text_pins(text,bus) for item in evidence(value) for text in strings(item)))
            pending.extend(p for p in transitive if p.suffix=='.json' and p.is_file() and bus in p.parents and not any(part in {'secrets','credentials','backups'} for part in p.parts))
    with collector_timing(timings,'linear'):
        client=bus/'scripts/linear_client.py'
        if reader is not None or client.is_file():
            import host_disk_auto_classes as auto
            if reader is None and os.geteuid()==0:
                import subprocess, sys
                code="import sys,json;sys.path.insert(0,str(__import__('pathlib').Path(sys.argv[1]).parent));import host_disk_auto_classes as a;print(json.dumps(sorted(map(str,a.linear_pins(sys.argv[1])))))"
                result=subprocess.run(['sudo','-n','-H','-u','ubuntu',sys.executable,'-c',code,str(client)],capture_output=True,text=True,timeout=150,check=True,env={**os.environ,'LINEAR_ROLE':'fleet'})
                pins.update(Path(p) for p in json.loads(result.stdout))
            else: pins.update(auto.linear_pins(client,reader=reader))
    with collector_timing(timings,'generation_validation'):
        # Include discovery drift during slow live issue pagination, not just edits
        # of files known at entry. A partially scanned generation is never authority.
        for path,expected in identities.items():
            if not matches(path,expected):
                error=ValueError('relay/lane discovery changed during collection')
                setattr(error,'changed_source',path)
                raise error
    return BindingPins(pins,identities)

# Keep admitted generations immutable and validate them at every effect gate;
# effect gates recollect live issues; D qualification alone may reuse a validated
# complete snapshot to deny already-held runs.
