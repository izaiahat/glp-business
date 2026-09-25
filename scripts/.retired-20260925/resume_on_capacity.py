#!/usr/bin/env python3
"""resume_on_capacity.py — request resumption by file pointer when capacity returns.

The 2026-09-12 burn emptied both codex pools, so the lanes started failing with HTTP 429 and were parked
(LANE-MAP set to IDLE). This requests each explicitly bound saved task, but only once the
capacity collector reports an available codex account — never on a clock, because a clock was wrong tonight
(the "midnight PT" reset had not landed at 07:04Z; see GLP-213).

It logs EVERY poll decision, so a silent no-op is visible rather than looking like a service that worked.

Usage: scripts/resume_on_capacity.py --saved <targets.json> [--poll 120] [--until ISO]
       scripts/resume_on_capacity.py --check

Each non-IDLE saved target must explicitly supply task, artifact (existing absolute
instruction file), issue (GLP-n), tmux_session ($n), and session_id (actual Hermes ID).
Legacy task strings do not identify a session or an authoritative file: refuse them.
Submission is never reported as resumed/consumed. Inspect recipient history before retry.
"""
import argparse, json, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path

B = Path(__file__).resolve().parents[1]
LATEST = B / 'ops/subscription-capacity/latest.json'
MAP = B / 'reports/ops/orchestration-restore-20260907/PLAN/LANE-MAP.json'
LOG = B / 'reports/ops/fastmode-20260912/resume-on-capacity.log'

def codex_available():
    """True when the collector reports a codex account whose status is not depleted/limited."""
    try:
        blob = json.loads(LATEST.read_text())
    except (OSError, ValueError):
        return False, 'collector unreadable'
    found = []
    def walk(o):
        if isinstance(o, dict):
            label = str(o.get('label') or o.get('account') or o.get('name') or '')
            status = str(o.get('capacity_status') or o.get('status') or '')
            if label.startswith('codex') and status:
                found.append((label, status))
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for x in o:
                walk(x)
    walk(blob)
    ok = [f for f in found if f[1] not in ('depleted', 'limited', 'exhausted')]
    return bool(ok), '; '.join(f'{a}={b}' for a, b in found) or 'no codex rows'

def note(line):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%H:%M:%SZ')
    with open(LOG, 'a') as fh:
        fh.write(f'{stamp} {line}\n')
    print(f'{stamp} {line}', flush=True)

def resume(saved):
    restored = json.loads(Path(saved).read_text())
    targets = []
    for pane, task in restored.items():
        if not task or task == 'IDLE':
            continue
        if not isinstance(task, dict) or not all(task.get(k) for k in
                ('task', 'artifact', 'issue', 'tmux_session', 'session_id')):
            raise ValueError(f'{pane}: explicit file/issue/session binding required; legacy saved map refused')
        artifact = Path(task['artifact'])
        if not artifact.is_absolute() or not artifact.is_file():
            raise ValueError(f'{pane}: existing absolute instruction file required')
        targets.append((pane, task))
    # No LANE-MAP promotion based on capacity or a successful tmux syscall.
    # The owner reconciles actual recipient results before restoring work state.
    for pane, task in targets:
        r = subprocess.run([str(B / 'scripts/tui_send.sh'), pane, task['tmux_session'],
                            task['session_id'], task['artifact'], task['issue']],
                           capture_output=True, text=True)
        note(f"{pane} {task['task']} transport_exit={r.returncode}: {(r.stdout or r.stderr).strip()}")
    return 3 if targets else 0

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--saved')
    ap.add_argument('--poll', type=int, default=120)
    ap.add_argument('--until', default='2026-09-12T16:00:00+00:00')
    ap.add_argument('--check', action='store_true')
    a = ap.parse_args()
    ok, detail = codex_available()
    if a.check:
        print(json.dumps({'codex_available': ok, 'detail': detail})); return 0
    if not a.saved:
        print('need --saved <lanemap.json>'); return 2
    until = datetime.fromisoformat(a.until)
    note(f'watcher started; resuming when a codex account is available; giving up at {a.until}')
    while True:
        ok, detail = codex_available()
        if ok:
            note('CAPACITY BACK: ' + detail)
            result = resume(a.saved)
            note('polling stopped; no retry; recipient consumption and lane state require reconciliation')
            return result
        if datetime.now(timezone.utc) >= until:
            note('GAVE UP at the bound; lanes stay parked and need a human: ' + detail)
            return 3
        note('still parked: ' + detail)
        time.sleep(a.poll)

if __name__ == '__main__':
    sys.exit(main())
