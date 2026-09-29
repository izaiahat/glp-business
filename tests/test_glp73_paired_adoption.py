"""Fixed GLP73 cohort; isolated business copy and native scheduler saver only."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys

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
    for rel in (*pair.PATHS, *pair.RETAINED, syncer.STATE):
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(LIVE / rel, target)
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
    return root, home, registry, jobs, scheduler


def receipt(root, home, registry, jobs):
    before, stages, sources, records = pair.plan(REPO, root, home)
    return {'schema': 'glp73-paired-adoption-review-v1', 'decision': 'ACCEPT_EXACT_BYTES',
            'source': pair.SOURCE, 'reviewer': 'isolated fixture only',
            'source_sha256': sources,
            'wrapper_sha256': {p.name: sha(b) for p, b in stages.items()},
            'source_before_sha256': {p: pair.SOURCE_BEFORE[i][0] for i, p in enumerate(pair.PATHS)},
            'state_before_sha256': pair.STATE_BEFORE, 'job_before': records,
            'registry_before_sha256': sha(before[registry][0])}


def test_exact_cohort_native_saver_and_hash_only(tmp_path, monkeypatch):
    root, home, registry, old_jobs, scheduler = cohort(tmp_path, monkeypatch)
    review = receipt(root, home, registry, old_jobs)
    original = scheduler._save_jobs_unlocked
    calls = []
    def observed_save(jobs):
        calls.append(copy.deepcopy(jobs))
        return original(jobs)
    monkeypatch.setattr(scheduler, '_save_jobs_unlocked', observed_save)
    result = pair.adopt(REPO, root, home, review, scheduler=scheduler)
    assert result['applied'] is True and len(calls) == 1
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
    assert syncer.sync(root, REPO, False, only=pair.PATHS) == 0


@pytest.mark.parametrize('drift', ['source_mode', 'wrapper', 'record', 'run_claim', 'fire_claim'])
def test_preimage_and_claim_refuse_before_first_write(tmp_path, monkeypatch, drift):
    root, home, registry, old_jobs, scheduler = cohort(tmp_path, monkeypatch)
    review = receipt(root, home, registry, old_jobs)
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
        pair.adopt(REPO, root, home, review, scheduler=scheduler)
    assert calls == [] and (root / syncer.STATE).read_bytes() == before_state
    assert registry.read_bytes() == registry_before
    assert {p: (root / p).read_bytes() for p in pair.PATHS} == before_source
    if drift != 'wrapper':
        assert {name: (home / 'scripts' / name).read_bytes() for _, name, *_ in pair.JOBS} == before_wrapper
    assert not (root / 'reports').exists()
