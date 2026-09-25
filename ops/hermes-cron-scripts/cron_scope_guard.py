#!/usr/bin/env python3
"""Default-profile recurring cron scope + Discord-output continuous guard.

Silent when healthy. The script never runs a job, posts directly, or reads any
other Hermes profile. Native scheduler delivery owns the one linted alert.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, '/home/ubuntu/business/ops')
from discord_fmt import kvline as fmt_kvline, meaning as fmt_meaning, render as fmt_render, subtext as fmt_subtext, title as fmt_title

JOBS = Path('/home/ubuntu/.hermes/cron/jobs.json')
REPORT_DIR = Path('/home/ubuntu/business/reports/cron-scope-guard')
OUTPUT_GUARD = Path('/home/ubuntu/business/ops/cron_discord_output_guard.py')
DEFAULT_MAX_TOKENS = 260000
SESSION_PRUNER_ID = 'fbf9ba7b76c2'
SESSION_PRUNER_SCRIPT = 'session_pruner_weekly.py'
SESSION_PRUNER_SCHEDULE = '0 */6 * * *'
SESSION_WATCHDOG_SCRIPT = 'session_bloat_watchdog.py'
SESSION_WATCHDOG_SCHEDULE = '30 */6 * * *'


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def load_jobs() -> list[dict[str, Any]]:
    data = json.loads(JOBS.read_text(encoding='utf-8'))
    rows = data.get('jobs') if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise RuntimeError('default-profile jobs store has invalid shape')
    return [job for job in rows if isinstance(job, dict)]


def is_recurring(job: dict[str, Any]) -> bool:
    schedule = job.get('schedule')
    if isinstance(schedule, dict):
        return schedule.get('kind') in {'cron', 'interval'}
    return bool(schedule)


def schedule_expression(job: dict[str, Any]) -> str:
    schedule = job.get('schedule')
    if isinstance(schedule, dict):
        return str(schedule.get('expr') or schedule.get('display') or '').strip()
    return str(schedule or job.get('schedule_display') or '').strip()


def session_protection_issues(jobs: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Require one working deterministic remedy whenever the detector is live.

    A script-only no-agent job intentionally has no legacy ``command`` field.
    Its executable contract is ``no_agent=true`` plus a non-empty ``script``.
    """
    issues: list[dict[str, str]] = []
    pruners = [job for job in jobs if job.get('id') == SESSION_PRUNER_ID or job.get('script') == SESSION_PRUNER_SCRIPT]
    watchdogs = [job for job in jobs if job.get('script') == SESSION_WATCHDOG_SCRIPT]

    if len(pruners) != 1:
        issues.append({
            'severity': 'action',
            'name': 'Session DB automatic pruner',
            'issue': f'expected exactly one protected pruner; found {len(pruners)}',
        })
    else:
        pruner = pruners[0]
        failures: list[str] = []
        if pruner.get('id') != SESSION_PRUNER_ID:
            failures.append(f'id={pruner.get("id")} expected={SESSION_PRUNER_ID}')
        if pruner.get('enabled') is not True:
            failures.append('enabled=false')
        if pruner.get('no_agent') is not True:
            failures.append('no_agent=false')
        if not str(pruner.get('script') or '').strip():
            failures.append('script missing')
        if pruner.get('script') != SESSION_PRUNER_SCRIPT:
            failures.append(f'script={pruner.get("script")}')
        expr = schedule_expression(pruner)
        if expr != SESSION_PRUNER_SCHEDULE:
            failures.append(f'schedule={expr or "missing"}')
        if failures:
            issues.append({
                'severity': 'action',
                'name': 'Session DB automatic pruner',
                'issue': '; '.join(failures),
            })

    if len(watchdogs) != 1:
        issues.append({
            'severity': 'action',
            'name': 'Session bloat watchdog',
            'issue': f'expected exactly one watchdog; found {len(watchdogs)}',
        })
    else:
        watchdog = watchdogs[0]
        failures: list[str] = []
        if watchdog.get('enabled') is not True:
            failures.append('enabled=false')
        expr = schedule_expression(watchdog)
        if expr != SESSION_WATCHDOG_SCHEDULE:
            failures.append(f'schedule={expr or "missing"}')
        if failures:
            issues.append({
                'severity': 'action',
                'name': 'Session bloat watchdog',
                'issue': '; '.join(failures),
            })
    return issues


def scope_issues(jobs: list[dict[str, Any]]) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = session_protection_issues(jobs)
    for job in jobs:
        if not job.get('enabled', True) or not is_recurring(job):
            continue
        max_tokens = job.get('max_tokens')
        if not job.get('no_agent') and (max_tokens is None or int(max_tokens) > DEFAULT_MAX_TOKENS):
            issues.append({'severity': 'warn', 'name': str(job.get('name') or 'unnamed cron'), 'issue': f'max_tokens={max_tokens} exceeds {DEFAULT_MAX_TOKENS}'})
        prompt = str(job.get('prompt') or '').lower()
        repeat = job.get('repeat')
        if any(term in prompt for term in ['site crawl', 'deep research', 'scrape all', 'full audit']) and not repeat:
            issues.append({'severity': 'warn', 'name': str(job.get('name') or 'unnamed cron'), 'issue': 'broad recurring scope without an explicit repeat contract'})
        if not job.get('no_agent') and not job.get('enabled_toolsets'):
            issues.append({'severity': 'warn', 'name': str(job.get('name') or 'unnamed cron'), 'issue': 'agent cron has no explicit enabled_toolsets'})
    return issues


def output_guard_result() -> dict[str, Any]:
    proc = subprocess.run(
        [sys.executable, '-B', str(OUTPUT_GUARD), '--json'],
        text=True, capture_output=True, timeout=180,
    )
    try:
        result = json.loads((proc.stdout or '').strip())
    except Exception:
        return {'status': 'fail', 'failures': ['continuous_output_guard_unparseable'], 'returncode': proc.returncode}
    result['returncode'] = proc.returncode
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--check-only', action='store_true', help='explicit read-only verification mode')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()

    jobs = load_jobs()
    scope = scope_issues(jobs)
    output = output_guard_result()
    style_failures = list(output.get('failures') or [])
    payload = {
        'schema': 'cron_scope_and_discord_guard_v1',
        'checked_at_utc': utc_now(),
        'status': 'pass' if not style_failures else 'fail',
        'scope_status': 'warn' if scope else 'pass',
        'enabled_recurring_jobs': sum(1 for job in jobs if job.get('enabled', True) and is_recurring(job)),
        'scope_issue_count': len(scope),
        'discord_output_failure_count': len(style_failures),
        'scope_issues': scope,
        'discord_output_guard': output,
        'safety': {
            'jobs_executed': False, 'discord_posts': False, 'other_profiles_read': False,
            'private_payloads_read': False,
        },
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    report = REPORT_DIR / f'cron-scope-{stamp}.json'
    report.write_text(json.dumps(payload, indent=2, sort_keys=True) + '\n', encoding='utf-8')

    if args.json:
        print(json.dumps(payload, sort_keys=True))
        return 0 if payload['status'] == 'pass' else 1
    if not scope and not style_failures:
        return 0
    status_key = 'action' if style_failures else 'warn'
    print(fmt_render(
        fmt_title(status_key, 'Cron Guard', f'{len(scope) + len(style_failures)} policy failure(s) need attention'),
        fmt_kvline(('scope issues', len(scope)), ('output-style failures', len(style_failures)), ('jobs executed', 0)),
        fmt_meaning('fix declarations/configuration before the next scheduling sweep; no cron was force-run'),
        fmt_subtext(f'report: {report}'),
    ))
    # Native alert content is valid even though policy failed; avoid a second
    # scheduler-generated traceback/error wrapper.
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
