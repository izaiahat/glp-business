#!/usr/bin/env python3
"""No-agent watchdog for lean STATE.md hygiene.

Silent when STATE validates. Emits a concise ops-alert when STATE exceeds policy
or contains forbidden run-log patterns. Does not mutate STATE.
"""
from __future__ import annotations
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, '/home/ubuntu/business/ops')
from discord_fmt import (
    title as fmt_title, kv as fmt_kv, kvline as fmt_kvline, section as fmt_section,
    meaning as fmt_meaning, subtext as fmt_subtext, render as fmt_render,
)

REPORT_DIR = Path('/home/ubuntu/business/reports/state-hygiene')
GUARD = Path('/home/ubuntu/business/scripts/state_guard.py')
PY = sys.executable

REPORT_DIR.mkdir(parents=True, exist_ok=True)
ts = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
report = REPORT_DIR / f'state-guard-{ts}.json'
proc = subprocess.run([PY, str(GUARD), 'validate', '--json'], text=True, capture_output=True, timeout=60)
raw = proc.stdout.strip() or '{}'
try:
    payload = json.loads(raw)
except Exception:
    payload = {'status': 'fail', 'errors': ['state_guard emitted non-json output'], 'stdout': raw, 'stderr': proc.stderr}
payload['watchdog_timestamp_utc'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
payload['state_guard_exit_code'] = proc.returncode
report.write_text(json.dumps(payload, indent=2) + '\n')
if proc.returncode == 0 and not payload.get('warnings'):
    sys.exit(0)
warn_cap = 14000
hard_cap = 18000
bytes_used = payload.get('bytes', 'unknown')
ok = proc.returncode == 0
try:
    over = max(0, int(bytes_used) - warn_cap)
    headroom = max(0, hard_cap - int(bytes_used))
    head_fact = f'{over:,} B over warn cap' if ok else 'STATE.md failed validation'
except Exception:
    headroom = None
    head_fact = 'guard warning' if ok else 'STATE.md failed validation'
detail = [f'- {x}' for x in (payload.get('errors') or [])[:4]] + [f'- {x}' for x in (payload.get('warnings') or [])[:4]]
print(fmt_render(
    fmt_title('warn' if ok else 'action', 'STATE Guard', head_fact),
    fmt_kvline(('bytes', bytes_used), ('warn cap', f'{warn_cap:,}'), ('hard cap', f'{hard_cap:,}'),
               ('headroom', f'{headroom:,}' if headroom is not None else 'unknown')),
    *detail,
    fmt_meaning('nothing urgent — prune/archive detail on the next STATE update instead of appending' if ok else 'stop state writes — run state_guard.py validate --json, prune, write through the guarded path'),
    fmt_subtext(f'report: {report}'),
))
sys.exit(0)
