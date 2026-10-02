"""Git successor discovery migration: disposable source/root, no discovery execution."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from test_discovery_predecessor_migration import repo, inputs, dump, sha, REL, BEFORE, FIXTURE
from scripts import discovery_predecessor_migration as migration
from scripts import sync_from_repo as syncer
from glp_ship_generation import GitGeneration, fingerprint, json_sha


@pytest.fixture
def successor(repo, inputs, tmp_path):
    candidate = tmp_path / 'candidate'
    subprocess.run(['git', 'clone', '-q', '--no-hardlinks', str(repo), str(candidate)], check=True)
    (candidate / REL).write_bytes((candidate / REL).read_bytes() + b'\n# Git generation fixture successor\n')
    manifest = json.loads((candidate / syncer.MANIFEST).read_text())
    manifest['files'][REL]['sha256'] = sha((candidate / REL).read_bytes())
    dump(candidate / syncer.MANIFEST, manifest)
    subprocess.run(['git', '-C', str(candidate), '-c', 'user.name=Fixture', '-c',
                    'user.email=fixture@example.invalid', 'commit', '-qam', 'Successor fixture'], check=True)
    source = subprocess.check_output(['git', '-C', str(candidate), 'rev-parse', 'HEAD'], text=True).strip()
    root, review_path, authority_path, _ = inputs
    hermes = root.parent / '.hermes'
    (hermes / 'cron').mkdir()
    dump(hermes / 'cron/jobs.json', {'jobs': []})
    pins = {REL: sha((candidate / REL).read_bytes())}
    proposal = {'source': source, 'paths': [REL], 'hashes': pins, 'kind': 'discovery',
                'merge_proof': {'merge_sha': source}, 'tier': 'A', 'prepared_by': 'author',
                'root': str(root), 'hermes': str(hermes), 'jobs': {},
                'before': {str(root / path): fingerprint(root / path)
                           for path in (REL, syncer.STATE, syncer.MANIFEST)}}
    generation = GitGeneration(source, 'discovery', (REL,), pins, proposal['merge_proof'], proposal)
    proposal['discovery_plan'] = migration.preview_generation(candidate, root, generation)
    authority = {'owner': 'owner', 'writer_exclusion': 'Isolated fixture, no readers or writers.',
                 'proposal_sha256': json_sha(proposal)}
    review = {'schema': 'glp-ship-review-v1', 'decision': 'ACCEPT_EXACT_PROPOSAL',
              'prepared_by': 'author', 'reviewer': 'independent reviewer',
              'proposal_sha256': json_sha(proposal), 'adoption_authority': authority}
    dump(review_path, review)
    dump(authority_path, authority)
    return candidate, generation


def test_preview_git_generation_needs_no_acceptance(successor, inputs):
    candidate, generation = successor
    root, review, authority, _ = inputs
    review.unlink()
    authority.unlink()
    assert hasattr(migration, 'preview_generation'), 'Git generation preview missing'
    proposal = migration.preview_generation(candidate, root, generation)
    assert proposal['source_head'] == generation.source
    assert proposal['predecessor_sha256'] == BEFORE
    assert sha((root / REL).read_bytes()) == BEFORE
    assert not (root / 'reports/ops/pin-archive').exists()


def test_git_generation_reuses_migration_custody_and_sync(successor, inputs):
    candidate, generation = successor
    root, review, authority, _ = inputs
    state = (root / syncer.STATE).read_bytes()
    with syncer.source_lock(root):
        assert migration.migrate(syncer._sync_locked, root, candidate, True, (), (REL,),
                                 review, authority, generation=generation) == 0
    assert (root / REL).read_bytes() == (candidate / REL).read_bytes()
    assert (root / REL).stat().st_mode & 0o777 == 0o600
    assert json.loads((root / syncer.STATE).read_text())['hashes'][REL] == generation.hashes[REL]
    custody = root / 'reports/ops/pin-archive/glp330-discovery' / sha(review.read_bytes())
    assert (custody / (BEFORE + '.bytes')).read_bytes() == (candidate / FIXTURE).read_bytes()
    assert (custody / (sha(state) + '.bytes')).read_bytes() == state


@pytest.mark.parametrize('mutation', [
    'same_reviewer', 'missing_exclusion', 'missing_owner', 'authority_mismatch',
    'unknown_predecessor', 'baseline_present', 'changed_plan', 'wrong_kind',
    'wrong_hash', 'missing_review', 'wrong_hermes', 'tier_b',
])
def test_git_generation_refuses_unreviewed_or_unknown_inputs(successor, inputs, mutation):
    from dataclasses import replace
    candidate, generation = successor
    root, review_path, authority_path, _ = inputs
    review = json.loads(review_path.read_text())
    authority = json.loads(authority_path.read_text())
    if mutation == 'same_reviewer':
        review['reviewer'] = review['prepared_by']
    elif mutation in ('missing_exclusion', 'missing_owner'):
        authority['writer_exclusion' if mutation == 'missing_exclusion' else 'owner'] = ''
        review['adoption_authority'] = authority
    elif mutation == 'authority_mismatch':
        authority['owner'] = 'different owner'
    elif mutation == 'unknown_predecessor':
        (root / REL).write_text('unknown edit')
    elif mutation == 'baseline_present':
        state = json.loads((root / syncer.STATE).read_text())
        state['hashes'][REL] = BEFORE
        dump(root / syncer.STATE, state)
    elif mutation == 'changed_plan':
        generation.proposal['discovery_plan']['installer_head'] = '0' * 40
    elif mutation == 'wrong_kind':
        generation = replace(generation, kind='reporting')
    elif mutation == 'wrong_hash':
        generation = replace(generation, hashes={REL: '0' * 64})
    elif mutation == 'wrong_hermes':
        generation.proposal['hermes'] = str(root / 'other-hermes')
    elif mutation == 'tier_b':
        generation.proposal['tier'] = 'B'
    dump(review_path, review)
    dump(authority_path, authority)
    if mutation == 'missing_review':
        review_path.unlink()
    before = (root / REL).read_bytes()
    state = (root / syncer.STATE).read_bytes()
    with pytest.raises((RuntimeError, ValueError, OSError)):
        migration.migrate(syncer._sync_locked, root, candidate, True, (), (REL,),
                          review_path, authority_path, generation=generation)
    assert (root / REL).read_bytes() == before
    assert (root / syncer.STATE).read_bytes() == state
    assert not (root / 'reports/ops/pin-archive').exists()


def test_git_generation_lock_contention_retains_predecessor(successor, inputs):
    import fcntl
    candidate, generation = successor
    root, review, authority, locks = inputs
    with locks[2].open('rb') as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            migration.migrate(syncer._sync_locked, root, candidate, True, (), (REL,),
                              review, authority, generation=generation)
    assert sha((root / REL).read_bytes()) == BEFORE
    assert not (root / 'reports/ops/pin-archive').exists()


def test_later_git_successor_uses_existing_verified_baseline(successor, inputs):
    candidate, generation = successor
    root, review_path, authority_path, _ = inputs
    historical = subprocess.check_output(['git', '-C', str(candidate), 'show', migration.SOURCE + ':' + REL])
    (root / REL).write_bytes(historical)
    state = json.loads((root / syncer.STATE).read_text())
    state['hashes'][REL] = sha(historical)
    dump(root / syncer.STATE, state)
    generation.proposal['before'] = {name: fingerprint(name) for name in generation.proposal['before']}
    generation.proposal['discovery_plan'] = migration.preview_generation(candidate, root, generation)
    review = json.loads(review_path.read_text())
    review['proposal_sha256'] = json_sha(generation.proposal)
    review['adoption_authority']['proposal_sha256'] = review['proposal_sha256']
    dump(review_path, review)
    dump(authority_path, review['adoption_authority'])
    assert migration.migrate(syncer._sync_locked, root, candidate, True, (), (REL,),
                             review_path, authority_path, generation=generation) == 0
    assert (root / REL).read_bytes() == (candidate / REL).read_bytes()


def test_legacy_null_baseline_refuses_before_writes(repo, inputs):
    root, review_path, authority_path, _ = inputs
    state = json.loads((root / syncer.STATE).read_text())
    state['hashes'][REL] = None
    dump(root / syncer.STATE, state)
    review = json.loads(review_path.read_text())
    review['proposal']['state_before_sha256'] = sha((root / syncer.STATE).read_bytes())
    dump(review_path, review)
    authority = json.loads(authority_path.read_text())
    authority['proposal'] = review['proposal']
    authority['review_sha256'] = sha(review_path.read_bytes())
    dump(authority_path, authority)
    before_state = (root / syncer.STATE).read_bytes()
    with pytest.raises(RuntimeError, match='^discovery_baseline_already_present$'):
        migration.checked(repo, root, review_path, authority_path, [REL])
    assert sha((root / REL).read_bytes()) == BEFORE
    assert (root / syncer.STATE).read_bytes() == before_state
    assert not (root / 'reports/ops/pin-archive').exists()


def test_git_generation_null_baseline_is_not_absence(successor, inputs):
    candidate, generation = successor
    root, _, _, _ = inputs
    state = json.loads((root / syncer.STATE).read_text())
    state['hashes'][REL] = None
    dump(root / syncer.STATE, state)
    before_state = (root / syncer.STATE).read_bytes()
    with pytest.raises(RuntimeError, match='^discovery_known_git_baseline_required$'):
        migration.preview_generation(candidate, root, generation)
    assert sha((root / REL).read_bytes()) == BEFORE
    assert (root / syncer.STATE).read_bytes() == before_state
    assert not (root / 'reports/ops/pin-archive').exists()
