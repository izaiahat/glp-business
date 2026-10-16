"""Publication regressions from PR159's independent review; disposable only."""
import copy
import json
from pathlib import Path

import pytest


def reporting_fixture(tmp_path, monkeypatch):
    from test_glp44_scoped_adoption import fixture, git, sha
    from scripts import glp44_scoped_adoption as pair
    from scripts import sync_from_repo as syncer
    import glp_ship_registry
    from glp_ship_registry import Job
    from glp_ship import prepare
    # This unit fixture isolates publication after preflight. Real projected
    # closure and all-four-surface planning run in test_glp_ship_preclaim.
    monkeypatch.setattr('glp_ship.authenticate_installer', lambda *a: None)
    monkeypatch.setattr('glp_ship.preclaim_plan', lambda *a: None)
    from maintenance_cohort import switch_files
    monkeypatch.setattr(glp_ship_registry, 'REPORTING_PATHS', pair.PATHS)
    repo, root, home, registry, scheduler = fixture(tmp_path, monkeypatch)
    producer = 'scripts/glp44_scoped_adoption.py'
    (repo / producer).write_bytes(Path(pair.__file__).read_bytes())
    manifest = json.loads((repo / syncer.MANIFEST).read_text())
    manifest['files'][producer] = {'sha256': sha((repo / producer).read_bytes())}
    manifest['tracked_count'] += 1
    (repo / syncer.MANIFEST).write_text(json.dumps(manifest))
    git(repo, 'add', '.'); git(repo, 'commit', '-qm', 'committed producer fixture')
    spec = Job('GLP333', 'reporting', 'B', pair.PATHS, (pair.JOB,), (pair.WRAPPER,), True)
    scheduler.window.journal = root / 'window.json'

    def generation():
        document = json.loads(registry.read_text())
        paused = copy.deepcopy(document['jobs'][0])
        original = copy.deepcopy(paused)
        original.update(enabled=True, state='scheduled')
        scheduler.window.original_job = lambda jid: copy.deepcopy(original)
        document['jobs'][0] = original
        registry.write_text(json.dumps(document))
        result = prepare(repo, root, home, spec, git(repo, 'rev-parse', 'HEAD'), {}, 'fixture')
        document['jobs'][0] = paused
        registry.write_text(json.dumps(document))
        scheduler.window.journal.write_text('{"schema":"glp44-glp73-maintenance-window-v1"}')
        scheduler.window.switch_files = lambda stage, before, commit=None: switch_files(
            scheduler.window.journal, stage, before, lambda: [], commit=commit)
        return result
    return repo, root, home, registry, scheduler, spec, generation


def test_successive_tier_b_generations_have_separate_custody(tmp_path, monkeypatch):
    from scripts import glp44_scoped_adoption as pair
    from scripts import sync_from_repo as syncer
    from test_glp44_scoped_adoption import git, sha
    repo, root, home, registry, scheduler, spec, make_generation = reporting_fixture(tmp_path, monkeypatch)
    first = make_generation()
    result = pair.adopt(repo, root, home, None, scheduler=scheduler,
                        window=scheduler.window.journal, generation=first)
    first_custody = Path(result['custody'])
    retained = (first_custody / 'preimages.json').read_bytes()
    rel = 'systems/analytics-brain/collector.py'
    (repo / rel).write_bytes(b'newer collector fixture\n')
    manifest = json.loads((repo / syncer.MANIFEST).read_text())
    manifest['files'][rel]['sha256'] = sha((repo / rel).read_bytes())
    (repo / syncer.MANIFEST).write_text(json.dumps(manifest))
    git(repo, 'add', '.'); git(repo, 'commit', '-qm', 'next registered generation fixture')
    second = make_generation()
    assert first.source != second.source
    try:
        result2 = pair.adopt(repo, root, home, None, scheduler=scheduler,
                             window=scheduler.window.journal, generation=second)
    finally:
        assert (first_custody / 'preimages.json').read_bytes() == retained
    assert result2['custody'] != result['custody']
    assert scheduler.saves == 2


def test_reporting_switch_excludes_maintained_source_writer(tmp_path, monkeypatch):
    from scripts import glp44_scoped_adoption as pair
    from scripts import sync_from_repo as syncer
    import maintenance_window as mw
    import fcntl
    from contextlib import ExitStack
    repo, root, home, registry, scheduler, spec, make_generation = reporting_fixture(tmp_path, monkeypatch)
    generation = make_generation()
    with ExitStack() as held:
        for path in mw.default_locks(root, home):
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = held.enter_context(path.open('a+'))
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        seen = []
        def excluded(event):
            with pytest.raises(ValueError, match='source_import_busy'):
                with syncer.source_lock(root):
                    pass
            # Ordinary imports also conflict; only the explicit lease may join.
            with pytest.raises(ValueError, match='source_import_busy'):
                syncer.sync(root, repo, False, only=generation.paths)
            seen.append(event)
        original_custody = pair.preserve_custody
        def custody(*args):
            excluded('custody')
            return original_custody(*args)
        monkeypatch.setattr(pair, 'preserve_custody', custody)
        original_save = scheduler._save_jobs_unlocked
        def save(*args):
            excluded('native-save')
            return original_save(*args)
        monkeypatch.setattr(scheduler, '_save_jobs_unlocked', save)
        original_switch = scheduler.window.switch_files
        def inspect_switch(stage, before, commit=None):
            excluded('switch')
            result = original_switch(stage, before, commit=commit)
            excluded('switch-readback')
            return result
        scheduler.window.switch_files = inspect_switch
        pair.adopt(repo, root, home, None, scheduler=scheduler,
                   window=scheduler.window.journal, generation=generation)
        assert seen == ['custody', 'switch', 'native-save', 'switch-readback']
    # Both descriptors and the explicit lease have been released after return.
    with syncer.source_lock(root):
        pass


@pytest.mark.parametrize('kind', ['reporting', 'retention', 'discovery'])
def test_command_holds_one_source_lease_through_readback_and_restore(tmp_path, monkeypatch, kind):
    from contextlib import contextmanager
    from types import SimpleNamespace
    import glp_ship as ship
    import glp_ship_runtime as runtime
    import maintenance_window as mw
    import glp44_scoped_adoption as reporting
    import glp73_paired_adoption as retention
    import glp_ship_discovery as discovery
    events, owners = [], []

    def excluded(event, owner=None):
        with pytest.raises(ValueError, match='source_import_busy'):
            with ship.syncer.source_lock(tmp_path):
                pass
        events.append(event)
        if owner is not None:
            with ship.syncer.source_session(tmp_path, owner):
                owners.append(owner)

    class Scheduler:
        @contextmanager
        def _fire_job_lock(self, jid):
            yield True
    monkeypatch.setattr(mw, 'LinuxBackend', lambda *a: SimpleNamespace(scheduler=Scheduler))

    class Window:
        def __init__(self, *a): pass
        def enter(self): excluded('enter')
        def exit(self): excluded('restore')
        def status(self):
            excluded('readback')
            return {'state': 'closed'}
    monkeypatch.setattr(mw, 'MaintenanceWindow', Window)

    @contextmanager
    def active(*a):
        excluded('window-admit')
        yield object()
    monkeypatch.setattr(mw, 'require_active_window', active)

    def preview(*a, **kw):
        excluded('preview', kw.get('_source_owner'))
        return {}
    def adopt(*a, **kw):
        excluded('adopt', kw.get('_source_owner'))
        return {'applied': True}
    def zero(*a, **kw):
        excluded('zero-diff', kw.get('_source_owner'))
    monkeypatch.setattr(ship, 'preview', preview)
    monkeypatch.setattr(ship, 'zero_diff', zero)
    monkeypatch.setattr(reporting, 'adopt', adopt)
    monkeypatch.setattr(retention, 'adopt', adopt)
    monkeypatch.setattr(discovery, 'apply', adopt)

    def run(*a, **kw):
        with ship.syncer.source_lock(tmp_path):
            events.append('run')
        return {'success': True}
    monkeypatch.setattr(runtime, 'run_registered', run)
    generation = SimpleNamespace(proposal={'tier': 'B'}, source='a' * 40, authorize=lambda *a: None)
    spec = SimpleNamespace(window=kind != 'discovery', kind=kind, name='fixture', jobs=('fixture',))
    assert ship.apply_generation(tmp_path, tmp_path, tmp_path, spec, generation, None, tmp_path)['success']
    assert events[-1] == 'run'
    assert len(owners) == (3 if spec.window else 2)
    assert all(owner is owners[0] for owner in owners)
