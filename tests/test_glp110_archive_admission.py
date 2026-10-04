"""Owning regression: labeled scratch fixtures, all cleanup effects denied.

Discovery is a one-candidate fixture, not a claim of live eligible bytes. The
registered full-pass run() orchestration and real archive_tree admission guards
are exercised. No compressor, retirement, deletion, live scan or CLI run occurs.
"""
import copy
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import evidence_preserving_pruner as ep


@pytest.fixture
def owner(tmp_path, monkeypatch):
    root = tmp_path / 'reports' / 'ops' / 'fixture-held-candidate'
    root.mkdir(parents=True)
    (root / 'evidence.txt').write_text('fixture evidence must stay intact')
    (tmp_path / 'ops').mkdir()
    (tmp_path / 'ops' / 'disk-retention-policy.json').write_text(
        json.dumps({'protected_roots': []}))
    source = tmp_path / 'ops' / 'current-manifest.json'
    source.write_text('{}')
    config = dict(business=str(tmp_path), archive_roots=[str(root.parent)],
                  dedupe_roots=[], reference_roots=[str(source.parent)],
                  A_budget_seconds=180, B_budget_seconds=150,
                  archive_max_depth=4, archive_min_bytes=50 * 1024 * 1024)
    state = {'C_contract': ['depth-v1', 4, 50 * 1024 * 1024,
                            config['archive_roots']], 'C_done': [str(root)]}
    monkeypatch.setattr(ep, 'BUS', tmp_path)
    monkeypatch.setattr(ep, 'STATE', tmp_path / 'runstate' / 'evidence.json')
    ep.STATE.parent.mkdir()
    def denied(*args, **kwargs):
        raise AssertionError('cleanup/subprocess effect denied by owning fixture')
    monkeypatch.setattr(ep.subprocess, 'run', denied)
    monkeypatch.setattr(ep.subprocess, 'Popen', denied)
    monkeypatch.setattr(ep.subprocess, 'check_output', denied)
    monkeypatch.setattr(ep.shutil, 'rmtree', denied)
    monkeypatch.setattr(ep, 'regenerable', lambda *a, **k: [])
    monkeypatch.setattr(ep, 'dedupe', lambda *a, **k: {'rule': 'B_fixture', 'ok': True})
    # No recovery/lifecycle admission is granted; its boundary is inert here.
    monkeypatch.setattr(ep, 'recover_retirements', lambda *a, **k: [])
    monkeypatch.setitem(sys.modules, 'screenshot_pruner',
                        SimpleNamespace(run_rules=lambda *a, **k: []))
    monkeypatch.setattr(ep, 'reference_files', lambda *a: [source])
    def discovery(*args, progress, **kwargs):
        progress(root.parent, 1)
        return [root]
    monkeypatch.setattr(ep, 'archive_candidates', discovery)
    api = SimpleNamespace(path_is_active_privileged=lambda p: False, run=denied)
    return SimpleNamespace(root=root, source=source, config=config, state=state,
                           api=api, discovery=discovery)


def execute(owner, *, deadline=None):
    rows = ep.run(owner.api, owner.config, owner.state, dry_run=False,
                  deadline=deadline or time.monotonic()+60, full_pass=True)
    return rows, [r for r in rows if r.get('rule') == 'C_cold_evidence_archive']


@pytest.mark.parametrize('guard', ['referenced', 'active', 'protected', 'young', 'small'])
def test_prior_terminal_path_never_skips_fresh_admission(owner, guard, monkeypatch):
    """The old path-only C_done memo must not bypass any fresh safety oracle."""
    expected = {'referenced': 'GATED_REFERENCED', 'active': 'GATED_ACTIVE_OR_UNKNOWN',
                'protected': 'GATED_PROTECTED', 'young': 'GATED_YOUNG_FILE',
                'small': 'GATED_SHARED_OR_SMALL_UNIT'}[guard]
    if guard == 'referenced':
        owner.source.write_text(json.dumps({'current_root': str(owner.root)}))
    elif guard == 'active':
        owner.api.path_is_active_privileged = lambda p: True
    elif guard == 'protected':
        monkeypatch.setattr(ep, 'protected', lambda p, **k: p == owner.root)
    if guard != 'young':
        old = time.time()-40*86400
        ep.os.utime(owner.root / 'evidence.txt', (old, old))
    history = copy.deepcopy(owner.state['C_done'])
    for _ in range(2):
        rows, candidates = execute(owner)
        assert len(candidates) == 1, 'cached path skipped fresh archive admission'
        assert candidates[0]['status'] == expected
        assert rows[-1]['archive_progress']['evaluated'] == 1
        assert owner.state['C_done'] == history, 'historical ledger reset or rewritten'
    assert (owner.root / 'evidence.txt').read_text() == 'fixture evidence must stay intact'
    assert not list(owner.root.parent.glob('*.zst'))
    assert not list((ep.STATE.parent / 'host-pruner-archives').iterdir())


def test_reference_drift_is_evaluated_and_remains_pending(owner, monkeypatch):
    def changed_discovery(*args, **kwargs):
        result = owner.discovery(*args, **kwargs)
        owner.source.write_text(json.dumps({'current_root': str(owner.root)}))
        return result
    monkeypatch.setattr(ep, 'archive_candidates', changed_discovery)
    rows, candidates = execute(owner)
    assert len(candidates) == 1, 'cached path suppressed real changed-source veto'
    assert candidates[0]['status'] == 'GATED_REFERENCE_CHANGED'
    assert candidates[0]['changed_reference_source'] == str(owner.source)
    assert rows[-1]['archive_progress']['evaluated'] == 1
    assert rows[-1]['archive_progress']['roots_evaluated'] == []
    assert rows[-1]['partial'] is True
    assert owner.state['full_pass_complete'] is False
    assert (owner.root / 'evidence.txt').exists()


def test_budget_does_not_turn_old_cached_paths_into_completed_work(owner):
    rows, candidates = execute(owner, deadline=time.monotonic()+10)
    assert candidates == []
    assert rows[-1]['archive_progress']['evaluated'] == 0
    assert rows[-1]['archive_progress']['roots_evaluated'] == []
    assert rows[-1]['partial'] is True
    assert owner.state['full_pass_complete'] is False


def test_safety_oracles_equal_authenticated_installed_predecessor():
    """Content/metadata/hardlink/retention/lifecycle guards are byte-unchanged.

    This is a source invariance assertion, not a live effect verification.
    Digest derives from authenticated predecessor commit 8f30fd7, not a mock.
    """
    import ast
    import hashlib
    source = (Path(__file__).resolve().parents[1] / 'scripts' /
              'evidence_preserving_pruner.py').read_text()
    names = {'ReferenceIndex', 'archive_candidates', 'archive_tree', 'dedupe',
             'git_veto', 'inventory', 'protected', 'recover_retirements',
             'reference_files', 'regenerable', 'sig', 'verify_archive'}
    nodes = {}
    for node in ast.parse(source).body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names:
            segment = ast.get_source_segment(source, node)
            assert segment is not None
            nodes[node.name] = segment
    assert set(nodes) == names
    digest = hashlib.sha256('\n'.join(nodes[n] for n in sorted(nodes)).encode()).hexdigest()
    assert digest == 'fa3ed2ae517e6bd327cfd24ce6d809e5e1bfd8615b9fa3437655d0c0e773313f'
