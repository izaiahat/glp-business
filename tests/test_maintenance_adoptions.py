"""Offline adoption gates and exact sync-state re-baseline evidence."""
import copy
from contextlib import contextmanager
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

import glp44_scoped_adoption as glp44
import glp73_paired_adoption as glp73
import glp73_sync_rebaseline as baseline


@pytest.mark.parametrize('adoption', [glp44, glp73])
def test_adoption_refuses_without_window_before_preflight(adoption, tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('preflight must not run before verified maintenance window')
    monkeypatch.setattr(adoption, 'plan', forbidden)
    with pytest.raises(RuntimeError, match='active_maintenance_window_required'):
        adoption.adopt(tmp_path, tmp_path, tmp_path, {}, scheduler=object())


@pytest.mark.parametrize('adoption', [glp44, glp73])
def test_adoption_propagates_failed_window_verification(adoption, tmp_path, monkeypatch):
    @contextmanager
    def refused(*args):
        raise RuntimeError('keeper_dead')
        yield
    monkeypatch.setitem(sys.modules, 'maintenance_window', SimpleNamespace(require_active_window=refused))
    monkeypatch.setattr(adoption, 'plan', lambda *a, **k: pytest.fail('unsafe preflight'))
    with pytest.raises(RuntimeError, match='keeper_dead'):
        adoption.adopt(tmp_path, tmp_path, tmp_path, {}, scheduler=object(), window=tmp_path / 'journal')


def state(hashes):
    return (json.dumps({'schema': 1, 'hashes': hashes}, indent=2, sort_keys=True) + '\n').encode()


def test_rebaseline_requires_complete_explained_diff():
    before = state({'x.py': 'a', 'unchanged.py': 'u'})
    after = state({'x.py': 'b', 'unchanged.py': 'u'})
    explanations = {'x.py': ('a', 'b', 'retained-receipt.json')}
    assert baseline.explained_diff(before, after, explanations) == [
        {'path': 'x.py', 'before_sha256': 'a', 'after_sha256': 'b', 'evidence': 'retained-receipt.json'}]
    with pytest.raises(RuntimeError, match='unexplained_sync_state_diff'):
        baseline.explained_diff(before, after, {})
    with pytest.raises(RuntimeError, match='unexplained_sync_state_diff'):
        baseline.explained_diff(before, state({'x.py': 'b', 'unchanged.py': 'tampered'}), explanations)
    with pytest.raises(RuntimeError, match='unexplained_sync_state_structure'):
        baseline.explained_diff(before, b'{"schema": 2, "hashes": {"x.py": "b"}}', explanations)


def make_rebaseline_fixture(tmp_path, monkeypatch):
    old, new = {}, {}
    for rel, (before, after, receipt) in baseline.EXPLAINED.items():
        old[rel] = before
        body = rel.encode()
        new[rel] = baseline.sha(body)
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    explanations = {rel: (old[rel], new[rel], values[2]) for rel, values in baseline.EXPLAINED.items()}
    before, after = state(old), state(new)
    monkeypatch.setattr(baseline, 'BEFORE', baseline.sha(before))
    monkeypatch.setattr(baseline, 'AFTER', baseline.sha(after))
    monkeypatch.setattr(baseline, 'EXPLAINED', explanations)
    evidence = {}
    for rel in baseline.EVIDENCE:
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'{"retained": true}')
        evidence[rel] = baseline.sha(target.read_bytes())
    monkeypatch.setattr(baseline, 'EVIDENCE', evidence)
    target = tmp_path / baseline.SNAPSHOT
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(before)
    (tmp_path / 'ops/.rebuild-sync-state.json').write_bytes(after)
    proposal = baseline.proposal(tmp_path)
    return {'schema': 'glp73-sync-rebaseline-review-v1', 'decision': 'ACCEPT_EXPLAINED_DIFF',
            'prepared_by': 'preparer', 'reviewer': 'independent-reviewer', 'proposal': proposal}


def test_rebaseline_requires_independent_review_and_unchanged_evidence(tmp_path, monkeypatch):
    review = make_rebaseline_fixture(tmp_path, monkeypatch)
    assert baseline.validate(tmp_path, review) == baseline.AFTER
    for key, value in [('reviewer', 'preparer'), ('decision', 'ACCEPT_EXACT_BYTES'), ('proposal', {})]:
        changed = copy.deepcopy(review)
        changed[key] = value
        with pytest.raises(RuntimeError, match='independent_explained_diff_review_required'):
            baseline.validate(tmp_path, changed)
    evidence = tmp_path / next(iter(baseline.EVIDENCE))
    evidence.write_text('{}')
    with pytest.raises(RuntimeError, match='sync_rebaseline_evidence_moved'):
        baseline.validate(tmp_path, review)


def test_rebaseline_cannot_accept_a_fresh_unexplained_whole_state(tmp_path, monkeypatch):
    review = make_rebaseline_fixture(tmp_path, monkeypatch)
    target = tmp_path / 'ops/.rebuild-sync-state.json'
    document = json.loads(target.read_bytes())
    document['hashes']['unexplained.py'] = 'tampered'
    target.write_bytes(state(document['hashes']))
    with pytest.raises(RuntimeError, match='sync_rebaseline_preimage_moved'):
        baseline.validate(tmp_path, review)
    assert glp73.STATE_BEFORE == '20a6396a78a4cb4cf82e473b0b3805b01ed279cd2e6526c2032caa10814e7436'


def test_glp44_preview_retains_original_schedule_and_exact_paused_job(tmp_path, monkeypatch):
    checkout, root, hermes = (tmp_path / name for name in ('checkout', 'root', 'hermes'))
    for directory in (checkout, root, hermes):
        directory.mkdir()
    for rel in glp44.PATHS:
        for base in (checkout, root):
            target = base / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b'{"files": {}}' if rel.endswith('source-manifest.json') else b'source')
    (root / glp44.syncer.STATE).write_bytes(state({rel: glp44.digest((root / rel).read_bytes()) for rel in glp44.PATHS}))
    wrapper = hermes / 'scripts' / glp44.WRAPPER
    wrapper.parent.mkdir()
    wrapper.write_bytes(b'source')
    original = {'id': glp44.JOB, 'enabled': True, 'state': 'scheduled',
                'script': glp44.WRAPPER, 'script_sha256': glp44.digest(b'source'),
                'schedule': {'kind': 'cron', 'expr': '0 8 * * 1'}, 'fire_claim': {'prior': 'drained'}}
    paused = {**original, 'enabled': False, 'state': 'paused', 'fire_claim': None}
    registry = hermes / 'cron/jobs.json'
    registry.parent.mkdir()
    registry.write_text(json.dumps({'jobs': [paused]}))
    monkeypatch.setattr(glp44.subprocess, 'run', lambda *a, **k: None)
    monkeypatch.setattr(glp44.subprocess, 'check_output', lambda command: (checkout / command[-1].split(':', 1)[1]).read_bytes())
    monkeypatch.setattr(glp44.syncer, 'sync', lambda *a: 0)
    window = SimpleNamespace(original_job=lambda jid: original, current_job=lambda jid: paused)
    proposal = glp44.plan(checkout, root, hermes, window=window)[0]
    assert proposal['job_before'] == original
    assert proposal['job_window_before'] == paused
    review = {**proposal, 'schema': 'glp44-scoped-adoption-review-v1',
              'approved_source': glp44.APPROVED, 'decision': 'ACCEPT_EXACT_BYTES', 'reviewer': 'reviewer'}
    assert glp44.plan(checkout, root, hermes, review, window=window)[0] == proposal
    changed = {**paused, 'enabled': True}
    registry.write_text(json.dumps({'jobs': [changed]}))
    with pytest.raises(RuntimeError, match='maintenance_reporting_job_moved_or_not_drained'):
        glp44.plan(checkout, root, hermes, review, window=window)
