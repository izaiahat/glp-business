#!/usr/bin/env python3
"""Guarded seat brief send: python3 scripts/seat_send.py [--operator-directed] <seat> <brief>.

The named seat and exact issue come from ops/seat-standing-work.json. The registered
transport runs brief_lint, the acceptance clause gate, native session preflight,
model/effort correction and status-bar verification before sending the pointer.
--operator-directed waives ONLY cached Linear acceptance binding when the operator
has expressly directed the task; MODEL, PROVIDER, REASONING, skill-router/loading,
delegation, lint, native identity and live route remain mandatory. No raw tmux
send-keys for orchestration. Exit 0 = native busy observed after submission;
2 = pre-effect refusal; 3 = submitted or corrected but unconfirmed, inspect before retry.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import time

BUSINESS = Path('/home/ubuntu/business')
CONFIG = BUSINESS / 'ops/seat-standing-work.json'
TRANSPORT = BUSINESS / 'scripts/tui_send.sh'


def send(seat: str, brief: Path, *, operator_directed: bool = False) -> int:
    try:
        seats = json.loads(CONFIG.read_text())['seats']
        row = seats[seat]
        if not brief.is_absolute() or not brief.is_file():
            raise ValueError('an existing absolute brief path is required')
        body = brief.read_text(encoding='utf-8-sig')
        if not re.match(r'^LINEAR:\s*' + re.escape(row['issue']) + r'\b', body):
            raise ValueError('brief LINEAR issue does not match named seat assignment')
        args = ['bash', str(TRANSPORT)]
        if operator_directed:
            args.append('--operator-directed')
        args.extend([row['pane'], row['tmux'], row['session'], str(brief), row['issue']])
        out = subprocess.run(args, capture_output=True, text=True, timeout=110)
    except (OSError, KeyError, ValueError, subprocess.SubprocessError) as exc:
        print(f'REFUSED: {exc}')
        return 2
    if out.stdout.strip():
        print(out.stdout.strip())
    if out.stderr.strip():
        print(out.stderr.strip())
    if out.returncode != 3 or 'SUBMITTED_UNCONFIRMED' not in out.stdout:
        return out.returncode if out.returncode in (2, 3) else 3
    # Submission is not consumption. Observe the native busy prompt, not conversation prose.
    for _ in range(20):
        try:
            screen = subprocess.run(['tmux', 'capture-pane', '-p', '-t', row['pane'], '-S', '-5'],
                                    capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            break
        lines = [line for line in screen.stdout.splitlines() if line.strip()]
        if screen.returncode == 0 and lines and 'Ctrl+C to interrupt' in lines[-1]:
            print(f'BUSY_CONFIRMED seat={seat} pane={row["pane"]} brief={brief}')
            return 0
        time.sleep(.25)
    print('SUBMITTED_UNCONFIRMED: native busy state not observed; inspect before retry')
    return 3


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--operator-directed', action='store_true')
    parser.add_argument('seat')
    parser.add_argument('brief', type=Path)
    args = parser.parse_args()
    return send(args.seat, args.brief, operator_directed=args.operator_directed)


if __name__ == '__main__':
    raise SystemExit(main())
