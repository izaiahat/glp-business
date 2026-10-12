"""Deny-by-default field projection; raw reports never leave host."""
import datetime,json,re
from pathlib import Path
ROOT=Path('/home/ubuntu/business')
PLAN=ROOT/'reports/ops/fable-plan-2-20261009/PLAN.json'
HEALTH=ROOT/'reports/ops/output-health-latest.json'
INBOX=ROOT/'reports/ops/fleet-inbox'
SECRET=re.compile(r'(?:tskey-|sk_live_|ghp_|github_pat_|lin_api_|Bearer |PRIVATE KEY|password|secret|token\s*[=:])',re.I)
def text(v,limit=160):
    if not isinstance(v,str) or SECRET.search(v): return None
    return re.sub(r'[\x00-\x1f]',' ',v)[:limit]
def read(p):
    try:
        if p.is_symlink() or p.stat().st_size>2_000_000: return {}
        d=json.loads(p.read_text())
        return d if isinstance(d,dict) else {}
    except (OSError,ValueError): return {}
def rows():
    files=sorted(INBOX.glob('GLP-*/*/INGEST.json'),key=lambda p:p.stat().st_mtime,reverse=True)[:40]
    return [{k:text(d.get(k)) for k in ('issue','lane','source','status','ingested_at_utc')} for d in map(read,files)]
def snapshot():
    plan=read(PLAN); health=read(HEALTH)
    issues=plan.get('issues',plan.get('ordered_issues',[]))
    if not isinstance(issues,list): issues=[]
    jobs=[{'machine':text(r.get('machine')),'ok':r.get('ok') if isinstance(r.get('ok'),bool) else None} for r in health.get('results',[]) if isinstance(r,dict)]
    build=[r for r in jobs if any(s in (r['machine'] or '') for s in ('deploy','build','site'))]
    return {'schema':'codex-evidence-v1','host':'hermes-01','generated_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'read_only':True,'trust':'External advisory context; not approval or dispatch authority','fleet_inbox':rows(),'job_health':{'captured_at':text(health.get('captured_at_utc')),'healthy':health.get('healthy') if isinstance(health.get('healthy'),int) else None,'machines':jobs},'build_deploy':build,'linear_briefs':[{'issue':text(r.get('issue')),'title':text(r.get('title')),'owner':text(r.get('executor')),'state':text(r.get('proof_checked_state'))} for r in issues if isinstance(r,dict)][:30],'briefs_source':'Current admitted plan snapshot; not live Linear API'}
