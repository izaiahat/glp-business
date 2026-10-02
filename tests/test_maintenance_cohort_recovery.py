"""Recover real sealed scratch cohorts without reopening any mixed source set."""
import copy
import json
import os
from pathlib import Path
import stat

import pytest

import maintenance_cohort as cohort
from maintenance_window import SCHEMA


def crashed(tmp_path, monkeypatch, *, caches=False):
    paths = [tmp_path / 'first.py', tmp_path / 'second.py']
    for path in paths:
        path.write_bytes(b'old ' + path.name.encode())
        path.chmod(0o640)
    if caches:
        cache = paths[0].with_suffix('.pyc')
        cache.write_bytes(b'old bytecode')
    original = {path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode)) for path in paths}
    stage = {path: b'new ' + path.name.encode() for path in paths}
    journal = tmp_path / 'window.json'
    journal.write_text(json.dumps({'schema': SCHEMA, 'source_transaction': {'outcome': 'uncertain', 'pins': 'unchanged'}}))
    replace = os.replace
    def power_loss(source, target):
        replace(source, target)
        if Path(target) == paths[0]:
            raise KeyboardInterrupt('fixture power loss after first sealed rename')
    with monkeypatch.context() as patch:
        patch.setattr(cohort.os, 'replace', power_loss)
        with pytest.raises(KeyboardInterrupt):
            cohort.switch_files(journal, stage, original, lambda: [])
    return journal, paths, original


def review(journal):
    return {'schema': 'glp-maintenance-cohort-recovery-review-v1',
            'decision': 'RESTORE_EXACT_COHORT_PREIMAGE', 'prepared_by': 'fixture author',
            'reviewer': 'independent fixture reviewer', 'proposal': cohort.recovery_proposal(journal)}


def test_read_only_proposal_and_reviewed_sealed_recovery(tmp_path, monkeypatch):
    journal, paths, original = crashed(tmp_path, monkeypatch)
    before = {path: (path.stat().st_mtime_ns, stat.S_IMODE(path.stat().st_mode)) for path in paths}
    accepted = review(journal)
    assert before == {path: (path.stat().st_mtime_ns, stat.S_IMODE(path.stat().st_mode)) for path in paths}
    assert all(mode == 0 for _, mode in before.values())
    transaction = json.loads(journal.read_text())['source_transaction']
    chmod = os.fchmod
    def checked_unseal(fd, mode):
        if mode:
            # Every public member must already be replaced with old bytes before
            # even the first public descriptor regains read permission.
            target = os.readlink(f'/proc/self/fd/{fd}')
            if target in {str(path) for path in paths}:
                data = json.loads(journal.read_text())['source_switch']
                assert all(cohort.metadata(path.stat()) == data['files'][str(path)]['recovery_metadata'] for path in paths)
        return chmod(fd, mode)
    monkeypatch.setattr(cohort.os, 'fchmod', checked_unseal)
    result = cohort.recover(journal, accepted, lambda: [])
    assert result['state'] == 'rolled_back'
    assert {path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode)) for path in paths} == original
    assert json.loads(journal.read_text())['source_transaction'] == transaction


@pytest.mark.parametrize('movement', ['journal', 'inode', 'mode', 'size', 'custody'])
def test_recovery_refuses_changed_review_inputs_without_unsealing(tmp_path, monkeypatch, movement):
    journal, paths, original = crashed(tmp_path, monkeypatch)
    accepted = review(journal)
    if movement == 'journal':
        data = json.loads(journal.read_text())
        data['source_transaction']['outcome'] = 'foreign'
        journal.write_text(json.dumps(data))
    elif movement == 'inode':
        replacement = tmp_path / 'foreign'
        replacement.write_bytes(b'foreign')
        replacement.chmod(0)
        os.replace(replacement, paths[0])
    elif movement == 'mode':
        paths[0].chmod(0o600)
    elif movement == 'size':
        paths[0].chmod(0o600)
        paths[0].write_bytes(b'changed bytes')
        paths[0].chmod(0)
    else:
        data = json.loads(journal.read_text())
        Path(data['source_switch']['files'][str(paths[0])]['original']).write_bytes(b'forged')
    with pytest.raises(RuntimeError):
        cohort.recover(journal, accepted, lambda: [])
    assert stat.S_IMODE(paths[1].stat().st_mode) == 0


@pytest.mark.parametrize('key,value', [('reviewer', 'fixture author'), ('decision', 'ACCEPT'), ('proposal', {})])
def test_recovery_requires_distinct_exact_review(tmp_path, monkeypatch, key, value):
    journal, paths, original = crashed(tmp_path, monkeypatch)
    accepted = review(journal)
    accepted[key] = value
    with pytest.raises(RuntimeError, match='review'):
        cohort.recover(journal, accepted, lambda: [])
    assert all(stat.S_IMODE(path.stat().st_mode) == 0 for path in paths)


def test_recovery_refuses_readers_and_can_recover_its_own_interrupted_switch(tmp_path, monkeypatch):
    journal, paths, original = crashed(tmp_path, monkeypatch)
    with pytest.raises(RuntimeError, match='reader'):
        cohort.recover(journal, review(journal), lambda: [{'pid': 123}])
    replace = os.replace
    def power_loss(source, target):
        replace(source, target)
        if Path(target) == paths[0]:
            raise KeyboardInterrupt('second power loss')
    with monkeypatch.context() as patch:
        patch.setattr(cohort.os, 'replace', power_loss)
        with pytest.raises(KeyboardInterrupt):
            cohort.recover(journal, review(journal), lambda: [])
    assert all(stat.S_IMODE(path.stat().st_mode) == 0 for path in paths)
    cohort.recover(journal, review(journal), lambda: [])
    assert {path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode)) for path in paths} == original


def test_recovery_restores_recorded_removed_bytecode_cache(tmp_path, monkeypatch):
    journal, paths, original = crashed(tmp_path, monkeypatch, caches=True)
    paths[0].with_suffix('.pyc').unlink()
    cohort.recover(journal, review(journal), lambda: [])
    assert paths[0].with_suffix('.pyc').read_bytes() == b'old bytecode'


def test_recovery_accepts_reacquired_lock_identity_but_binds_scheduler_entries(tmp_path, monkeypatch):
    journal, paths, original = crashed(tmp_path, monkeypatch)
    accepted = review(journal)
    data = json.loads(journal.read_text())
    for key in ('keeper', 'locks', 'controller', 'adopter', 'exit_requested'):
        data[key] = {'replacement': True}
    journal.write_text(json.dumps(data))
    assert cohort.recovery_proposal(journal) == accepted['proposal']
    data['entries'] = [{'id': 'foreign-scheduler-custody'}]
    journal.write_text(json.dumps(data))
    with pytest.raises(RuntimeError, match='review_moved'):
        cohort.recover(journal, accepted, lambda: [])
    del data['entries']
    journal.write_text(json.dumps(data))
    cohort.recover(journal, accepted, lambda: [])
    assert {path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode)) for path in paths} == original
