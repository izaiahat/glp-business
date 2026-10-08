#!/usr/bin/env python3
"""Registered Weekly Strategy pre-run: collect/consume live GLP report evidence.

Scheduler remains the only delivery owner. --output is a manual report-local
sink, not an alternate sender. No retired ventures, queue activation or spend.
"""
from __future__ import annotations
import argparse
import datetime as dt
import fcntl
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

B=Path('/home/ubuntu/business')
MARKER=B/'ops/weekly_strategy_review_gate_state.json'
PRODUCER=B/'scripts/glp_weekly_strategy_report.py'

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    now=dt.datetime.now(dt.timezone.utc); week=(now-dt.timedelta(days=now.weekday())).date().isoformat()
    (B/'ops/glp').mkdir(parents=True,exist_ok=True)
    MARKER.parent.mkdir(parents=True,exist_ok=True)
    with (B/'ops/glp/.weekly-strategy-gate.lock').open('a') as stream:
        fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
        previous={}
        if MARKER.exists():
            try: previous=json.loads(MARKER.read_text())
            except (ValueError,OSError): pass
        # A bound current-week report may be re-summarized after a delivery
        # failure; do not replay collection or infer a completed delivery.
        cached=previous.get('report_result') if previous.get('week_start')==week else None
        if cached and not args.output:
            binding=previous.get('report_binding',{})
            raw=Path(binding['path']).read_bytes()
            if hashlib.sha256(raw).hexdigest()!=binding['sha256']: raise ValueError('weekly_report_changed')
            report_doc=json.loads(raw)
            captured=dt.datetime.fromisoformat(report_doc['generated_at_utc'].replace('Z','+00:00'))
            if not 0 <= (now-captured).total_seconds() <= 3*3600: cached=None
        if cached and not args.output:
            result=cached
        else:
            command=[sys.executable,str(PRODUCER),'--update-strategy']
            if args.output: command+=['--output',str(args.output)]
            completed=subprocess.run(command,cwd=B,capture_output=True,text=True,timeout=1800,check=True)
            result=json.loads(completed.stdout)
            report=Path(result['report'])
            # Record report completion only, never delivery or natural effects.
            marker={'week_start':week,'completed':False,'report_completed':True,
                    'report_result':result,'report_binding':{'path':str(report),'sha256':hashlib.sha256(report.read_bytes()).hexdigest()},
                    'checked_at_utc':now.isoformat()}
            if not args.output:
                temp=MARKER.with_suffix('.tmp');temp.write_text(json.dumps(marker,indent=2)+'\n');temp.replace(MARKER)
        print(json.dumps({'wakeAgent':True,'gate':'weekly_strategy_review_once_per_week',
            'week_start':week,'status':result['status'],'context':{
                'focus':'GLP Clinic Scout only; affiliate revenue first; all ads remain paused',
                'live_review':result['review'],'live_report':result['report'],
                'seo_refill_recommendations':result['topic_recommendations'],
                'report_summary':result['summary'],
                'strategy_update':result.get('strategy_update'),
                'instruction':'Use the produced live report and exact-readback Cogny strategy input. GLP-73 owns governed topic/refill adoption; require its receipt, not a report-only claim. Unknown is not zero. No publication or new paid collection; scheduler alone delivers the final summary.'}},ensure_ascii=False))
    return 0

if __name__=='__main__': sys.exit(main())
