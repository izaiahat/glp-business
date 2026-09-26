#!/usr/bin/env python3
"""Account-scoped relay runner; global relay admission is authoritative.

No startup cleanup: a queued wave never closes another job's composer.
Only this wave's own jobs are polled; every send reserves shared capacity.
"""
import argparse
import importlib
import json
import subprocess
import sys
import time
from pathlib import Path

import gpt6pro_relay as relay

BUSINESS = Path('/home/ubuntu/business')
JOBS = BUSINESS / 'reports/ops/gpt6pro-relay/jobs'
RELAY = BUSINESS / 'scripts/gpt6pro_relay.py'


def state(job):
    p = JOBS / job / 'JOB.json'
    return json.loads(p.read_text()) if p.exists() else {}


def run(args, timeout=900):
    result = subprocess.run([sys.executable, str(RELAY), *args], capture_output=True, text=True, timeout=timeout)
    lines = (result.stdout + result.stderr).strip().splitlines()
    return result.returncode, lines[-1] if lines else ''


def has_capacity(account, envelope):
    # A wave can outlive a relay repair. Reload the registry policy on each
    # admission check, rather than retaining stale terminal-state semantics for
    # the life of the process (failed/THINKING_FAILED must release its slot).
    importlib.reload(relay)
    rows = relay.active_registry()
    return (len(rows) < min(6, envelope['max_concurrent_chats_per_ip']) and
            sum(row['account'] == account for row in rows) < min(3, envelope['max_concurrent_chats_per_account']))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--wave', required=True)
    parser.add_argument('--account', required=True)
    parser.add_argument('--interval', type=int, default=150)
    parser.add_argument('--max-wait', type=int, default=5400)
    args = parser.parse_args()
    cfg = relay.load_config()
    relay.account_of(cfg, args.account)
    envelope = cfg['envelope']
    interval = max(150, args.interval, envelope['poll_interval_s'])
    stagger = max(20, envelope['poll_stagger_s'])
    submit_gap = max(60, envelope['min_seconds_between_submits'])
    wave = json.loads(Path(args.wave).read_text())
    jobs = [j['job'] for j in wave['jobs']]
    if len(jobs) != len(set(jobs)):
        raise SystemExit('DUPLICATE_WAVE_JOB')
    for job in jobs:
        st = state(job)
        if st and st.get('account') != args.account:
            raise SystemExit('ACCOUNT_MISMATCH ' + job)
    # Preserve the registered pre-submit mirror refresh for packets that request it.
    if wave.get('github_mirror') and any(state(j).get('status') not in ('running', 'done') for j in jobs):
        mr = subprocess.run([sys.executable, str(BUSINESS / 'scripts/gpt6pro_mirror.py')], capture_output=True, text=True, timeout=600)
        if mr.returncode:
            raise SystemExit('MIRROR_REFRESH_FAILED; packet not submitted')
    def poll_owned():
        for job in jobs:
            if state(job).get('status') == 'running':
                rc, last = run(['poll', '--job', job])
                print(f'{job}: {last}', flush=True)
                if 'RATE_LIMITED' in last:
                    time.sleep(900)
                    break
                if rc not in (0, 3, 4):
                    raise SystemExit('POLL_REFUSED ' + last)
                time.sleep(stagger)
    started = time.monotonic()
    for job in jobs:
        st = state(job)
        if st.get('status') in ('running', 'done'):
            continue
        if st.get('retry_blocked') or st.get('status') in ('submitting', 'followup_submitting'):
            raise SystemExit('UNCERTAIN_JOB_REQUIRES_RECONCILIATION ' + job)
        manifest = json.loads((JOBS / job / 'MANIFEST.json').read_text())
        if any(not manifest.get(k) for k in relay.PROVENANCE_FIELDS):
            raise SystemExit('PROVENANCE_REQUIRED ' + job)
        submit = ['submit', '--account', args.account, '--job', job, '--job-class', manifest['job_class'],
                  '--prompt', str(JOBS / job / 'PROMPT.md'), '--prompt-on-drive', '--attach', *manifest['attach']]
        if manifest.get('drive_attach'):
            submit += ['--drive-attach', *manifest['drive_attach']]
        for key in relay.PROVENANCE_FIELDS:
            submit += ['--' + key.replace('_', '-'), manifest[key]]
        while True:
            if time.monotonic() - started > args.max_wait:
                raise SystemExit('WAVE_TIMEOUT waiting for admission; uncertain sends never retried')
            if has_capacity(args.account, envelope):
                rc, last = run(submit, timeout=1200)
                print(f'submit {job}: rc={rc} {last}', flush=True)
                if rc == 0:
                    break
                if not last.startswith(('CAPACITY_', 'PACING_SUBMIT')):
                    raise SystemExit('SUBMIT_REFUSED ' + last)
            poll_owned()
            time.sleep(submit_gap)
        time.sleep(submit_gap)
    while any(state(j).get('status') == 'running' for j in jobs):
        poll_owned()
        if time.monotonic() - started > args.max_wait:
            return 3
        time.sleep(interval)
    states = {j: state(j).get('status') for j in jobs}
    print('WAVE_DONE', states)
    return 0 if all(v == 'done' for v in states.values()) else 1


if __name__ == '__main__':
    sys.exit(main())
