#!/usr/bin/env python3
"""No-agent/local scheduler entrypoint. Verify immutable manifest BEFORE imports.

This wrapper has no sender and no cash-ingestion adapter. It produces reporting
coverage receipts only. Default is offline; --live-cj-readonly explicitly reads.
"""
import argparse
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import uuid


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-manifest',type=Path,required=True)
    parser.add_argument('--manifest-sha256',required=True)
    parser.add_argument('--roster',type=Path,required=True)
    parser.add_argument('--identity-registry',type=Path,required=True)
    parser.add_argument('--report-root',type=Path,required=True)
    parser.add_argument('--lookback-days',type=int,default=90)
    parser.add_argument('--max-requests',type=int,default=32)
    parser.add_argument('--budget-seconds',type=int,default=240)
    parser.add_argument('--reconciliation-input',type=Path)
    parser.add_argument('--xentraffic-intake',type=Path)
    parser.add_argument('--xentraffic-sha256')
    parser.add_argument('--live-cj-readonly',action='store_true')
    parser.add_argument('--live-network-readonly',action='store_true')
    args=parser.parse_args(argv)
    data=args.source_manifest.read_bytes()
    if hashlib.sha256(data).hexdigest()!=args.manifest_sha256:
        raise ValueError('manifest_pin_mismatch')
    pins=json.loads(data)['files']
    here=Path(__file__).resolve().parent
    required=[str(here/n) for n in ('glp_affiliate_weekly_reporting_j7_job.py','glp_affiliate_weekly_reporting_j7.py','glp_network_revenue_v2.py','glp_network_revenue_import_v2.py')]
    if not all(p in pins for p in required):
        raise ValueError('source_pin_missing')
    for path,sha in pins.items():
        if hashlib.sha256(Path(path).read_bytes()).hexdigest()!=sha:
            raise ValueError('source_pin_mismatch')
    from glp_affiliate_weekly_reporting_j7 import main as report
    run_id=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:12]
    command=['--source-manifest',str(args.source_manifest),'--roster',str(args.roster),
        '--identity-registry',str(args.identity_registry),'--output',str(args.report_root/run_id),
        '--lookback-days',str(args.lookback_days),'--max-requests',str(args.max_requests),
        '--budget-seconds',str(args.budget_seconds)]
    if args.xentraffic_intake:
        command += ['--xentraffic-intake',str(args.xentraffic_intake),'--xentraffic-sha256',args.xentraffic_sha256]
    if args.reconciliation_input:
        command += ['--reconciliation-input', str(args.reconciliation_input)]
    if args.live_cj_readonly:
        command.append('--live-cj-readonly')
    if args.live_network_readonly:
        command.append('--live-network-readonly')
    return report(command)


if __name__=='__main__':
    try:
        raise SystemExit(main())
    except (OSError,ValueError,KeyError) as exc:
        print(json.dumps({'status':'BLOCKED','error_class':type(exc).__name__}))
        raise SystemExit(2)
