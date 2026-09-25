#!/home/ubuntu/.hermes/hermes-agent/venv/bin/python
"""K3 cron entry candidate. Independently enforce pins before any import/run.

Parent installs this file in the verified Hermes allowed script root; this
candidate performs no installation. No paid capture route or application.
"""
import hashlib
import json
import os
from pathlib import Path
import sys

BASE = Path('/home/ubuntu/business/reports/ops/orchestration-restore-20260907/PLAN/K3/cadence-candidate')
PYTHON = '/home/ubuntu/.hermes/hermes-agent/venv/bin/python'
PINS = {
    'daily_evidence_refresh.py': '11e154dfb82af38a5f1db7121b6723959a49c7ca086bc07918e4380c6a9c32a8',
    'review_evidence.py': 'b09e35b546459235da30cb9a7a2922e34e4ef0d39d1c1927d48fa6eac79238a6',
    'SOURCE-PINS.json': '69cab555577da889fd008998cdfd274d6762bbbe33840816a5223a3f4645730c',
    'ASTRA-LOW-PROMPT.md': '6913b4eeae3a3d4c74bbd2e2c55c13536a3da44670a495b10e316fb2a7edfac0',
}


def main():
    try:
        for name, expected in PINS.items():
            if hashlib.sha256((BASE / name).read_bytes()).hexdigest() != expected:
                raise RuntimeError('ENTRY_PIN_DRIFT:' + name)
    except (OSError, RuntimeError) as exc:
        print(json.dumps({'status': 'BLOCKED', 'reason': str(exc)}))
        return 2
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    os.execv(PYTHON, [PYTHON, '-B', str(BASE / 'daily_evidence_refresh.py'), *sys.argv[1:]])


if __name__ == '__main__':
    sys.exit(main())
