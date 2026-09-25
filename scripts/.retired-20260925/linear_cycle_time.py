#!/usr/bin/env python3
"""linear_cycle_time.py [--since ISO] [--json] — weekly flow metrics for plan children, derived from issue.history.

Operator 2026-09-11: measure the standard. Everything that can be derived is derived from Linear's own
history stream rather than from hand-typed comments (reports/ops/efficiency-20260911/AUDIT-LINEAR-SETUP-VS-PRACTICE.md §3,
RESEARCH-FLEET-QUEUE-PRACTICE.md §4 M1-M7):

  queue time      startedAt − createdAt                       (dispatch latency)
  blockers        transitions into Blocked                    (history)
  blocked time    Σ time in Blocked + In Review               (history; M4 non-value time)
  flow efficiency (cycle − blocked) / cycle                   (M4)
  first-pass yield no Done→non-Done transition                (history)
  rework          `rework:*` labels AND **REWORK[...]** comments (labels are the machine-readable half)
  root inflation  children created after the root started     (M5)
  escaped defects the `escaped` label                         (M7)

`blocked_writes` is gone: it counted suppressed RESULT-REWRITE lines in monitor-events.log, which is not a
blocker signal. Blocked transitions from history replace it. monitor-events.log is read once, not per row."""
import json, os, re, sys, datetime, math
sys.path.insert(0, os.path.dirname(__file__))
import linear_client as lc

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MONITOR_LOG = os.path.join(ROOT, 'reports/ops/orchestration-restore-20260907/PLAN/monitor-events.log')
NON_VALUE_STATES = ('Blocked', 'In Review')
REWORK_CAUSES = ('brief', 'machine', 'supply')

Q = '''query($after:String,$since:DateTimeOrDuration){ issues(first:50,after:$after,filter:{team:{key:{eq:"GLP"}}, parent:{null:false}, updatedAt:{gt:$since}}){
 nodes{identifier title state{name} startedAt completedAt createdAt description
  parent{identifier startedAt}
  labels{nodes{name}}
  comments{nodes{body}}
  history(first:50){nodes{createdAt fromState{name} toState{name}}}}
 pageInfo{hasNextPage endCursor}}}'''


def ts(value):
    return datetime.datetime.fromisoformat(value.replace('Z', '+00:00')) if value else None


def minutes(a, b):
    return round((b - a).total_seconds() / 60) if a and b else None


def percentile(values, fraction):
    """Nearest-rank percentile on a sorted copy; None when there is nothing to rank."""
    ordered = sorted(v for v in values if v is not None)
    if not ordered:
        return None
    return ordered[min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))]


def monitor_counts(path=MONITOR_LOG):
    """Read monitor-events.log exactly once; return {lane key: {stalls, nudges, resumes}}."""
    counts = {}
    if not os.path.exists(path):
        return counts
    with open(path, errors='replace') as stream:
        for line in stream:
            key = None
            match = re.search(r'\b(?:key|task)=([A-Za-z0-9_.-]+)', line) or re.search(r'\b([A-Z]{1,4}\d+[a-z]?)\b', line)
            if match:
                key = match.group(1)
            if not key:
                continue
            row = counts.setdefault(key, {'stalls': 0, 'nudges': 0, 'resumes': 0})
            if 'STALL' in line:
                row['stalls'] += 1
            if 'NUDGED' in line:
                row['nudges'] += 1
            if 'RE-RESUMED' in line or 'RESUMED' in line:
                row['resumes'] += 1
    return counts


def state_timeline(row, now):
    """(blocked_minutes, blockers, reopened) from the history stream."""
    events = sorted((h for h in ((row.get('history') or {}).get('nodes') or []) if h.get('toState')),
                    key=lambda h: h['createdAt'])
    blockers = sum(1 for h in events if h['toState']['name'] == 'Blocked')
    reopened = any(h.get('fromState') and h['fromState']['name'] == 'Done' and h['toState']['name'] != 'Done' for h in events)
    finished = ts(row.get('completedAt'))
    blocked = 0.0
    for index, h in enumerate(events):
        start = ts(h['createdAt'])
        end = ts(events[index + 1]['createdAt']) if index + 1 < len(events) else (finished or now)
        if h['toState']['name'] in NON_VALUE_STATES and end and start and end > start:
            blocked += (end - start).total_seconds() / 60
    return round(blocked), blockers, reopened


def rework_counts(row):
    """Both halves: the `rework:*` labels (set by linear_orchestrator.py rework) and the legacy comments."""
    labels = [l['name'] for l in (row.get('labels') or {}).get('nodes', [])]
    counts = {c: 0 for c in REWORK_CAUSES}
    for cause in REWORK_CAUSES:
        if 'rework:' + cause in labels:
            counts[cause] += 1
    for comment in (row.get('comments') or {}).get('nodes') or []:
        match = re.match(r'\*\*REWORK\[(brief|machine|supply)\]\*\*', comment.get('body') or '')
        if match:
            counts[match.group(1)] += 1
    return counts


def result_counts(row):
    match = re.search(r'Result: `([^`]+)`', row.get('description') or '')
    if not match or not os.path.isfile(match.group(1)):
        return None, None, None, None
    try:
        data = json.load(open(match.group(1)))
    except Exception:
        return None, None, None, os.path.basename(os.path.dirname(match.group(1)))
    return (len(data.get('artifacts_created') or []), len(data.get('changed_files') or []),
            data.get('steps_used'), os.path.basename(os.path.dirname(match.group(1))))


def compute(rows, monitor=None, now=None):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    monitor = monitor if monitor is not None else {}
    out = []
    for row in rows:
        if row['state']['name'] not in ('Done', 'In Progress', 'In Review', 'Blocked'):
            continue
        labels = [l['name'] for l in (row.get('labels') or {}).get('nodes', [])]
        blocked, blockers, reopened = state_timeline(row, now)
        cycle = minutes(ts(row.get('startedAt')), ts(row.get('completedAt')))
        arts, files, steps, key = result_counts(row)
        parent = row.get('parent') or {}
        out.append({
            'issue': row['identifier'],
            'lane': next((l[5:] for l in labels if l.startswith('lane:')), ''),
            'state': row['state']['name'],
            'root': parent.get('identifier'),
            'created_at': row.get('createdAt'),
            'root_started_at': parent.get('startedAt'),
            'minutes': cycle,
            'queue_minutes': minutes(ts(row.get('createdAt')), ts(row.get('startedAt'))),
            'blockers': blockers,
            'blocked_minutes': blocked,
            'blocked_transitions': blockers,
            'flow_efficiency': round((cycle - min(blocked, cycle)) / cycle, 2) if cycle else None,
            'first_pass': not reopened,
            'rework': rework_counts(row),
            'escaped': 'escaped' in labels,
            'artifacts': arts, 'changed_files': files, 'steps_used': steps,
            'monitor': monitor.get(key or '', {'stalls': 0, 'nudges': 0, 'resumes': 0}),
            'title': (row.get('title') or '')[:44],
        })
    out.sort(key=lambda x: (x['state'] != 'Done', x['issue']))
    return out


def summarize(out, since):
    done = [o for o in out if o['state'] == 'Done']
    done_minutes = [o['minutes'] for o in done if o['minutes'] is not None]
    started = [o for o in out if o['queue_minutes'] is not None or o['minutes'] is not None or o['state'] != 'Done']
    brief_defects = [o for o in out if o['rework']['brief']]
    rework_total = {c: sum(o['rework'][c] for o in out) for c in REWORK_CAUSES}
    attributed = sum(rework_total.values())
    blockers_total = sum(o['blockers'] for o in out)
    cycle_sum = sum(o['minutes'] for o in out if o['minutes'])
    blocked_sum = sum(o['blocked_minutes'] for o in out if o['minutes'])
    roots = {}
    for o in out:
        if not o['root']:
            continue
        row = roots.setdefault(o['root'], {'children': 0, 'late': 0})
        row['children'] += 1
        if o['root_started_at'] and o['created_at'] and ts(o['created_at']) > ts(o['root_started_at']):
            row['late'] += 1
    for row in roots.values():
        row['inflation'] = round(row['late'] / row['children'], 2) if row['children'] else None
    return {
        'since': since,
        'done_children': len(done_minutes),
        'median_minutes': percentile(done_minutes, 0.5),
        'p50_minutes': percentile(done_minutes, 0.5),
        'p85_minutes': percentile(done_minutes, 0.85),
        'p50_queue_minutes': percentile([o['queue_minutes'] for o in out], 0.5),
        'rework_total': rework_total,
        'rework_per_child': round(attributed / max(1, len(out)), 2),
        'brief_defect_rate': round(len(brief_defects) / max(1, len(started)), 2),
        'blocker_cause_mix': dict(rework_total, unattributed=max(0, blockers_total - attributed)),
        'blocked_transitions_total': blockers_total,
        'blocked_minutes_total': round(blocked_sum),
        'flow_efficiency': round((cycle_sum - min(blocked_sum, cycle_sum)) / cycle_sum, 2) if cycle_sum else None,
        'first_pass_yield': round(sum(1 for o in done if o['first_pass']) / len(done), 2) if done else None,
        'root_inflation': {k: v['inflation'] for k, v in sorted(roots.items())},
        'root_inflation_mean': round(sum(v['inflation'] for v in roots.values()) / len(roots), 2) if roots else None,
        'escaped_defects': sum(1 for o in out if o['escaped']),
        'children_started': len(started),
    }


def render(out, summary):
    print(f"{'issue':8} {'lane':7} {'state':12} {'min':>5} {'queue':>5} {'blk':>3} {'blkmin':>6} {'flow':>5} {'1st':>3} {'rw b/m/s':>8} {'stp':>3}  title")
    for o in out:
        cell = lambda v: '-' if v is None else str(v)
        print(f"{o['issue']:8} {o['lane']:7} {o['state']:12} {cell(o['minutes']):>5} {cell(o['queue_minutes']):>5} "
              f"{o['blockers']:>3} {o['blocked_minutes']:>6} {cell(o['flow_efficiency']):>5} {('y' if o['first_pass'] else 'n'):>3} "
              f"{o['rework']['brief']}/{o['rework']['machine']}/{o['rework']['supply']:>4} {cell(o['steps_used']):>3}  {o['title']}")
    print()
    print('WEEKLY METRICS (since ' + str(summary['since'])[:19] + ')')
    print(f"  done children          {summary['done_children']}   (started in window: {summary['children_started']})")
    print(f"  cycle minutes          p50 {summary['p50_minutes']}  p85 {summary['p85_minutes']}   queue p50 {summary['p50_queue_minutes']}")
    print(f"  brief-defect rate      {summary['brief_defect_rate']}   (target < 0.10)")
    print(f"  blocker cause mix      " + ', '.join(f"{k}={v}" for k, v in summary['blocker_cause_mix'].items()))
    print(f"  flow efficiency        {summary['flow_efficiency']}   (blocked+in-review {summary['blocked_minutes_total']} min over {summary['blocked_transitions_total']} blocked transitions)")
    print(f"  first-pass yield       {summary['first_pass_yield']}")
    print(f"  root inflation         mean {summary['root_inflation_mean']}   " + ', '.join(f"{k}={v}" for k, v in summary['root_inflation'].items()))
    print(f"  escaped defects        {summary['escaped_defects']}")


def main():
    since = sys.argv[sys.argv.index('--since') + 1] if '--since' in sys.argv else (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=7)).isoformat()
    client = lc.Client(role=os.environ.get('LINEAR_ROLE') or 'fleet')
    rows = client.pages(Q, {'since': since}, ('issues',))
    out = compute(rows, monitor_counts())
    summary = summarize(out, since)
    if '--json' in sys.argv:
        print(json.dumps({'summary': summary, 'children': out}, indent=1))
        return 0
    render(out, summary)
    print(json.dumps(summary))
    return 0


if __name__ == '__main__':
    sys.exit(main())
