"""Deadline-gated CBS input and Alexandria evidence, before any model startup."""
import argparse
import fcntl
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path('/home/ubuntu/projects/cbs-confidence-pool')


def prepare(root, now, refresh, read, decide, collect, manual=False):
    """The pre-run wake gate owns retry eligibility; content hashes do not."""
    config = json.loads((root / 'config.json').read_text())
    if (config.get('research_enabled') is not True or config.get('season_ended')
            or config.get('mock_mode') is not True or config.get('access_scope') != 'read_only'
            or config.get('auto_submit_enabled', False)):
        return {'status': 'DISABLED', 'wakeAgent': False}
    # Preserve rollover discovery without a browser read on every idle tick.
    snapshot_path = root / 'snapshot-latest.json'
    if snapshot_path.exists():
        snapshot = json.loads(snapshot_path.read_text())
        age = now - datetime.fromisoformat(snapshot['retrieved_at'].replace('Z', '+00:00'))
        if age > timedelta(hours=6):
            read()
    data = refresh()
    state_path = root / 'experiment-state.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    plan = decide(config, state, data, now)
    if not manual and not plan.get('due_event'):
        return {'status': 'IDLE', 'wakeAgent': False, 'plan': plan}
    event = plan.get('due_event')
    if not manual and event:
        prior = state.get('attempts', {}).get(event['key'])
        minutes = 20 if plan.get('expires_at') and datetime.fromisoformat(plan['expires_at']) - now < timedelta(hours=2) else 60
        if prior and now - datetime.fromisoformat(prior['at']) < timedelta(minutes=minutes):
            return {'status': 'BUSY_OR_BACKOFF', 'wakeAgent': False}
    read()
    data = refresh()
    with (root / '.experiment-state.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        plan = decide(config, state, data, now)
        event = plan.get('due_event')
        if manual:
            # Explicit manual review never resurrects/marks an expired prelock event.
            event = {'key': f"{data['season']}:{data['week']}:manual:{now.isoformat()}", 'kind': 'manual'}
        elif not event:
            return {'status': 'IDLE', 'wakeAgent': False, 'plan': plan}
        prior = state.get('attempts', {}).get(event['key'])
        minutes = 20 if plan.get('expires_at') and datetime.fromisoformat(plan['expires_at']) - now < timedelta(hours=2) else 60
        if prior and now - datetime.fromisoformat(prior['at']) < timedelta(minutes=minutes):
            return {'status': 'BUSY_OR_BACKOFF', 'wakeAgent': False}
        if not manual:
            state.setdefault('attempts', {})[event['key']] = {
                'at': now.isoformat(), 'count': 1 if not prior else prior.get('count', 0) + 1}
            temp = state_path.with_suffix('.tmp')
            temp.write_text(json.dumps(state, indent=2))
            temp.replace(state_path)
        digest = hashlib.sha256(event['key'].encode()).hexdigest()[:24]
        directory = root / 'reports' / f"{data['season']}-week-{data['week']}" / ('research-' + digest)
        alexandria = collect(data, event, directory / 'raw' / 'alexandria')
        context = {'status': 'MANUAL_REVIEW' if manual else 'RESEARCH_DUE', 'wakeAgent': True,
                   'event_key': event['key'], 'event': event, 'plan': plan, 'data': data,
                   'report_directory': str(directory), 'alexandria': alexandria,
                   'instruction': 'Alexandria was executed before model startup. Read retained raw responses; do not repeat these calls. Copy manifest status and paths into RESULT.json. Incomplete source execution must be disclosed; use official web fallback. Verify per-team source age and final inactives; never label old data current.'}
        context_path = root / ('manual-research-context.json' if manual else 'active-research-context.json')
        context_path.write_text(json.dumps(context, indent=2))
        return context


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--manual-review', action='store_true', help='Explicit read-only review; does not complete or resurrect a scheduled event')
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, '/home/ubuntu/business/scripts')
    from research_input import refresh_local
    from experiment_schedule import gate
    from firecrawl_client import FirecrawlClient
    from cbs_pool_alexandria import collect

    def read():
        p = subprocess.run([sys.executable, '/home/ubuntu/.hermes/scripts/cbs_pool_read.py'],
                           capture_output=True, text=True, timeout=170)
        if p.returncode:
            code = re.search(r'CBS_(?:READ_FAILED|READER_BUSY): [A-Z_]+(?::(?:weekly|overall|picks))?', p.stderr)
            raise SystemExit('ACTION REQUIRED: ' + (code[0] if code else 'CBS_READ_FAILED: UNKNOWN_READER_ERROR') + '; no current card claimed')

    def sources(data, event, directory):
        return collect(data, event, directory, FirecrawlClient('cbs_scheduled_research', run_id=event['key']))

    # Hermes reads wakeAgent from the LAST single stdout line, not multiline JSON.
    print(json.dumps(prepare(ROOT, datetime.now(timezone.utc), refresh_local, read, gate, sources, args.manual_review)))


if __name__ == '__main__':
    main()
