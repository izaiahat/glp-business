"""Fresh scheduler, skill and current-lane custody for report retirement."""
import json
import os
from pathlib import Path
import re
import subprocess

BUS = Path('/home/ubuntu/business')
HOME = Path('/home/ubuntu/.hermes')
_CACHE = {}
EXTENSIONS = {'.py','.sh','.md','.json','.yaml','.yml','.toml','.service','.timer','.conf'}


def refs(text, bus=BUS):
    absolute = re.escape(str(bus))
    pattern = rf'(?:{absolute}/|/home/ubuntu/\.hermes/|(?:reports|site-system|\.worktrees)/)[^\s\"<>\x60,;\)\]\}}]+'
    result=set()
    for value in re.findall(pattern,text):
        value=value.rstrip("'/:.")
        p=Path(value) if value.startswith('/') else bus/value
        result.add(p)
    # Structural layout names are not artifacts and must not veto every child.
    result -= {bus,bus/'reports',bus/'reports/ops',bus/'reports/glp-clinic-scout',
               bus/'site-system',bus/'.worktrees',HOME,HOME/'cache',HOME/'cache/scratch',Path('/tmp'),
               bus/'reports/glp-clinic-scout/affiliate-machine',
               bus/'reports/glp-clinic-scout/affiliate-machine/registry-site-sync',
               bus/'reports/host-disk-prune'}
    return result


def source_refs(path, bus):
    p=Path(path)
    if any(part in {'secrets','credentials','backups','data'} for part in p.parts):return {p}
    if p.is_symlink():return {p,p.resolve(strict=False)}
    try:s=p.stat()
    except FileNotFoundError:return set()
    if not p.is_file():return {p}
    identity=(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
    cached=_CACHE.get((str(p),str(bus)))
    if cached and cached[0]==identity:return set(cached[1])
    if s.st_size>64*1024**2:raise ValueError('oversized custody source')
    content=p.read_text(errors='replace')
    after=p.stat()
    if identity!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns):
        raise ValueError('custody source changed during read')
    pins=refs(content,bus)
    _CACHE[(str(p),str(bus))]=(identity,frozenset(pins))
    return pins


def collect(*, bus=BUS, home=HOME, unit_roots=None, cron_text=None):
    """Fresh discovery/metadata every effect; cached content only if identity holds.

    Operational timing fields are NOT a global drift veto: cron updates them
    during a long archive. Script/unit/skill references themselves stay current.
    No credential, database, backup or runtime output contents are read.
    """
    bus=Path(bus);home=Path(home);pins={bus/'data'};sources=set()
    if unit_roots is None:
        unit_roots=(home.parent/'.config/systemd/user',Path('/etc/systemd/system'))
    if not home.exists() and cron_text is None:return pins  # Empty CI home, no runtime registry.
    registry=home/'cron/jobs.json'
    if registry.exists():
        raw=registry.read_text(); data=json.loads(raw)
        jobs=data.get('jobs',[]) if isinstance(data,dict) else data
        if not isinstance(jobs,list):raise ValueError('invalid native scheduler registry')
        pins.update(refs(raw,bus))
        for job in jobs:
            script=job.get('script')
            if isinstance(script,str) and script:
                p=Path(script) if script.startswith('/') else home/'scripts'/script
                pins.add(p);sources.add(p)
    if cron_text is None:
        command=['crontab','-l']
        if os.geteuid()==0:command=['sudo','-n','-u','ubuntu',*command]
        r=subprocess.run(command,capture_output=True,text=True,timeout=5)
        if r.returncode and 'no crontab for' not in r.stderr:
            raise ValueError('unreadable host crontab')
        cron_text=r.stdout
    pins.update(refs(cron_text,bus))
    # Explicit maintained transitive scheduler bindings supplement shell literals.
    manifest=bus/'ops/scheduled-source-drift-manifest.json'
    if manifest.exists():
        data=json.loads(manifest.read_text())
        for job in data['jobs']:
            for item in job.get('sources',[])+job.get('payloads',[]):
                if item.get('path'):
                    p=Path(item['path']);pins.add(p);sources.add(p)
    sources.update(p for p in pins if p.suffix in {'.py','.sh'})
    skills=home/'skills'
    if skills.exists():
        sources.update(p for p in skills.rglob('*') if p.suffix in EXTENSIONS and not p.is_symlink() and p.is_file())
    for root in map(Path,unit_roots):
        if root.exists():
            sources.update(p for p in root.rglob('*') if p.suffix in {'.service','.timer','.conf'} and p.is_file())
    for p in sorted(sources):pins.update(source_refs(p,bus))
    lanes=bus/'reports/ops/fable-plan-2-20261009/lanes'
    if lanes.exists():
        for p in lanes.iterdir():
            match=re.fullmatch(r'([QP])(\d+)',p.name)
            if p.name=='D1' or match and ((match[1]=='Q' and 4<=int(match[2])<=11) or (match[1]=='P' and int(match[2])>=10)):
                pins.add(p)
    return pins
