"""Real curated discovery path/bytes; synthetic authority, no discovery execution."""
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from scripts import sync_from_repo as syncer

ROOT = Path(__file__).resolve().parents[1]
REL = 'scripts/affiliate_supply_discovery.py'
BEFORE = '088e2557d19f34173af2d52f2515116355968d90e40bc0ba981ac47ae34280bf'
AFTER = '8f882b3fc6ae1d6fc81590c94c84b1e28c1f094e273b7b0643c013366682ca7f'
SOURCE = 'f6f5704157bc988d406732eb040ac21d343f82fa'
FIXTURE = 'tests/fixtures/discovery_predecessor/088e2557.bytes'


def sha(body):
    return hashlib.sha256(body).hexdigest()


def dump(path, data):
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + '\n')


@pytest.fixture(scope='module')
def repo(tmp_path_factory):
    repo = tmp_path_factory.mktemp('source') / 'repo'
    subprocess.run(['git', 'clone', '-q', '--no-hardlinks', str(ROOT), str(repo)], check=True)
    # Before the author commit, exercise the actual maintained candidate code in a
    # fixture-only commit. Never commit predecessor bytes at their production path.
    paths = ['scripts/sync_from_repo.py', 'scripts/discovery_predecessor_migration.py',
             'tests/test_discovery_predecessor_migration.py', FIXTURE]
    for rel in paths:
        if (ROOT / rel).exists():
            (repo / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / rel, repo / rel)
    # Keep this historical migration cohort at its accepted committed bytes;
    # newer registered wrappers are a separate ordinary scoped adoption.
    from scripts import discovery_predecessor_migration as migration
    for rel in migration.COHORT:
        (repo / rel).write_bytes(subprocess.check_output(
            ['git', '-C', str(repo), 'show', SOURCE + ':' + rel]))
    manifest = json.loads((repo / syncer.MANIFEST).read_text())
    for rel in paths + sorted(migration.COHORT):
        if (repo / rel).exists():
            manifest['files'][rel] = {'sha256': sha((repo / rel).read_bytes())}
    manifest['tracked_count'] = len(manifest['files']) + 1
    dump(repo / syncer.MANIFEST, manifest)
    subprocess.run(['git', '-C', str(repo), 'add', '-f',
                    *[p for p in paths + sorted(migration.COHORT) if (repo / p).exists()],
                    syncer.MANIFEST], check=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Fixture', '-c',
                    'user.email=fixture@example.invalid', 'commit', '--allow-empty', '-qm',
                    'Synthetic exact candidate fixture'], check=True)
    return repo


@pytest.fixture
def inputs(tmp_path, repo):
    root = tmp_path / 'business'
    (root / 'scripts').mkdir(parents=True)
    (root / 'ops').mkdir()
    before = (repo / FIXTURE).read_bytes()
    assert sha(before) == BEFORE
    assert sha((repo / REL).read_bytes()) == AFTER
    (root / REL).write_bytes(before)
    (root / REL).chmod(0o600)
    dump(root / syncer.STATE, {'schema': 1, 'hashes': {'scripts/unselected.py': 'a' * 64}})
    (root / syncer.MANIFEST).write_bytes(b'installed manifest stays unchanged\n')
    (root / 'scripts/unselected.py').write_bytes(b'unselected\n')
    locks = [root / 'locks/glp-affiliate-sheet.lock',
             root / 'reports/glp-clinic-scout/affiliate-machine/applications/.application-machine.lock',
             root.parent / '.hermes/state/affiliate-supply-discovery.lock']
    for lock in locks:
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.touch()
    head = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
    review = tmp_path / 'review.json'
    authority = tmp_path / 'authority.json'
    proposal = {'installer_head': head, 'source_head': SOURCE, 'root': str(root),
                'selected': [REL], 'predecessor_sha256': BEFORE,
                'successor_sha256': AFTER, 'state_before_sha256': sha((root / syncer.STATE).read_bytes()),
                'installed_manifest_before': {'sha256': sha((root / syncer.MANIFEST).read_bytes()),
                                              'mode': (root / syncer.MANIFEST).stat().st_mode & 0o777},
                'installed_before': {REL: {'sha256': BEFORE, 'mode': 0o600}}}
    dump(review, {'schema': 'glp330-discovery-migration-review-v1',
                  'decision': 'ACCEPT_EXACT_MIGRATION', 'prepared_by': 'fixture-author',
                  'reviewer': 'fixture-independent-reviewer', 'proposal': proposal})
    dump(authority, {'schema': 'glp330-discovery-adoption-authority-v1',
                     'decision': 'AUTHORIZE_EXACT_MIGRATION', 'owner': 'fixture-owner',
                     'review_sha256': sha(review.read_bytes()), 'proposal': proposal,
                     'writer_exclusion': 'Synthetic isolated fixture only; no real caller exists.'})
    return root, review, authority, locks


def invoke(repo, inputs, apply=False):
    root, review, authority, _ = inputs
    return syncer.sync(root, repo, apply, only=[REL],
                       discovery_review=review, discovery_authority=authority)


def test_authentic_predecessor_migrates_with_custody(repo, inputs, capsys):
    root, review, authority, _ = inputs
    state = (root / syncer.STATE).read_bytes()
    manifest = (root / syncer.MANIFEST).read_bytes()
    assert syncer.sync(root, repo, False, [REL], [REL]) == 2
    assert invoke(repo, inputs) == 0
    assert (root / REL).read_bytes() == (repo / FIXTURE).read_bytes()
    assert (root / syncer.STATE).read_bytes() == state
    assert not (root / 'reports/ops/pin-archive/glp330-discovery').exists()
    assert invoke(repo, inputs, True) == 0
    assert sha((root / REL).read_bytes()) == AFTER
    assert (root / REL).stat().st_mode & 0o777 == 0o600
    assert (root / syncer.MANIFEST).read_bytes() == manifest
    hashes = json.loads((root / syncer.STATE).read_text())['hashes']
    assert hashes == {'scripts/unselected.py': 'a' * 64, REL: AFTER}
    custody = root / 'reports/ops/pin-archive/glp330-discovery' / sha(review.read_bytes())
    for body in ((repo / FIXTURE).read_bytes(), (repo / REL).read_bytes(), state,
                 review.read_bytes(), authority.read_bytes()):
        assert (custody / (sha(body) + '.bytes')).read_bytes() == body
    assert syncer.sync(root, repo, False, only=[REL]) == 0
    assert 'DRY_RUN diffs=0' in capsys.readouterr().out
    with pytest.raises((RuntimeError, ValueError), match='baseline'):
        invoke(repo, inputs, True)


@pytest.mark.parametrize('mutation', [
    'wrong-preimage', 'moved-path', 'symlink', 'baseline', 'same-reviewer',
    'missing-authority', 'changed-authority', 'changed-state', 'moved-head',
    'wrong-committed-successor', 'dirty-source', 'unselected-path', 'changed-mode'])
def test_wrong_or_moved_inputs_refuse_before_any_write(repo, inputs, tmp_path, monkeypatch, mutation):
    from scripts import discovery_predecessor_migration as migration
    root, review, authority, locks = inputs
    target_repo = repo
    if mutation == 'wrong-preimage':
        (root / REL).write_bytes(b'unknown local edit\n')
    elif mutation == 'changed-mode':
        (root / REL).chmod(0o644)
    elif mutation == 'moved-path':
        (root / REL).rename(root / 'scripts/moved.py')
    elif mutation == 'symlink':
        (root / REL).unlink()
        (root / REL).symlink_to(repo / FIXTURE)
    elif mutation == 'baseline':
        doc = json.loads((root / syncer.STATE).read_text())
        doc['hashes'][REL] = BEFORE
        dump(root / syncer.STATE, doc)
    elif mutation == 'same-reviewer':
        doc = json.loads(review.read_text()); doc['reviewer'] = doc['prepared_by']
        dump(review, doc)
    elif mutation == 'missing-authority':
        authority.unlink()
    elif mutation == 'changed-authority':
        doc = json.loads(authority.read_text()); doc['review_sha256'] = '0' * 64
        dump(authority, doc)
    elif mutation == 'changed-state':
        (root / syncer.STATE).write_bytes((root / syncer.STATE).read_bytes() + b'\n')
    elif mutation in {'moved-head', 'wrong-committed-successor', 'dirty-source'}:
        target_repo = tmp_path / 'changed-repo'
        subprocess.run(['git', 'clone', '-q', '--no-hardlinks', str(repo), str(target_repo)], check=True)
        if mutation != 'moved-head':
            (target_repo / REL).write_bytes(b'not the independently accepted successor\n')
        if mutation != 'dirty-source':
            subprocess.run(['git', '-C', str(target_repo), '-c', 'user.name=Fixture', '-c',
                            'user.email=fixture@example.invalid', 'commit', '-qam',
                            'Changed fixture', '--allow-empty'], check=True)
        if mutation == 'wrong-committed-successor':
            # Re-sign the synthetic head binding so this negative cannot pass
            # merely because the old head moved: PR111 target bytes must win.
            head = subprocess.check_output(['git', '-C', str(target_repo), 'rev-parse', 'HEAD'], text=True).strip()
            doc = json.loads(review.read_text()); doc['proposal']['installer_head'] = head
            dump(review, doc)
            doc = json.loads(authority.read_text()); doc['proposal']['installer_head'] = head
            doc['review_sha256'] = sha(review.read_bytes()); dump(authority, doc)
    snapshot = {p.relative_to(root).as_posix(): p.read_bytes()
                for p in root.rglob('*') if p.is_file()}
    def forbidden(*args, **kwargs):
        raise AssertionError('invalid input reached a write boundary')
    monkeypatch.setattr(migration, 'preserve_custody', forbidden)
    monkeypatch.setattr(syncer.tempfile, 'mkstemp', forbidden)
    selected = ['scripts/affiliate_auto_apply.py'] if mutation == 'unselected-path' else [REL]
    with pytest.raises((RuntimeError, ValueError, OSError)):
        syncer.sync(root, target_repo, True, only=selected,
                    discovery_review=review, discovery_authority=authority)
    assert snapshot == {p.relative_to(root).as_posix(): p.read_bytes()
                        for p in root.rglob('*') if p.is_file()}


def test_lock_contention_refuses_before_custody(repo, inputs, monkeypatch):
    import fcntl
    from scripts import discovery_predecessor_migration as migration
    def forbidden(*args, **kwargs):
        raise AssertionError('busy consuming lock reached custody')
    monkeypatch.setattr(migration, 'preserve_custody', forbidden)
    with inputs[3][2].open('rb') as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            invoke(repo, inputs, True)
    assert sha((inputs[0] / REL).read_bytes()) == BEFORE


def test_late_preimage_edit_preserves_editor_and_sync_state(repo, inputs, monkeypatch):
    root, _, _, _ = inputs
    state = (root / syncer.STATE).read_bytes()
    real_fsync = syncer.os.fsync
    # Inject at the actual destination staging barrier, after custody, not at a
    # mocked planning helper. Existing importer CAS must protect this editor.
    def intervene(fd):
        import os
        path = os.readlink('/proc/self/fd/' + str(fd))
        if '/scripts/.sync-' in path:
            (root / REL).write_bytes(b'late editor\n')
        return real_fsync(fd)
    monkeypatch.setattr(syncer.os, 'fsync', intervene)
    with pytest.raises(ValueError, match='destination changed during sync'):
        invoke(repo, inputs, True)
    assert (root / REL).read_bytes() == b'late editor\n'
    assert (root / syncer.STATE).read_bytes() == state


def test_late_state_edit_is_not_overwritten(repo, inputs, monkeypatch):
    root, _, _, _ = inputs
    real_fsync = syncer.os.fsync
    def intervene(fd):
        import os
        if '/ops/.sync-state-' in os.readlink('/proc/self/fd/' + str(fd)):
            (root / syncer.STATE).write_bytes(b'late state editor\n')
        return real_fsync(fd)
    monkeypatch.setattr(syncer.os, 'fsync', intervene)
    with pytest.raises(ValueError, match='sync state changed'):
        invoke(repo, inputs, True)
    assert (root / syncer.STATE).read_bytes() == b'late state editor\n'
    # Ordinary scoped imports are not multi-file transactions. Custody survives
    # this partial apply; explicit owner reconciliation is required, not retry.
    assert sha((root / REL).read_bytes()) == AFTER
    assert list((root / 'reports/ops/pin-archive/glp330-discovery').rglob(BEFORE + '.bytes'))


def test_coherent_six_file_subset_uses_ordinary_scoped_sync(repo, inputs):
    from scripts import discovery_predecessor_migration as migration
    root, review, authority, _ = inputs
    selected = sorted(migration.COHORT)
    before_manifest = (root / syncer.MANIFEST).read_bytes()
    doc = json.loads(review.read_text())
    doc['proposal']['selected'] = selected
    doc['proposal']['installed_before'].update({p: {'sha256': None, 'mode': None}
                                              for p in selected if p != REL})
    dump(review, doc)
    auth = json.loads(authority.read_text())
    auth.update(proposal=doc['proposal'], review_sha256=sha(review.read_bytes()))
    dump(authority, auth)
    assert syncer.sync(root, repo, True, only=selected,
                       discovery_review=review, discovery_authority=authority) == 0
    for rel in selected:
        assert (root / rel).read_bytes() == (repo / rel).read_bytes()
    state = json.loads((root / syncer.STATE).read_text())
    assert all(state['hashes'][p] == sha((repo / p).read_bytes()) for p in selected)
    assert state['hashes']['scripts/unselected.py'] == 'a' * 64
    assert (root / syncer.MANIFEST).read_bytes() == before_manifest
    assert syncer.sync(root, repo, False, only=selected) == 0


def test_cli_uses_same_guarded_migration(repo, inputs):
    root, review, authority, _ = inputs
    import sys
    run = subprocess.run([sys.executable, '-B', str(repo / 'scripts/sync_from_repo.py'),
                          '--checkout', str(repo), '--root', str(root), '--only', REL,
                          '--discovery-predecessor-review', str(review),
                          '--discovery-adoption-authority', str(authority), '--dry-run'],
                         capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    assert 'DRY_RUN diffs=1' in run.stdout
    assert sha((root / REL).read_bytes()) == BEFORE
