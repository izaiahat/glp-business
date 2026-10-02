"""Ship policy and disposable runtime tests. Never import the host scheduler."""
import importlib
import sys
import json
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))


def ship_module():
    try:
        return importlib.import_module('glp_ship_generation')
    except ModuleNotFoundError:
        pytest.fail('git-derived ship generation policy is missing')


def test_tier_a_requires_independent_proposal_bound_review():
    ship = ship_module()
    proposal = {'tier': 'A', 'prepared_by': 'preparer', 'source': 'a' * 40}
    with pytest.raises(RuntimeError, match='independent_review_required'):
        ship.validate_review(proposal, None)
    review = {'schema': 'glp-ship-review-v1', 'decision': 'ACCEPT_EXACT_PROPOSAL',
              'prepared_by': 'preparer', 'reviewer': ' PREPARER ',
              'proposal_sha256': ship.json_sha(proposal)}
    with pytest.raises(RuntimeError, match='independent_review_required'):
        ship.validate_review(proposal, review)
    review['reviewer'] = 'independent'
    ship.validate_review(proposal, review)
    review['proposal_sha256'] = '0' * 64
    with pytest.raises(RuntimeError, match='review_proposal_moved'):
        ship.validate_review(proposal, review)


def test_tier_b_needs_no_runtime_review():
    ship_module().validate_review({'tier': 'B', 'prepared_by': 'preparer'}, None)


def test_git_generation_refuses_changed_live_preimage(tmp_path):
    ship = ship_module()
    root, home = tmp_path / 'root', tmp_path / 'home'
    root.mkdir(); (home / 'cron').mkdir(parents=True)
    leaf = root / 'source.py'
    leaf.write_bytes(b'known predecessor')
    (home / 'cron/jobs.json').write_text('{"jobs": []}')
    proposal = {'tier': 'B', 'prepared_by': 'fixture', 'root': str(root),
                'hermes': str(home), 'before': {str(leaf): ship.fingerprint(leaf)}, 'jobs': {}, 'source': 'a' * 40, 'kind': 'reporting', 'paths': ['source.py'],
                'hashes': {'source.py': 'b' * 64}, 'merge_proof': {}}
    generation = ship.GitGeneration('a' * 40, 'reporting', ('source.py',),
                                    {'source.py': 'b' * 64}, {}, proposal)
    generation.authorize(None, root, home, None)
    leaf.write_bytes(b'unknown local edit')
    with pytest.raises(RuntimeError, match='ship_preimage_moved'):
        generation.authorize(None, root, home, None)


def test_tier_b_git_generation_applies_through_reporting_adopter(tmp_path, monkeypatch):
    from test_glp44_scoped_adoption import fixture, git, sha
    from scripts import glp44_scoped_adoption as pair
    from scripts import sync_from_repo as syncer
    import glp_ship_registry
    monkeypatch.setattr(glp_ship_registry, 'REPORTING_PATHS', pair.PATHS)
    from maintenance_cohort import switch_files
    from glp_ship_provenance import derive_pins
    ship = ship_module()
    repo, root, home, registry, scheduler = fixture(tmp_path, monkeypatch)
    producer = 'scripts/glp44_scoped_adoption.py'
    (repo / producer).write_bytes(Path(pair.__file__).read_bytes())
    manifest = json.loads((repo / syncer.MANIFEST).read_text())
    manifest['files'][producer] = {'sha256': sha((repo / producer).read_bytes())}
    manifest['tracked_count'] += 1
    (repo / syncer.MANIFEST).write_text(json.dumps(manifest))
    git(repo, 'add', '.'); git(repo, 'commit', '-qm', 'committed producer')
    source = git(repo, 'rev-parse', 'HEAD')
    from glp_ship_registry import Job
    from glp_ship import prepare, zero_diff
    spec = Job('GLP333', 'reporting', 'B', pair.PATHS, (pair.JOB,), (pair.WRAPPER,), True)
    document = json.loads(registry.read_text())
    paused = document['jobs'][0]
    document['jobs'][0] = scheduler.window.original_job(pair.JOB)
    registry.write_text(json.dumps(document))
    generation = prepare(repo, root, home, spec, source, {}, 'fixture')
    document['jobs'][0] = paused
    registry.write_text(json.dumps(document))
    scheduler.window.journal = root / 'window.json'
    scheduler.window.journal.write_text('{"schema": "glp44-glp73-maintenance-window-v1"}')
    scheduler.window.switch_files = lambda stage, before, commit=None: switch_files(
        scheduler.window.journal, stage, before, lambda: [], commit=commit)
    result = pair.adopt(repo, root, home, None, scheduler=scheduler,
                        window=scheduler.window.journal, generation=generation)
    assert result['applied'] and scheduler.saves == 1
    zero_diff(repo, root, home, generation, spec)
    assert syncer.sync(root, repo, False, only=generation.paths) == 0
    assert all(sha((root / p).read_bytes()) == pin for p, pin in generation.hashes.items())
    assert json.loads(registry.read_text())['jobs'][0]['script_sha256'] == sha(
        (home / 'scripts' / pair.WRAPPER).read_bytes())


def test_ship_registry_classifies_retention_and_discovery_as_tier_a():
    from glp_ship_registry import registered_job
    assert registered_job('GLP73').tier == 'A'
    assert registered_job('F01').tier == 'A'
    assert registered_job('GLP333').tier == 'B'
    assert registered_job('F01').window is False
    with pytest.raises(RuntimeError, match='unregistered_ship_job'):
        registered_job('arbitrary-writer')


def test_ship_command_has_no_arbitrary_path_or_tier_override():
    try:
        ship = importlib.import_module('glp_ship')
    except ModuleNotFoundError:
        pytest.fail('one-command ship entry is missing')
    args = ship.parser().parse_args(['GLP333', '--merge-sha', 'a' * 40])
    assert not args.apply
    with pytest.raises(SystemExit):
        ship.parser().parse_args(['GLP73', '--merge-sha', 'a' * 40, '--tier', 'B'])


def test_ship_attempt_intent_refuses_uncertain_replay(tmp_path):
    import glp_ship as ship
    receipt = tmp_path / 'attempt.json'
    ship.claim_attempt(receipt, {'source': 'a' * 40})
    with pytest.raises(RuntimeError, match='ship_attempt_exists_reconcile'):
        ship.claim_attempt(receipt, {'source': 'a' * 40})


def test_absent_registered_source_cannot_be_created_after_review(tmp_path):
    ship = ship_module()
    root, home = tmp_path / 'root', tmp_path / 'home'
    root.mkdir(); (home / 'cron').mkdir(parents=True)
    (home / 'cron/jobs.json').write_text('{"jobs": []}')
    leaf = root / 'new.py'
    proposal = {'tier': 'B', 'prepared_by': 'fixture', 'root': str(root), 'hermes': str(home),
                'before': {str(leaf): None}, 'jobs': {}, 'source': 'a' * 40,
                'paths': ['new.py'], 'hashes': {'new.py': 'b' * 64},
                'kind': 'reporting', 'merge_proof': {}}
    generation = ship.GitGeneration('a' * 40, 'reporting', ('new.py',),
                                    {'new.py': 'b' * 64}, {}, proposal)
    generation.authorize(None, root, home, None)
    leaf.write_bytes(b'unknown late creation')
    with pytest.raises(RuntimeError, match='ship_preimage_moved'):
        generation.authorize(None, root, home, None)


def test_retention_descriptor_cannot_downgrade_its_tier(tmp_path):
    ship = ship_module()
    (tmp_path / 'cron').mkdir()
    (tmp_path / 'cron/jobs.json').write_text('{"jobs": []}')
    proposal = {'tier': 'B', 'source': 'a' * 40, 'kind': 'retention', 'paths': [],
                'hashes': {}, 'merge_proof': {}, 'root': str(tmp_path), 'hermes': str(tmp_path),
                'before': {}, 'jobs': {}}
    generation = ship.GitGeneration('a' * 40, 'retention', (), {}, {}, proposal)
    with pytest.raises(RuntimeError, match='ship_registered_tier_moved'):
        generation.authorize(None, tmp_path, tmp_path, None)


def test_native_fire_fence_spans_window_restore_and_registered_run(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace
    import glp_ship as ship
    import glp_ship_runtime as runtime
    import glp44_scoped_adoption as adopter
    import maintenance_window as mw
    held, events = set(), []
    class Scheduler:
        @contextmanager
        def _fire_job_lock(self, jid):
            held.add(jid)
            try:
                yield True
            finally:
                held.remove(jid)
    scheduler = Scheduler()
    monkeypatch.setattr(mw, 'LinuxBackend', lambda *a: SimpleNamespace(scheduler=lambda: scheduler))
    class Window:
        def __init__(self, *a): pass
        def enter(self): events.append('enter')
        def exit(self):
            assert held == {'registered'}, 'restoration admitted a scheduled run before smoke run'
            events.append('restore')
        def status(self): return {'state': 'closed'}
    monkeypatch.setattr(mw, 'MaintenanceWindow', Window)
    @contextmanager
    def active(*a): yield object()
    monkeypatch.setattr(mw, 'require_active_window', active)
    monkeypatch.setattr(ship, 'preview', lambda *a: {})
    monkeypatch.setattr(ship, 'zero_diff', lambda *a: None)
    monkeypatch.setattr(adopter, 'adopt', lambda *a, **k: {'applied': True})
    def run(*a, **k):
        assert held == {'registered'}
        events.append('run')
        return {'success': True, 'runs': []}
    monkeypatch.setattr(runtime, 'run_registered', run)
    generation = SimpleNamespace(proposal={'tier': 'B'}, source='a' * 40, authorize=lambda *a: None)
    spec = SimpleNamespace(window=True, kind='reporting', name='GLP333', jobs=('registered',))
    result = ship.apply_generation(tmp_path, tmp_path, tmp_path, spec, generation, None, tmp_path)
    assert result['success'] and events == ['enter', 'restore', 'run'] and not held
