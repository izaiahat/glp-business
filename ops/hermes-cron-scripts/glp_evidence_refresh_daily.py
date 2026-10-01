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

BASE = Path('/home/ubuntu/business/ops/glp/evidence-refresh-k3')
PYTHON = '/home/ubuntu/.hermes/hermes-agent/venv/bin/python'
PINS = {
    'daily_evidence_refresh.py': 'ae64133e8a9ddcb3a6efff7274454aa9ec75a4b01744474caaf2cd0572077dc9',
    'review_evidence.py': 'b09e35b546459235da30cb9a7a2922e34e4ef0d39d1c1927d48fa6eac79238a6',
    'SOURCE-PINS.json': '12e726467dc6ace0816d8b26347cece8f8e1bbbec0e2a2a9d08dfa4c879f19b8',
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
