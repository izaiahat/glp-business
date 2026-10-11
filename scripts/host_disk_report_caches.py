"""Only old regenerable browser-cache subtrees in reports; never profiles/DBs."""
import os
from pathlib import Path
import time
NAMES={'Cache','Code Cache','GPUCache','DawnCache','ShaderCache','GrShaderCache','CacheStorage'}


def profile_part(name):
    return name in {'browser-profiles','browser-profile','Default'} or name.startswith(('browser-profile-','chrome-profile-','chromium-profile-'))


def run(api,m,reports,*,pins,dry_run,deadline):
    from host_disk_auto_classes import _guarded_remove
    reports=Path(reports);pins=m.custody_pins(pins);rows=[];stack=[reports]
    compiled={}
    def protect(p,fresh):
        p=Path(p);key=id(fresh);entry=compiled.get(key)
        if entry is None or entry[0] is not fresh:
            entry=(fresh,frozenset(map(str,fresh)));compiled[key]=entry
        names=entry[1]
        if any(str(a) in names for a in (p,*p.parents)):return True
        sanitized=Path(*[part for part in p.parts if not profile_part(part)])
        return m.protected(sanitized,())
    targets=[]
    while stack:
        if time.monotonic()>deadline:raise TimeoutError('report-cache discovery')
        p=stack.pop()
        if p.is_symlink() or protect(p,pins):continue
        if p.name in NAMES:
            anchors=[a for a in p.parents if reports in a.parents and a.name!='Default' and profile_part(a.name)]
            if anchors:targets.append((p,anchors[-1]))
            continue
        with os.scandir(p) as entries:
            stack.extend(Path(e.path) for e in entries if e.is_dir(follow_symlinks=False) and e.name not in {'.git','node_modules'})
    for p,profile in sorted(targets):
        if time.monotonic()>deadline:break
        def effect_protect(q,fresh):
            # Any named descendant also vetoes whole-cache removal.
            return protect(q,fresh) or (Path(q)==p and any(p in Path(pin).parents for pin in fresh))
        def owner_check(q):
            if reports not in q.parents or q.name not in NAMES or profile not in q.parents:
                raise ValueError('report-cache scope')
            if m.dirty_worktree(profile) or not m.inactive(api,profile):
                raise ValueError('profile owner dirty/active')
        rows.append(_guarded_remove(api,m,p,pins,deadline,'old_report_browser_cache',dry_run,
            extra_check=owner_check,cutoff=time.time()-86400,check_atime=True,protection=effect_protect))
    return rows
