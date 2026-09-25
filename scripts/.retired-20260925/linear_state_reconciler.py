#!/usr/bin/env python3
"""Keep Linear's state current automatically from observable fleet reality.

Operator 2026-09-15: "linear isnt updating showing tasks as in progress it needs to update
always automatically so i can always see our current state".

The orchestrator kept dispatching work to a seat without moving the issue out of Todo, so
Linear showed stale state while a seat was actively working. Fixing the orchestrator's memory
is not a fix; this reconciles from OBSERVED state on a schedule.

Reality sources (both cheap and read-only):
  1. ops/linear/dispatch-ledger.jsonl  - append-only, written by scripts/linear_dispatch.sh
  2. live tmux pane state              - is that seat actually working right now

Rules honoured:
  * Only the orchestrator changes Linear state, so this runs with LINEAR_ROLE=orchestrator.
  * It NEVER closes an issue and never sets Done/Canceled - closure keeps its single pass path
    with its RESULT receipt. This only moves Todo -> In Progress for work that has a live owner.
  * It never invents evidence: no RESULT.json is fabricated to satisfy a transition.
  * Idempotent: an issue already In Progress is left alone.
"""
import fcntl, json, os, pathlib, subprocess, sys

BUSINESS = pathlib.Path('/home/ubuntu/business')
LEDGER = BUSINESS / 'ops/linear/dispatch-ledger.jsonl'
sys.path.insert(0, str(BUSINESS / 'scripts'))



def pane_busy(pane: str) -> bool:
    """A pane is busy when its native status bar is NOT at a ready prompt."""
    try:
        out = subprocess.run(['tmux', 'capture-pane', '-p', '-t', pane, '-S', '-4'],
                             capture_output=True, text=True, timeout=15).stdout
    except Exception:
        return False
    status = [l for l in out.splitlines() if '─ ' in l]
    if not status:
        return False
    return '─ ready' not in status[-1]


def live_owners() -> dict:
    """issue -> pane, for the most recent dispatch per pane whose seat is still working."""
    if not LEDGER.exists():
        return {}
    latest = {}
    for line in LEDGER.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        if row.get('pane') and row.get('issue'):
            latest[row['pane']] = row          # last dispatch wins per pane
    return {r['issue']: p for p, r in latest.items() if pane_busy(p)}


def main() -> int:
    from linear_client import Client, LinearError
    from linear_project_dashboard import sync_projects
    if os.environ.get('LINEAR_ROLE') != 'orchestrator':
        raise LinearError('Reconciler requires LINEAR_ROLE=orchestrator in its invocation')
    # Cron ticks can overlap on a slow API; one process owns both issue and project writes.
    with open(BUSINESS / 'ops/linear/linear-state-reconciler.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({'status': 'SKIP', 'reason': 'previous reconciler still running'}))
            return 0
        return reconcile(Client(), sync_projects)


def reconcile(c, sync_projects):
    owners = live_owners()
    changed, skipped = [], []
    if owners:
        team = c.team('GLP')
        states = {s['name']: s['id'] for s in c.connection('states', {'id': team['id']})}
        for issue, pane in owners.items():
            try:
                g = c.gql('query($i:String!){issue(id:$i){id identifier state{name}}}', {'i': issue})['issue']
            except Exception as exc:
                skipped.append({'issue': issue, 'reason': type(exc).__name__})
                continue
            now = g['state']['name']
            if now == 'Todo':
                c.operation('issue_update', {'id': g['id'], 'i': {'stateId': states['In Progress']}})
                back = c.gql('query($i:String!){issue(id:$i){identifier state{name}}}', {'i': issue})['issue']
                changed.append({'issue': issue, 'pane': pane, 'from': 'Todo', 'to': back['state']['name']})
            else:
                skipped.append({'issue': issue, 'state': now, 'reason': 'not Todo; left alone'})
    dashboard = sync_projects(c, owners)
    print(json.dumps({'status': 'PASS', 'changed': len(changed), 'changes': changed,
                      'skipped': skipped, 'dashboard': dashboard}, indent=1))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
