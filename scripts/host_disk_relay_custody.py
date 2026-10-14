"""Retention custody for relay jobs/ledgers and live issue descriptions."""
import json, os, re, stat
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

def text_pins(text,bus):
    pins=set()
    literal=(not any(char.isspace() for char in text) or text.endswith(('.json','.md','.txt','.png','.jpg','.yml','.yaml','.toml')))
    if literal and not any(char in text for char in '\n\r\x00;:') and text.startswith(('/home/ubuntu/','/tmp/','reports/','site-system/','ops/','lanes/','.worktrees/')):
        value=text.rstrip("'/:.")
        if len(value)>4096 or any(len(part)>255 for part in Path(value).parts): raise ValueError('invalid literal custody path')
        pins.add(Path(value) if value.startswith('/') else bus/value)
    for name in PATHS.findall(text):
        name=name.rstrip("'/:.")
        pins.add(Path(name) if name.startswith('/') else bus/name)
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

def collect(*,bus=BUS,reader=None):
    bus=Path(bus)
    relay=bus/'ops/gpt6pro-relay'
    pins={relay,bus/'reports/ops/system-resume-20261006'}
    identities={}
    if relay.exists():
        if relay.is_symlink(): raise ValueError('relay custody root alias')
        for directory,dirs,files in os.walk(relay,followlinks=False):
            identities[str(directory)]=signature(directory)
            for name in dirs:
                p=Path(directory)/name
                if p.is_symlink(): pins.update((p,p.resolve(strict=False)))
            for name in files:
                if name!='JOB.json' and not ('ledger' in name.lower() and name.endswith(('.json','.jsonl','.ndjson'))): continue
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
    lane_sources=[]
    for lanes in (bus/'lanes',bus/'reports/ops/fable-plan-2-20261009/lanes'):
        if lanes.exists(): lane_sources.extend(lanes.rglob('RESULT.json'))
    pending=lane_sources+[Path(p) for p in pins if Path(p).suffix=='.json' and Path(p).is_file()]
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
        for item in evidence(value): bound_targets(item,bus)
        transitive=set().union(*(text_pins(text,bus) for item in evidence(value) for text in strings(item)))
        pending.extend(p for p in transitive if p.suffix=='.json' and p.is_file() and bus in p.parents and not any(part in {'secrets','credentials','backups'} for part in p.parts))
    client=bus/'scripts/linear_client.py'
    if reader is not None or client.is_file():
        import host_disk_auto_classes as auto
        if reader is None and os.geteuid()==0:
            import subprocess, sys
            code="import sys,json;sys.path.insert(0,str(__import__('pathlib').Path(sys.argv[1]).parent));import host_disk_auto_classes as a;print(json.dumps(sorted(map(str,a.linear_pins(sys.argv[1])))))"
            result=subprocess.run(['sudo','-n','-H','-u','ubuntu',sys.executable,'-c',code,str(client)],capture_output=True,text=True,timeout=150,check=True,env={**os.environ,'LINEAR_ROLE':'fleet'})
            pins.update(Path(p) for p in json.loads(result.stdout))
        else: pins.update(auto.linear_pins(client,reader=reader))
    return BindingPins(pins,identities)

# Reference sources are immutable within an admitted effect; callers must
# refuse when a returned identity changes and recollect only at a new boundary.
