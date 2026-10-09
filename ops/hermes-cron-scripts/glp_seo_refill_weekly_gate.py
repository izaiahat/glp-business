#!/usr/bin/env python3
"""Bind the nightly refill to this week's strategy, even on capacity no-op."""
import datetime as dt
import hashlib
import json
from pathlib import Path
import subprocess
import sys

B = Path('/home/ubuntu/business')
sys.path.insert(0, str(B/'systems/seo-blog-machine/scripts'))
from weekly_strategy_consumer import load_strategy
from seo_weekly_governance import CADENCE

def main():
    strategy, meta = load_strategy()
    root = B/'reports/glp-clinic-scout/seo-machine/weekly-refill'
    folder = root/dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    folder.mkdir(parents=True)
    path = folder/'selection.json'
    command = [sys.executable,str(B/'systems/seo-blog-machine/scripts/select_opus_content_queue.py'),
               '--output-receipt',str(path)]
    proc = subprocess.run(command, cwd=B, capture_output=True, text=True, timeout=600)
    (folder/'selector.stdout').write_text(proc.stdout)
    (folder/'selector.stderr').write_text(proc.stderr)
    if proc.returncode:
        raise RuntimeError('weekly_refill_selection_failed:'+str(proc.returncode))
    selection = json.loads(path.read_text())
    bound = selection['cogny_weekly_strategy']
    bound = {key:value for key,value in bound.items() if key != 'source_observations'}
    if bound.get('updates') != meta.get('updates'):
        raise RuntimeError('weekly_strategy_changed_during_refill')
    result = {'schema':'glp-weekly-refill-gate-v1','status':'PASS_GOVERNED_REFILL_SELECTION',
              'strategy':bound,'cadence':CADENCE,
              'selection':{'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()},
              'selected_count':len(selection['selected']), 'wakeAgent':bool(selection['selected']),
              'no_op':not selection['selected'], 'published':False,
              'reason':None if selection['selected'] else 'current_week_has_no_admitted_new_topic',
              'effects':{'publishes':0,'consumes':0,'sends':0,'n8n_activations':0}}
    (folder/'receipt.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result))

if __name__=='__main__':main()
