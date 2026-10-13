"""Fixed GLP73 cohort; isolated business copy and native scheduler saver only."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import subprocess
from contextlib import nullcontext

import pytest

from scripts import glp73_paired_adoption as pair
from scripts import sync_from_repo as syncer

REPO = Path(__file__).resolve().parents[1]
LIVE = Path('/home/ubuntu/business')


def sha(body):
    return hashlib.sha256(body).hexdigest()


def cohort(tmp_path, monkeypatch):
    root, home = tmp_path / 'business', tmp_path / 'hermes'
    for p in (root, home / 'scripts', home / 'cron'):
        p.mkdir(parents=True)
    archive = LIVE / 'reports/ops/pin-archive/glp73/c890cf1ddec28cef655e5f8c7e5387e94714b0611c96a3ee06f93821e31a05ea'
    before = dict(zip(pair.PATHS, pair.SOURCE_BEFORE))
    for rel in (*pair.PATHS, *pair.RETAINED):
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        digest, mode = before.get(rel, (pair.RETAINED.get(rel), 0o644))
        saved = archive / (digest + '.bytes')
        body = (saved if saved.is_file() else LIVE / rel).read_bytes()
        assert sha(body) == digest, 'historical preimage custody moved: ' + rel
        target.write_bytes(body)
        target.chmod(mode)
    # Authenticate the retained old precondition; copying today's shared live
    # state silently broke this test after unrelated authorized scoped syncs.
    old_state = LIVE / 'reports/ops/glp44-scoped-live-adoption-20260929/read-only-preimages/05.bytes'
    assert sha(old_state.read_bytes()) == pair.STATE_BEFORE
    shutil.copy2(old_state, root / syncer.STATE)
    # Commit the current producer in an isolated checkout so its real committed-
    # HEAD guard remains exercised even when this working tree has pending edits.
    checkout = tmp_path / 'checkout'
    subprocess.run(['git', 'clone', '--quiet', '--shared', str(REPO), str(checkout)], check=True)
    rel = 'scripts/glp73_paired_adoption.py'
    shutil.copy2(REPO / rel, checkout / rel)
    manifest = checkout / syncer.MANIFEST
    document = json.loads(manifest.read_text())
    for historical in pair.PATHS:
        body = subprocess.check_output(['git', '-C', str(REPO), 'show', pair.SOURCE + ':' + historical])
        (checkout / historical).write_bytes(body)
        document['files'][historical]['sha256'] = sha(body)
    document['files'][rel]['sha256'] = sha((checkout / rel).read_bytes())
    manifest.write_text(json.dumps(document, indent=2, sort_keys=True) + '\n')
    subprocess.run(['git', '-C', str(checkout), 'add', rel, syncer.MANIFEST, *pair.PATHS], check=True)
    subprocess.run(['git', '-C', str(checkout), '-c', 'user.name=Fixture',
                    '-c', 'user.email=fixture@example.invalid', 'commit', '--allow-empty', '-qm',
                    'fixture producer'], check=True)
    for jid, name, old, new, mode in pair.JOBS:
        target = home / 'scripts' / name
        target.write_bytes((REPO / pair.SNAPSHOTS / name).read_bytes())
        target.chmod(mode)
    jobs = [dict(id=jid, script=name, script_sha256=old, enabled=False,
                 state='paused', paused_reason='fixture hold', paused_at='2026-09-29T00:00:00Z',
                 schedule=f'{i} 6 * * 1', next_run_at='2026-10-05T06:00:00Z',
                 run_claim=None, fire_claim=None, running=None)
            for i, (jid, name, old, new, mode) in enumerate(pair.JOBS)]
    jobs.append(dict(id='other', enabled=True, script_sha256='other', next_run_at='untouched'))
    registry = home / 'cron/jobs.json'
    registry.write_text(json.dumps({'jobs': jobs}))
    # Select the fixture home BEFORE importing the actual installed scheduler.
    monkeypatch.setenv('HERMES_HOME', str(home))
    sys.path.insert(0, '/home/ubuntu/.hermes/hermes-agent')
    from cron import jobs as scheduler
    assert scheduler._current_cron_store().jobs_file == registry
    class Window:
        def __init__(self):
            self.file_updates = []
            self.job_updates = []
        def accept_file_updates(self, before, after):
            assert before.keys() == after.keys()
            assert all(sha(Path(path).read_bytes()) == digest for path, digest in before.items())
            self.file_updates.append((before, after))
        def switch_files(self, stage, before):
            from maintenance_cohort import switch_files
            journal = root / 'window.json'
            if not journal.exists():
                journal.write_text(json.dumps({'schema': 'glp44-glp73-maintenance-window-v1'}))
            receipt = switch_files(journal, stage, before, lambda: [])
            assert receipt['state'] in {'published', 'rolled_back'}
            return receipt
        def accept_job_update(self, jid, before, after):
            actual = next(j for j in json.loads(registry.read_text())['jobs'] if j['id'] == jid)
            assert actual == before
            assert {k: v for k, v in before.items() if k != 'script_sha256'} == {
                k: v for k, v in after.items() if k != 'script_sha256'}
            self.job_updates.append((jid, before, after))
    window = Window()
    def require_active(journal, business, hermes):
        assert journal == root / 'window.json' and business == root and hermes == home
        return nullcontext(window)
    import maintenance_window
    monkeypatch.setattr(maintenance_window, 'require_active_window', require_active)
    return root, home, registry, jobs, scheduler, checkout, window


def receipt(root, home, registry, jobs, checkout):
    before, stages, sources, records = pair.plan(checkout, root, home)
    return {'schema': 'glp73-paired-adoption-review-v1', 'decision': 'ACCEPT_EXACT_BYTES',
            'source': pair.SOURCE, 'reviewer': 'isolated fixture only',
            'source_sha256': sources,
            'wrapper_sha256': {p.name: sha(b) for p, b in stages.items()},
            'source_before_sha256': {p: pair.SOURCE_BEFORE[i][0] for i, p in enumerate(pair.PATHS)},
            'state_before_sha256': pair.STATE_BEFORE, 'state_rebaseline_sha256': None, 'job_before': records,
            'registry_before_sha256': sha(before[registry][0])}


def test_exact_cohort_native_saver_and_hash_only(tmp_path, monkeypatch):
    root, home, registry, old_jobs, scheduler, checkout, window = cohort(tmp_path, monkeypatch)
    review = receipt(root, home, registry, old_jobs, checkout)
    original = scheduler._save_jobs_unlocked
    calls = []
    def observed_save(jobs):
        calls.append(copy.deepcopy(jobs))
        return original(jobs)
    monkeypatch.setattr(scheduler, '_save_jobs_unlocked', observed_save)
    original_sync = pair.syncer.sync
    def preview_only(root, checkout, apply, *args):
        assert apply is False, 'adoption must publish through the cohort switch'
        return original_sync(root, checkout, apply, *args)
    monkeypatch.setattr(pair.syncer, 'sync', preview_only)
    result = pair.adopt(checkout, root, home, review, scheduler=scheduler, window=root / 'window.json')
    assert result['applied'] is True and len(calls) == 1
    assert len(window.file_updates) == 1 and len(window.job_updates) == 3
    for path, expected in window.file_updates[0][1].items():
        assert sha(Path(path).read_bytes()) == expected
    after = json.loads(registry.read_text())['jobs']
    assert after[3] == old_jobs[3]
    for i, (_, name, _, candidate, mode) in enumerate(pair.JOBS):
        assert {k: v for k, v in after[i].items() if k != 'script_sha256'} == {
            k: v for k, v in old_jobs[i].items() if k != 'script_sha256'}
        assert after[i]['script_sha256'] == candidate
        assert sha((home / 'scripts' / name).read_bytes()) == candidate
        assert (home / 'scripts' / name).stat().st_mode & 0o777 == mode
    for i, rel in enumerate(pair.PATHS):
        assert (root / rel).read_bytes() == (REPO / rel).read_bytes()
        assert (root / rel).stat().st_mode & 0o777 == pair.SOURCE_BEFORE[i][1]
    assert syncer.sync(root, checkout, False, only=pair.PATHS) == 0


@pytest.mark.parametrize('drift', ['source_mode', 'wrapper', 'record', 'run_claim', 'fire_claim'])
def test_preimage_and_claim_refuse_before_first_write(tmp_path, monkeypatch, drift):
    root, home, registry, old_jobs, scheduler, checkout, window = cohort(tmp_path, monkeypatch)
    review = receipt(root, home, registry, old_jobs, checkout)
    before_state = (root / syncer.STATE).read_bytes()
    before_source = {p: (root / p).read_bytes() for p in pair.PATHS}
    before_wrapper = {name: (home / 'scripts' / name).read_bytes() for _, name, *_ in pair.JOBS}
    if drift == 'source_mode':
        (root / pair.PATHS[1]).chmod(0o755)
    elif drift == 'wrapper':
        (home / 'scripts' / pair.JOBS[0][1]).write_bytes(b'moved')
    else:
        doc = json.loads(registry.read_text())
        doc['jobs'][0]['next_run_at' if drift == 'record' else drift] = 'moved'
        registry.write_text(json.dumps(doc))
    registry_before = registry.read_bytes()
    calls = []
    monkeypatch.setattr(scheduler, '_save_jobs_unlocked', lambda jobs: calls.append(jobs))
    with pytest.raises(RuntimeError, match='moved|claim|preflight'):
        pair.adopt(checkout, root, home, review, scheduler=scheduler, window=root / 'window.json')
    assert calls == [] and (root / syncer.STATE).read_bytes() == before_state
    assert registry.read_bytes() == registry_before
    assert {p: (root / p).read_bytes() for p in pair.PATHS} == before_source
    if drift != 'wrapper':
        assert {name: (home / 'scripts' / name).read_bytes() for _, name, *_ in pair.JOBS} == before_wrapper
    assert not (root / 'reports').exists()


@pytest.mark.parametrize('failure', ['source', 'native_save'])
def test_paired_adoption_restores_exact_preimages_before_scheduler_lands(tmp_path, monkeypatch, failure):
    root, home, registry, old_jobs, scheduler, checkout, window = cohort(tmp_path, monkeypatch)
    review = receipt(root, home, registry, old_jobs, checkout)
    paths = [root / rel for rel in pair.PATHS] + [root / syncer.STATE, registry]
    paths += [home / 'scripts' / name for _, name, *_ in pair.JOBS]
    before = {p: (p.read_bytes(), p.stat().st_mode & 0o777) for p in paths}
    if failure == 'source':
        original_switch = window.switch_files
        calls = []
        def fail_after_switch(stage, before):
            result = original_switch(stage, before)
            calls.append(result)
            if len(calls) == 1:
                raise RuntimeError('fixture source write landed then raised')
            return result
        monkeypatch.setattr(window, 'switch_files', fail_after_switch)
    else:
        def fail_save(jobs):
            raise RuntimeError('fixture native save refused')
        monkeypatch.setattr(scheduler, '_save_jobs_unlocked', fail_save)
    with pytest.raises(RuntimeError, match='fixture'):
        pair.adopt(checkout, root, home, review, scheduler=scheduler, window=root / 'window.json')
    assert {p: (p.read_bytes(), p.stat().st_mode & 0o777) for p in paths} == before
    assert len(window.file_updates) == 1


def test_uncertain_landed_scheduler_save_is_never_blindly_rolled_back(tmp_path, monkeypatch):
    root, home, registry, old_jobs, scheduler, checkout, window = cohort(tmp_path, monkeypatch)
    review = receipt(root, home, registry, old_jobs, checkout)
    original = scheduler._save_jobs_unlocked
    calls = []
    def landed_then_raise(jobs):
        calls.append(copy.deepcopy(jobs))
        original(jobs)
        raise RuntimeError('fixture save landed then raised')
    monkeypatch.setattr(scheduler, '_save_jobs_unlocked', landed_then_raise)
    with pytest.raises(RuntimeError, match='fixture save landed then raised'):
        pair.adopt(checkout, root, home, review, scheduler=scheduler, window=root / 'window.json')
    assert len(calls) == 1
    assert json.loads(registry.read_text())['jobs'] == calls[0]
    for _, name, _, candidate, _ in pair.JOBS:
        assert sha((home / 'scripts' / name).read_bytes()) == candidate
    assert all((root / rel).read_bytes() == (checkout / rel).read_bytes() for rel in pair.PATHS)
