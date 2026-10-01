#!/usr/bin/env python3
"""Explain the retained GLP-73 sync-state drift without changing any live state.

The output is a proposal. A separate reviewer must accept its exact contents;
--state-rebaseline on the adoption consumes that receipt and rechecks evidence.
This is deliberately a fixed three-path transition, not a generic hash override.
"""
import argparse
import hashlib
import json
from pathlib import Path

from maxbounty_pin_release import read_regular

BEFORE = '20a6396a78a4cb4cf82e473b0b3805b01ed279cd2e6526c2032caa10814e7436'
AFTER = 'bfc81ed021e5c7da93498eb450c5f71797919de0b37554ed34915e30e012cb56'
SNAPSHOT = 'reports/ops/glp44-scoped-live-adoption-20260929/read-only-preimages/05.bytes'
EVIDENCE = {
    'reports/ops/seat-continuation-sol61-20260930/RESULT.json': 'e418cc3628261ffd7898418dc8a010d21c43bf6348acf0ecf2f997cf2724a3c3',
    'reports/ops/cleanup-p0-drift-repair-20261001/INSTALL-WRITER.json': '78bdda539dc1c0a16d65691b1502ddb8276cefbe48d8178f24f4070f94e4496e',
    'reports/ops/cleanup-p0-drift-repair-20261001/RESULT.json': '793baae86804b90fcf5d4eb15abb769285feea265f6f0d109bed7a154ebe9464',
}
EXPLAINED = {
    'ops/protected-oracles.json': (
        'ee12c01f76fdab6ee645370b261459146330ed74257d8c6d24725f61c0fa2384',
        '9f7d68ee3d79f6cb1cabe739f68628c31b7b566a626d82adf04838b18e02775d',
        'reports/ops/seat-continuation-sol61-20260930/RESULT.json'),
    'scripts/seat_continuation.py': (
        'f00211d4fb278cdbacb5fa09b419597785ebb321bb58bb2ded3a28fec26a6c0c',
        'f23afdbfe3040d2c19712a57a5ac6ddb4cf977d2af142af1ccd7a040f83b4716',
        'reports/ops/seat-continuation-sol61-20260930/RESULT.json'),
    'scripts/affiliate_sheet_batch_writer.py': (
        'dc713c15af72f5dc2af6311fbcc9f0a56c887ff5843da7a741a6af84d376690c',
        'e5a05f3d14b5e525cab9dcb76ff733ca0c9e19f1518a327567433d1a2d75c656',
        'reports/ops/cleanup-p0-drift-repair-20261001/INSTALL-WRITER.json'),
}


def sha(body):
    return hashlib.sha256(body).hexdigest()


def explained_diff(before, after, explanations):
    """Require exact full-state semantic coverage, not a prose explanation flag."""
    old, new = json.loads(before), json.loads(after)
    if (set(old) != {'schema', 'hashes'} or set(new) != {'schema', 'hashes'}
            or old['schema'] != new['schema'] or old['schema'] != 1
            or not isinstance(old['hashes'], dict) or not isinstance(new['hashes'], dict)):
        raise RuntimeError('unexplained_sync_state_structure')
    changes = []
    for path in sorted(set(old['hashes']) | set(new['hashes'])):
        a, b = old['hashes'].get(path), new['hashes'].get(path)
        if a == b:
            continue
        explanation = explanations.get(path)
        if explanation is None or (a, b) != explanation[:2] or not explanation[2]:
            raise RuntimeError('unexplained_sync_state_diff:' + path)
        changes.append({'path': path, 'before_sha256': a, 'after_sha256': b,
                        'evidence': explanation[2]})
    if {item['path'] for item in changes} != set(explanations):
        raise RuntimeError('explained_sync_state_diff_incomplete')
    return changes


def proposal(root):
    before = read_regular(root / SNAPSHOT)[0]
    current = read_regular(root / 'ops/.rebuild-sync-state.json')[0]
    if sha(before) != BEFORE or sha(current) != AFTER:
        raise RuntimeError('sync_rebaseline_preimage_moved')
    changes = explained_diff(before, current, EXPLAINED)
    for rel, expected in EVIDENCE.items():
        if sha(read_regular(root / rel)[0]) != expected:
            raise RuntimeError('sync_rebaseline_evidence_moved:' + rel)
    for item in changes:
        if sha(read_regular(root / item['path'])[0]) != item['after_sha256']:
            raise RuntimeError('sync_rebaseline_installed_source_moved:' + item['path'])
    return {'schema': 'glp73-sync-rebaseline-proposal-v1',
            'old_state_sha256': BEFORE, 'new_state_sha256': AFTER,
            'before_snapshot': SNAPSHOT, 'diff': changes, 'evidence_sha256': EVIDENCE}


def validate(root, review):
    expected = proposal(root)
    if (review.get('schema') != 'glp73-sync-rebaseline-review-v1'
            or review.get('decision') != 'ACCEPT_EXPLAINED_DIFF'
            or not review.get('prepared_by') or not review.get('reviewer')
            or review['prepared_by'] == review['reviewer']
            or review.get('proposal') != expected):
        raise RuntimeError('independent_explained_diff_review_required')
    return expected['new_state_sha256']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('/home/ubuntu/business'))
    args = parser.parse_args()
    try:
        print(json.dumps(proposal(args.root), indent=2, sort_keys=True))
        return 0
    except (ValueError, OSError, RuntimeError) as exc:
        parser.exit(2, 'GLP73_REBASELINE_REFUSED: ' + str(exc) + '\n')


if __name__ == '__main__':
    raise SystemExit(main())
