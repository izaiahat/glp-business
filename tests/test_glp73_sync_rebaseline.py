"""Runtime, receipt-bound GLP-73 baseline proposals; all state is disposable."""
import copy
import json

import pytest

import glp73_sync_rebaseline as baseline


def state(hashes):
    return (json.dumps({'schema': 1, 'hashes': hashes}, sort_keys=True) + '\n').encode()


def fixture(tmp_path, monkeypatch):
    hashes = {'arbitrary/new-reader.py': baseline.sha(b'old'), 'unchanged.py': baseline.sha(b'same')}
    before = state(hashes)
    monkeypatch.setattr(baseline, 'BEFORE', baseline.sha(before))
    target = tmp_path / baseline.SNAPSHOT
    target.parent.mkdir(parents=True)
    target.write_bytes(before)
    target = tmp_path / 'ops/.rebuild-sync-state.json'
    target.parent.mkdir(exist_ok=True)
    target.write_bytes(before)
    return hashes, []


def move(root, hashes, receipts, path, body):
    before = hashes.get(path)
    after = baseline.sha(body) if body is not None else None
    receipt = {'path': path, 'before_sha256': before, 'after_sha256': after,
               'authority': 'Fixture owner authorized this scoped sync.'}
    rel = f'receipts/sync-{len(receipts)}.json'
    target = root / rel
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(receipt))
    receipt_hash = baseline.sha(target.read_bytes())
    receipts.append({'path': rel, 'sha256': receipt_hash,
                     'authorization': {'path': rel, 'sha256': receipt_hash, 'pointer': '/authority'},
                     'changes': [{'path': path, 'before_sha256': before, 'after_sha256': after,
                                  'before_pointer': '/before_sha256', 'after_pointer': '/after_sha256'}]})
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    if body is None:
        target.unlink(missing_ok=True)
        hashes.pop(path)
    else:
        target.write_bytes(body)
        hashes[path] = after
    (root / 'ops/.rebuild-sync-state.json').write_bytes(state(hashes))


def review(root, receipts):
    return {'schema': 'glp73-sync-rebaseline-review-v1', 'decision': 'ACCEPT_EXPLAINED_DIFF',
            'prepared_by': 'preparer', 'reviewer': 'independent-reviewer',
            'proposal': baseline.proposal(root, receipts)}


def test_runtime_arbitrary_paths_and_repeated_receipt_chain(tmp_path, monkeypatch):
    hashes, receipts = fixture(tmp_path, monkeypatch)
    move(tmp_path, hashes, receipts, 'arbitrary/new-reader.py', b'intermediate')
    move(tmp_path, hashes, receipts, 'arbitrary/new-reader.py', b'current')
    move(tmp_path, hashes, receipts, 'other/added.py', b'added')
    result = review(tmp_path, receipts)
    assert [row['path'] for row in result['proposal']['diff']] == ['arbitrary/new-reader.py', 'other/added.py']
    assert len(result['proposal']['diff'][0]['evidence']) == 2
    assert baseline.validate(tmp_path, result) == baseline.sha(state(hashes))


def test_unexplained_path_and_receipt_chain_gap_refuse(tmp_path, monkeypatch):
    hashes, receipts = fixture(tmp_path, monkeypatch)
    move(tmp_path, hashes, receipts, 'arbitrary/new-reader.py', b'intermediate')
    move(tmp_path, hashes, receipts, 'arbitrary/new-reader.py', b'current')
    with pytest.raises(RuntimeError, match='unexplained_sync_state'):
        baseline.proposal(tmp_path, receipts[1:])
    move(tmp_path, hashes, receipts, 'other/added.py', b'added')
    with pytest.raises(RuntimeError, match='unexplained_sync_state'):
        baseline.proposal(tmp_path, receipts[:2])


@pytest.mark.parametrize('change', ['receipt', 'authority', 'installed', 'state', 'snapshot'])
def test_review_rechecks_all_inputs(tmp_path, monkeypatch, change):
    hashes, receipts = fixture(tmp_path, monkeypatch)
    move(tmp_path, hashes, receipts, 'arbitrary/new-reader.py', b'current')
    authority = tmp_path / 'authority.json'
    authority.write_text('{"approval":"scoped owner authority"}')
    receipts[0]['authorization'] = {'path': 'authority.json', 'sha256': baseline.sha(authority.read_bytes()), 'pointer': '/approval'}
    accepted = review(tmp_path, receipts)
    target = {'receipt': receipts[0]['path'], 'authority': 'authority.json',
              'installed': 'arbitrary/new-reader.py', 'state': 'ops/.rebuild-sync-state.json',
              'snapshot': baseline.SNAPSHOT}[change]
    (tmp_path / target).write_bytes(b'{}')
    with pytest.raises(RuntimeError):
        baseline.validate(tmp_path, accepted)


def test_receipt_must_bind_transition_and_authority(tmp_path, monkeypatch):
    hashes, receipts = fixture(tmp_path, monkeypatch)
    move(tmp_path, hashes, receipts, 'arbitrary/new-reader.py', b'current')
    for key, value in [('after_pointer', '/before_sha256'), ('before_pointer', '/missing')]:
        changed = copy.deepcopy(receipts)
        changed[0]['changes'][0][key] = value
        with pytest.raises(RuntimeError, match='receipt'):
            baseline.proposal(tmp_path, changed)
    changed = copy.deepcopy(receipts)
    changed[0]['authorization']['pointer'] = '/missing'
    with pytest.raises(RuntimeError, match='receipt'):
        baseline.proposal(tmp_path, changed)


def test_review_independence_exactness_and_no_implicit_receipts(tmp_path, monkeypatch):
    hashes, receipts = fixture(tmp_path, monkeypatch)
    move(tmp_path, hashes, receipts, 'arbitrary/new-reader.py', b'current')
    accepted = review(tmp_path, receipts)
    for key, value in [('reviewer', 'preparer'), ('decision', 'ACCEPT_EXACT_BYTES'), ('proposal', {})]:
        changed = copy.deepcopy(accepted)
        changed[key] = value
        with pytest.raises(RuntimeError, match='independent_explained_diff_review_required'):
            baseline.validate(tmp_path, changed)
    with pytest.raises(RuntimeError, match='sync_receipts_required'):
        baseline.proposal(tmp_path)


def test_receipt_path_escape_symlink_and_duplicate_refuse(tmp_path, monkeypatch):
    hashes, receipts = fixture(tmp_path, monkeypatch)
    move(tmp_path, hashes, receipts, 'arbitrary/new-reader.py', b'current')
    with pytest.raises(RuntimeError, match='duplicate_sync_receipt'):
        baseline.proposal(tmp_path, receipts + receipts)
    changed = copy.deepcopy(receipts)
    changed[0]['path'] = '../escape.json'
    with pytest.raises(RuntimeError, match='unsafe_rebaseline_path'):
        baseline.proposal(tmp_path, changed)
    original = tmp_path / receipts[0]['path']
    original.rename(original.with_suffix('.saved'))
    original.symlink_to(original.with_suffix('.saved'))
    with pytest.raises(RuntimeError):
        baseline.proposal(tmp_path, receipts)


def test_deleted_path_requires_receipt_and_absence(tmp_path, monkeypatch):
    hashes, receipts = fixture(tmp_path, monkeypatch)
    move(tmp_path, hashes, receipts, 'arbitrary/new-reader.py', None)
    accepted = review(tmp_path, receipts)
    assert baseline.validate(tmp_path, accepted)
    (tmp_path / 'arbitrary/new-reader.py').write_bytes(b'lingering')
    with pytest.raises(RuntimeError, match='installed_source_moved'):
        baseline.validate(tmp_path, accepted)


def test_unused_receipts_and_structural_state_movement_refuse(tmp_path, monkeypatch):
    hashes, receipts = fixture(tmp_path, monkeypatch)
    move(tmp_path, hashes, receipts, 'arbitrary/new-reader.py', b'current')
    move(tmp_path, hashes, receipts, 'arbitrary/new-reader.py', b'old')
    with pytest.raises(RuntimeError, match='explained_sync_state_diff_incomplete'):
        baseline.proposal(tmp_path, receipts)
    (tmp_path / 'ops/.rebuild-sync-state.json').write_text(json.dumps({'schema': 1, 'hashes': hashes, 'unknown': True}))
    with pytest.raises(RuntimeError, match='unexplained_sync_state_structure'):
        baseline.proposal(tmp_path, receipts)


def test_exact_generated_proposal_cannot_be_edited_or_reused_after_new_movement(tmp_path, monkeypatch):
    hashes, receipts = fixture(tmp_path, monkeypatch)
    move(tmp_path, hashes, receipts, 'arbitrary/new-reader.py', b'current')
    accepted = review(tmp_path, receipts)
    changed = copy.deepcopy(accepted)
    changed['proposal']['new_state_sha256'] = baseline.sha(b'invented')
    with pytest.raises(RuntimeError, match='independent_explained_diff_review_required'):
        baseline.validate(tmp_path, changed)
    move(tmp_path, hashes, receipts, 'arbitrary/new-reader.py', b'newer')
    with pytest.raises(RuntimeError, match='unexplained_sync_state_diff'):
        baseline.validate(tmp_path, accepted)
    # A newly generated proposal needs fresh independent acceptance.
    assert review(tmp_path, receipts)['proposal'] != accepted['proposal']


def test_cli_requires_receipt_list_and_only_prints_proposal(tmp_path, monkeypatch, capsys):
    hashes, receipts = fixture(tmp_path, monkeypatch)
    move(tmp_path, hashes, receipts, 'arbitrary/new-reader.py', b'current')
    listing = tmp_path / 'supplied-receipts.json'
    listing.write_text(json.dumps(receipts))
    before = {str(path): (path.read_bytes(), path.stat().st_mtime_ns) for path in tmp_path.rglob('*') if path.is_file()}
    monkeypatch.setattr('sys.argv', ['glp73_sync_rebaseline.py', '--root', str(tmp_path), '--receipts', str(listing)])
    assert baseline.main() == 0
    assert json.loads(capsys.readouterr().out) == baseline.proposal(tmp_path, receipts)
    assert before == {str(path): (path.read_bytes(), path.stat().st_mtime_ns) for path in tmp_path.rglob('*') if path.is_file()}
    monkeypatch.setattr('sys.argv', ['glp73_sync_rebaseline.py', '--root', str(tmp_path)])
    with pytest.raises(SystemExit) as exc:
        baseline.main()
    assert exc.value.code == 2
