#!/usr/bin/env bash
set -euo pipefail
exec python3 - "$@" <<'PY'
import json
import os
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERMES_HOME = Path('/home/ubuntu/.hermes')
LOG_DIR = HERMES_HOME / 'logs'
STATE_DIR = HERMES_HOME / 'runstate'
LOG_FILE = LOG_DIR / 'disk-monitor.log'
STATE_FILE = STATE_DIR / 'disk-monitor.last'
OPS_ALERT = HERMES_HOME / 'scripts' / 'ops_alert.py'
CHANNEL_ID = '1487300132054765629'

LOG_DIR.mkdir(parents=True, exist_ok=True)
STATE_DIR.mkdir(parents=True, exist_ok=True)


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')


def log(msg: str) -> None:
    with LOG_FILE.open('a', encoding='utf-8') as fh:
        fh.write(f'[{now_utc()}] {msg}\n')


def run(cmd: list[str]) -> str:
    return subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL).strip()


def human(n: int) -> str:
    val = float(n)
    for unit in ['B', 'KB', 'MB', 'GB', 'TB', 'PB']:
        if val < 1024 or unit == 'PB':
            return f'{int(val)}B' if unit == 'B' else f'{val:.1f}{unit}'
        val /= 1024
    return f'{n}B'


def read_df() -> tuple[int, str, str, str]:
    if os.environ.get('DISK_MONITOR_FORCE_PCT'):
        pct = int(os.environ['DISK_MONITOR_FORCE_PCT'])
    else:
        pct = int(run(['bash', '-lc', "df -P / | awk 'NR==2 {gsub(/%/, \"\", $5); print $5}'"]))
    row = run(['bash', '-lc', "df -h / | awk 'NR==2 {print $2, $3, $4}'"])
    size_h, used_h, avail_h = row.split()
    return pct, size_h, used_h, avail_h


def top_consumers() -> list[tuple[int, str]]:
    candidates = [
        '/swapfile',
        '/home',
        '/home/ubuntu/.hermes',
        '/home/ubuntu/.hermes/heapdumps',
        '/home/ubuntu/business',
        '/home/ubuntu/business/backups',
        '/home/ubuntu/.npm',
        '/home/ubuntu/.npm/_npx',
        '/home/ubuntu/.cache',
        '/usr',
        '/var',
        '/snap',
        '/tmp',
        '/boot',
    ]
    rows = []
    for path in candidates:
        if not os.path.exists(path):
            continue
        try:
            out = run(['du', '-sx', '--block-size=1', path])
            size_s, actual = out.split('\t', 1)
            rows.append((int(size_s), actual))
        except Exception:
            continue
    rows.sort(key=lambda x: x[0], reverse=True)
    return rows[:5]


def recommendation_lines() -> list[str]:
    checks = [
        ('/home/ubuntu/.hermes/heapdumps', 'prune legacy heap snapshots / confirm cap stays at 3'),
        ('/home/ubuntu/.npm/_npx', 'clear npm _npx cache'),
        ('/home/ubuntu/business/backups', 'remember backup tree is hardlink-based before deleting'),
        ('/swapfile', 'revisit swap size only if disk pressure returns'),
    ]
    lines = []
    for path, note in checks:
        if not os.path.exists(path):
            continue
        try:
            size_s, _ = run(['du', '-sx', '--block-size=1', path]).split('\t', 1)
            size = int(size_s)
        except Exception:
            continue
        if size >= 500 * 1024 * 1024:
            lines.append(f'- {path}: {note}')
    if not lines:
        lines.append('- no oversized cleanup target detected from the quick ruleset')
    return lines[:4]


def post(message: str) -> None:
    subprocess.check_call([
        'python3',
        str(OPS_ALERT),
        message,
    ], env={**os.environ, 'DISCORD_CHANNEL_ID': CHANNEL_ID})


def main() -> int:
    # Q36: run before disk's healthy/unchanged early returns; existing sender.
    sys.path.insert(0, '/home/ubuntu/business/scripts')
    from host_capacity_observer import notify_state_store
    state_rc = notify_state_store(HERMES_HOME, post, log)
    pct, size_h, used_h, avail_h = read_df()
    if pct >= 90:
        severity = 'critical'
    elif pct >= 80:
        severity = 'warning'
    else:
        severity = 'ok'

    last_state = STATE_FILE.read_text(encoding='utf-8').strip() if STATE_FILE.exists() else ''
    STATE_FILE.write_text(severity, encoding='utf-8')
    log(f'severity={severity} usage_pct={pct} used={used_h} avail={avail_h} size={size_h} last_state={last_state or "none"}')

    if severity == 'ok':
        return state_rc
    if severity == last_state:
        log(f'alert suppressed: state unchanged ({severity})')
        return state_rc

    prefix = '🚨 Disk critical' if severity == 'critical' else '⚠️ Disk warning'
    top5 = '\n'.join(f'- {path} — {human(size)}' for size, path in top_consumers())
    recs = '\n'.join(recommendation_lines())
    message = (
        f'{prefix}\n\n'
        f'Root usage: {pct}% ({used_h} used / {avail_h} free / {size_h} total)\n'
        f'Host: {socket.gethostname()}\n'
        f'Time: {now_utc()}\n\n'
        f'Top 5 consumers:\n{top5}\n\n'
        f'Recommended cleanup actions:\n{recs}'
    )
    post(message)
    log(f'alert sent: severity={severity} usage_pct={pct}')
    return state_rc


if __name__ == '__main__':
    raise SystemExit(main())
PY
