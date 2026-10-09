"""Authentic fixed Pallas cohort; synthetic roots/authority, no business effects."""
import hashlib
import importlib
import importlib.util
import json
import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest
from scripts import sync_from_repo as importer

ROOT = Path(__file__).resolve().parents[1]
SOURCE = '323b92c59276fdd06a082008d61e8716d97d9a9a'
W = 'scripts/affiliate_sheet_batch_writer.py'
D = 'scripts/glp_sheet_sync_drain.py'
S = 'ops/hermes-cron-scripts/glp_affiliate_reconciliation_honesty.sh'
R = 'scripts/glp_affiliate_registry_sync.py'
P = 'scripts/pallas_relationship_ending.py'
SELECTED = (W, D, S, R, P)
PREDECESSORS = {W: '02bbcb6fe79d3e2e8dab558cd20f1b81c57a9302',
                D: '3c7daedd61c62575b356fefe435d06ed566d54c5',
                S: '5d0b43ebc4e6a79c34a43a17f6928ac4c0b97c50'}
BASELINES = {W: 'e5a05f3d14b5e525cab9dcb76ff733ca0c9e19f1518a327567433d1a2d75c656',
             D: '118b32dec2f20bcf2f085c049524244a629a7c5b92ff3f6144ce74d29b7442fe',
             S: '119fdf2c23d748888cf29e6449e4e7a149826141213e944e38ea8a1e122b0e6d',
             R: '7abb012bf345b08ad4424ffe629614b3221152b4c1e1b5cd1a3f7c7496e83299'}


def sha(body):
    return hashlib.sha256(body).hexdigest()


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], stderr=subprocess.PIPE)


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')


@pytest.fixture
def inputs(tmp_path):
    # Share read-only git objects instead of cloning the whole business checkout.
    repo, root = tmp_path / 'repo', tmp_path / 'business'
    repo.mkdir(); root.mkdir()
    git(repo, 'init', '-q')
    common = Path(git(ROOT, 'rev-parse', '--git-common-dir').decode().strip())
    if not common.is_absolute():
        common = ROOT / common
    alternates = repo / '.git/objects/info/alternates'
    alternates.write_text(str((common / 'objects').resolve()) + '\n')
    for p in SELECTED:
        dst = repo / p
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(git(ROOT, 'show', SOURCE + ':' + p))
        if p != P:
            live = root / p
            live.parent.mkdir(parents=True, exist_ok=True)
            live.write_bytes(git(ROOT, 'show', PREDECESSORS[p] + ':' + p)
                             if p in PREDECESSORS else (ROOT / p).read_bytes())
            if p == R:
                # Exact retained registry predecessor, never a synthesized body.
                candidates = git(ROOT, 'log', '--format=%H', '--', p).decode().splitlines()
                for commit in candidates:
                    body = git(ROOT, 'show', commit + ':' + p)
                    if sha(body) == BASELINES[R]:
                        live.write_bytes(body); break
                assert sha(live.read_bytes()) == BASELINES[R]
            live.chmod(0o640)
    for p in ('scripts/sync_from_repo.py', 'scripts/pallas_predecessor_migration.py',
              'scripts/maxbounty_pin_release.py'):
        if (ROOT / p).exists():
            (repo / p).write_bytes((ROOT / p).read_bytes())
    files = {str(p.relative_to(repo)): {'sha256': sha(p.read_bytes())}
             for folder in (repo / 'scripts', repo / 'ops')
             for p in folder.rglob('*') if p.is_file()}
    dump(repo / importer.MANIFEST, {'schema': 'GLP_BUSINESS_SOURCE_V1',
                                  'files': files, 'tracked_count': len(files) + 1})
    git(repo, 'add', '.')
    tree = git(repo, 'write-tree').decode().strip()
    head = git(repo, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
               'commit-tree', tree, '-p', SOURCE, '-m', 'Synthetic exact migration fixture').decode().strip()
    git(repo, 'update-ref', 'HEAD', head)
    dump(root / importer.STATE, {'schema': 1, 'hashes': {**BASELINES, 'scripts/unrelated.py': 'a' * 64}})
    (root / importer.STATE).chmod(0o600)
    (root / importer.MANIFEST).write_bytes(b'unselected installed manifest\n')
    (root / 'scripts/unrelated.py').write_bytes(b'unrelated retained bytes\n')
    lock = root / 'locks/glp-affiliate-sheet.lock'
    lock.parent.mkdir(); lock.touch(); lock.chmod(0o600)
    return repo, root


@contextmanager
def isolated_readers():
    yield True


def receipts(tmp_path, module, repo, root):
    proposal = module.prepare(repo, root)
    review, authority = tmp_path / 'review.json', tmp_path / 'authority.json'
    dump(review, {'schema': 'glp347-pallas-migration-review-v1', 'decision': 'ACCEPT_EXACT_MIGRATION',
                  'prepared_by': 'fixture-author', 'reviewer': 'fixture-independent', 'proposal': proposal})
    dump(authority, {'schema': 'glp347-pallas-migration-authority-v1', 'decision': 'AUTHORIZE_EXACT_MIGRATION',
                     'owner': 'fixture-owner', 'reader_exclusion': 'Isolated synthetic fixture with no live readers.',
                     'review_sha256': sha(review.read_bytes()), 'proposal': proposal})
    return review, authority


def migration_module():
    assert importlib.util.find_spec('scripts.pallas_predecessor_migration') is not None, 'fixed Pallas migration is missing'
    return importlib.import_module('scripts.pallas_predecessor_migration')


def test_fixed_predecessor_copin_install_via_real_importer(inputs, tmp_path, capsys):
    module = migration_module()
    repo, root = inputs
    state = (root / importer.STATE).read_bytes()
    manifest = (root / importer.MANIFEST).read_bytes()
    assert importer.sync(root, repo, False, only=SELECTED) == 2
    review, authority = receipts(tmp_path, module, repo, root)
    assert module.migrate(repo, root, False, review, authority) == 0
    assert (root / importer.STATE).read_bytes() == state
    assert not (root / 'reports').exists()
    assert module.migrate(repo, root, True, review, authority, reader_exclusion=isolated_readers) == 0
    for p in SELECTED:
        assert (root / p).read_bytes() == (repo / p).read_bytes()
        if p != P:
            assert (root / p).stat().st_mode & 0o777 == 0o640
    assert (root / importer.STATE).stat().st_mode & 0o777 == 0o600
    assert (root / importer.MANIFEST).read_bytes() == manifest
    hashes = json.loads((root / importer.STATE).read_text())['hashes']
    assert hashes == {'scripts/unrelated.py': 'a' * 64, **{p: sha((repo / p).read_bytes()) for p in SELECTED}}
    assert (root / 'scripts/unrelated.py').read_bytes() == b'unrelated retained bytes\n'
    custody = root / 'reports/ops/pin-archive/glp347-pallas' / sha(review.read_bytes())
    assert (custody / (sha(state) + '.bytes')).read_bytes() == state
    assert importer.sync(root, repo, False, only=SELECTED) == 0
    assert 'DRY_RUN diffs=0' in capsys.readouterr().out


@pytest.mark.parametrize('path', (W, D, S, R))
def test_changed_predecessor_refuses_without_effects(inputs, path):
    module = migration_module()
    repo, root = inputs
    state = (root / importer.STATE).read_bytes()
    (root / path).write_bytes((root / path).read_bytes() + b'\n# unreviewed change\n')
    with pytest.raises(RuntimeError, match='pallas_exact_predecessor_required'):
        module.prepare(repo, root)
    assert (root / importer.STATE).read_bytes() == state
    assert not (root / 'reports').exists()


@pytest.mark.parametrize('path', (W, D, S, R, P))
def test_wrong_existing_baseline_refuses(inputs, path):
    module = migration_module()
    repo, root = inputs
    state = json.loads((root / importer.STATE).read_text())
    state['hashes'][path] = 'b' * 64
    dump(root / importer.STATE, state)
    with pytest.raises(RuntimeError, match='pallas_fixed_baseline_required'):
        module.prepare(repo, root)
    assert not (root / 'reports').exists()


def test_existing_pallas_helper_not_adopted(inputs):
    module = migration_module()
    repo, root = inputs
    (root / P).write_bytes((repo / P).read_bytes())
    with pytest.raises(RuntimeError, match='pallas_exact_absence_required'):
        module.prepare(repo, root)


def test_generic_existing_baseline_adoption_stays_prohibited(inputs):
    repo, root = inputs
    with pytest.raises(ValueError, match='absent from sync state'):
        importer.sync(root, repo, False, adopt_reviewed_baseline=[W], only=SELECTED)


def test_fake_private_import_admission_refuses(inputs):
    repo, root = inputs
    with pytest.raises(RuntimeError, match='pallas_live_migration_lease_required'):
        importer._sync_locked(root, repo, True, only=SELECTED, _pallas_migration=object())
    assert not (root / P).exists()


@pytest.mark.parametrize('changed', ('self_review', 'proposal', 'authority_review_hash', 'missing_exclusion'))
def test_review_and_authority_fail_closed(inputs, tmp_path, changed):
    module = migration_module()
    repo, root = inputs
    review, authority = receipts(tmp_path, module, repo, root)
    state = (root / importer.STATE).read_bytes()
    if changed == 'self_review':
        value = json.loads(review.read_text()); value['reviewer'] = value['prepared_by']; dump(review, value)
    elif changed == 'proposal':
        value = json.loads(review.read_text()); value['proposal']['installer_head'] = '0' * 40; dump(review, value)
    elif changed == 'authority_review_hash':
        value = json.loads(authority.read_text()); value['review_sha256'] = '0' * 64; dump(authority, value)
    with pytest.raises(RuntimeError, match='pallas_.*required'):
        module.migrate(repo, root, True, review, authority)
    assert (root / importer.STATE).read_bytes() == state
    assert not (root / 'reports').exists()


def test_apply_requires_real_external_exclusion(inputs, tmp_path):
    module = migration_module()
    repo, root = inputs
    review, authority = receipts(tmp_path, module, repo, root)
    @contextmanager
    def refused():
        yield False
    with pytest.raises(RuntimeError, match='pallas_external_reader_exclusion_required'):
        module.migrate(repo, root, True, review, authority, reader_exclusion=refused)
    assert not (root / 'reports').exists()


def test_preimage_race_under_exclusion_refuses(inputs, tmp_path):
    module = migration_module()
    repo, root = inputs
    review, authority = receipts(tmp_path, module, repo, root)
    state = (root / importer.STATE).read_bytes()
    @contextmanager
    def raced():
        (root / W).chmod(0o600)
        yield True
    with pytest.raises(RuntimeError):
        module.migrate(repo, root, True, review, authority, reader_exclusion=raced)
    assert (root / importer.STATE).read_bytes() == state
    assert not (root / 'reports').exists()


def test_existing_sheet_lock_contention_refuses(inputs, tmp_path):
    import fcntl
    module = migration_module()
    repo, root = inputs
    review, authority = receipts(tmp_path, module, repo, root)
    with (root / 'locks/glp-affiliate-sheet.lock').open('r+') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            module.migrate(repo, root, True, review, authority, reader_exclusion=isolated_readers)
    assert not (root / 'reports').exists()


def test_successor_symlink_refused(inputs):
    module = migration_module()
    repo, root = inputs
    (root / W).unlink()
    (root / W).symlink_to(repo / W)
    with pytest.raises(RuntimeError, match='symlink_refused'):
        module.prepare(repo, root)


def test_uncommitted_successor_change_refuses(inputs):
    module = migration_module()
    repo, root = inputs
    (repo / W).write_bytes((repo / W).read_bytes() + b'\n# altered successor\n')
    with pytest.raises(RuntimeError, match='pallas_fixed_exact_head_successor_required'):
        module.prepare(repo, root)


def test_no_public_force_baseline_flag():
    source = (ROOT / 'scripts/sync_from_repo.py').read_text()
    assert 'add_argument("--pallas' not in source
    assert 'add_argument("--force' not in source
