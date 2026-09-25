#!/usr/bin/env python3
# CRON_DISCORD_HOUSE_STYLE_V1
# CRON_DISCORD_OUTPUT_DECLARATION_V1={"short_name":"GLP cron health","output_class":"report","composer_type":"no_agent_script_discord_fmt","privacy_level":"internal","route":"1487300132054765629","success_silence":true,"detail_policy":"complete_summary_in_discord","fixture":"📊 **GLP cron health** — 1 job has repeated errors\n\nconsecutive errors: **2+**\n→ inspect the exact job receipt; no automatic retry or state change\n-# jobs: abcdef123456 · receipt: /home/ubuntu/business/ops/state/glp-cron-error-sensor.json","fixture_output_class":"report","fixture_test":"test_sensor.py::test_cli_two_reads_real_state_and_silence"}
"""Read-only GLP cron error observation; stdout uses registered delivery."""
from __future__ import annotations
import argparse
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import re
import tempfile

HOME = Path('/home/ubuntu/.hermes')
STATE = Path('/home/ubuntu/business/ops/state/glp-cron-error-sensor.json')


def assess(jobs, previous):
    if not isinstance(jobs, list) or not isinstance(previous, dict):
        raise ValueError('invalid_sensor_input')
    seen = previous.get('seen', {})
    if not isinstance(seen, dict):
        raise ValueError('invalid_sensor_state')
    current, alerts = {}, []
    for job in jobs:
        if not isinstance(job, dict):
            raise ValueError('invalid_job')
        if not re.search(r'glp', str(job.get('name', '')), re.I):
            continue
        jid = job.get('id')
        if not isinstance(jid, str) or not re.fullmatch(r'[a-f0-9]{12}', jid):
            raise ValueError('invalid_glp_job_id')
        streak = job.get('failure_streak', 0)
        if type(streak) is not int or streak < 0:
            raise ValueError('invalid_failure_streak')
        if job.get('last_status') != 'error' or streak < 2:
            continue
        last = job.get('last_run_at')
        if not isinstance(last, str) or not last:
            raise ValueError('missing_error_time')
        signature = [last, streak]
        current[jid] = signature
        if seen.get(jid) != signature:
            alerts.append({'job_id': jid, 'failure_streak': streak,
                           'last_run_at': last, 'enabled': job.get('enabled') is True})
    return {'schema': 'glp-cron-error-sensor-v1', 'seen': current,
            'alerts': sorted(alerts, key=lambda x: x['job_id']),
            'checked_at_utc': dt.datetime.now(dt.timezone.utc).isoformat()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--jobs', type=Path, default=HOME / 'cron/jobs.json')
    parser.add_argument('--state', type=Path, default=STATE)
    args = parser.parse_args()
    args.state.parent.mkdir(parents=True, exist_ok=True)
    with args.state.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = json.loads(args.jobs.read_text())
        jobs = data['jobs'] if isinstance(data, dict) else data
        previous = json.loads(args.state.read_text()) if args.state.exists() else {}
        result = assess(jobs, previous)
        fd, name = tempfile.mkstemp(prefix='.cron-error-', dir=args.state.parent)
        try:
            with os.fdopen(fd, 'w') as out:
                json.dump(result, out, indent=2)
                out.write('\n')
                out.flush()
                os.fsync(out.fileno())
            os.replace(name, args.state)
        finally:
            if os.path.exists(name):
                os.unlink(name)
        if result['alerts']:
            print(f"📊 **GLP cron health** — {len(result['alerts'])} jobs have repeated errors\n")
            print('consecutive errors: **2+**')
            print('→ inspect the exact job receipt; no automatic retry or state change')
            ids = ','.join(row['job_id'] for row in result['alerts'])
            print(f'-# jobs: {ids} · receipt: {args.state}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
