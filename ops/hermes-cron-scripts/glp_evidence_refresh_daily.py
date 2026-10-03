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
    'daily_evidence_refresh.py': 'b130c0071156fbe54397ea6aee9d7edbcf8020799a4e913b9b6e65a5abab5022',
    'review_evidence.py': '5ae7911e00b33c96650e0cba182fa0e3eda474c0f95973cd5fc85310fef40d06',
    'SOURCE-PINS.json': '7876f11ecd02441d0ced0fe13a54c014fb32fb3152a719067bf76c949c10b6e3',
    'ASTRA-LOW-PROMPT.md': 'dd82f3885007de1d9f1fb01e4ddda49ca0253e23c3229ef6245bb1acca7f29e1',
    'compose_current_base.py': 'f7052b55ee4992e66f7cfced530d72478141ab02cc68135b9dbb762281cc1300',
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
