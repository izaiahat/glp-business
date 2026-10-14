"""Host-owned weekly analysis: durable result, once completion, once consumer ACK.

Cogny is a read rail, never the owner or completion authority. Stable task IDs
are genuine host tasks, not invented Cogny IDs. No sends or provider mutations.
"""
from __future__ import annotations
import datetime as dt
import fcntl
import hashlib
import json
from decimal import Decimal
from pathlib import Path
import uuid

from ops.glp.seo_weekly_budget import week_start
from ops.glp.weekly_strategy_sync import atomic

ROOT = Path('/home/ubuntu/business/reports/glp-clinic-scout/weekly-analytics-owner')
CONSUMER = '020bcdd7f0e1'


def metered_status(report):
    spend = report.get('weekly_spend', {})
    cap = Decimal(str(spend.get('cap_usd', 0)))
    known = Decimal(str(spend.get('committed_usd', 0)))
    if cap != 25 or not known.is_finite() or not 0 <= known <= cap:
        raise ValueError('host_weekly_budget_unproved')
    if spend.get('within_cap') is True:
        return 'WITHIN_CAP'
    # Completion of free, nonempty analysis is not permission to spend.
    # Carry the unchanged guard's genuine hold rather than claiming a known
    # total or stalling GSC/GA4 planning on an unrelated ambiguous SERP charge.
    if (spend.get('rail_journal_unknown') is True
            and not spend.get('open_reservations')
            and not spend.get('unknown_cost_reservations')
            and any(r.get('reason') == 'unknown_prior_spend_this_week'
                    for r in spend.get('refusals', []))
            and report.get('module_statuses', {}).get('keyword_gap') == 'UNKNOWN_INPUT'):
        return 'HELD_UNKNOWN_PRIOR_SPEND'
    raise ValueError('host_weekly_budget_unproved')


def binding(path):
    path = Path(path).resolve()
    return {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def read_bound(spec):
    path = Path(spec['path'])
    if path.is_symlink():
        raise ValueError('host_binding_symlink')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != spec['sha256']:
        raise ValueError('host_binding_changed:' + str(path))
    return json.loads(raw)


def validate(result, week):
    report = json.loads(Path(result['report']).read_text())
    topics = json.loads(Path(result['topic_recommendations']).read_text())
    if topics['week_start'] != week:
        raise ValueError('host_analysis_week_mismatch')
    rows = topics.get('recommendations')
    if (not isinstance(rows, list) or not rows or
            not any((n or 0) > 0 for n in report.get('gsc_rows', {}).values()) or
            report.get('module_statuses', {}).get('page2') != 'PASS_ANALYSIS_ONLY'):
        raise ValueError('nonempty_weekly_analysis_required')
    metered_status(report)
    strategy = result['strategy_update']
    if strategy.get('status') != 'HOST_DURABLE_STRATEGY_INPUT':
        raise ValueError('host_strategy_required')
    updates = read_bound(strategy['updates'])
    if updates['week_start'] != week or not updates.get('topic_priorities'):
        raise ValueError('host_strategy_week_or_rows_invalid')
    return report, rows


def verify_completion(spec):
    completed = read_bound(spec)
    if (completed.get('schema') != 'glp-host-weekly-completion-v1' or
            completed.get('completion_count') != 1 or
            completed.get('task_id') != 'glp336-weekly-' + completed['week_start']):
        raise ValueError('host_completion_contract')
    for item in completed['analysis_bindings'].values():
        read_bound(item)
    return completed


def run_owned(root, producer, *, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    week = week_start(now).isoformat()
    task_id = 'glp336-weekly-' + week
    folder = Path(root)/week
    folder.mkdir(parents=True, exist_ok=True)
    with (folder/'.owner.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        completion = folder/'COMPLETED.json'
        staged = folder/'RESULT.json'
        if completion.exists():
            done = verify_completion(binding(completion))
            if done['week_start'] != week:
                raise ValueError('host_completion_week_mismatch')
            result = read_bound(done['result'])
        else:
            # Reuse a finished result after an interruption before completion.
            # Failed partial collection uses a new attempt under the same stable
            # host task; the shared spend ledger still bounds every request.
            if staged.exists():
                result = json.loads(staged.read_text())
            else:
                atomic(folder/'TASK.json', {'schema': 'glp-host-weekly-task-v1',
                    'task_id': task_id, 'week_start': week, 'owner': 'host',
                    'consumer': CONSUMER, 'claimed_at_utc': now.isoformat()})
                output = folder/('analysis-' + uuid.uuid4().hex[:8])
                result = producer(output)
                atomic(staged, result)
            report, rows = validate(result, week)
            analysis_bindings = {'report': binding(result['report']),
                'topics': binding(result['topic_recommendations']),
                'strategy': result['strategy_update']['updates']}
            atomic(completion, {'schema': 'glp-host-weekly-completion-v1',
                'task_id': task_id, 'week_start': week, 'owner': 'host',
                'completed_at_utc': now.isoformat(), 'completion_count': 1,
                'analysis_rows': len(rows), 'gsc_rows': report['gsc_rows'],
                'input_observations': report.get('input_observations'),
                'module_statuses': report['module_statuses'],
                'weekly_spend': report['weekly_spend'],
                'metered_collection_status': metered_status(report),
                'analysis_bindings': analysis_bindings, 'result': binding(staged),
                'cogny_queued_task_dependency': False})
        result = dict(result)
        result['host_completion'] = dict(binding(completion), task_id=task_id)
        return result


def consume(result, consumer):
    if consumer != CONSUMER:
        raise ValueError('host_consumer_not_registered')
    spec = result['host_completion']
    completed = verify_completion(spec)
    expected = read_bound(completed['result'])
    if {k: v for k, v in result.items() if k != 'host_completion'} != expected:
        raise ValueError('host_consumer_result_mismatch')
    folder = Path(spec['path']).parent
    with (folder/'.owner.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = folder/'CONSUMED-GLP44.json'
        doc = {'schema': 'glp-host-weekly-consumer-ack-v1', 'task_id': completed['task_id'],
            'consumer': consumer, 'completion': spec, 'ack_count': 1,
            'analysis_rows': completed['analysis_rows'],
            'report': completed['analysis_bindings']['report'],
            'strategy': completed['analysis_bindings']['strategy']}
        if path.exists():
            if json.loads(path.read_text()) != doc:
                raise ValueError('host_consumer_ack_changed')
        else:
            atomic(path, doc)
        return dict(doc, **binding(path))
