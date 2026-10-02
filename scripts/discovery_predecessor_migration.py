"""One retained uncommitted discovery predecessor; not a force/import registry.

Only the PR111 bytes at the original curated path qualify. Receipts authenticate
scope, not their authors: the adoption owner must verify reviewer identity and
exclude direct callers/other source writers before invoking this source-only path.
"""
from contextlib import ExitStack
import fcntl
import hashlib
from pathlib import Path
import subprocess

try:
    from .maxbounty_pin_release import (read_regular, strict_json, preserve_custody,
                                       assert_unchanged)
except ImportError:  # direct sync_from_repo.py CLI
    from maxbounty_pin_release import (read_regular, strict_json, preserve_custody,
                                      assert_unchanged)

REL = 'scripts/affiliate_supply_discovery.py'
BEFORE = '088e2557d19f34173af2d52f2515116355968d90e40bc0ba981ac47ae34280bf'
AFTER = '8f882b3fc6ae1d6fc81590c94c84b1e28c1f094e273b7b0643c013366682ca7f'
SOURCE = 'f6f5704157bc988d406732eb040ac21d343f82fa'
FIXTURE = 'tests/fixtures/discovery_predecessor/088e2557.bytes'
COHORT = frozenset((REL, 'scripts/affiliate_monday_discovery_card.py',
    'scripts/affiliate_monday_marketplace_receipt.py', 'scripts/affiliate_supply_expansion.py',
    'ops/hermes-cron-scripts/affiliate_supply_discovery_daily.sh',
    'tests/test_affiliate_discovery_resume.py'))


def digest(body):
    return hashlib.sha256(body).hexdigest()


def git(checkout, *args):
    return subprocess.check_output(['git', '-C', str(checkout), *args])


def checked(checkout, root, review_path, authority_path, only, *, generation=None):
    head = git(checkout, 'rev-parse', 'HEAD').decode().strip()
    if git(checkout, 'status', '--porcelain', '--untracked-files=no'):
        raise RuntimeError('discovery_checkout_not_clean')
    git(checkout, 'merge-base', '--is-ancestor', SOURCE, head)
    selected = set(only)
    if REL not in selected or not selected <= COHORT or len(selected) != len(only):
        raise RuntimeError('discovery_exact_selected_paths_required')
    source, successor = SOURCE, AFTER
    if generation is not None:
        from glp_ship_provenance import derive_pins
        if (generation.kind != 'discovery' or selected != set(generation.paths)
                or len(generation.paths) != len(selected)):
            raise RuntimeError('discovery_git_generation_scope_invalid')
        source = generation.source
        git(checkout, 'merge-base', '--is-ancestor', source, head)
        pins = derive_pins(checkout, source, generation.paths)
        if pins != generation.hashes:
            raise RuntimeError('discovery_git_generation_hashes_moved')
        successor = pins[REL]
        hermes = Path(generation.proposal['hermes']).resolve()
        if (hermes != (root.parent / '.hermes').resolve()
                or Path(generation.proposal['root']).resolve() != root.resolve()):
            raise RuntimeError('discovery_original_lock_root_required')
    observed = {}

    def retain(path):
        value = read_regular(path)
        observed[path] = value
        return value[0]

    # Bind the executing installer and its existing custody primitives, not just
    # an arbitrary reviewed checkout whose producer the caller has substituted.
    for rel, running in (('scripts/sync_from_repo.py', Path(__file__).with_name('sync_from_repo.py')),
                         ('scripts/discovery_predecessor_migration.py', Path(__file__)),
                         ('scripts/maxbounty_pin_release.py', Path(__file__).with_name('maxbounty_pin_release.py'))):
        committed = git(checkout, 'show', head + ':' + rel)
        if retain(checkout / rel) != committed or running.read_bytes() != committed:
            raise RuntimeError('discovery_installer_not_exact_head:' + rel)
    for rel in selected:
        body = git(checkout, 'show', source + ':' + rel)
        if retain(checkout / rel) != body or git(checkout, 'show', head + ':' + rel) != body:
            raise RuntimeError('discovery_accepted_successor_moved:' + rel)
        if (root / rel).exists():
            retain(root / rel)
    manifest = 'ops/rebuild-source-manifest.json'
    if retain(checkout / manifest) != git(checkout, 'show', head + ':' + manifest):
        raise RuntimeError('discovery_manifest_not_exact_head')
    retained = retain(checkout / FIXTURE)
    if digest(retained) != BEFORE or git(checkout, 'show', head + ':' + FIXTURE) != retained:
        raise RuntimeError('discovery_retained_predecessor_moved')
    local = retain(root / REL)
    state = retain(root / 'ops/.rebuild-sync-state.json')
    baselines = strict_json(state).get('hashes', {})
    retained_mode = REL not in baselines
    if not retained_mode:
        if generation is None:
            raise RuntimeError('discovery_baseline_already_present')
        baseline = baselines[REL]
        from sync_from_repo import reviewed_prior_hashes
        if baseline != digest(local) or baseline not in reviewed_prior_hashes(checkout, REL) | {successor}:
            raise RuntimeError('discovery_known_git_baseline_required')
    elif local != retained or digest(local) != BEFORE:
        raise RuntimeError('discovery_exact_predecessor_required')
    if digest(observed[checkout / REL][0]) != successor:
        raise RuntimeError('discovery_exact_predecessor_required' if generation is None
                           else 'discovery_exact_successor_required')
    if (root / manifest).exists():
        retain(root / manifest)
    proposal = {'installer_head': head, 'source_head': source, 'root': str(root),
                'selected': sorted(selected), 'predecessor_sha256': digest(local),
                'successor_sha256': successor, 'state_before_sha256': digest(state),
                'installed_manifest_before': {
                    'sha256': digest(observed[root / manifest][0]),
                    'mode': observed[root / manifest][1],
                } if root / manifest in observed else None,
                'installed_before': {p: {
                    'sha256': digest(observed[root / p][0]) if root / p in observed else None,
                    'mode': observed[root / p][1] if root / p in observed else None,
                } for p in sorted(selected)}}
    if generation is not None:
        proposal['predecessor_mode'] = 'retained' if retained_mode else 'git'
    if generation is not None and review_path is None and authority_path is None:
        return observed, proposal  # Read-only preview, never an acceptance receipt.
    review_body = retain(review_path)
    review = strict_json(review_body)
    if generation is not None:
        from glp_ship_generation import json_sha
        generation.verify(checkout)
        if (generation.proposal.get('tier') != 'A'
                or generation.proposal.get('discovery_plan') != proposal):
            raise RuntimeError('discovery_reviewed_plan_moved')
        generation.authorize(review, root, hermes, None)
        authority = strict_json(retain(authority_path))
        if (review_path.resolve() == authority_path.resolve()
                or authority != review.get('adoption_authority')
                or not str(authority.get('owner') or '').strip()
                or not str(authority.get('writer_exclusion') or '').strip()
                or authority.get('proposal_sha256') != json_sha(generation.proposal)):
            raise RuntimeError('discovery_separate_adoption_authority_required')
        return observed, digest(review_body)
    if (review.get('schema') != 'glp330-discovery-migration-review-v1'
            or review.get('decision') != 'ACCEPT_EXACT_MIGRATION'
            or not str(review.get('prepared_by') or '').strip()
            or not str(review.get('reviewer') or '').strip()
            or review['prepared_by'].strip() == review['reviewer'].strip()
            or review.get('proposal') != proposal):
        raise RuntimeError('discovery_independent_exact_review_required')
    authority = strict_json(retain(authority_path))
    if (review_path.resolve() == authority_path.resolve()
            or authority.get('schema') != 'glp330-discovery-adoption-authority-v1'
            or authority.get('decision') != 'AUTHORIZE_EXACT_MIGRATION'
            or not str(authority.get('owner') or '').strip()
            or not str(authority.get('writer_exclusion') or '').strip()
            or authority.get('review_sha256') != digest(review_body)
            or authority.get('proposal') != proposal):
        raise RuntimeError('discovery_separate_adoption_authority_required')
    return observed, digest(review_body)



def preview_generation(checkout, root, generation):
    """Read-only exact source/predecessor plan; never mints review or owner authority."""
    root, checkout = Path(root).resolve(strict=True), Path(checkout).resolve(strict=True)
    _, proposal = checked(checkout, root, None, None, generation.paths, generation=generation)
    return proposal


def migrate(syncer, root, checkout, apply, adopted, only, review_path, authority_path, *, generation=None, _ship_publish=None):
    if _ship_publish is not None and generation is None:
        raise RuntimeError('discovery_publish_requires_git_generation')
    root, checkout = root.resolve(strict=True), checkout.resolve(strict=True)
    if review_path is None or authority_path is None or REL in adopted:
        raise RuntimeError('discovery_review_and_separate_authority_required')
    # No writes (including lock/custody creation) before complete refusal checks.
    before, receipt_hash = checked(checkout, root, review_path, authority_path, only, generation=generation)
    preimage = BEFORE if generation is None or generation.proposal['discovery_plan']['predecessor_mode'] == 'retained' else None
    if syncer(root, checkout, False, adopted, only, _discovery_preimage=preimage) != 0:
        return 2
    if not apply:
        return 0
    # Same consuming locks used by the discovery/application installation owner.
    # Existing locks only; external launch/source-owner exclusion is still required.
    locks = (root / 'locks/glp-affiliate-sheet.lock',
             root / 'reports/glp-clinic-scout/affiliate-machine/applications/.application-machine.lock',
             root.parent / '.hermes/state/affiliate-supply-discovery.lock')
    with ExitStack() as stack:
        for path in locks:
            read_regular(path)  # refuse symlinks/nonregular paths; never create
            lock = stack.enter_context(path.open('rb'))
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert_unchanged(before)
        fresh, fresh_hash = checked(checkout, root, review_path, authority_path, only, generation=generation)
        if fresh != before or fresh_hash != receipt_hash:
            raise RuntimeError('discovery_inputs_moved_under_lock')
        custody = root / 'reports/ops/pin-archive/glp330-discovery' / receipt_hash
        preserve_custody(custody, before, {root / p: before[checkout / p][0] for p in only})
        assert_unchanged(before)
        result = syncer(root, checkout, True, adopted, only, _discovery_preimage=preimage)
        if result == 0:
            for rel in only:
                if read_regular(root / rel)[0] != before[checkout / rel][0]:
                    raise RuntimeError('discovery_readback_mismatch_no_retry')
            if _ship_publish is not None:
                _ship_publish()
            print('discovery custody:', custody)
        return result
