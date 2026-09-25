#!/usr/bin/env python3
"""Cron carrier for the shared armed-off lifecycle-email scheduler."""
from __future__ import annotations

import os
import sys

PACKAGE = '/home/ubuntu/business/ops/email_flows'
OPS = '/home/ubuntu/business/ops'
for path in (PACKAGE, OPS):
    if path not in sys.path:
        sys.path.insert(0, path)
os.chdir('/home/ubuntu/business')

from scheduler import main  # noqa: E402


if __name__ == '__main__':
    sys.argv = [sys.argv[0], '--db', '/home/ubuntu/business/ops/email_flows/email_flows.sqlite', '--tick']
    raise SystemExit(main())
