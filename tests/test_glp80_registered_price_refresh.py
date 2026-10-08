import hashlib
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def test_existing_price_refresh_registration_uses_paired_writer():
    actual=json.loads((ROOT/'ops/n8n-bridge/actions.json').read_text())
    changed=actual['pricing_refresh']
    assert changed['argv']==['python3','scripts/glp_price_freshness.py']
    assert changed['cwd']=='/home/ubuntu/business'
    assert changed['timeout_s']==2400
    assert changed['allowed_args']==['release','output','state','concurrency','manual-registered']
    assert changed['value_flags']=={'manual-registered':{'1':['--manual-registered']}}


def test_existing_price_job_and_twenty_launcher_copins_remain_exact():
    doc=json.loads((ROOT/'ops/scheduled-source-drift-manifest.json').read_text())
    pin=hashlib.sha256((ROOT/'scripts/cron_bridge_action.py').read_bytes()).hexdigest()
    launchers=[s for job in doc['jobs'] for s in job.get('sources',[]) if s.get('source')=='scripts/cron_bridge_action.py']
    assert len(launchers)==20
    assert all(s['reviewed_sha256']==pin for s in launchers)
    price=[j for j in doc['jobs'] if j['id']=='crontab:daf0c212391b0319']
    assert len(price)==1
    assert price[0]['sources'][1]=={'path':'/home/ubuntu/business/scripts/glp_price_freshness.py','source':'scripts/glp_price_freshness.py','reviewed_sha256':hashlib.sha256((ROOT/'scripts/glp_price_freshness.py').read_bytes()).hexdigest()}
