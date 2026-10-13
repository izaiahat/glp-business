#!/usr/bin/env python3
"""Registered exact-head workflow runner. See ops/local-ci-runner.json.

Receipt provenance is the existing trusted host/filesystem boundary, not a
signature or protection against an operator able to rewrite code and evidence.
Commands come only from applicable committed PR workflows, never CLI strings.
"""
import argparse
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from local_ci_admission import produce, gh_json, ROOT


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--worktree', type=Path, required=True)
    p.add_argument('--pr', required=True, help='full authorized GitHub PR URL')
    p.add_argument('--head', required=True)
    p.add_argument('--out', type=Path, required=True, help='new external lane directory')
    a = p.parse_args(argv)
    import re
    match = re.fullmatch(r'https://github.com/(izaiahat/glp-(?:business|site-candidate))/pull/([1-9]\d*)', a.pr)
    if not match:
        p.error('authorized full PR URL required')
    repo = match[1]
    remote = gh_json('pr', 'view', a.pr, '--json', 'headRefOid,files')
    if remote['headRefOid'] != a.head:
        raise ValueError('local_ci_remote_head_mismatch')
    receipt = produce(a.worktree, repo, a.pr, a.head, [f['path'] for f in remote['files']], a.out)
    # Never register a failed or partial run. Canonical index is a hash-bound
    # locator; consumers still validate the receipt, source, commands and logs.
    if receipt['status'] == 'PASS':
        from local_ci_admission import pin, write_new
        index = ROOT / 'reports/ops/local-ci' / repo.split('/')[1] / (a.head + '.json')
        write_new(index, pin(a.out / 'LOCAL-CI.json'))
    print(json.dumps({'status': receipt['status'], 'receipt': str(a.out / 'LOCAL-CI.json'),
                      'head_sha': a.head, 'pass_count': receipt['pass_count'], 'fail_count': receipt['fail_count']}))
    return 0 if receipt['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
