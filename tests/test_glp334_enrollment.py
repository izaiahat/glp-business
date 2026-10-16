"""Exact target regressions. Native metadata is nonsecret; all writes disposable.

Git blobs are genuine, not re-pinned synthetic wrapper fixtures. The optional
full retained-input rehearsal uses only an explicit local read-only fixture root;
CI does not pretend the private untracked reports are available.
"""
import copy
from contextlib import contextmanager, nullcontext
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from types import SimpleNamespace

import pytest

import glp44_scoped_adoption as pair
import glp_ship as ship
from glp_ship_generation import GitGeneration, json_sha
from glp_ship_registry import (registered_job, enrollment_hashes, GLP334_JOBS,
                               GLP334_SOURCE, GLP334_PREDECESSOR, GLP334_PATHS)

ROOT = Path(__file__).resolve().parents[1]


def blob(commit, path):
    return subprocess.check_output(['git', '-C', str(ROOT), 'show', commit + ':' + path])


def native_metadata():
    # Authenticated receiving metadata; no prompt, provider config or credentials.
    return [{'id': jid, 'script': name, 'script_sha256': old,
             'enabled': True, 'state': 'scheduled', 'workdir': '/home/ubuntu/business',
             'no_agent': True, 'script_timeout_seconds': timeout,
             'schedule': {'kind': 'cron', 'expr': cron, 'display': cron}, 'fire_claim': None}
            for jid, name, old, timeout, cron in GLP334_JOBS]


def native_fixture(tmp_path):
    home = tmp_path / 'home'
    (home / 'scripts').mkdir(parents=True)
    (home / 'cron').mkdir()
    for jid, name, old, *_ in GLP334_JOBS:
        path = home / 'scripts' / name
        path.write_bytes(blob(GLP334_PREDECESSOR, 'ops/hermes-cron-scripts/' + name))
        path.chmod(0o755)
        assert pair.digest(path.read_bytes()) == old
    registry = home / 'cron/jobs.json'
    registry.write_text(json.dumps({'jobs': native_metadata() + [{'id': 'unrelated', 'schedule': 'retained'}]}))
    return home, registry


@pytest.mark.parametrize('job_id', ['aadf6677c7f0', '2ca359464bfa'])
def test_real_registered_target_selects_paired_j7_and_retained_consumer(job_id):
    spec = registered_job(job_id)
    assert spec.name == 'GLP334'
    assert spec.kind == 'reporting-enrollment'
    assert spec.tier == 'A'
    assert spec.jobs == ('aadf6677c7f0', '2ca359464bfa')
    assert spec.wrappers == ('glp_affiliate_weekly_reporting_j7.sh', 'glp334_live_reconciliation.sh')
    assert spec.paths == GLP334_PATHS and len(spec.paths) == 14
    assert spec.window


def test_genuine_old_external_native_identities_and_reviewed_successors(tmp_path):
    home, registry = native_fixture(tmp_path)
    hashes = enrollment_hashes(ROOT)
    for jid, name, old, *_ in GLP334_JOBS:
        snap = 'ops/hermes-cron-scripts/' + name
        plan, before = pair._target_preimage(tmp_path, home, registry, None, jid, name, snap, ROOT)
        assert before[0] == blob(GLP334_PREDECESSOR, snap)
        assert plan['job_before']['script_sha256'] == old
        assert hashes[snap] == pair.digest(blob(GLP334_SOURCE, snap)) != old
    assert hashes['ops/hermes-cron-scripts/glp_affiliate_weekly_reporting_j7.sh'] == 'debdfe9fc7bb3591b76d3e3891869cedf6f24c65cbc37132aa11178c71818087'
    assert hashes['ops/hermes-cron-scripts/glp334_live_reconciliation.sh'] == 'a9c71fbd7afa4971eca21436facad2a09eb20ff91de96a15ce4866a1b23194f8'


@pytest.mark.parametrize('field,value', [('script', 'glp_affiliate_weekly_reporting.sh'),
    ('workdir', '/unrelated'), ('no_agent', False), ('script_timeout_seconds', 301),
    ('schedule', {'kind': 'cron', 'expr': '0 0 * * *'}), ('running', True),
    ('fire_claim', {'owner': 'other'}), ('script_sha256', '0' * 64)])
def test_wrong_j7_or_native_identity_refuses_without_writes(tmp_path, field, value):
    home, registry = native_fixture(tmp_path)
    data = json.loads(registry.read_text())
    data['jobs'][0][field] = value
    registry.write_text(json.dumps(data))
    before = registry.read_bytes()
    jid, name, *_ = GLP334_JOBS[0]
    with pytest.raises(RuntimeError):
        pair._target_preimage(tmp_path, home, registry, None, jid, name,
                              'ops/hermes-cron-scripts/' + name, ROOT)
    assert registry.read_bytes() == before


@pytest.mark.parametrize('fault', ['drift', 'absent', 'symlink', 'already_successor'])
def test_missing_drift_or_replay_external_custody_refused(tmp_path, fault):
    home, registry = native_fixture(tmp_path)
    jid, name, *_ = GLP334_JOBS[1]
    wrapper = home / 'scripts' / name
    if fault == 'drift': wrapper.write_bytes(wrapper.read_bytes() + b'\n')
    elif fault == 'absent': wrapper.unlink()
    elif fault == 'symlink':
        wrapper.unlink(); wrapper.symlink_to(ROOT / 'ops/hermes-cron-scripts' / name)
    else:
        wrapper.write_bytes(blob(GLP334_SOURCE, 'ops/hermes-cron-scripts/' + name))
        data = json.loads(registry.read_text())
        data['jobs'][1]['script_sha256'] = pair.digest(wrapper.read_bytes())
        registry.write_text(json.dumps(data))
    with pytest.raises((RuntimeError, OSError)):
        pair._target_preimage(tmp_path, home, registry, None, jid, name,
                              'ops/hermes-cron-scripts/' + name, ROOT)


def test_no_other_registered_id_or_cohort_and_static_inventory(tmp_path):
    from maintenance_inventory import affected_paths
    for value in ('2ca359464bfb', 'aadf6677c7f1', 'glp334', 'all', 'fleet'):
        with pytest.raises(RuntimeError, match='unregistered_ship_job'):
            registered_job(value)
    paths = affected_paths(tmp_path / 'live', tmp_path / 'home')
    assert {tmp_path / 'live' / p for p in GLP334_PATHS} <= paths
    assert {tmp_path / 'home/scripts' / r[1] for r in GLP334_JOBS} <= paths


def test_enrollment_route_never_calls_registered_runtime(tmp_path, monkeypatch):
    # Exercise the real caller routing after a simulated maintained adopter;
    # production runtime must remain unreachable for this registered kind.
    import maintenance_window as mw
    import glp_ship_runtime as runtime
    class Window:
        def __init__(self, *a): pass
        def enter(self): pass
        def exit(self): pass
        def status(self): return {'state': 'closed'}
    monkeypatch.setattr(mw, 'MaintenanceWindow', Window)
    monkeypatch.setattr(mw, 'require_active_window', lambda *a: nullcontext(None))
    monkeypatch.setattr(ship, 'preview', lambda *a, **kw: {})
    monkeypatch.setattr(ship, 'zero_diff', lambda *a, **kw: None)
    monkeypatch.setattr(pair, 'adopt', lambda *a, **kw: {'applied': True})
    monkeypatch.setattr(runtime, 'run_registered', lambda *a, **kw: pytest.fail('native replay'))
    generation = SimpleNamespace(source='fixture', proposal={})
    spec = registered_job(GLP334_JOBS[0][0])
    result = ship._apply_and_run(tmp_path, tmp_path, tmp_path, spec, generation, {},
                                  tmp_path, tmp_path / 'window.json', object(), object())
    assert result['runs'] == [] and result['policy'] == 'ENROLLMENT_ONLY_NO_EXECUTION'


def retained_fixture(tmp_path):
    origin = os.environ.get('GLP334_RETAINED_FIXTURE_ROOT')
    if not origin:
        pytest.skip('full genuine retained-input fixture requires explicit local read-only root')
    origin = Path(origin)
    live = tmp_path / 'live'
    live.mkdir()
    co = json.loads(blob(GLP334_SOURCE, 'ops/glp/reporting-v2/glp334-install-cohort.json'))
    paths = set(GLP334_PATHS)
    for name in ('', '-j5', '-j6', '-j7'):
        rel = 'ops/glp/reporting-v2/source-manifest' + name + '.json'
        paths.update(str(Path(p).relative_to('/home/ubuntu/business'))
                     for p in json.loads(blob(GLP334_SOURCE, rel))['files'])
    for rel in [p for p in GLP334_PATHS if p.endswith('.sh')]:
        text = blob(GLP334_SOURCE, rel).decode()
        paths.update(re.findall(r'^check_source (\S+) ', text, re.M))
        paths.update(str(Path(p).relative_to('/home/ubuntu/business')) for p in
                     re.findall(r'--(?:xentraffic-intake|eden-intake|statement-intake) (\S+)', text))
    for rel in paths:
        target = live / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blob(GLP334_SOURCE, rel) if rel in GLP334_PATHS else (origin / rel).read_bytes())
        target.chmod(int(subprocess.check_output(['git', '-C', str(ROOT), 'ls-tree', GLP334_SOURCE, '--', rel]).split()[0], 8) & 0o777
                     if rel in GLP334_PATHS else (origin / rel).stat().st_mode & 0o777)
    # Disposable post-receiving state, never the installed source baseline.
    hashes = {rel: pair.digest((live / rel).read_bytes()) for rel in GLP334_PATHS}
    (live / pair.syncer.STATE).write_text(json.dumps({'hashes': hashes}))
    (live / pair.syncer.MANIFEST).write_bytes(blob(GLP334_SOURCE, pair.syncer.MANIFEST))
    home, registry = native_fixture(tmp_path)
    spec = registered_job(GLP334_JOBS[0][0])
    generation = ship.prepare(ROOT, live, home, spec,
        subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip(), {}, 'offline-preparer')
    return live, home, registry, spec, generation


def test_genuine_complete_offline_enrollment_and_source_unchanged(tmp_path, monkeypatch):
    from maintenance_cohort import switch_files
    import maintenance_window as mw
    live, home, registry, spec, generation = retained_fixture(tmp_path)
    original = copy.deepcopy(json.loads(registry.read_text())['jobs'])
    current = copy.deepcopy(original)
    for row in current[:2]: row.update(enabled=False, state='paused')
    registry.write_text(json.dumps({'jobs': current}))
    journal = tmp_path / 'window.json'
    journal.write_text('{"schema":"glp44-glp73-maintenance-window-v1"}')
    source_before = {p: pair.read_regular(live / p) for p in GLP334_PATHS + (pair.syncer.STATE, pair.syncer.MANIFEST)}
    events = []
    class Window:
        def __init__(self): self.journal = journal
        def original_job(self, jid): return next(copy.deepcopy(j) for j in original if j['id'] == jid)
        def current_job(self, jid): return pair.job_record(registry, jid)[0]
        def accept_job_update(self, jid, before, after):
            assert {k:v for k,v in before.items() if k != 'script_sha256'} == {k:v for k,v in after.items() if k != 'script_sha256'}
            events.append(('intent', jid))
        def accept_file_updates(self, before, after):
            assert set(before) == {str((home / 'scripts' / name).resolve()) for _, name, *_ in GLP334_JOBS}
        def switch_files(self, stage, before, commit=None):
            return switch_files(journal, stage, before, lambda: [], commit=commit)
    window = Window()
    monkeypatch.setattr(mw, 'require_active_window', lambda *a: nullcontext(window))
    class Scheduler:
        def _current_cron_store(self): return SimpleNamespace(jobs_file=registry)
        def _jobs_lock_file(self): return registry.parent / '.jobs.lock'
        @contextmanager
        def _fire_job_lock(self, jid):
            events.append(('fire', jid)); yield True
        @contextmanager
        def _jobs_lock(self, *, require_cross_process):
            assert require_cross_process
            self.lease = True
            yield
            self.lease = False
        def _record_load_stamp(self, _): pass
        def _save_jobs_unlocked(self, jobs):
            assert self.lease
            events.append(('save', None))
            registry.write_text(json.dumps({'jobs': jobs}))
    review = {'schema': 'glp-ship-review-v1', 'prepared_by': 'offline-preparer',
              'reviewer': 'offline-independent-fixture', 'decision': 'ACCEPT_EXACT_PROPOSAL',
              'proposal_sha256': json_sha(generation.proposal)}
    result = pair.adopt(ROOT, live, home, review, scheduler=Scheduler(), window=journal, generation=generation)
    assert result['applied']
    assert {p: pair.read_regular(live / p) for p in source_before} == source_before
    after = json.loads(registry.read_text())['jobs']
    assert after[2:] == current[2:]
    for index, (jid, name, *_rest) in enumerate(GLP334_JOBS):
        assert pair.read_regular(home / 'scripts' / name)[0] == blob(GLP334_SOURCE, 'ops/hermes-cron-scripts/' + name)
        assert {k:v for k,v in after[index].items() if k != 'script_sha256'} == {k:v for k,v in current[index].items() if k != 'script_sha256'}
    assert events.count(('save', None)) == 1
    assert {jid for event,jid in events if event == 'fire'} == set(spec.jobs)
    assert Path(result['custody'], 'preimages.json').is_file()
    with pytest.raises(RuntimeError):
        pair.adopt(ROOT, live, home, review, scheduler=Scheduler(), window=journal, generation=generation)
