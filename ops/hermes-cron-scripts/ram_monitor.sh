#!/usr/bin/env bash
set -euo pipefail
exec python3 - "$@" <<'PY'
import os
import socket
import subprocess
from datetime import datetime, timezone
from pathlib import Path

HERMES_HOME = Path('/home/ubuntu/.hermes')
LOG_DIR = HERMES_HOME / 'logs'
STATE_DIR = HERMES_HOME / 'runstate'
LOG_FILE = LOG_DIR / 'ram-monitor.log'
STATE_FILE = STATE_DIR / 'ram-monitor.last'
OPS_ALERT = HERMES_HOME / 'scripts' / 'ops_alert.py'
CHANNEL_ID = '1487300132054765629'

LOG_DIR.mkdir(parents=True, exist_ok=True)
STATE_DIR.mkdir(parents=True, exist_ok=True)


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')


def log(msg: str) -> None:
    with LOG_FILE.open('a', encoding='utf-8') as fh:
        fh.write(f'[{now_utc()}] {msg}\n')


def post(message: str) -> None:
    subprocess.check_call([
        'python3',
        str(OPS_ALERT),
        message,
    ], env={**os.environ, 'DISCORD_CHANNEL_ID': CHANNEL_ID})


def mem_available_mb() -> int:
    forced = os.environ.get('RAM_MONITOR_FORCE_AVAILABLE_MB')
    if forced:
        return int(forced)
    with open('/proc/meminfo', 'r', encoding='utf-8') as fh:
        for line in fh:
            if line.startswith('MemAvailable:'):
                kb = int(line.split()[1])
                return kb // 1024
    raise RuntimeError('MemAvailable missing from /proc/meminfo')


def main() -> int:
    avail_mb = mem_available_mb()
    avail_gib = avail_mb / 1024
    free_block = subprocess.check_output(['free', '-h'], text=True).strip()
    proc_block = subprocess.check_output(
        ['bash', '-lc', 'ps -eo pid,ppid,comm,rss,%mem --sort=-rss | head -11'],
        text=True,
    ).strip()

    if avail_mb < 2048:
        severity = 'critical'
    elif avail_mb < 4096:
        severity = 'warning'
    else:
        severity = 'ok'

    last_state = STATE_FILE.read_text(encoding='utf-8').strip() if STATE_FILE.exists() else ''
    STATE_FILE.write_text(severity, encoding='utf-8')
    log(f'severity={severity} available_mb={avail_mb} available_gib={avail_gib:.2f} last_state={last_state or "none"}')

    if severity == 'ok':
        return 0
    if severity == last_state:
        log(f'alert suppressed: state unchanged ({severity})')
        return 0

    if severity == 'critical':
        prefix = '🚨 RAM critical'
        body = (
            f'{prefix}\n\n'
            f'Available RAM: {avail_gib:.2f}GiB\n'
            f'Host: {socket.gethostname()}\n'
            f'Time: {now_utc()}\n\n'
            f'free -h:\n```text\n{free_block}\n```\n\n'
            f'Top processes by RSS:\n```text\n{proc_block}\n```'
        )
    else:
        prefix = '⚠️ RAM warning'
        body = (
            f'{prefix}\n\n'
            f'Available RAM: {avail_gib:.2f}GiB\n'
            f'Host: {socket.gethostname()}\n'
            f'Time: {now_utc()}\n\n'
            f'free -h:\n```text\n{free_block}\n```\n\n'
            f'Top processes by RSS:\n```text\n{proc_block}\n```'
        )

    post(body)
    log(f'alert sent: severity={severity} available_mb={avail_mb}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
PY
