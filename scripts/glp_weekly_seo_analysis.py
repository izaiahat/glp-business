#!/usr/bin/env python3
"""Pinned installed weekly SEO analysis only; no collection, queue or release."""
import argparse, hashlib, json, sys
from pathlib import Path
B = Path('/home/ubuntu/business')
PIN_PATH = B / 'ops/glp/seo-weekly-installed-pins.json'
PIN_SHA256 = 'c3868b2920bdfb6db22b227236a1d41ffb22fa2e43e44b873423c31e4d6c6a3c'
def verify_pins():
    raw = PIN_PATH.read_bytes()
    if hashlib.sha256(raw).hexdigest() != PIN_SHA256:
        raise ValueError('weekly_pin_manifest_drift')
    pins = json.loads(raw)
    for path, expected in pins['paths'].items():
        if hashlib.sha256(Path(path).read_bytes()).hexdigest() != expected:
            raise ValueError('weekly_installed_source_drift:' + path)
    return pins

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--module-input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    verify_pins()
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(B))
    from ops.glp.seo_weekly_continuation import run_week
    result = run_week(args.module_input, args.output)
    print(json.dumps({k: result[k] for k in ('status', 'module_statuses', 'source_operations', 'module_implementation')}))
    return 0 if result['status'] == 'PASS_DRY_RUN' else 2

if __name__ == '__main__':
    raise SystemExit(main())
