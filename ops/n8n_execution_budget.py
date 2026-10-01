#!/usr/bin/env python3
"""Read-only execution budget guard; REST reads do NOT start workflows.

Count every retained mode conservatively. Observations are not the vendor's
billing counter: deletions/retention can undercount. Never disable event ingress,
replay an effect or change producer holds in response to a forecast.
"""
from __future__ import annotations
import calendar
import math
from collections import Counter
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode
from pathlib import Path
from zoneinfo import ZoneInfo
import fcntl
import hashlib
import json
import os
import tempfile

DEFAULT_STATE = Path('/home/ubuntu/business/ops/state/n8n-execution-budget.json')


def atomic_json(path, value):
    fd, temp = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(value, handle, sort_keys=True, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def health_check(state_path=DEFAULT_STATE, *, now=None, get=None, dry_run=False, audit=None):
    """Cached monitor on the existing health runner, no scheduler or sender.

    Refresh at most hourly, including failed reads. Daily line follows Pacific
    day; the UTC calendar-month boundary is an explicit, unverified billing
    assumption (public API/MCP do not expose this account's billing boundary).
    Stable threshold episodes are delivered by the existing incident ledger.
    Dry-run does not consume the live daily line or advance monthly state.
    """
    now = now or datetime.now(timezone.utc)
    if get is None:
        from n8n_dt import _native_http
        get = _native_http
    path = Path(state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            state = json.loads(path.read_text()) if path.exists() else {}
            if not isinstance(state, dict):
                raise ValueError('state_shape')
            if state.get('snapshot'):
                saved = state['snapshot']
                parse_time(saved['generated_at_utc'])
                if saved['status'] not in ('PASS', 'DRIFT', 'WARN_70_PERCENT', 'CAP_RISK', 'UNKNOWN'):
                    raise ValueError('state_status')
                if saved['status'] != 'UNKNOWN':
                    for key in ('month_execution_high_water', 'projected_month_end'):
                        if type(saved[key]) is not int or saved[key] < 0:
                            raise ValueError('state_snapshot_count')
            for key in ('month_high_water', 'warning_episode'):
                if type(state.get(key, 0)) is not int or state.get(key, 0) < 0:
                    raise ValueError('state_count')
            if not isinstance(state.get('observed_execution_ids', []), list):
                raise ValueError('state_ids')
            if type(state.get('warning_active', False)) is not bool:
                raise ValueError('state_warning')
        except (ValueError, KeyError, TypeError, OSError, AttributeError):
            return corrupt_state_health(path, now, dry_run)
        month = now.astimezone(timezone.utc).strftime('%Y-%m')
        snapshot = state.get('snapshot')
        age = (now - parse_time(snapshot['generated_at_utc'])).total_seconds() if snapshot else None
        # UNKNOWN is cached too: outages must not bypass the hourly read bound.
        month_changed = snapshot and snapshot.get('month_utc') != month and snapshot['status'] != 'UNKNOWN'
        if snapshot is None or month_changed or age is None or age < 0 or age >= 3600:
            try:
                rows, proof = collect(get, now)
                same_month = state.get('month_utc') == month
                known_ids = set(state.get('observed_execution_ids', [])) if same_month else set()
                known_ids.update(str(row['id']) for row in rows)
                previous = max(len(known_ids), state.get('month_high_water', 0) if same_month else 0)
                snapshot = summarize(rows, now, previous)
                state.update(month_utc=month, month_high_water=snapshot['month_execution_high_water'],
                             observed_execution_ids=sorted(known_ids))
                snapshot['pagination'] = proof
                snapshot['invariants'] = (audit or audit_invariants)(get)
                if snapshot['invariants']['status'] != 'PASS' and snapshot['status'] == 'PASS':
                    snapshot['status'] = 'DRIFT'
                state.update(month_utc=month, month_high_water=snapshot['month_execution_high_water'])
            except Exception as exc:
                snapshot = {'schema': 'glp.n8n_execution_budget.v1', 'generated_at_utc': now.isoformat(),
                    'month_utc': month, 'status': 'UNKNOWN', 'cap': CAP,
                    'month_executions_observed': None, 'projected_month_end': None,
                    'error_class': type(exc).__name__}
            state['snapshot'] = snapshot
        day = now.astimezone(ZoneInfo('America/Los_Angeles')).strftime('%Y-%m-%d')
        reporting_path, reporting = reporting_state(path, state)
        line = None
        if reporting.get('daily_line_day') != day:
            count = snapshot.get('month_execution_high_water')
            projected = snapshot.get('projected_month_end')
            line = (f"n8n execution budget: {count if count is not None else 'UNKNOWN'}/{CAP} observed high-water; "
                    f"projected month-end {projected if projected is not None else 'UNKNOWN'}/{CAP}; "
                    f"month={month} UTC (billing boundary assumed); alert=70%; status={snapshot['status']}; billing counter separate")
            reporting['daily_line_day'] = day
        state['daily_line_day'] = reporting.get('daily_line_day')
        status = snapshot['status']
        issue = None
        if status != 'UNKNOWN':
            above = max(snapshot['month_execution_high_water'], snapshot['projected_month_end']) >= CAP * .7
            if above and not state.get('warning_active', False):
                state['warning_episode'] = state.get('warning_episode', 0) + 1
            state['warning_active'] = above
        # UNKNOWN cannot prove recovery; neither unknown reads nor elapsed time
        # re-arm a threshold episode. Keep changing counts in evidence, not keys.
        if status == 'UNKNOWN' or state.get('warning_active', False):
            issue = {'classification': ('n8n_execution_budget_unknown' if status == 'UNKNOWN' else
                                        'n8n_execution_budget_70_percent'),
                     'severity': 'red' if status == 'CAP_RISK' else 'yellow',
                     'reason': status,
                     'evidence': f"month={month}; observed={snapshot.get('month_execution_high_water')}; projected={snapshot.get('projected_month_end')}; cap={CAP}",
                     'operator_fix': 'Reconcile the vendor usage counter and event callers; keep migrated polling on the direct host bridge. Do not disable postbacks or replay pending effects.'}
            if status != 'UNKNOWN':
                issue.update(incident_key=f"n8n-execution-budget:warning:{state['warning_episode']}", alert_once=True)
        if not dry_run:
            atomic_json(path, state)
            atomic_json(reporting_path, reporting)
        issues = []
        if issue:
            issues.append(issue)
        invariants = snapshot.get('invariants') or {}
        if invariants.get('article_inactive') is False:
            issues.append({'job_id': 'n8n-article-carrier-hold',
                          'classification': 'n8n_article_carrier_active', 'severity': 'red',
                          'reason': 'article_carrier_active',
                          'evidence': f'workflow={ARTICLE}; active=true; host cron owns publishing',
                          'operator_fix': 'Investigate unauthorized article-carrier activation under the existing release gate; do not execute or replay it.'})
        if invariants.get('enabled_schedule_nodes') or invariants.get('error_handler_recursion_findings'):
            issues.append({'job_id': 'n8n-published-invariants',
                          'classification': 'n8n_execution_budget_schedule_drift', 'severity': 'yellow',
                          'reason': 'published_schedule_or_error_handler_drift',
                          'evidence': json.dumps({k: invariants.get(k) for k in ('enabled_schedule_nodes', 'error_handler_recursion_findings')}, sort_keys=True),
                          'operator_fix': 'Reconcile published schedules and error-handler recursion without disabling event ingress or replaying effects.'})
        return {'snapshot': snapshot, 'daily_line': line, 'issue': issue, 'issues': issues}


def reporting_state(path, accounting=None):
    """Daily output custody is independent of the accounting ledger's health."""
    reporting_path = path.with_name(path.name + '.health.json')
    try:
        reporting = json.loads(reporting_path.read_text()) if reporting_path.exists() else {}
        if not isinstance(reporting, dict):
            raise ValueError('reporting_state_shape')
    except (ValueError, OSError):
        reporting = {}
    if not reporting.get('daily_line_day') and accounting:
        reporting['daily_line_day'] = accounting.get('daily_line_day')
    return reporting_path, reporting


def corrupt_state_health(path, now, dry_run):
    """Quarantine exact bytes, never repair accounting to suppress reporting.

    A separate reporting-only sidecar survives an unreadable monthly ledger.
    The original stays in place (fail closed) until an authorized repair.
    """
    try:
        raw = path.read_bytes()
    except OSError:
        raw = None
    digest = hashlib.sha256(raw).hexdigest() if raw is not None else 'unreadable'
    quarantine = path.with_name(path.name + '.quarantine.' + digest)
    reporting_path, reporting = reporting_state(path)
    day = now.astimezone(ZoneInfo('America/Los_Angeles')).strftime('%Y-%m-%d')
    line = None
    if reporting.get('daily_line_day') != day:
        line = 'n8n execution budget: UNKNOWN/10000; monthly ledger unreadable; quarantined; not reset'
        reporting['daily_line_day'] = day
    if not dry_run:
        if raw is not None and not quarantine.exists():
            fd = os.open(quarantine, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
        reporting['quarantine_sha256'] = digest
        atomic_json(reporting_path, reporting)
    issue = {'classification': 'n8n_execution_budget_unknown', 'severity': 'yellow',
             'reason': 'budget_state_unreadable', 'evidence': f'Monthly observation ledger not reset; quarantine_sha256={digest}',
             'operator_fix': 'Reconcile quarantined ledger and vendor usage before repairing state.',
             'incident_key': f'n8n-execution-budget:corrupt:{digest}', 'alert_once': True}
    return {'snapshot': {'status': 'UNKNOWN', 'month_executions_observed': None,
                         'projected_month_end': None, 'cap': CAP, 'generated_at_utc': now.isoformat()},
            'daily_line': line, 'issue': issue, 'issues': [issue]}


def collect(get, now, max_pages=250):
    """Exhaust newest-first metadata to the month boundary or fail UNKNOWN.

    One page must include the last completed month when the current month has
    no rows; an empty/current count never follows from a failed API call.
    """
    start = now.astimezone(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    cursor = None
    seen_cursors = set()
    rows = {}
    prior_time = None
    for page in range(1, max_pages + 1):
        query = {'limit': 100, 'includeData': 'false'}
        if cursor:
            query['cursor'] = cursor
        response = get('GET', '/executions?' + urlencode(query))
        batch = response.get('data')
        if not isinstance(batch, list):
            raise ValueError('execution_page_invalid')
        reached_boundary = False
        for row in batch:
            stamp = parse_time(row['startedAt'])
            if prior_time is not None and stamp > prior_time:
                raise ValueError('execution_history_order_unknown')
            prior_time = stamp
            if stamp < start:
                reached_boundary = True
            else:
                rows[str(row['id'])] = row
        next_cursor = response.get('nextCursor')
        if reached_boundary or not next_cursor:
            return list(rows.values()), {'complete': True, 'pages': page,
                'boundary': 'month_start' if reached_boundary else 'history_exhausted',
                'last_execution_utc': batch[0]['startedAt'] if page == 1 and batch else None}
        if next_cursor in seen_cursors or not batch:
            raise ValueError('execution_cursor_invalid')
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    raise ValueError('execution_pagination_incomplete')

CAP = 10000
TOP_FIVE = ('5GAXmg5MLRIRYulZ', 'eqTjIVg9gTjpBLgD', 'tIcpuDF4jsEbGCP4', '3x1JYTskM5cq15aI', 'JY7iv9pljAu1R3TI')
ARTICLE = 'uabEqV8bzEYanLVJ'


def audit_invariants(get):
    enabled = []
    errors = []
    for wid in TOP_FIVE:
        w = get('GET', '/workflows/' + wid)
        active = w.get('activeVersion') or {}
        if not w.get('active') or active.get('versionId') != w.get('activeVersionId') or not active.get('nodes'):
            raise ValueError('published_workflow_binding_unknown')
        for node in active['nodes']:
            if node['type'] in ('n8n-nodes-base.scheduleTrigger', 'n8n-nodes-base.cron') and not node.get('disabled'):
                enabled.append({'workflow_id': wid, 'node': node['name']})
        if wid == 'tIcpuDF4jsEbGCP4' and (w.get('settings') or {}).get('errorWorkflow'):
            errors.append('error_handler_error_workflow_configured')
    article = get('GET', '/workflows/' + ARTICLE)
    inactive = article.get('active') is False
    return {'status': 'PASS' if not enabled and not errors and inactive else 'DRIFT',
            'enabled_schedule_nodes': enabled, 'error_handler_recursion_findings': errors,
            'article_inactive': inactive, 'mutation_count': 0}


def parse_time(value):
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('execution_timestamp_not_aware')
    return dt.astimezone(timezone.utc)


def summarize(executions, now, previous_count=0):
    if now.tzinfo is None:
        raise ValueError('budget_clock_not_aware')
    now = now.astimezone(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    days = calendar.monthrange(now.year, now.month)[1]
    end = start + timedelta(days=days)
    unique = {str(r['id']): r for r in executions}
    current = [r for r in unique.values() if start <= parse_time(r['startedAt']) <= now]
    observed = len(current)
    count = max(observed, previous_count)
    elapsed = max((now - start).total_seconds() / 86400, 1 / 86400)
    remaining = (end - now).total_seconds() / 86400
    day_count = sum(parse_time(r['startedAt']) >= now - timedelta(days=1) for r in current)
    week_count = sum(parse_time(r['startedAt']) >= now - timedelta(days=7) for r in current)
    daily_rate = max(count / elapsed, day_count / min(elapsed, 1), week_count / min(elapsed, 7))
    projected = math.ceil(count + daily_rate * remaining)
    status = 'CAP_RISK' if max(count, projected) >= CAP else 'WARN_70_PERCENT' if max(count, projected) >= CAP * .7 else 'PASS'
    return {'schema': 'glp.n8n_execution_budget.v1', 'generated_at_utc': now.isoformat(),
            'month_utc': start.strftime('%Y-%m'), 'cap': CAP, 'alert_threshold': 7000,
            'month_executions_observed': observed, 'month_execution_high_water': count,
            'last_24h_executions': day_count, 'last_7d_executions': week_count,
            'daily_rate': daily_rate, 'projected_month_end': projected,
            'projection_method': 'max(month-to-date, last24h, last7d) rate for remaining UTC month',
            'status': status, 'billed_usage': None,
            'billing_boundary': 'UTC calendar month assumed; account billing timezone/period not exposed by public API/MCP',
            'usage_semantics': 'retained execution lower bound; all modes conservatively counted; monotonic monthly high-water',
            'by_workflow': dict(Counter(r['workflowId'] for r in current))}
