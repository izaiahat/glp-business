#!/usr/bin/env python3
"""Wake gate for weekly strategy review.

Skips duplicate same-week runs and passes compact context for real weekly runs.
"""
from __future__ import annotations
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

REPORT_ROOT = Path('/home/ubuntu/business/reports')
STATE = Path('/home/ubuntu/business/STATE.md')
now = datetime.now(timezone.utc)
week_start = (now - timedelta(days=now.weekday())).date().isoformat()
marker = Path('/home/ubuntu/business/ops/weekly_strategy_review_gate_state.json')
marker.parent.mkdir(parents=True, exist_ok=True)
prev = {}
if marker.exists():
    try:
        prev = json.loads(marker.read_text())
    except Exception:
        prev = {}
if prev.get('week_start') == week_start and prev.get('completed') is True:
    print(json.dumps({'wakeAgent': False, 'gate': 'weekly_strategy_review_once_per_week', 'week_start': week_start, 'status': 'already_completed'}))
else:
    marker.write_text(json.dumps({'week_start': week_start, 'completed': False, 'checked_at_utc': now.isoformat(timespec='seconds')}, indent=2) + '\n')
    print(json.dumps({
        'wakeAgent': True,
        'gate': 'weekly_strategy_review_once_per_week',
        'week_start': week_start,
        'status': 'proceed',
        'context': {
            'focus': 'GLP Clinic Scout, Allocant, and Boring AI only; TLCC/SWFA recurring work is shelved.',
            'state_path': str(STATE),
            'state_bytes': STATE.stat().st_size if STATE.exists() else None,
            'report_root': str(REPORT_ROOT),
        }
    }))
