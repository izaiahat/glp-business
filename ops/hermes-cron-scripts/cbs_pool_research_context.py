"""Fresh read-only account input and a bounded experimental event lease."""
import fcntl,json,subprocess,sys
from datetime import datetime,timezone,timedelta
from pathlib import Path
ROOT=Path('/home/ubuntu/projects/cbs-confidence-pool')
sys.path.insert(0,str(ROOT))
from research_input import refresh_local
from experiment_schedule import gate
p=subprocess.run([sys.executable,'/home/ubuntu/.hermes/scripts/cbs_pool_read.py'],capture_output=True,text=True,timeout=170)
if p.returncode:raise SystemExit('ACTION REQUIRED: fresh CBS read failed; no current card claimed')
data=refresh_local();c=json.loads((ROOT/'config.json').read_text());now=datetime.now(timezone.utc)
with (ROOT/'.experiment-state.lock').open('a') as lock:
    fcntl.flock(lock,fcntl.LOCK_EX)
    sp=ROOT/'experiment-state.json';s=json.loads(sp.read_text()) if sp.exists() else {}
    plan=gate(c,s,data,now)
    event=plan.get('due_event')
    if not event:
        print(json.dumps({'status':'IDLE','instruction':'Output SILENT; no duplicate research','plan':plan}));raise SystemExit(0)
    attempted=s.get('attempts',{}).get(event['key'])
    interval=20 if plan.get('expires_at') and datetime.fromisoformat(plan['expires_at'])-now<timedelta(hours=2) else 60
    if attempted and now-datetime.fromisoformat(attempted['at'])<timedelta(minutes=interval):
        print(json.dumps({'status':'BUSY_OR_BACKOFF','instruction':'Output SILENT; previous event lease still active'}));raise SystemExit(0)
    s.setdefault('attempts',{})[event['key']]={'at':now.isoformat(),'count':1 if not attempted else attempted.get('count',0)+1}
    tmp=sp.with_suffix('.tmp');tmp.write_text(json.dumps(s,indent=2));tmp.replace(sp)
    context={'status':'RESEARCH_DUE','event_key':event['key'],'event':event,'plan':plan,'data':data}
    (ROOT/'active-research-context.json').write_text(json.dumps(context,indent=2))
    print(json.dumps(context,indent=2))
