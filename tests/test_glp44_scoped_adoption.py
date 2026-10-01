"""Isolated GLP-44 importer and paired scheduler regressions; never touch host jobs."""
import copy
from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
import subprocess
import threading
from types import SimpleNamespace

import pytest

from scripts import sync_from_repo as syncer
from scripts import glp44_scoped_adoption as pair


def sha(body):
    return hashlib.sha256(body).hexdigest()


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), '-c', 'user.name=Fixture',
                                    '-c', 'user.email=fixture@example.invalid', *args], text=True).strip()


def fixture(tmp_path, monkeypatch):
    repo, root, hermes = (tmp_path / name for name in ('repo', 'live', 'hermes'))
    for p in (repo, root, hermes / 'scripts', hermes / 'cron'):
        p.mkdir(parents=True)
    paths = pair.PATHS + ('scripts/unapproved.py',)
    for rel in paths:
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'old\n')
    # The reporting pin map carries an absolute production identity, but the
    # fixture resolves it beneath its isolated live root.
    report = repo / 'ops/glp/reporting-v2/source-manifest.json'
    report.write_text(json.dumps({'files': {
        '/home/ubuntu/business/systems/analytics-brain/analytics_brain.py': sha(b'old\n'),
        '/home/ubuntu/business/scripts/host-only.py': sha(b'host\n'),
    }}))
    host = root / 'scripts/host-only.py'
    host.parent.mkdir(parents=True)
    host.write_bytes(b'host\n')
    manifest = repo / syncer.MANIFEST
    def seal():
        files = {p: {'sha256': sha((repo / p).read_bytes())} for p in paths}
        manifest.write_text(json.dumps({'schema': 'GLP_BUSINESS_SOURCE_V1',
                                        'tracked_count': len(files) + 1, 'files': files}))
    seal()
    git(repo, 'init', '-q')
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'predecessor')
    assert syncer.sync(root, repo, True) == 0
    wrapper = hermes / 'scripts' / pair.WRAPPER
    wrapper.write_bytes((root / pair.SNAPSHOT).read_bytes())
    job = {'id': pair.JOB, 'script': pair.WRAPPER, 'script_sha256': sha(wrapper.read_bytes()),
           'enabled': True, 'state': 'scheduled', 'schedule': '0 4 * * 1',
           'next_run_at': '2026-10-05T04:00:00Z', 'fire_claim': None, 'run_claim': None}
    registry = hermes / 'cron/jobs.json'
    original_job = copy.deepcopy(job)
    job.update(enabled=False, state='paused')
    registry.write_text(json.dumps({'jobs': [job, {'id': 'other', 'schedule': 'daily'}]}))
    class Window:
        file_updates = []
        job_updates = []
        def original_job(self, jid):
            assert jid == pair.JOB
            return copy.deepcopy(original_job)
        def current_job(self, jid):
            return next(j for j in json.loads(registry.read_text())['jobs'] if j['id'] == jid)
        def accept_file_updates(self, before, after):
            assert before.keys() == after.keys()
            assert all(sha(Path(path).read_bytes()) == digest for path, digest in before.items())
            self.file_updates.append((before, after))
        def accept_job_update(self, jid, before, after):
            assert self.current_job(jid) == before
            assert {k: v for k, v in before.items() if k != 'script_sha256'} == {
                k: v for k, v in after.items() if k != 'script_sha256'}
            self.job_updates.append((jid, before, after))
    window = Window()
    def require_active(journal, business, home):
        assert journal == root / 'window.json' and business == root and home == hermes
        return nullcontext(window)
    import maintenance_window
    monkeypatch.setattr(maintenance_window, 'require_active_window', require_active)
    for rel in pair.PATHS:
        if rel == 'ops/glp/reporting-v2/source-manifest.json':
            report.write_text(json.dumps({'files': {
                '/home/ubuntu/business/systems/analytics-brain/analytics_brain.py': sha(b'new\n'),
                '/home/ubuntu/business/scripts/host-only.py': sha(b'host\n'),
            }}))
        else:
            (repo / rel).write_bytes(b'new\n')
    (repo / 'scripts/unapproved.py').write_bytes(b'unapproved\n')
    seal()
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'candidate')
    monkeypatch.setattr(pair, 'APPROVED', git(repo, 'rev-parse', 'HEAD'))
    class Scheduler:
        _jobs_file_lock = threading.RLock()
        @staticmethod
        def _fire_job_lock(_job_id):
            return nullcontext(True)
        def __init__(self):
            self.saves = 0
            self.window = window
        def _current_cron_store(self):
            return SimpleNamespace(jobs_file=registry)
        def _jobs_lock_file(self):
            return registry.parent / '.jobs.lock'
        def _record_load_stamp(self, _):
            pass
        def _save_jobs_unlocked(self, jobs):
            self.saves += 1
            registry.write_text(json.dumps({'jobs': jobs}))
    return repo, root, hermes, registry, Scheduler()


def test_scoped_import_excludes_unapproved_cochanges_and_retains_baselines(tmp_path, monkeypatch):
    repo, root, hermes, registry, scheduler = fixture(tmp_path, monkeypatch)
    before = json.loads((root / syncer.STATE).read_text())['hashes']
    global_manifest = (root / syncer.MANIFEST).read_bytes()
    assert syncer.sync(root, repo, False, only=pair.PATHS) == 0
    assert (root / 'scripts/unapproved.py').read_bytes() == b'old\n'
    assert syncer.sync(root, repo, True, only=pair.PATHS) == 0
    after = json.loads((root / syncer.STATE).read_text())['hashes']
    assert after['scripts/unapproved.py'] == before['scripts/unapproved.py']
    assert after[syncer.MANIFEST] == before[syncer.MANIFEST]
    assert (root / syncer.MANIFEST).read_bytes() == global_manifest
    assert (root / 'scripts/unapproved.py').read_bytes() == b'old\n'
    with pytest.raises(ValueError, match='unique curated'):
        syncer.sync(root, repo, False, only=('scripts/unapproved.py', 'scripts/unapproved.py'))


def test_paired_hash_only_adoption_and_preimage_refusal(tmp_path, monkeypatch):
    repo, root, hermes, registry, scheduler = fixture(tmp_path, monkeypatch)
    proposal = pair.plan(repo, root, hermes, window=scheduler.window)[0]
    receipt = {**copy.deepcopy(proposal), 'schema': 'glp44-scoped-adoption-review-v1',
               'approved_source': pair.APPROVED, 'decision': 'ACCEPT_EXACT_BYTES',
               'reviewer': 'independent fixture'}
    before_jobs = json.loads(registry.read_text())['jobs']
    state = (root / syncer.STATE).read_bytes()
    (root / 'systems/analytics-brain/collector.py').write_bytes(b'intervening\n')
    with pytest.raises(RuntimeError, match='scoped_source_preflight_refused|reviewed_preimage'):
        pair.adopt(repo, root, hermes, receipt, scheduler=scheduler, window=root / 'window.json')
    assert scheduler.saves == 0
    assert (root / syncer.STATE).read_bytes() == state
    assert json.loads(registry.read_text())['jobs'] == before_jobs
    (root / 'systems/analytics-brain/collector.py').write_bytes(b'old\n')
    changed = copy.deepcopy(receipt)
    changed['job_before']['next_run_at'] = 'moved'
    with pytest.raises(RuntimeError, match='reviewed_preimage'):
        pair.adopt(repo, root, hermes, changed, scheduler=scheduler, window=root / 'window.json')
    assert scheduler.saves == 0
    result = pair.adopt(repo, root, hermes, receipt, scheduler=scheduler, window=root / 'window.json')
    assert result['applied'] and scheduler.saves == 1
    assert len(scheduler.window.file_updates) == len(scheduler.window.job_updates) == 1
    for path, expected in scheduler.window.file_updates[0][1].items():
        assert sha(Path(path).read_bytes()) == expected
    jobs = json.loads(registry.read_text())['jobs']
    assert jobs[1] == before_jobs[1]
    assert {k: v for k, v in jobs[0].items() if k != 'script_sha256'} == {
        k: v for k, v in before_jobs[0].items() if k != 'script_sha256'}
    assert jobs[0]['script_sha256'] == sha((hermes / 'scripts' / pair.WRAPPER).read_bytes())
    assert (root / 'scripts/unapproved.py').read_bytes() == b'old\n'


def test_scoped_import_copies_retired_archive_without_moving_unselected_original(tmp_path):
    repo, root = tmp_path / 'repo', tmp_path / 'live'
    root.mkdir()
    live, archive = 'scripts/fastmode_revert.py', 'scripts/.retired-20260925/fastmode_revert.py'
    body = b'retire me\n'
    def commit(paths):
        files = {p: {'sha256': sha((repo / p).read_bytes())} for p in paths}
        (repo / syncer.MANIFEST).write_text(json.dumps({'schema': 'GLP_BUSINESS_SOURCE_V1',
                                                        'tracked_count': len(files) + 1, 'files': files}))
        git(repo, 'add', '-A')
        git(repo, 'commit', '-qm', 'rev')
    for d in ('scripts', 'ops'):
        (repo / d).mkdir(parents=True)
    (repo / live).write_bytes(body)
    git(repo, 'init', '-q')
    commit([live])
    assert syncer.sync(root, repo, True) == 0
    (repo / archive).parent.mkdir()
    git(repo, 'mv', live, archive)
    commit([archive])
    baseline = json.loads((root / syncer.STATE).read_text())['hashes'][live]
    assert syncer.sync(root, repo, True, only=(archive,)) == 0
    assert (root / live).read_bytes() == body
    assert (root / archive).read_bytes() == body
    assert json.loads((root / syncer.STATE).read_text())['hashes'][live] == baseline
    # Full-mode positive control: the retirement move still removes the original.
    assert syncer.sync(root, repo, True) == 0
    assert not (root / live).exists() and (root / archive).read_bytes() == body


def test_scoped_adoption_rolls_back_files_when_native_save_did_not_land(tmp_path, monkeypatch):
    repo, root, hermes, registry, scheduler = fixture(tmp_path, monkeypatch)
    proposal = pair.plan(repo, root, hermes, window=scheduler.window)[0]
    receipt = {**proposal, 'schema': 'glp44-scoped-adoption-review-v1',
               'approved_source': pair.APPROVED, 'decision': 'ACCEPT_EXACT_BYTES',
               'reviewer': 'independent fixture'}
    paths = [root / p for p in pair.PATHS] + [root / syncer.STATE, hermes / 'scripts' / pair.WRAPPER, registry]
    before = {p: (p.read_bytes(), p.stat().st_mode & 0o777) for p in paths}
    def fail_save(jobs):
        raise RuntimeError('fixture native save refused')
    monkeypatch.setattr(scheduler, '_save_jobs_unlocked', fail_save)
    with pytest.raises(RuntimeError, match='fixture native save refused'):
        pair.adopt(repo, root, hermes, receipt, scheduler=scheduler, window=root / 'window.json')
    assert {p: (p.read_bytes(), p.stat().st_mode & 0o777) for p in paths} == before
    assert len(scheduler.window.file_updates) == len(scheduler.window.job_updates) == 1
