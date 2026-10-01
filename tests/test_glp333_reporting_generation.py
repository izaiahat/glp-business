"""Exact-generation adoption in disposable roots; never execute a reporting job.

Portable CI uses a labelled scheduler double. Set GLP333_NATIVE_CRON_SOURCE only
in a fresh host interpreter to exercise the unchanged native saver in fixture home.
External scheduler surfaces are inert fixtures; ActiveWindow, inventory parser,
locks, cohort switch and receiving guards are always the maintained implementation.
"""
import ast
import copy
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import glp44_scoped_adoption as pair
import maintenance_inventory as inventory
import maintenance_window as window_module
import sync_from_repo as syncer

EXTRA = {
    'ops/glp/reporting-v2/identity-registry.json',
    'scripts/glp_affiliate_weekly_reporting.py',
    'scripts/glp_affiliate_weekly_reporting_job.py',
}


def sha(body):
    return hashlib.sha256(body).hexdigest()


def blob(ref, rel):
    return subprocess.check_output(['git', '-C', str(ROOT), 'show', ref + ':' + rel])


def put(path, body, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    path.chmod(mode)


class PortableScheduler:
    """No normalization: native acceptance is separately run on the host."""
    _jobs_file_lock = threading.RLock()

    def __init__(self, registry):
        self.registry = registry

    def _current_cron_store(self):
        return SimpleNamespace(jobs_file=self.registry)

    def _jobs_lock_file(self):
        return self.registry.parent / '.jobs.lock'

    def _fire_job_lock(self, jid):
        return nullcontext(True)

    def _record_load_stamp(self, value):
        pass

    def load_jobs(self):
        return json.loads(self.registry.read_text())['jobs']

    def _save_jobs_unlocked(self, jobs, **kwargs):
        assert not kwargs.get('replace') and not kwargs.get('removed_ids')
        doc = json.loads(self.registry.read_text())
        doc['jobs'] = jobs
        put(self.registry, (json.dumps(doc) + '\n').encode())


def generation_fixture(tmp_path, monkeypatch):
    root, hermes = tmp_path / 'business', tmp_path / 'hermes'
    root.mkdir()
    hermes.mkdir()
    # Candidate deps are already installed by the earlier coherent source stage;
    # the five reporting generation predecessors are exactly post-GLP44 PR71.
    pins = json.loads(blob(pair.PR119_SOURCE, 'ops/glp/reporting-v2/source-manifest.json'))['files']
    for absolute, expected in pins.items():
        rel = str(Path(absolute).relative_to(syncer.DEFAULT_ROOT))
        body = blob(pair.PR119_SOURCE, rel)
        assert sha(body) == expected
        put(root / rel, body)
    for rel, expected in pair.PR119_PREREQUISITES.items():
        body = blob(pair.APPROVED, rel)
        assert sha(body) == expected
        put(root / rel, body)
    for rel, expected in pair.PR119_BEFORE.items():
        body = blob(pair.APPROVED, rel)
        assert sha(body) == expected
        put(root / rel, body, 0o640)
    put(root / syncer.MANIFEST, (ROOT / syncer.MANIFEST).read_bytes())
    put(root / 'scripts/unselected.py', b'not selected\n')
    hashes = {str(p.relative_to(root)): sha(p.read_bytes()) for p in root.rglob('*') if p.is_file()}
    put(root / syncer.STATE, (json.dumps({'hashes': hashes, 'receipt': 'fixture post GLP73 then GLP44'}) + '\n').encode())
    wrapper = hermes / 'scripts' / pair.WRAPPER
    put(wrapper, (root / pair.SNAPSHOT).read_bytes(), 0o750)
    job = {'id': pair.JOB, 'script': pair.WRAPPER, 'script_sha256': sha(wrapper.read_bytes()),
           'enabled': True, 'state': 'scheduled', 'no_agent': True,
           'schedule': {'kind': 'cron', 'expr': '0 4 * * 1'},
           'next_run_at': '2026-10-05T04:00:00Z', 'pending_slot': '2026-10-05T04:00:00Z',
           'fire_claim': None, 'run_claim': None, 'last_status': 'fixture',
           'repeat': {'times': None, 'completed': 2}, 'fixture_unknown_total': None}
    other = {'id': 'unrelated', 'enabled': False, 'state': 'paused', 'script': 'unrelated.sh',
             'no_agent': True, 'paused_reason': 'provider hold', 'schedule': 'daily'}
    put(hermes / 'scripts/unrelated.sh', b'echo unrelated\n')
    registry = hermes / 'cron/jobs.json'
    put(registry, (json.dumps({'jobs': [job, other], 'version': 1}) + '\n').encode())
    native = os.environ.get('GLP333_NATIVE_CRON_SOURCE')
    if native:
        # Fresh interpreter only; the home is selected before first cron import.
        assert not any(k == 'cron' or k.startswith('cron.') for k in sys.modules)
        monkeypatch.setenv('HERMES_HOME', str(hermes))
        sys.path.insert(0, native)
        from cron import jobs as scheduler
        assert scheduler._current_cron_store().jobs_file == registry
        assert Path(scheduler.__file__).resolve() == Path(native).resolve() / 'cron/jobs.py'
    else:
        scheduler = PortableScheduler(registry)
    saves = []
    saver = scheduler._save_jobs_unlocked
    def counted(jobs, **kwargs):
        saves.append(copy.deepcopy(jobs))
        return saver(jobs, **kwargs)
    monkeypatch.setattr(scheduler, '_save_jobs_unlocked', counted)
    put(root / 'ops/n8n-bridge/actions.json', b'{}\n')
    bridge = {'paused': False}
    def probe(argv):
        if argv == ['crontab', '-l'] or 'list-unit-files' in argv or 'list-units' in argv:
            return ''
        assert argv[3] == 'n8n-bridge.service'
        return ('LoadState=loaded\nActiveState=active\nUnitFileState=enabled\nMainPID=123\n'
                'ExecStart={ path=/usr/bin/true ; argv[]=true ; ignore_errors=no ; }\n')
    class Backend(window_module.LinuxBackend):
        def scheduler(self):
            return scheduler
        def inventory(self):
            return inventory.discover(root, hermes, probe=probe)
        def snapshot(self, entry):
            return {'fixture_paused': bridge['paused']} if entry['kind'] == 'n8n' else super().snapshot(entry)
        def pause(self, entry, prior):
            if entry['kind'] == 'n8n':
                bridge['paused'] = True
            else:
                super().pause(entry, prior)
        def paused(self, entry, prior):
            return bridge['paused'] if entry['kind'] == 'n8n' else super().paused(entry, prior)
        def restore(self, entry, prior):
            if entry['kind'] == 'n8n':
                bridge['paused'] = prior['fixture_paused']
            else:
                super().restore(entry, prior)
        def cron_tool(self, action, jid):
            # Simulate the external pause tool; native saver and exact_pause are
            # unchanged. No live tool, gateway, bridge, or scheduler is invoked.
            assert action == 'pause'
            jobs = self.scheduler().load_jobs()
            next(j for j in jobs if j['id'] == jid).update(enabled=False, state='paused')
            self.scheduler()._save_jobs_unlocked(jobs)
        def inflight(self, entries):
            return []  # isolated roots have no launched readers/jobs
    journal = tmp_path / 'window.json'
    backend = Backend(root, hermes, journal)
    locks = [tmp_path / 'reader.lock', tmp_path / 'writer.lock']
    window = window_module.MaintenanceWindow(backend, journal, locks)
    monkeypatch.setattr(window_module, 'LinuxBackend', lambda *_: backend)
    monkeypatch.setattr(window_module, 'default_locks', lambda *_: locks)
    window.enter()
    assert window.status()['verified']
    assert set(backend.inventory()['coverage']) == {'hermes', 'crontab', 'systemd', 'n8n'}
    saves.clear()
    return SimpleNamespace(root=root, hermes=hermes, registry=registry, scheduler=scheduler,
                           window=window, backend=backend, journal=journal, saves=saves, native=native)


def preview(f):
    with window_module.require_active_window(f.journal, f.root, f.hermes) as active:
        return pair.plan(ROOT, f.root, f.hermes, window=active, generation=pair.PR119_GENERATION)[0]


def acceptance(proposal):
    # Labels are test data, NOT an actual independent runtime approval.
    return {**copy.deepcopy(proposal), 'schema': 'glp44-scoped-adoption-review-v1',
            'approved_source': pair.PR119_SOURCE, 'decision': 'ACCEPT_EXACT_BYTES',
            'prepared_by': 'fixture preparer', 'reviewer': 'fixture independent source reviewer',
            'runtime_review': {'schema': 'glp333-reporting-runtime-review-v1',
                               'decision': 'ACCEPT_EXACT_BYTES', 'reviewer': 'fixture receiving reviewer',
                               'proposal_sha256': sha(json.dumps(proposal, sort_keys=True).encode())}}


def apply(f, review):
    return pair.adopt(ROOT, f.root, f.hermes, review, scheduler=f.scheduler,
                      window=f.journal, generation=pair.PR119_GENERATION)


def payloads(f):
    return {p: (p.read_bytes(), p.stat().st_mode & 0o777) for base in (f.root, f.hermes)
            for p in base.rglob('*') if p.is_file() and not p.name.endswith('.lock')}


def test_literal_inventory_adds_only_three_paths_and_old_guard_counterexample(tmp_path, monkeypatch):
    root, hermes = tmp_path / 'root', tmp_path / 'hermes'
    root.mkdir(); hermes.mkdir()
    supported = inventory.affected_paths(root, hermes)
    old = blob('f5e4220b1e01e4b2ff96204aae4ac9a2424deccd', 'scripts/glp44_scoped_adoption.py').decode()
    directory = tmp_path / 'old-controls'
    put(directory / 'glp44_scoped_adoption.py', old.encode())
    put(directory / 'glp73_paired_adoption.py', (ROOT / 'scripts/glp73_paired_adoption.py').read_bytes())
    monkeypatch.setattr(inventory, '__file__', str(directory / 'maintenance_inventory.py'))
    previous = inventory.affected_paths(root, hermes)
    assert supported - previous == {root / p for p in EXTRA}
    journal = tmp_path / 'journal.json'
    put(journal, json.dumps({'schema': window_module.SCHEMA}).encode())
    active = window_module.ActiveWindow(journal, SimpleNamespace(root=root, hermes=hermes))
    paths = {str(root / p): sha(b'old') for p in EXTRA}
    for path in paths:
        put(Path(path), b'old')
    before = {p: p.read_bytes() for p in [journal, *(Path(p) for p in paths)]}
    with pytest.raises(window_module.WindowError, match='window_file_update_outside_adoption_scope'):
        active.accept_file_updates(paths, {p: sha(b'new') for p in paths})
    assert {p: p.read_bytes() for p in before} == before
    # Unknown declaration names are ignored; dynamic expressions for the exact
    # supported name are refused, never evaluated or executed.
    put(directory / 'glp44_scoped_adoption.py', (old + "\nARBITRARY_PATHS = ('scripts/unknown.py',)\n").encode())
    assert inventory.affected_paths(root, hermes) == previous
    put(directory / 'glp44_scoped_adoption.py', (old + '\nPR119_PATHS = tuple(PR119_HASHES)\n').encode())
    with pytest.raises(ValueError):
        inventory.affected_paths(root, hermes)


def test_supported_preview_accept_apply_readback_and_no_replay(tmp_path, monkeypatch):
    f = generation_fixture(tmp_path, monkeypatch)
    try:
        before = payloads(f)
        proposal = preview(f)
        assert payloads(f) == before and not f.saves
        assert set(proposal['source_sha256']) == set(pair.PR119_PATHS)
        review = acceptance(proposal)
        old_jobs = json.loads(f.registry.read_text())['jobs']
        old_state = json.loads((f.root / syncer.STATE).read_text())
        result = apply(f, review)
        assert result['applied'] and len(f.saves) == 1
        jobs = json.loads(f.registry.read_text())['jobs']
        expected = copy.deepcopy(old_jobs)
        expected[0]['script_sha256'] = pair.PR119_HASHES[pair.SNAPSHOT]
        assert jobs == expected
        state = json.loads((f.root / syncer.STATE).read_text())
        expected_state = copy.deepcopy(old_state)
        expected_state['hashes'].update(pair.PR119_HASHES)
        assert state == expected_state
        for rel, expected_hash in pair.PR119_HASHES.items():
            assert sha((f.root / rel).read_bytes()) == expected_hash
            assert (f.root / rel).stat().st_mode & 0o777 == before[f.root / rel][1]
        for rel, expected_hash in pair.PR119_PREREQUISITES.items():
            assert sha((f.root / rel).read_bytes()) == expected_hash
        assert (f.hermes / 'scripts' / pair.WRAPPER).read_bytes() == blob(pair.PR119_SOURCE, pair.SNAPSHOT)
        assert (f.hermes / 'scripts' / pair.WRAPPER).stat().st_mode & 0o777 == 0o750
        custody = Path(result['custody'])
        for path, (body, mode) in before.items():
            if path in {f.root / p for p in pair.PR119_PATHS} | {f.registry, f.root / syncer.STATE, f.hermes / 'scripts' / pair.WRAPPER}:
                assert (custody / (sha(body) + '.bytes')).read_bytes() == body
        assert window_module.read_journal(f.journal)['source_transaction']['outcome'] == 'applied'
        after = payloads(f)
        with pytest.raises(RuntimeError, match='reporting_generation_predecessor_refused'):
            apply(f, review)
        assert len(f.saves) == 1 and payloads(f) == after
        assert f.window.status()['verified']
        if f.native:
            native_sha = sha(Path(f.scheduler.__file__).read_bytes())
            print('NATIVE_SAVER_SHA256=' + native_sha)
            print('NATIVE_EXACT_COHORT_PREVIEW_ACCEPT_APPLY_READBACK_NO_REPLAY=PASS')
            receipt = {
                'fixture_only': True,
                'head': proposal['producer_commit'],
                'proposal': proposal,
                'fixture_review': review,
                'result': result,
                'native_scheduler_sha256': native_sha,
                'native_save_calls': len(f.saves),
                'verified': ['preview_no_writes', 'exact_cohort', 'native_hash_only_save',
                             'full_record_readback', 'custody', 'modes', 'no_replay'],
            }
            destination = os.environ.get('GLP333_FIXTURE_RECEIPT')
            if destination:
                Path(destination).write_text(json.dumps(receipt, indent=2, sort_keys=True) + '\n')
    finally:
        f.window.release_locks()


@pytest.mark.parametrize('mutation', ['extra_cohort', 'runtime_binding', 'self_review', 'job_record',
                                      'source_mode', 'retained_mode', 'registry_mode', 'analytics_baseline'])
def test_exact_review_and_prerequisite_refusals_before_payload_writes(tmp_path, monkeypatch, mutation):
    f = generation_fixture(tmp_path, monkeypatch)
    try:
        review = acceptance(preview(f))
        if mutation == 'extra_cohort':
            review['source_sha256']['scripts/unknown.py'] = '0' * 64
        elif mutation == 'runtime_binding':
            review['runtime_review']['proposal_sha256'] = '0' * 64
        elif mutation == 'self_review':
            review['runtime_review']['reviewer'] = review['prepared_by']
        elif mutation == 'job_record':
            doc = json.loads(f.registry.read_text())
            doc['jobs'][0]['fixture_unknown_total'] = 0
            put(f.registry, json.dumps(doc).encode())
        elif mutation == 'source_mode':
            (f.root / pair.PR119_PATHS[0]).chmod(0o644)
        elif mutation == 'retained_mode':
            (f.root / 'scripts/affiliate_network_access.py').chmod(0o644)
        elif mutation == 'registry_mode':
            f.registry.chmod(0o640)
        else:
            state = json.loads((f.root / syncer.STATE).read_text())
            state['hashes']['systems/analytics-brain/analytics_brain.py'] = '0' * 64
            put(f.root / syncer.STATE, json.dumps(state).encode())
        before = payloads(f)
        refusal = ('active_maintenance_window_required' if mutation == 'job_record' else
                   'reviewed_preimage|independent_matching|prerequisite')
        with pytest.raises(RuntimeError, match=refusal):
            apply(f, review)
        assert not f.saves and payloads(f) == before
        assert not (f.root / 'reports/ops/pin-archive').exists()
    finally:
        f.window.release_locks()


def test_unknown_generation_and_unknown_window_path_refuse(tmp_path, monkeypatch):
    assert pair.generation_contract('pr71') == (pair.APPROVED, pair.PATHS)
    with pytest.raises(RuntimeError, match='unknown_reporting_generation'):
        pair.generation_contract('caller-defined')
    f = generation_fixture(tmp_path, monkeypatch)
    try:
        proposal = preview(f)
        unknown = f.root / 'scripts/unknown.py'
        put(unknown, b'old')
        journal = f.journal.read_bytes()
        with window_module.require_active_window(f.journal, f.root, f.hermes) as active:
            with pytest.raises(window_module.WindowError, match='window_file_update_outside_adoption_scope'):
                active.accept_file_updates({str(unknown): sha(b'old')}, {str(unknown): sha(b'new')})
        assert unknown.read_bytes() == b'old' and not f.saves
        assert set(proposal['source_sha256']) == set(pair.PR119_PATHS)
        assert window_module.read_journal(f.journal).get('source_updates') is None
    finally:
        f.window.release_locks()


def test_uncertain_native_save_is_not_rolled_back_or_replayed(tmp_path, monkeypatch):
    f = generation_fixture(tmp_path, monkeypatch)
    try:
        review = acceptance(preview(f))
        saver = f.scheduler._save_jobs_unlocked
        def land_then_raise(jobs):
            saver(jobs)
            raise RuntimeError('fixture save landed acknowledgement lost')
        monkeypatch.setattr(f.scheduler, '_save_jobs_unlocked', land_then_raise)
        with pytest.raises(RuntimeError, match='adoption_outcome_uncertain_reconcile_before_exit') as refused:
            apply(f, review)
        assert 'acknowledgement lost' in str(refused.value.__context__)
        assert len(f.saves) == 1
        assert window_module.read_journal(f.journal)['source_transaction']['outcome'] == 'uncertain'
        after = payloads(f)
        with pytest.raises(RuntimeError, match='active_maintenance_window_required'):
            apply(f, review)
        assert payloads(f) == after and len(f.saves) == 1
        assert all(sha((f.root / p).read_bytes()) == h for p, h in pair.PR119_HASHES.items())
    finally:
        f.window.release_locks()
