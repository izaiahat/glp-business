#!/usr/bin/env python3
from __future__ import annotations

import shutil
import time
from pathlib import Path
from datetime import datetime, timezone

PAIRS = [
    (Path('/home/ubuntu/.hermes-handoff/signal/outbox'), Path('/home/ubuntu/.hermes-handoff/signal/inbox')),
    (Path('/home/ubuntu/.hermes-handoff/dialtone/outbox'), Path('/home/ubuntu/.hermes-handoff/dialtone/inbox')),
]
LOG = Path('/home/ubuntu/business/logs/fix_misrouted_handoffs.log')


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open('a', encoding='utf-8') as f:
        f.write(f'[{ts}] {msg}\n')


def main() -> int:
    moved = 0
    removed_dupes = 0
    patterns = ['kael-*.md', 'bookmark-*.md', 'task-*.md']
    max_age_seconds = 72 * 3600
    now_ts = time.time()
    for outbox, inbox in PAIRS:
        inbox.mkdir(parents=True, exist_ok=True)
        seen_paths = set()
        for pattern in patterns:
            for f in sorted(outbox.glob(pattern)):
                if f in seen_paths:
                    continue
                seen_paths.add(f)
                try:
                    age_seconds = now_ts - f.stat().st_mtime
                except FileNotFoundError:
                    continue
                if age_seconds > max_age_seconds:
                    log(f'skipped stale misrouted file {f} (age_seconds={int(age_seconds)})')
                    continue
                dest = inbox / f.name
                if dest.exists():
                    f.unlink()
                    removed_dupes += 1
                    log(f'removed duplicate misrouted file {f} (already existed in inbox)')
                else:
                    shutil.move(str(f), str(dest))
                    moved += 1
                    log(f'moved misrouted file {f} -> {dest}')
    if moved == 0 and removed_dupes == 0:
        log('no misrouted Kael handoff files found')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
