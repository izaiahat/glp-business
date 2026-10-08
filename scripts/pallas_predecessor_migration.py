#!/usr/bin/env python3
"""Fixed GLP347 predecessor/co-pin import, not a baseline adoption/force tool.

Apply requires independent exact-proposal review, separate receiving-host authority
and a caller-established reader exclusion context. No Sheet, cron or site effects.
"""
from contextlib import contextmanager, nullcontext
import fcntl
import hashlib
import os
from pathlib import Path
import subprocess

from scripts import sync_from_repo as importer
from scripts.maxbounty_pin_release import read_regular, strict_json, assert_unchanged, preserve_custody

SOURCE = '323b92c59276fdd06a082008d61e8716d97d9a9a'
W = 'scripts/affiliate_sheet_batch_writer.py'
D = 'scripts/glp_sheet_sync_drain.py'
S = 'ops/hermes-cron-scripts/glp_affiliate_reconciliation_honesty.sh'
R = 'scripts/glp_affiliate_registry_sync.py'
P = 'scripts/pallas_relationship_ending.py'
SELECTED = (W, D, S, R, P)
# (existing importer baseline, authentic installed predecessor, exact successor)
FIXED = {
    W: ('e5a05f3d14b5e525cab9dcb76ff733ca0c9e19f1518a327567433d1a2d75c656',
        '0fd0453ed5ce2c696befb7687a15b35223fad97889a90f93e0b4286f7c563d66',
        'aef4747d1e9ca683df5bf0edc31c4be12d6769f28a9fe438f439f09e83bfefe3'),
    D: ('118b32dec2f20bcf2f085c049524244a629a7c5b92ff3f6144ce74d29b7442fe',
        'aa12fc53f878bb8c0505fca4681cd116eae1a81bacf99969b1b8ffab48a76abd',
        '10bcc641001f360d9d51046ed84e63bd9e451ad2c800e7e5d010b9e15840628e'),
    S: ('119fdf2c23d748888cf29e6449e4e7a149826141213e944e38ea8a1e122b0e6d',
        '45ad2a9b2c435dc96725456721f688c37cdd73d8f58e57351960c242c4d6e4a9',
        '99bcc4cca7c63f41e3546bb0e470976907027557bf1e7b89d1a0dbb8aaebcfb4'),
    R: ('7abb012bf345b08ad4424ffe629614b3221152b4c1e1b5cd1a3f7c7496e83299',
        '7abb012bf345b08ad4424ffe629614b3221152b4c1e1b5cd1a3f7c7496e83299',
        '037979554a29ea9e0feec557d9700dadba5231c295e6376ff92e9b4fced5174f'),
    P: (None, None, '1b647bcca499e20af8495f5eda5a39e45be27d05e06bbfe68c655e8641273a35'),
}
PRODUCERS = ('scripts/pallas_predecessor_migration.py', 'scripts/sync_from_repo.py',
             'scripts/maxbounty_pin_release.py')
_LEASES = {}


def digest(body):
    return hashlib.sha256(body).hexdigest()


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], stderr=subprocess.PIPE)


def checked(repo, root):
    repo, root = Path(repo).resolve(strict=True), Path(root).resolve(strict=True)
    head = git(repo, 'rev-parse', 'HEAD').decode().strip()
    git(repo, 'merge-base', '--is-ancestor', SOURCE, head)
    observed = {}
    def retain(path):
        observed[path] = read_regular(path)
        return observed[path][0]
    state = strict_json(retain(root / importer.STATE))
    if state.get('schema') != 1 or not isinstance(state.get('hashes'), dict):
        raise RuntimeError('pallas_fixed_state_required')
    for p, (baseline, predecessor, successor) in FIXED.items():
        if state['hashes'].get(p) != baseline or (baseline is None and p in state['hashes']):
            raise RuntimeError('pallas_fixed_baseline_required:' + p)
        live = root / p
        if predecessor is None:
            if live.exists() or live.is_symlink():
                raise RuntimeError('pallas_exact_absence_required:' + p)
        elif digest(retain(live)) != predecessor:
            raise RuntimeError('pallas_exact_predecessor_required:' + p)
        target = retain(repo / p)
        if (digest(target) != successor or target != git(repo, 'show', head + ':' + p)
                or target != git(repo, 'show', SOURCE + ':' + p)):
            raise RuntimeError('pallas_fixed_exact_head_successor_required:' + p)
    for p in PRODUCERS:
        if retain(repo / p) != git(repo, 'show', head + ':' + p):
            raise RuntimeError('pallas_producer_not_exact_head:' + p)
    # Bind the actual running producer and importer, not just a named checkout.
    for p, path in ((PRODUCERS[0], Path(__file__)), (PRODUCERS[1], Path(importer.__file__))):
        if read_regular(path)[0] != observed[repo / p][0]:
            raise RuntimeError('pallas_running_producer_not_exact_head:' + p)
    manifest = retain(repo / importer.MANIFEST)
    if manifest != git(repo, 'show', head + ':' + importer.MANIFEST):
        raise RuntimeError('pallas_manifest_not_exact_head')
    retain(root / importer.MANIFEST)
    proposal = {'schema': 'glp347-pallas-migration-proposal-v1', 'installer_head': head,
                'source_head': SOURCE, 'root': str(root), 'selected': list(SELECTED),
                'absent': [P], 'inputs': {str(p): {'sha256': digest(body), 'mode': mode}
                for p, (body, mode) in sorted(observed.items())}}
    return proposal, observed


def prepare(repo, root):
    """Read-only exact receiving-host proposal; no locks or baseline writes."""
    return checked(repo, root)[0]


def validate_admission(root, repo, token, selected):
    """Opaque same-process handoff only while source and sole-writer locks live."""
    held = _LEASES.get(token)
    if held is None or tuple(selected) != SELECTED:
        raise RuntimeError('pallas_live_migration_lease_required')
    proposal, before, owner = held
    with importer.source_session(root, owner):
        assert_unchanged(before)
        if checked(repo, root)[0] != proposal:
            raise RuntimeError('pallas_migration_inputs_moved')
    return {p: FIXED[p] for p in (W, D, S)}


@contextmanager
def sheet_exclusion(root):
    """Hold the existing sole-writer lock, never create a competing queue/lock."""
    lock_path = root / 'locks/glp-affiliate-sheet.lock'
    read_regular(lock_path)
    fd = os.open(lock_path, os.O_RDWR | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (os.fstat(fd).st_dev, os.fstat(fd).st_ino) != (lock_path.stat().st_dev, lock_path.stat().st_ino):
            raise RuntimeError('pallas_sheet_lock_moved')
        yield
    finally:
        os.close(fd)


def migrate(repo, root, apply, review_path, authority_path, *, reader_exclusion=None):
    """Guarded dry/apply; use ordinary scoped importer for zero-diff readback."""
    repo, root = Path(repo).resolve(strict=True), Path(root).resolve(strict=True)
    proposal, before = checked(repo, root)
    review_path, authority_path = Path(review_path), Path(authority_path)
    review_body, review_mode = read_regular(review_path)
    authority_body, authority_mode = read_regular(authority_path)
    review, authority = strict_json(review_body), strict_json(authority_body)
    if (not isinstance(review, dict) or review.get('schema') != 'glp347-pallas-migration-review-v1'
            or review.get('decision') != 'ACCEPT_EXACT_MIGRATION'
            or not str(review.get('prepared_by') or '').strip()
            or not str(review.get('reviewer') or '').strip()
            or review['prepared_by'].strip() == review['reviewer'].strip()
            or review.get('proposal') != proposal):
        raise RuntimeError('pallas_independent_exact_review_required')
    if (not isinstance(authority, dict) or review_path.resolve() == authority_path.resolve()
            or authority.get('schema') != 'glp347-pallas-migration-authority-v1'
            or authority.get('decision') != 'AUTHORIZE_EXACT_MIGRATION'
            or not str(authority.get('owner') or '').strip()
            or not str(authority.get('reader_exclusion') or '').strip()
            or authority.get('proposal') != proposal
            or authority.get('review_sha256') != digest(review_body)):
        raise RuntimeError('pallas_separate_exact_authority_required')
    if apply and reader_exclusion is None:
        raise RuntimeError('pallas_external_reader_exclusion_required')
    before[review_path] = review_body, review_mode
    before[authority_path] = authority_body, authority_mode
    context = nullcontext(True)
    if apply:
        assert reader_exclusion is not None
        context = reader_exclusion()
    with context as established:
        if established is not True:
            raise RuntimeError('pallas_external_reader_exclusion_required')
        with importer.source_lock(root) as owner, sheet_exclusion(root):
            assert_unchanged(before)
            if checked(repo, root)[0] != proposal:
                raise RuntimeError('pallas_migration_inputs_moved_under_exclusion')
            token = object()
            _LEASES[token] = proposal, before, owner
            try:
                kwargs = {'_pallas_migration': token}
                if importer._sync_locked(root, repo, False, (), SELECTED, **kwargs) != 0:
                    raise RuntimeError('pallas_guarded_preflight_refused')
                if not apply:
                    return 0
                custody = root / 'reports/ops/pin-archive/glp347-pallas' / digest(review_body)
                preserve_custody(custody, before, {root / p: before[repo / p][0] for p in SELECTED})
                assert_unchanged(before)
                if importer._sync_locked(root, repo, True, (), SELECTED, **kwargs) != 0:
                    raise RuntimeError('pallas_guarded_apply_refused_no_retry')
                for p in SELECTED:
                    body, mode = read_regular(root / p)
                    expected_mode = before[root / p][1] if root / p in before else before[repo / p][1]
                    if (body, mode) != (before[repo / p][0], expected_mode):
                        raise RuntimeError('pallas_readback_mismatch_no_retry:' + p)
                after_state, state_mode = read_regular(root / importer.STATE)
                expected = strict_json(before[root / importer.STATE][0])['hashes']
                expected.update({p: FIXED[p][2] for p in SELECTED})
                if (strict_json(after_state) != {'schema': 1, 'hashes': expected}
                        or state_mode != before[root / importer.STATE][1]
                        or read_regular(root / importer.MANIFEST) != before[root / importer.MANIFEST]):
                    raise RuntimeError('pallas_state_or_manifest_readback_mismatch_no_retry')
                return 0
            finally:
                del _LEASES[token]
