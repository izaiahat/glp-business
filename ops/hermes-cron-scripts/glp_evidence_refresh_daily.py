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
    'daily_evidence_refresh.py': '935568353b63b97fc8020e22ef7aa34f67dd182bbd1fffb9b5fb591c271e927e',
    'review_evidence.py': '5ae7911e00b33c96650e0cba182fa0e3eda474c0f95973cd5fc85310fef40d06',
    'SOURCE-PINS.json': '4b321bfd692be579761795155c05bf6ffd0a1f20e276c4532913d1d111e08dd5',
    'ASTRA-LOW-PROMPT.md': 'dd82f3885007de1d9f1fb01e4ddda49ca0253e23c3229ef6245bb1acca7f29e1',
    'compose_current_base.py': '0db2d0eccab90f843489618711afdbca24a58997fd0d357509e6a66af242fc3c',
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
    os.execv(PYTHON, [PYTHON, '-B', str(BASE / 'daily_evidence_refresh.py'), '--continue-day', *sys.argv[1:]])


if __name__ == '__main__':
    sys.exit(main())
