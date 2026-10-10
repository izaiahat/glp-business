#!/usr/bin/env python3
"""Register only the existing GLP updater's output declaration."""
import copy
import json
from pathlib import Path

JOB = 'a0e87c0a4569'
ROOT = Path('/home/ubuntu/business')

def project(prior, generated):
    selected = [x for x in generated['entries'] if x['job_id'] == JOB]
    if len(selected) != 1:
        raise ValueError('updater_declaration_not_unique')
    result = copy.deepcopy(prior)
    peers = [x for x in result['entries'] if x['job_id'] != JOB]
    result['entries'] = peers + selected
    result['entry_count'] = len(result['entries'])
    return result


def main():
    import argparse, fcntl, os, sys
    p = argparse.ArgumentParser()
    p.add_argument('--apply', action='store_true')
    args = p.parse_args()
    sys.path.insert(0, str(ROOT / 'ops'))
    import importlib
    builder = importlib.import_module('cron_discord_output_registry_builder_20260710')
    with builder.LOCK.open('a+') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        prior = json.loads(builder.OUT.read_text())
        jobs = builder.load_jobs()
        if sum(x['id'] == JOB for x in jobs) != 1:
            raise ValueError('updater_job_not_unique')
        # Project a declaration while paused; do not change scheduler state.
        original_loader = builder.load_jobs
        setattr(builder, 'load_jobs', lambda: [dict(x, enabled=True) if x['id'] == JOB else x for x in jobs])
        try:
            payload = project(prior, builder.build())
        finally:
            setattr(builder, 'load_jobs', original_loader)
        if args.apply and payload != prior:
            import tempfile
            fd, temporary = tempfile.mkstemp(dir=os.environ['TMPDIR'], prefix='glp-updater-registry-')
            try:
                with os.fdopen(fd, 'w') as out:
                    out.write(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + '\n')
                os.replace(temporary, builder.OUT)
            finally:
                Path(temporary).unlink(missing_ok=True)
        print(json.dumps({'status': 'PASS_UPDATER_REGISTRY_PROJECTED', 'applied': bool(args.apply and payload != prior), 'would_change': payload != prior, 'job': JOB}))
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
