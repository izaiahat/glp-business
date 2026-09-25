#!/usr/bin/env python3
"""Hermes session bloat watchdog + reload/resume handoff generator.

No-agent script. It scans Hermes session DBs read-only, writes compact handoff
files for oversized live/recent sessions, and alerts only when a new issue
fingerprint appears unless --force is passed.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import sys
from typing import Any

sys.path.insert(0, '/home/ubuntu/business/ops')
from discord_fmt import (
    title as fmt_title, kv as fmt_kv, kvline as fmt_kvline, section as fmt_section,
    meaning as fmt_meaning, subtext as fmt_subtext, render as fmt_render,
)

ROOT = Path('/home/ubuntu/business')
REPORT_ROOT = ROOT / 'reports' / 'session-bloat'
STATE_PATH = ROOT / 'ops' / 'session_bloat_watchdog_state.json'
DEFAULT_HERMES_HOME = Path('/home/ubuntu/.hermes')
CRON_JOBS_PATH = DEFAULT_HERMES_HOME / 'cron' / 'jobs.json'
SESSION_PRUNER_ID = 'fbf9ba7b76c2'
SESSION_PRUNER_SCRIPT = 'session_pruner_weekly.py'
SESSION_PRUNER_SCHEDULE = '0 */6 * * *'
SESSION_INPUT_WARN = int(os.getenv('HERMES_SESSION_INPUT_WARN', '700000'))
SESSION_MESSAGES_WARN = int(os.getenv('HERMES_SESSION_MESSAGES_WARN', '220'))
SESSION_TOOL_WARN = int(os.getenv('HERMES_SESSION_TOOL_WARN', '120'))
DB_SIZE_WARN_MB = int(os.getenv('HERMES_SESSION_DB_WARN_MB', '2500'))
RECENT_LIMIT = int(os.getenv('HERMES_SESSION_RECENT_LIMIT', '25'))
RECENT_MESSAGE_LIMIT = int(os.getenv('HERMES_SESSION_HANDOFF_MESSAGES', '24'))
CONTENT_TAIL = int(os.getenv('HERMES_SESSION_HANDOFF_CONTENT_TAIL', '1200'))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def epoch_to_utc(value: Any) -> str | None:
    if value in (None, ''):
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat(timespec='seconds')
    except Exception:
        return None


def profile_paths() -> dict[str, Path]:
    out = {'default': DEFAULT_HERMES_HOME}
    profiles = DEFAULT_HERMES_HOME / 'profiles'
    if profiles.exists():
        for child in sorted(profiles.iterdir()):
            if child.is_dir() and (child / 'state.db').exists():
                out[child.name] = child
    return out


def connect_ro(db: Path) -> sqlite3.Connection:
    return sqlite3.connect(f'file:{db}?mode=ro', uri=True)


def tail(value: str | None, limit: int = CONTENT_TAIL) -> str:
    value = value or ''
    return value if len(value) <= limit else value[-limit:]


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {}


def save_state(payload: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(payload, indent=2, sort_keys=True) + '\n')


def _schedule_expression(job: dict[str, Any]) -> str:
    schedule = job.get('schedule')
    if isinstance(schedule, dict):
        return str(schedule.get('expr') or schedule.get('display') or '').strip()
    return str(schedule or job.get('schedule_display') or '').strip()


def automatic_pruner_issue() -> dict[str, Any] | None:
    """Report detector-without-remedy drift without executing or mutating cron."""
    try:
        data = json.loads(CRON_JOBS_PATH.read_text(encoding='utf-8'))
        rows = data.get('jobs') if isinstance(data, dict) else data
        jobs = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    except Exception as exc:
        return {
            'profile': 'default',
            'severity': 'red',
            'kind': 'automatic_pruner_missing_or_misconfigured',
            'reasons': [f'cron_registry_unreadable:{type(exc).__name__}'],
        }
    matches = [job for job in jobs if job.get('id') == SESSION_PRUNER_ID or job.get('script') == SESSION_PRUNER_SCRIPT]
    reasons: list[str] = []
    if len(matches) != 1:
        reasons.append(f'expected_one_pruner_found_{len(matches)}')
    else:
        job = matches[0]
        if job.get('id') != SESSION_PRUNER_ID:
            reasons.append('canonical_job_id_drift')
        if job.get('enabled') is not True:
            reasons.append('pruner_disabled')
        if job.get('no_agent') is not True:
            reasons.append('pruner_not_no_agent')
        if job.get('script') != SESSION_PRUNER_SCRIPT:
            reasons.append('pruner_script_mismatch')
        if _schedule_expression(job) != SESSION_PRUNER_SCHEDULE:
            reasons.append('pruner_schedule_mismatch')
    if not reasons:
        return None
    return {
        'profile': 'default',
        'severity': 'red',
        'kind': 'automatic_pruner_missing_or_misconfigured',
        'reasons': reasons,
        'expected_job_id': SESSION_PRUNER_ID,
        'expected_script': SESSION_PRUNER_SCRIPT,
        'expected_schedule': SESSION_PRUNER_SCHEDULE,
    }


def recent_messages(con: sqlite3.Connection, session_id: str) -> list[dict[str, Any]]:
    rows = list(con.execute(
        '''select id, role, tool_name, timestamp, content
           from messages
           where session_id = ? and active = 1
           order by id desc
           limit ?''',
        (session_id, RECENT_MESSAGE_LIMIT),
    ))
    rows.reverse()
    return [
        {
            'id': mid,
            'role': role,
            'tool_name': tool_name,
            'timestamp_utc': epoch_to_utc(ts),
            'content_tail': tail(content),
            'content_chars': len(content or ''),
        }
        for mid, role, tool_name, ts, content in rows
    ]


def write_handoff(report_dir: Path, profile: str, session: dict[str, Any], messages: list[dict[str, Any]]) -> Path:
    sid = session['id']
    safe_sid = ''.join(ch if ch.isalnum() or ch in '-_' else '-' for ch in sid)[:64]
    path = report_dir / f'{profile}-{safe_sid}-handoff.md'
    lines = [
        f'# Hermes session reload/resume handoff — {profile}',
        '',
        f'- Generated: {utc_now()}',
        f'- Session: `{sid}`',
        f'- Source: `{session.get("source")}`',
        f'- Title: {session.get("title") or "(untitled)"}',
        f'- Started: {session.get("started_at_utc")}',
        f'- Ended: {session.get("ended_at_utc") or "LIVE / not ended"}',
        f'- Message count: `{session.get("message_count")}`',
        f'- Tool calls: `{session.get("tool_call_count")}`',
        f'- Input tokens: `{session.get("input_tokens")}`',
        f'- Output tokens: `{session.get("output_tokens")}`',
        '',
        '## Resume instruction',
        '',
        'Start a fresh Hermes/Discord session and say:',
        '',
        f'> Resume from handoff `{path}`. Read `/home/ubuntu/business/STATE.md`, preserve the current task list, and continue from the latest incomplete item. Do not reload old full transcript unless required.',
        '',
        '## Recent message tails',
        '',
    ]
    for m in messages:
        tool = f" / {m['tool_name']}" if m.get('tool_name') else ''
        lines.append(f"### {m['id']} — {m['role']}{tool} — {m.get('timestamp_utc')}")
        lines.append('')
        lines.append('```')
        lines.append(m.get('content_tail') or '')
        lines.append('```')
        lines.append('')
    path.write_text('\n'.join(lines))
    return path


def scan_profile(profile: str, home: Path, report_dir: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    db = home / 'state.db'
    if not db.exists():
        return [], {'profile': profile, 'db_exists': False}
    db_mb = db.stat().st_size / (1024 * 1024)
    con = connect_ro(db)
    con.row_factory = sqlite3.Row
    issues: list[dict[str, Any]] = []
    meta = {'profile': profile, 'db_path': str(db), 'db_mb': round(db_mb, 1)}
    if db_mb >= DB_SIZE_WARN_MB:
        issues.append({'profile': profile, 'severity': 'yellow', 'kind': 'db_size_large', 'db_mb': round(db_mb, 1), 'threshold_mb': DB_SIZE_WARN_MB})
    rows = list(con.execute(
        '''select id, source, title, started_at, ended_at, message_count, tool_call_count,
                  input_tokens, output_tokens, reasoning_tokens, cache_read_tokens, cache_write_tokens
           from sessions
           where archived = 0
           order by started_at desc
           limit ?''',
        (RECENT_LIMIT,),
    ))
    for row in rows:
        session = dict(row)
        session['started_at_utc'] = epoch_to_utc(session.get('started_at'))
        session['ended_at_utc'] = epoch_to_utc(session.get('ended_at'))
        reasons = []
        live = session.get('ended_at') is None
        if int(session.get('input_tokens') or 0) >= SESSION_INPUT_WARN:
            reasons.append(f"input_tokens>={SESSION_INPUT_WARN}")
        if int(session.get('message_count') or 0) >= SESSION_MESSAGES_WARN:
            reasons.append(f"message_count>={SESSION_MESSAGES_WARN}")
        if int(session.get('tool_call_count') or 0) >= SESSION_TOOL_WARN:
            reasons.append(f"tool_call_count>={SESSION_TOOL_WARN}")
        if not reasons:
            continue
        messages = recent_messages(con, session['id'])
        handoff = write_handoff(report_dir, profile, session, messages)
        issues.append({
            'profile': profile,
            'severity': 'orange' if live else 'yellow',
            'kind': 'session_bloat_or_live_handoff',
            'session_id': session['id'],
            'source': session.get('source'),
            'title': session.get('title'),
            'message_count': session.get('message_count'),
            'tool_call_count': session.get('tool_call_count'),
            'input_tokens': session.get('input_tokens'),
            'started_at_utc': session.get('started_at_utc'),
            'ended_at_utc': session.get('ended_at_utc'),
            'reasons': reasons,
            'handoff_path': str(handoff),
        })
    con.close()
    return issues, meta


def fingerprint(issues: list[dict[str, Any]]) -> str:
    stable = [
        {k: v for k, v in item.items() if k not in {'handoff_path'}}
        for item in issues
    ]
    return hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--force', action='store_true', help='emit report even if issue fingerprint is unchanged')
    args = ap.parse_args()
    ts = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    report_dir = REPORT_ROOT / ts
    report_dir.mkdir(parents=True, exist_ok=True)
    all_issues: list[dict[str, Any]] = []
    pruner_issue = automatic_pruner_issue()
    if pruner_issue:
        all_issues.append(pruner_issue)
    metas: list[dict[str, Any]] = []
    for profile, home in profile_paths().items():
        issues, meta = scan_profile(profile, home, report_dir)
        all_issues.extend(issues)
        metas.append(meta)
    payload = {'timestamp_utc': utc_now(), 'profiles': metas, 'issues': all_issues}
    report_path = report_dir / 'session-bloat-report.json'
    report_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + '\n')
    fp = fingerprint(all_issues)
    state = load_state()
    if not all_issues:
        save_state({'last_ok_at_utc': utc_now(), 'last_fingerprint': fp})
        return 0
    if not args.force and state.get('last_fingerprint') == fp:
        save_state({**state, 'last_seen_same_fingerprint_at_utc': utc_now()})
        return 0
    save_state({'last_alert_at_utc': utc_now(), 'last_fingerprint': fp, 'last_report_path': str(report_path)})
    first = all_issues[0]
    if first['kind'] == 'db_size_large':
        head_fact = f"state.db {first['db_mb']} MB ≥ {first['threshold_mb']} MB cap"
    elif first['kind'] == 'automatic_pruner_missing_or_misconfigured':
        head_fact = 'automatic session pruner missing or misconfigured'
    else:
        head_fact = f"{len(all_issues)} session issue(s)"
    body = []
    for item in all_issues[:10]:
        if item['kind'] == 'db_size_large':
            body.append(f"**{item['severity']}** {item['profile']}: state.db **{item['db_mb']} MB** ≥ **{item['threshold_mb']} MB**")
        elif item['kind'] == 'automatic_pruner_missing_or_misconfigured':
            body.append(f"**{item['severity']}** {item['profile']}: automatic pruner · {', '.join(item['reasons'])}")
        else:
            body.append(f"**{item['severity']}** {item['profile']}: {', '.join(item['reasons'])}")
    if pruner_issue:
        meaning_line = 'automatic pruner is missing or misconfigured; restore the deterministic pruner before relying on this watchdog'
    elif any(item.get('kind') == 'db_size_large' for item in all_issues):
        meaning_line = 'automatic pruning runs before this watchdog; residual protected-session bloat requires the controlled repair path'
    else:
        meaning_line = 'reload handoff risk remains; inspect the report before continuing the affected session'
    print(fmt_render(
        fmt_title('action', 'Session Bloat', head_fact),
        fmt_kv('issues', len(all_issues)),
        *body,
        fmt_meaning(meaning_line),
        fmt_subtext(f'report: {report_path} · handoff paths in report'),
    ))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
