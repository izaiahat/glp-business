#!/usr/bin/env python3
"""Read-only weekly updater wake gate; scheduled agent authors/reviews sources."""
import datetime as dt
import hashlib
import json
from pathlib import Path
import sys

B = Path('/home/ubuntu/business')
sys.path.insert(0,str(B/'scripts'))
import glp_seo_updater_source as updater

def main():
    value=updater.plan()
    value={key:item for key,item in value.items() if key != 'selection'}
    value['strategy']={key:item for key,item in value['strategy'].items() if key != 'source_observations'}
    folder=B/'reports/glp-clinic-scout/seo-machine/weekly-updater-plans'
    folder.mkdir(parents=True,exist_ok=True)
    path=folder/(dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.json')
    path.write_text(json.dumps(value,indent=2)+'\n')
    print(json.dumps({'wakeAgent':bool(value['selected']), 'status':value['status'],
        'plan':{'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()},
        'strategy':value['strategy'],'cadence':value['cadence'],'selected':value['selected'],
        'no_op':not value['selected'],'publication_authorized':False}))

if __name__=='__main__':main()
