"""Reviewed live preimages are explicit custody, not invented Git history."""
import hashlib
import json
import subprocess

import pytest

from scripts import sync_from_repo


def test_reviewed_preimage_enrollment(tmp_path):
    repo = tmp_path / 'repo'
    root = tmp_path / 'live'
    (repo / 'scripts').mkdir(parents=True)
    (repo / 'ops').mkdir()
    (root / 'scripts').mkdir(parents=True)
    rel = 'scripts/caller.py'
    target = b'reviewed replacement\n'
    live = b'reviewed legacy transport\n'
    (repo / rel).write_bytes(target)
    manifest = {'schema': 'GLP_BUSINESS_SOURCE_V1', 'tracked_count': 2,
                'files': {rel: {'sha256': hashlib.sha256(target).hexdigest()}}}
    (repo / sync_from_repo.MANIFEST).write_text(json.dumps(manifest))
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
    (root / rel).write_bytes(live)
    reviewed = [rel + '=' + hashlib.sha256(live).hexdigest()]
    assert sync_from_repo.sync(root, repo, False, only=[rel]) == 2
    assert sync_from_repo.sync(root, repo, False, only=[rel],
                               reviewed_preimage=reviewed) == 0
    assert not (root / sync_from_repo.STATE).exists()
    (root / rel).write_bytes(b'intervening logic\n')
    assert sync_from_repo.sync(root, repo, True, only=[rel],
                               reviewed_preimage=reviewed) == 2
    assert (root / rel).read_bytes() == b'intervening logic\n'
    assert not (root / sync_from_repo.STATE).exists()
    (root / rel).write_bytes(live)
    for invalid in ([reviewed[0], reviewed[0]], [rel + '=bad'],
                    ['scripts/unselected.py=' + '0' * 64]):
        with pytest.raises(ValueError, match='reviewed preimage'):
            sync_from_repo.sync(root, repo, False, only=[rel],
                                reviewed_preimage=invalid)
    with pytest.raises(ValueError, match='reviewed preimage'):
        sync_from_repo.sync(root, repo, False, reviewed_preimage=reviewed)
    assert sync_from_repo.sync(root, repo, True, only=[rel],
                               reviewed_preimage=reviewed) == 0
    assert (root / rel).read_bytes() == target
    assert json.loads((root / sync_from_repo.STATE).read_text())['hashes'][rel] == manifest['files'][rel]['sha256']
    with pytest.raises(ValueError, match='reviewed preimage'):
        sync_from_repo.sync(root, repo, False, only=[rel],
                            reviewed_preimage=reviewed)
    assert sync_from_repo.sync(root, repo, False, only=[rel]) == 0
