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
    'daily_evidence_refresh.py': 'fd1d126f21e9055be3492a00f6b77672274e71dbeaf9bab55476e32a06ce81f2',
    'review_evidence.py': '6b629e2b7bbd9a313515cdd6ad7155f530565832849fcc12f4f5b2a80143e4f7',
    'SOURCE-PINS.json': 'a12858fb8db8bff1dc4f231f69f50c00f14f2cdd6b9b495287d7d3a8a03b6908',
    'ASTRA-LOW-PROMPT.md': 'e9dd833c54292ddf69abd6d696f8ab4cfd4d3713d91cabfb60c42a6066a0b865',
    'compose_current_base.py': '44b1cb1284a4b971c54e6645102a8443bc901fe5223f54d808fbee2f18bbde6a',
    'registered_taxonomy.py': 'a221943b5a779acf9de78e41bfc2a6b5e3faf6973b2c56c81b118818ac53421e',
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
