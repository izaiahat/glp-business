#!/usr/bin/env python3
"""Build a read-only GLP-73 sync-state proposal from supplied sync receipts.

--receipts is a JSON list of receipt descriptors (paths relative to --root):
  {"path": "reports/sync.json", "sha256": "<retained receipt digest>",
   "authorization": {"path": "reports/owner.json", "sha256": "<digest>",
                     "pointer": "/authority"},
   "changes": [{"path": "scripts/example.py", "before_sha256": "<old>",
                "after_sha256": "<new>", "after_pointer": "/after_sha256",
                "before_pointer": "/before_sha256"}]}

JSON pointers select evidence in the retained documents. before_pointer is
optional for legacy receipts that record only the successor; the fixed original
snapshot and contiguous chain still bind the predecessor. Authorization evidence
is retained verbatim, not interpreted as approval by this producer: the distinct
reviewer must authenticate its source, scope and each transition before accepting
the exact proposal. A proposal is never permission to apply. Repeated changes to
a path need a complete ordered receipt chain. Unknown movement refuses.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path, PurePosixPath
import re

from maxbounty_pin_release import read_regular

BEFORE = '20a6396a78a4cb4cf82e473b0b3805b01ed279cd2e6526c2032caa10814e7436'
SNAPSHOT = 'reports/ops/glp44-scoped-live-adoption-20260929/read-only-preimages/05.bytes'


def sha(body):
    return hashlib.sha256(body).hexdigest()


def _path(root, rel):
    if (not isinstance(rel, str) or not rel or PurePosixPath(rel).is_absolute()
            or '..' in PurePosixPath(rel).parts or str(PurePosixPath(rel)) != rel):
        raise RuntimeError('unsafe_rebaseline_path:' + str(rel))
    target = root / rel
    # read_regular rejects a symlink leaf; also reject symlink ancestors.
    if any(part.is_symlink() for part in [target, *target.parents] if part != root.parent):
        raise RuntimeError('unsafe_rebaseline_path:' + rel)
    if not target.resolve().is_relative_to(root.resolve()):
        raise RuntimeError('unsafe_rebaseline_path:' + rel)
    return target


def _digest(value, nullable=False):
    if nullable and value is None:
        return
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
        raise RuntimeError('invalid_sync_receipt_digest')


def _pointer(document, pointer):
    if not isinstance(pointer, str) or not pointer.startswith('/'):
        raise RuntimeError('invalid_sync_receipt_pointer')
    try:
        for part in pointer[1:].split('/'):
            part = part.replace('~1', '/').replace('~0', '~')
            document = document[int(part)] if isinstance(document, list) else document[part]
    except (KeyError, IndexError, ValueError, TypeError):
        raise RuntimeError('missing_sync_receipt_pointer:' + pointer) from None
    return document


def _state(body):
    value = json.loads(body)
    if (not isinstance(value, dict) or set(value) != {'schema', 'hashes'}
            or type(value['schema']) is not int or value['schema'] != 1
            or not isinstance(value['hashes'], dict)):
        raise RuntimeError('unexplained_sync_state_structure')
    return value['hashes']


def explained_diff(before, after, explanations):
    """Require full semantic coverage; an explanation cannot bless extra paths."""
    old, new = _state(before), _state(after)
    changes = []
    for path in sorted(set(old) | set(new)):
        a, b = old.get(path), new.get(path)
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


def _explanations(root, receipts, old):
    if not isinstance(receipts, list):
        raise RuntimeError('sync_receipts_required')
    evidence, documents, chains, seen = {}, {}, {}, set()

    def read_evidence(descriptor):
        if not isinstance(descriptor, dict):
            raise RuntimeError('invalid_sync_receipt')
        rel, expected = descriptor.get('path'), descriptor.get('sha256')
        _digest(expected)
        target = _path(root, rel)
        body = read_regular(target)[0]
        if sha(body) != expected or (rel in evidence and evidence[rel] != expected):
            raise RuntimeError('sync_rebaseline_evidence_moved:' + rel)
        evidence[rel] = expected
        documents[rel] = json.loads(body)
        return documents[rel]

    for receipt in receipts:
        if not isinstance(receipt, dict) or set(receipt) != {'path', 'sha256', 'authorization', 'changes'}:
            raise RuntimeError('invalid_sync_receipt')
        document = read_evidence(receipt)
        rel = receipt['path']
        if rel in seen:
            raise RuntimeError('duplicate_sync_receipt:' + rel)
        seen.add(rel)
        authority = receipt['authorization']
        if not isinstance(authority, dict) or set(authority) != {'path', 'sha256', 'pointer'}:
            raise RuntimeError('sync_receipt_authorization_required:' + rel)
        authority_value = _pointer(read_evidence(authority), authority['pointer'])
        if not isinstance(authority_value, str) or not authority_value.strip():
            raise RuntimeError('sync_receipt_authorization_required:' + rel)
        rows = receipt['changes']
        if not isinstance(rows, list) or not rows:
            raise RuntimeError('sync_receipt_changes_required:' + rel)
        for row in rows:
            required = {'path', 'before_sha256', 'after_sha256', 'after_pointer'}
            if (not isinstance(row, dict) or not required <= set(row)
                    or set(row) - required - {'before_pointer'}):
                raise RuntimeError('invalid_sync_receipt_change:' + rel)
            path, a, b = row['path'], row['before_sha256'], row['after_sha256']
            _path(root, path)
            _digest(a, nullable=True)
            _digest(b, nullable=True)
            if a == b or _pointer(document, row['after_pointer']) != b:
                raise RuntimeError('sync_receipt_transition_mismatch:' + path)
            if 'before_pointer' in row and _pointer(document, row['before_pointer']) != a:
                raise RuntimeError('sync_receipt_transition_mismatch:' + path)
            chain = chains.setdefault(path, [old.get(path), old.get(path), []])
            if chain[1] != a:
                raise RuntimeError('unexplained_sync_state_receipt_chain:' + path)
            chain[1] = b
            chain[2].append(rel)
    return {path: tuple(chain) for path, chain in chains.items()}, evidence


def proposal(root, receipts=None):
    root = Path(root)
    if receipts is None:
        raise RuntimeError('sync_receipts_required')
    before = read_regular(_path(root, SNAPSHOT))[0]
    current = read_regular(_path(root, 'ops/.rebuild-sync-state.json'))[0]
    if sha(before) != BEFORE:
        raise RuntimeError('sync_rebaseline_preimage_moved')
    old, new = _state(before), _state(current)
    for path, digest in {**old, **new}.items():
        _path(root, path)
        _digest(digest)
    explanations, evidence = _explanations(root, receipts, old)
    changes = explained_diff(before, current, explanations)
    for item in changes:
        target = _path(root, item['path'])
        expected = item['after_sha256']
        if expected is None:
            matches = not target.exists()
        else:
            matches = sha(read_regular(target)[0]) == expected
        if not matches:
            raise RuntimeError('sync_rebaseline_installed_source_moved:' + item['path'])
    return {'schema': 'glp73-sync-rebaseline-proposal-v2',
            'old_state_sha256': BEFORE, 'new_state_sha256': sha(current),
            'before_snapshot': SNAPSHOT, 'diff': changes, 'evidence_sha256': evidence,
            'receipts': copy.deepcopy(receipts)}


def validate(root, review):
    if (not isinstance(review, dict)
            or review.get('schema') != 'glp73-sync-rebaseline-review-v1'
            or review.get('decision') != 'ACCEPT_EXPLAINED_DIFF'
            or not isinstance(review.get('prepared_by'), str) or not review['prepared_by'].strip()
            or not isinstance(review.get('reviewer'), str) or not review['reviewer'].strip()
            or review['prepared_by'].strip() == review['reviewer'].strip()
            or not isinstance(review.get('proposal'), dict)
            or review['proposal'].get('schema') != 'glp73-sync-rebaseline-proposal-v2'
            or not isinstance(review['proposal'].get('receipts'), list)):
        raise RuntimeError('independent_explained_diff_review_required')
    expected = proposal(root, review['proposal']['receipts'])
    if review['proposal'] != expected:
        raise RuntimeError('independent_explained_diff_review_required')
    return expected['new_state_sha256']


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--root', type=Path, default=Path('/home/ubuntu/business'))
    parser.add_argument('--receipts', type=Path, required=True, help='JSON list of pinned authorized sync receipt descriptors')
    args = parser.parse_args()
    try:
        receipts = json.loads(read_regular(args.receipts)[0])
        print(json.dumps(proposal(args.root, receipts), indent=2, sort_keys=True))
        return 0
    except (ValueError, OSError, RuntimeError) as exc:
        parser.exit(2, 'GLP73_REBASELINE_REFUSED: ' + str(exc) + '\n')


if __name__ == '__main__':
    raise SystemExit(main())
