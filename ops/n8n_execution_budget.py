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

    Refresh at most hourly. Daily line follows Pacific day, accounting UTC
    month is explicit. The existing incident ledger owns alert deduplication.
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
                parse_time(state['snapshot']['generated_at_utc'])
            if not isinstance(state.get('month_high_water', 0), int):
                raise ValueError('state_count')
        except (ValueError, KeyError, TypeError, OSError):
            return {'snapshot': {'status': 'UNKNOWN', 'month_executions_observed': None,
                    'projected_month_end': None, 'cap': CAP, 'generated_at_utc': now.isoformat()},
                    'daily_line': 'n8n execution budget: UNKNOWN/10000; monthly ledger unreadable; not reset',
                    'issue': {'classification': 'n8n_execution_budget_unknown', 'severity': 'yellow',
                              'reason': 'budget_state_unreadable', 'evidence': 'Monthly observation ledger not reset.',
                              'operator_fix': 'Reconcile retained ledger and vendor usage before repairing state.'}}
        month = now.astimezone(timezone.utc).strftime('%Y-%m')
        snapshot = state.get('snapshot')
        age = (now - parse_time(snapshot['generated_at_utc'])).total_seconds() if snapshot else None
        if snapshot is None or snapshot.get('month_utc') != month or age is None or age < 0 or age >= 3600 or snapshot['status'] == 'UNKNOWN':
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
        line = None
        if state.get('daily_line_day') != day:
            count = snapshot.get('month_execution_high_water')
            projected = snapshot.get('projected_month_end')
            line = (f"n8n execution budget: {count if count is not None else 'UNKNOWN'}/{CAP} observed high-water; "
                    f"projected month-end {projected if projected is not None else 'UNKNOWN'}/{CAP}; "
                    f"month={month} UTC; alert=70%; status={snapshot['status']}; billing counter separate")
            state['daily_line_day'] = day
        status = snapshot['status']
        issue = None
        if status != 'PASS':
            issue = {'classification': ('n8n_execution_budget_unknown' if status == 'UNKNOWN' else
                                        'n8n_execution_budget_schedule_drift' if status == 'DRIFT' else
                                        'n8n_execution_budget_70_percent'),
                     'severity': 'red' if status == 'CAP_RISK' else 'yellow',
                     'reason': status,
                     'evidence': f"month={month}; observed={snapshot.get('month_execution_high_water')}; projected={snapshot.get('projected_month_end')}; cap={CAP}",
                     'operator_fix': 'Reconcile the vendor usage counter and event callers; keep migrated polling on the direct host bridge. Do not disable postbacks or replay pending effects.'}
        if not dry_run:
            atomic_json(path, state)
        return {'snapshot': snapshot, 'daily_line': line, 'issue': issue}


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
            'usage_semantics': 'retained execution lower bound; all modes conservatively counted; monotonic monthly high-water',
            'by_workflow': dict(Counter(r['workflowId'] for r in current))}
