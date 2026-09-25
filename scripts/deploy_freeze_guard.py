#!/usr/bin/env python3
"""Fail closed on deploys while a Fable acceptance pass is running.

Set ALLOW_DEPLOY_DURING_ACCEPTANCE_PASS=1 and DEPLOY_DURING_PASS_RECEIPT=/path/to/receipt.json
only for an explicit reconciled mid-pass deploy receipt naming the running pass.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

LOCK = Path('/home/ubuntu/business/reports/claude-audit-20260707/acceptance/.pass-lock')


def utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def main() -> int:
    surface = sys.argv[1] if len(sys.argv) > 1 else 'unknown-deploy-surface'
    if not LOCK.exists():
        return 0
    try:
        lock = json.loads(LOCK.read_text())
    except Exception:
        lock = {'raw': LOCK.read_text(errors='replace')[:500]}
    if os.environ.get('ALLOW_DEPLOY_DURING_ACCEPTANCE_PASS') == '1':
        receipt_path = os.environ.get('DEPLOY_DURING_PASS_RECEIPT')
        if receipt_path:
            receipt = {
                'ts_utc': utc(),
                'status': 'allowed_mid_pass_deploy',
                'surface': surface,
                'running_pass': lock,
                'operator_or_parent_override': True,
                'secret_printed': False,
            }
            Path(receipt_path).parent.mkdir(parents=True, exist_ok=True)
            Path(receipt_path).write_text(json.dumps(receipt, indent=2, sort_keys=True) + '\n')
            return 0
    print(json.dumps({
        'status': 'blocked_deploy_freeze_during_acceptance_pass',
        'surface': surface,
        'running_pass': lock,
        'instruction': 'wait for pass close, or set ALLOW_DEPLOY_DURING_ACCEPTANCE_PASS=1 with DEPLOY_DURING_PASS_RECEIPT naming this pass',
        'secret_printed': False,
    }, indent=2, sort_keys=True), file=sys.stderr)
    return 75


if __name__ == '__main__':
    raise SystemExit(main())
