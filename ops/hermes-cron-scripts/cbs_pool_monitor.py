# CRON_DISCORD_HOUSE_STYLE_V1
# CRON_DISCORD_OUTPUT_DECLARATION_V1={"short_name": "CBS Pool", "output_class": "alert", "composer_type": "python", "privacy_level": "internal", "fixture": "\u26a0\ufe0f **CBS Pool** \u2014 Read-only refresh failed\n\nLast snapshot retained but not fresh. No picks changed.\n\u2192 Reopen CBS login if expired; otherwise the reader needs checking.", "fixture_output_class": "alert", "fixture_test": "discord_fmt.lint fixture and rendered output", "route": "discord:1487269649753313300", "detail_policy": "complete_summary_in_discord"}
import subprocess,json,sys
from pathlib import Path
sys.path.insert(0,'/home/ubuntu/business/ops')
from discord_fmt import lint
ROOT=Path('/home/ubuntu/projects/cbs-confidence-pool')
config=json.loads((ROOT/'config.json').read_text())
if config.get('season_ended'): raise SystemExit(0)
p=subprocess.run([sys.executable,'/home/ubuntu/.hermes/scripts/cbs_pool_read.py'],capture_output=True,text=True,timeout=170)
f=ROOT/'monitor-status.json'
previous=json.loads(f.read_text()) if f.exists() else {}
status='ok' if p.returncode==0 else 'error'
message=''; kind='silence'
if status=='error' and previous.get('status')!='error':
 message='⚠️ **CBS Pool** — Read-only refresh failed\n\nLast snapshot retained but not fresh. No picks changed.\n→ Reopen CBS login if expired; otherwise the reader needs checking.'
 kind='alert'
elif status=='ok' and previous.get('status')=='error':
 message='✅ **CBS Pool** — Read-only access recovered\n\nStandings and saved picks refreshed. No picks changed.'
 kind='report'
assert lint(message,kind)['status']=='pass', 'Delivery formatting failed'
f.write_text(json.dumps({'status':status}))
if message: print(message)
