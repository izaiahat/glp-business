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


def test_glp44_committed_outcome_reconstruction_never_replays_save(tmp_path):
    """Actual same-pin commit refusal, sealed cohort, independent forward recovery."""
    import glp44_commit_recovery as recovery
    import glp44_scoped_adoption as pair
    import maintenance_window as mw
    from contextlib import contextmanager, nullcontext
    from glp_ship_registry import REPORTING_PATHS
    root, hermes = tmp_path / 'business', tmp_path / 'hermes'
    root.mkdir(); (hermes / 'cron').mkdir(parents=True)
    paths = [root / p for p in REPORTING_PATHS] + [root / 'ops/.rebuild-sync-state.json',
                                                 hermes / 'scripts' / pair.WRAPPER]
    for p in paths:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b'old fixture'); p.chmod(0o640)
    original = {p: (p.read_bytes(), 0o640) for p in paths}
    stage = {p: (b'old fixture' if p.name == pair.WRAPPER else b'candidate fixture') for p in paths}
    registry = hermes / 'cron/jobs.json'
    job = {'id': pair.JOB, 'enabled': False, 'state': 'paused', 'script': pair.WRAPPER,
           'script_sha256': cohort.digest(stage[paths[-1]]), 'fire_claim': None, 'run_claim': None}
    registry.write_text(json.dumps({'jobs': [job]}))
    journal = tmp_path / 'window.json'
    class Scheduler:
        locked = False
        _fire_job_lock = lambda self, jid: nullcontext(True)
        @contextmanager
        def _jobs_lock(self, *, require_cross_process):
            assert require_cross_process
            self.locked = True
            try: yield
            finally: self.locked = False
        def _current_cron_store(self):
            from types import SimpleNamespace
            return SimpleNamespace(jobs_file=registry)
        def _jobs_lock_file(self): return registry.parent / '.jobs.lock'
        def _save_jobs_unlocked(self, *args):
            pytest.fail('reconciliation must never replay native saver')
    scheduler = Scheduler()
    class Backend:
        def __init__(self): self.root, self.hermes = root, hermes
        def scheduler(self): return scheduler
        def job(self, jid): return json.loads(registry.read_text())['jobs'][0]
        def paused(self, *args): return True
        def inflight(self, entries):
            assert scheduler.locked
            return []
    window = mw.MaintenanceWindow(Backend(), journal, [tmp_path / 'window.lock'])
    data = {'schema': SCHEMA, 'state': 'recovery_required', 'root': str(root), 'hermes': str(hermes),
            'journal': str(journal), 'entries': [{'entry': {'kind': 'hermes', 'id': pair.JOB},
                'prior': {}, 'intent': True, 'paused': True, 'restore_intent': False, 'restored': False}],
            'source_transaction': {'before': {str(p): cohort.digest(v[0]) for p, v in original.items()},
                'after': {str(p): cohort.digest(v) for p, v in stage.items()}, 'outcome': 'uncertain',
                'job_ids': [pair.JOB]}, 'job_updates': {pair.JOB: {
                    'before_sha256': mw.json_sha(job), 'after_sha256': mw.json_sha(job),
                    'before_pin': job['script_sha256'], 'after_pin': job['script_sha256']}}}
    mw.atomic_json(journal, data)
    def failed_commit(): raise RuntimeError('Cron jobs cross-process lock required')
    with pytest.raises(RuntimeError, match='cross-process'):
        cohort.switch_files(journal, stage, original, lambda: [], commit=failed_commit)
    before = journal.read_bytes(), registry.read_bytes(), [p.stat() for p in paths]
    proposal = recovery.proposal(window)
    assert (journal.read_bytes(), registry.read_bytes(), [p.stat() for p in paths]) == before
    review = {'schema': recovery.REVIEW_SCHEMA, 'decision': 'ACCEPT_EXACT_LANDED_JOB_AND_CANDIDATE',
              'prepared_by': 'fixture author', 'reviewer': 'independent fixture reviewer', 'proposal': proposal}
    with pytest.raises(RuntimeError, match='review'):
        recovery.recover(window, dict(review, reviewer='fixture author'))
    assert (journal.read_bytes(), registry.read_bytes(), [p.stat() for p in paths]) == before
    # Full-record drift must refuse even though pin remains identical.
    changed = dict(job, last_status='foreign')
    registry.write_text(json.dumps({'jobs': [changed]}))
    with pytest.raises(RuntimeError, match='job'):
        recovery.recover(window, review)
    registry.write_bytes(before[1])
    try:
        result = recovery.recover(window, review)
        assert result['native_save_replayed'] is False
        assert registry.read_bytes() == before[1]
        assert {p: p.read_bytes() for p in paths} == stage
        assert all(stat.S_IMODE(p.stat().st_mode) == 0o640 for p in paths)
        data = mw.read_journal(journal)
        assert data['source_switch']['commit_started'] is True
        assert data['source_switch']['commit_verified'] is True
        assert data['source_switch']['state'] == 'published'
        assert data['source_transaction']['outcome'] == 'applied'
        assert mw.locks_verified(data)  # custody continues until owner's exit
    finally:
        window.release_locks()
