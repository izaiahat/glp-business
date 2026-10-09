#!/usr/bin/env python3
"""No-agent native cron entry; exact-source pin then the one route singleton."""
import hashlib
import json
import os
from pathlib import Path

ROOT = Path('/home/ubuntu/business')
PAYLOADS = {'scripts/pro_route.py', 'scripts/gpt6pro_relay.py', 'scripts/gpt6pro_packet.py'}


def load_launch(root=ROOT):
    cfg = json.loads((root / 'ops/gpt6pro-relay/route/EXECUTION.json').read_text())
    checkout = Path(cfg['checkout']).resolve(strict=True)
    if not checkout.is_relative_to(root.resolve()) or set(cfg['sha256']) != PAYLOADS:
        raise ValueError('INVALID_EXECUTION_BINDING')
    for rel, digest in cfg['sha256'].items():
        path = checkout / rel
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError('SOURCE_DRIFT ' + rel)
    return checkout / 'scripts/pro_route.py'


def main():
    route = load_launch()
    python = '/home/ubuntu/.hermes/hermes-agent/venv/bin/python3'
    os.chdir(ROOT)
    os.execv(python, [python, '-B', str(route), 'dispatch', '--max-actions', '6'])


if __name__ == '__main__':
    main()
