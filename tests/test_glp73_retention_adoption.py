"""Bounded disposable-home adoption; native storage only, never execute a job.

Run each case in a fresh interpreter: cron binds its home on first import.
The public pinned scheduler used by existing native CI is also supported.
"""
import copy
import inspect
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import glp73_paired_adoption as pair
import maintenance_inventory as inventory
import maintenance_window as mw

GENERATION = 'pr136-retention-20261001'
SOURCE = '95660fd8b8f9ec07e46d90c92a77846776c11c88'
ARCHIVER = 'scripts/reports_archiver.py'
BEFORE = 'e305cd57898e49b2dc57e3486aade3dc22c361b81844cf667c492c725a730020'
AFTER = 'd4c2a0bfd0bf4944e3d23da77f047ac09d3abb21c4ad7f188f819a1a66317124'
SCHEMA = 'scripts/glp_current_content_base_schema.py'
SCHEMA_HASH = 'e1c2c57c90f08503742a1d0ad7256c10f89473e39f1feb7c717764e749bbc479'
SUCCESSOR = 'pr154-retention-20261002'
SUCCESSOR_SOURCE = 'e0df91e1d5b40faee1966527a677fa788dabae8e'
SUCCESSOR_HASH = 'ee6eab73a99a9cb35741cd5ec75f3d39ea588e986a7ddd9bc3ab2f52733680d1'


def blob(ref, rel):
    return subprocess.check_output(['git', '-C', str(ROOT), 'show', ref + ':' + rel])


def put(path, body, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    path.chmod(mode)


def exact(path):
    st = path.stat()
    return path.read_bytes(), stat.S_IMODE(st.st_mode), st.st_uid, st.st_gid


def fixture(tmp_path, monkeypatch, *, generation=GENERATION):
    root, home, repo = (tmp_path / name for name in ('business', 'hermes', 'checkout'))
    root.mkdir(); home.mkdir()
    successor = generation == SUCCESSOR
    source = SUCCESSOR_SOURCE if successor else SOURCE
    predecessor = AFTER if successor else BEFORE
    put(root / ARCHIVER, blob(SOURCE if successor else pair.SOURCE, ARCHIVER))
    put(root / SCHEMA, blob(SOURCE, SCHEMA), 0o644)
    assert pair.sha((root / ARCHIVER).read_bytes()) == predecessor
    assert pair.sha((root / SCHEMA).read_bytes()) == SCHEMA_HASH
    put(root / 'scripts/unselected.py', b'# retained unrelated source\n', 0o640)
    state = {'schema': 1, 'hashes': {ARCHIVER: predecessor, SCHEMA: SCHEMA_HASH,
             'scripts/unselected.py': pair.sha(b'# retained unrelated source\n')},
             'receipt': 'disposable fixture only'}
    put(root / pair.syncer.STATE, (json.dumps(state) + '\n').encode())
    jobs = []
    for i, (jid, name, old, new, mode) in enumerate(pair.JOBS):
        body = blob(SOURCE, pair.SNAPSHOTS + '/' + name)
        if i == 0:
            updates = {n: (body.splitlines()[n - 1].decode().split('"')[-2],
                       pair.RETAINED[p] if p in pair.RETAINED else pair.sha(blob(pair.SOURCE, p)))
                       for n, p in pair.WEEKLY.items()}
        else:
            updates = {16 if i == 1 else 19: (pair.SOURCE_BEFORE[13][0], BEFORE),
                       20 if i == 1 else 23: (pair.SOURCE_BEFORE[0][0], SCHEMA_HASH)}
        body = pair.replace_lines(body, updates)
        assert pair.sha(body) == new
        if successor and i != 0:
            body = pair.replace_lines(body, {16 if i == 1 else 19: (BEFORE, AFTER)})
            new = pair.RETENTION_JOBS[i - 1][3]
            assert pair.sha(body) == new
        put(home / 'scripts' / name, body, mode)
        job = {'id': jid, 'script': name, 'script_sha256': new, 'enabled': False,
               'state': 'paused', 'paused_reason': 'fixture hold', 'no_agent': True,
               'schedule': {'kind': 'cron', 'expr': f'{i} 6 * * 1'},
               'next_run_at': '2026-10-05T06:00:00Z', 'unknown_optional': {'keep': None}}
        if i == 1: job.update(run_claim=None, fire_claim=None, running=None)
        jobs.append(job)
    jobs.append({'id': 'unrelated', 'enabled': False, 'state': 'paused', 'no_agent': True,
                 'script': 'unrelated.sh', 'script_sha256': pair.sha(b'echo unrelated\n'),
                 'schedule': 'daily', 'paused_reason': 'provider hold'})
    put(home / 'scripts/unrelated.sh', b'echo unrelated\n', 0o700)
    registry = home / 'cron/jobs.json'
    put(registry, (json.dumps({'jobs': jobs, 'updated_at': 'fixture'}) + '\n').encode())
    subprocess.run(['git', 'clone', '--quiet', '--shared', str(ROOT), str(repo)], check=True)
    manifest = json.loads((ROOT / pair.syncer.MANIFEST).read_bytes())
    for rel in manifest['files']:
        if not (repo / rel).exists() or (repo / rel).read_bytes() != (ROOT / rel).read_bytes():
            put(repo / rel, (ROOT / rel).read_bytes(), (ROOT / rel).stat().st_mode & 0o777)
    # Historical success uses authentic PR136 bytes, not the changed worktree.
    put(repo / ARCHIVER, blob(source, ARCHIVER))
    manifest['files'][ARCHIVER]['sha256'] = pair.sha(blob(source, ARCHIVER))
    put(repo / pair.syncer.MANIFEST, (json.dumps(manifest, indent=2) + '\n').encode(), 0o644)
    subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(repo), 'add', '-f', 'tests/test_glp73_retention_adoption.py'], check=True)
    # Worktree producers may be newer than the parent's committed manifest.
    # Rebuild the manifest against this fixture's real index before committing.
    from test_glp333_reporting_generation import commit_fixture_tree
    commit_fixture_tree(repo)
    native = Path(os.environ.get('GLP73_NATIVE_CRON_SOURCE', '/home/ubuntu/.hermes/hermes-agent'))
    assert (native / 'cron/jobs.py').is_file(), 'real native fixture dependency required'
    assert not any(k == 'cron' or k.startswith('cron.') for k in sys.modules)
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('HERMES_SKIP_ENV_FILE', '1')
    monkeypatch.syspath_prepend(str(native))
    from cron import jobs as scheduler
    assert scheduler._current_cron_store().jobs_file == registry
    assert Path(scheduler.__file__).resolve() == native.resolve() / 'cron/jobs.py'
    modules = ('cron.jobs', 'cron.constants', 'cron.env_settings', 'agent.secret_scope',
               'hermes_constants', 'hermes_time', 'utils')
    print('NATIVE_FIXTURE', json.dumps({'home': str(home), 'registry': str(registry),
          'source_hashes': {name: pair.sha(Path(sys.modules[name].__file__).read_bytes())
                            for name in modules}}, sort_keys=True))
    # Deny effects; only local Git fixture/history commands may spawn processes.
    def guard(event, args):
        if event in {'socket.connect', 'socket.bind', 'os.system', 'os.kill'}:
            raise AssertionError('effect denied: ' + event)
        if event == 'subprocess.Popen':
            assert Path(args[0]).name == 'git', 'only local Git is permitted'
            assert not set(args[1]) & {'fetch', 'push', 'pull'}, 'network Git denied'
        if event == 'open' and isinstance(args[0], (str, bytes)):
            assert not Path(os.fsdecode(args[0])).name.startswith('.env'), 'credential read denied'
    sys.addaudithook(guard)
    bridge = {'paused': False}
    def probe(argv):
        if argv == ['crontab', '-l'] or 'list-unit-files' in argv or 'list-units' in argv: return ''
        assert argv[3] == 'n8n-bridge.service'
        return ('LoadState=loaded\nActiveState=active\nUnitFileState=enabled\nMainPID=123\n'
                'ExecStart={ path=/usr/bin/true ; argv[]=true ; ignore_errors=no ; }\n')
    put(root / 'ops/n8n-bridge/actions.json', b'{}\n')
    class Backend(mw.LinuxBackend):
        def scheduler(self): return scheduler
        def inventory(self): return inventory.discover(root, home, probe=probe)
        def snapshot(self, entry):
            return {'fixture_paused': bridge['paused']} if entry['kind'] == 'n8n' else super().snapshot(entry)
        def pause(self, entry, prior):
            if entry['kind'] == 'n8n': bridge['paused'] = True
            else: super().pause(entry, prior)
        def paused(self, entry, prior):
            return bridge['paused'] if entry['kind'] == 'n8n' else super().paused(entry, prior)
        def cron_tool(self, action, jid):
            assert action == 'pause'
            scheduler._save_jobs_unlocked(scheduler.load_jobs())
        def inflight(self, entries): return []  # no launched job/reader in these disposable roots
    journal = tmp_path / 'window.json'
    backend = Backend(root, home, journal)
    locks = [tmp_path / 'source.lock', tmp_path / 'consumer.lock']
    window = mw.MaintenanceWindow(backend, journal, locks)
    monkeypatch.setattr(mw, 'LinuxBackend', lambda *_: backend)
    monkeypatch.setattr(mw, 'default_locks', lambda *_: locks)
    window.enter()
    assert window.status()['verified']
    assert scheduler.load_jobs() == jobs  # optional-field presence, weekly and holds are exact
    saves = []
    save = scheduler._save_jobs_unlocked
    def counted(candidate):
        saves.append(copy.deepcopy(candidate)); return save(candidate)
    monkeypatch.setattr(scheduler, '_save_jobs_unlocked', counted)
    return SimpleNamespace(root=root, home=home, repo=repo, registry=registry, jobs=jobs,
                           journal=journal, window=window, scheduler=scheduler, saves=saves)


def test_narrow_preview_reproduces_and_avoids_wrong_cohort(tmp_path, monkeypatch):
    f = fixture(tmp_path, monkeypatch)
    try:
        with pytest.raises(RuntimeError, match='preimage_or_mode_moved:.*glp_current_content_base_schema.py'):
            pair.plan(f.repo, f.root, f.home)
        # On base this executes the actual old refusal, rather than an absent-API RED.
        kwargs = {'generation': GENERATION} if 'generation' in inspect.signature(pair.plan).parameters else {}
        before, stages, sources, records = pair.plan(f.repo, f.root, f.home, **kwargs)
        assert sources == {ARCHIVER: AFTER}
        assert set(records) == {'f3d2db07d83f', '69a28f878ce3'}
        assert {p.name for p in stages} == {'f12_generator_archiver.py', 'f13_growth_retention.py'}
    finally:
        f.window.release_locks()


def test_successor_accepts_fixed_bytes_but_pr136_and_tampering_refuse(tmp_path, monkeypatch, capsys):
    f = fixture(tmp_path, monkeypatch, generation=SUCCESSOR)
    try:
        before = payloads(f)
        planned = pair.plan(f.repo, f.root, f.home, generation=SUCCESSOR)
        assert planned[2] == {ARCHIVER: SUCCESSOR_HASH}
        assert {p.name: pair.sha(body) for p, body in planned[1].items()} == {
            'f12_generator_archiver.py': '27153229b42fa7901266166c86b8c0f6f6d0c925572f427f0ae5b21e6cf4a3aa',
            'f13_growth_retention.py': '4884ad0d2bba772472cc713907f94dc31560267d5850f591dca5a6cc7ebd5cb6'}
        proposal = pair.retention_proposal(f.repo, f.root, f.home, planned, generation=SUCCESSOR)
        assert proposal['generation'] == SUCCESSOR and proposal['source'] == SUCCESSOR_SOURCE
        assert proposal['source_before_sha256'] == {ARCHIVER: AFTER}
        capsys.readouterr()
        monkeypatch.setattr(sys, 'argv', ['glp73_paired_adoption.py', '--generation', SUCCESSOR,
                            '--checkout', str(f.repo), '--root', str(f.root), '--hermes', str(f.home)])
        assert pair.main() == 0
        assert json.loads(capsys.readouterr().out) == {'applied': False, 'proposal': proposal}
        with pytest.raises(RuntimeError, match='retention_approved_source_moved'):
            pair.plan(f.repo, f.root, f.home, generation=GENERATION)
        put(f.repo / ARCHIVER, blob(SUCCESSOR_SOURCE, ARCHIVER) + b'# tampered\n')
        with pytest.raises(RuntimeError, match='retention_approved_source_moved'):
            pair.plan(f.repo, f.root, f.home, generation=SUCCESSOR)
        assert payloads(f) == before and not f.saves
    finally:
        f.window.release_locks()


def review(f, *, generation=GENERATION):
    planned = pair.plan(f.repo, f.root, f.home, generation=generation)
    proposal = pair.retention_proposal(f.repo, f.root, f.home, planned, generation=generation)
    return {'schema': 'glp73-retention-adoption-review-v1', 'decision': 'ACCEPT_EXACT_BYTES',
            'prepared_by': 'fixture author', 'reviewer': 'fixture independent reviewer',
            'proposal': proposal,
            'runtime_review': {'schema': 'glp73-retention-runtime-review-v1',
                              'decision': 'ACCEPT_EXACT_BYTES', 'reviewer': 'fixture receiver',
                              'proposal_sha256': pair.sha(json.dumps(proposal, sort_keys=True).encode())}}


def apply(f, receipt, *, generation=GENERATION):
    return pair.adopt(f.repo, f.root, f.home, receipt, scheduler=f.scheduler,
                      window=f.journal, generation=generation)


def payloads(f):
    paths = [f.root / ARCHIVER, f.root / SCHEMA, f.root / pair.syncer.STATE,
             f.root / 'scripts/unselected.py', f.registry]
    paths += list((f.home / 'scripts').iterdir())
    return {p: exact(p) for p in paths}


def test_successor_keeps_review_preimage_and_window_guards(tmp_path, monkeypatch):
    f = fixture(tmp_path, monkeypatch, generation=SUCCESSOR)
    try:
        receipt = review(f, generation=SUCCESSOR)
        before = payloads(f)
        for change in ('old_generation', 'self_review', 'stale_runtime'):
            moved = copy.deepcopy(receipt)
            if change == 'old_generation': moved['proposal']['generation'] = GENERATION
            elif change == 'self_review': moved['reviewer'] = moved['prepared_by']
            else: moved['runtime_review']['proposal_sha256'] = '0' * 64
            with pytest.raises(RuntimeError, match='independent_exact_source_and_runtime_review_required_or_moved'):
                apply(f, moved, generation=SUCCESSOR)
            assert payloads(f) == before and not f.saves
        with pytest.raises(RuntimeError, match='active_maintenance_window_required'):
            pair.adopt(f.repo, f.root, f.home, receipt, scheduler=f.scheduler, generation=SUCCESSOR)
        with pytest.raises(RuntimeError, match='retention_generation_does_not_rebaseline'):
            pair.plan(f.repo, f.root, f.home, generation=SUCCESSOR, state_rebaseline={})
        with pytest.raises(RuntimeError, match='retention_generation_does_not_rebaseline'):
            pair.adopt(f.repo, f.root, f.home, receipt, scheduler=f.scheduler, window=f.journal,
                       generation=SUCCESSOR, state_rebaseline={})
        for path in (f.root / ARCHIVER, f.root / SCHEMA, f.root / pair.syncer.STATE,
                     f.home / 'scripts/f12_generator_archiver.py', f.home / 'scripts/f13_growth_retention.py'):
            original = exact(path)
            put(path, original[0] + b'\n', original[1])
            changed = payloads(f)
            with pytest.raises(RuntimeError): apply(f, receipt, generation=SUCCESSOR)
            assert payloads(f) == changed and not f.saves
            put(path, original[0], original[1])
        assert payloads(f) == before and not (f.root / 'reports').exists()
    finally:
        f.window.release_locks()


def test_exact_two_job_native_install_readback(tmp_path, monkeypatch):
    _native_install_readback(tmp_path, monkeypatch, GENERATION)


def test_successor_exact_two_job_native_install_readback(tmp_path, monkeypatch):
    _native_install_readback(tmp_path, monkeypatch, SUCCESSOR)


def _native_install_readback(tmp_path, monkeypatch, generation):
    f = fixture(tmp_path, monkeypatch, generation=generation)
    source = SUCCESSOR_SOURCE if generation == SUCCESSOR else SOURCE
    predecessor = AFTER if generation == SUCCESSOR else BEFORE
    after = SUCCESSOR_HASH if generation == SUCCESSOR else AFTER
    try:
        receipt = review(f, generation=generation)
        before = payloads(f)
        state = json.loads(before[f.root / pair.syncer.STATE][0])
        expected_jobs = copy.deepcopy(f.jobs)
        expected_wrappers = receipt['proposal']['wrapper_sha256']
        for job in expected_jobs:
            if job['id'] in receipt['proposal']['job_before']:
                job['script_sha256'] = expected_wrappers[job['script']]
        original = f.scheduler._save_jobs_unlocked
        seen = []
        def sealed_save(candidate):
            data = mw.read_journal(f.journal)
            switch = data['source_switch']
            assert switch['state'] == 'commit_uncertain' and switch['commit_started']
            assert set(data['source_transaction']['job_ids']) == {'f3d2db07d83f', '69a28f878ce3'}
            for name in data['source_transaction']['after']:
                assert Path(name).stat().st_mode & 0o777 == 0
                with pytest.raises(PermissionError): Path(name).read_bytes()
            seen.append(copy.deepcopy(data))
            return original(candidate)
        monkeypatch.setattr(f.scheduler, '_save_jobs_unlocked', sealed_save)
        result = apply(f, receipt, generation=generation)
        assert result['applied'] and result['source'] == source and len(f.saves) == len(seen) == 1
        assert f.scheduler.load_jobs() == expected_jobs
        assert (f.root / ARCHIVER).read_bytes() == blob(source, ARCHIVER)
        state['hashes'][ARCHIVER] = after
        assert json.loads((f.root / pair.syncer.STATE).read_bytes()) == state
        changed = {f.root / ARCHIVER, f.root / pair.syncer.STATE, f.registry,
                   *(f.home / 'scripts' / name for name in expected_wrappers)}
        for path, value in before.items():
            if path not in changed: assert exact(path) == value
        for name, digest in expected_wrappers.items():
            path = f.home / 'scripts' / name
            assert pair.sha(path.read_bytes()) == digest
            # Only the archiver digest moves; schema, R2, payload, alert and binary pins remain exact.
            assert path.read_bytes() == before[path][0].replace(predecessor.encode(), after.encode(), 1)
            assert exact(path)[1:] == before[path][1:]
        admitted = {Path(name) for name in seen[0]['source_transaction']['after']}
        assert admitted == {f.root / ARCHIVER, f.root / pair.syncer.STATE,
                            *(f.home / 'scripts' / name for name in expected_wrappers)}
        assert admitted <= inventory.affected_paths(f.root, f.home)
        data = mw.read_journal(f.journal)
        assert data['source_transaction']['outcome'] == 'applied'
        assert data['source_switch']['commit_verified'] and data['source_switch']['state'] == 'published'
        assert f.window.status()['verified']
        with pytest.raises(RuntimeError): apply(f, receipt, generation=generation)
        assert len(f.saves) == 1
    finally:
        f.window.release_locks()


def test_outer_switch_ack_seals_new_ordinary_bytecode(tmp_path, monkeypatch):
    """R1: discard ordinary first-reader code, then challenge disk aliases anew."""
    import importlib.machinery
    import importlib.util
    import maintenance_cohort as cohort
    f = fixture(tmp_path, monkeypatch)
    try:
        receipt = review(f)
        source = f.root / ARCHIVER
        cache = Path(importlib.util.cache_from_source(str(source)))
        sibling = cache.parent / 'unselected.cpython-311.pyc'
        put(sibling, b'unselected cache stays exact', 0o640)
        sibling_before = exact(sibling)
        real_switch = mw.ActiveWindow.switch_files
        original_files = []
        emitted = []
        def lost(active, stage, before, **kwargs):
            real_switch(active, stage, before, **kwargs)
            original_files.append(copy.deepcopy(mw.read_journal(f.journal)['source_switch']['files']))
            prior = sys.dont_write_bytecode
            try:
                sys.dont_write_bytecode = False
                code = importlib.machinery.SourceFileLoader('disposable_archiver', str(source)).get_code('disposable_archiver')
                assert code is not None and cache.is_file()
                del code  # no execution or retained-memory consumer
                emitted.append((cache.read_bytes(), cohort.metadata(cache.stat()), cache.stat().st_mode & 0o777))
            finally:
                sys.dont_write_bytecode = prior
            raise OSError('independent outer switch acknowledgement lost')
        monkeypatch.setattr(mw.ActiveWindow, 'switch_files', lost)
        with pytest.raises(mw.WindowError, match='adoption_outcome_uncertain'):
            apply(f, receipt)
        data = mw.read_journal(f.journal)
        assert data['source_transaction']['outcome'] == 'uncertain'
        assert data['source_switch']['state'] == 'commit_uncertain'
        assert len(f.saves) == 1
        assert data['source_switch']['files'] == original_files[0]
        assert str(cache) not in original_files[0]
        for name in data['source_transaction']['after']:
            assert Path(name).stat().st_mode & 0o777 == 0
            with pytest.raises(PermissionError): Path(name).read_bytes()
        # The same fresh-loader first-read oracle: denial is success, not an
        # unhandled PermissionError in the original diagnostic observation.
        with pytest.raises(PermissionError):
            importlib.machinery.SourceFileLoader('disposable_archiver', str(source)).get_code('disposable_archiver')
        with pytest.raises(PermissionError): cache.read_bytes()
        assert cache.stat().st_mode & 0o777 == 0
        held = data['source_switch']['retention_ack_caches']
        assert set(held) == {str(cache)}
        item = held[str(cache)]
        assert item['sealed'] is True
        assert item['sha256'] == pair.sha(emitted[0][0])
        assert item['metadata'] == emitted[0][1] == cohort.metadata(cache.stat())
        assert item['mode'] == emitted[0][2]
        assert Path(item['custody']).parent == Path(data['source_switch']['custody'])
        assert Path(item['custody']).read_bytes() == emitted[0][0]
        assert exact(sibling) == sibling_before
        before_replay = f.journal.read_bytes(), cache.stat(), Path(item['custody']).read_bytes()
        with pytest.raises(RuntimeError): apply(f, receipt)
        assert len(f.saves) == 1
        assert before_replay == (f.journal.read_bytes(), cache.stat(), Path(item['custody']).read_bytes())
        with pytest.raises(mw.WindowError, match='restore_refused_source_or_adoption_outcome'):
            f.window.exit()
    finally:
        f.window.release_locks()


def test_stale_source_refuses_without_save(tmp_path, monkeypatch):
    f = fixture(tmp_path, monkeypatch)
    try:
        receipt = review(f)
        for path in (f.repo / ARCHIVER, f.root / ARCHIVER, f.root / SCHEMA):
            old = exact(path)
            put(path, b'# moved source\n', old[1])
            before = payloads(f)
            with pytest.raises(RuntimeError): apply(f, receipt)
            assert payloads(f) == before and not f.saves
            assert not (f.root / 'reports').exists()
            put(path, old[0], old[1])
    finally:
        f.window.release_locks()


def test_stale_full_job_record_refuses_without_save(tmp_path, monkeypatch):
    f = fixture(tmp_path, monkeypatch)
    try:
        receipt = review(f)
        # Adding an optional null field changes the exact full target record.
        moved = copy.deepcopy(f.jobs)
        moved[2]['run_claim'] = None
        put(f.registry, json.dumps({'jobs': moved}).encode())
        before = payloads(f)
        with pytest.raises(RuntimeError): apply(f, receipt)
        assert payloads(f) == before and not f.saves
        assert not (f.root / 'reports').exists()
    finally:
        f.window.release_locks()


def test_stale_state_refuses_without_save(tmp_path, monkeypatch):
    f = fixture(tmp_path, monkeypatch)
    try:
        receipt = review(f)
        state = json.loads((f.root / pair.syncer.STATE).read_bytes())
        state['receipt'] = 'unrelated baseline writer advanced'
        put(f.root / pair.syncer.STATE, json.dumps(state).encode())
        before = payloads(f)
        with pytest.raises(RuntimeError): apply(f, receipt)
        assert payloads(f) == before and not f.saves
        assert not (f.root / 'reports').exists()
    finally:
        f.window.release_locks()


def test_precommit_failure_rolls_back_source_and_bytecode(tmp_path, monkeypatch):
    f = fixture(tmp_path, monkeypatch)
    try:
        receipt = review(f)
        source = f.root / ARCHIVER
        caches = [source.with_suffix('.pyc'), source.parent / '__pycache__' / 'reports_archiver.cpython-311.pyc']
        for p in caches: put(p, b'fixture retained bytecode', 0o640)
        before = {**payloads(f), **{p: exact(p) for p in caches}}
        import maintenance_cohort as cohort
        replace = cohort.os.replace
        count = []
        def interrupted(src, dst):
            result = replace(src, dst)
            if Path(src).name.startswith('.glp-cohort-') and not Path(src).name.startswith('.glp-cohort-rollback-'):
                count.append(dst)
                if len(count) == 1: raise OSError('fixture switch acknowledgement lost before commit')
            return result
        monkeypatch.setattr(cohort.os, 'replace', interrupted)
        with pytest.raises(OSError, match='before commit'): apply(f, receipt)
        assert {p: exact(p) for p in before} == before and not f.saves
        data = mw.read_journal(f.journal)
        assert data['source_switch']['state'] == 'rolled_back'
        assert not data['source_switch'].get('commit_started')
        assert data['source_transaction']['outcome'] == 'rolled_back'
        assert all(Path(item['original']).is_file() for item in data['source_switch']['files'].values())
    finally:
        f.window.release_locks()


@pytest.mark.parametrize('fault', ['save_refused', 'lost_save_ack', 'bad_readback', 'lost_switch_ack'])
def test_postcommit_uncertainty_seals_and_refuses_replay(tmp_path, monkeypatch, fault):
    f = fixture(tmp_path, monkeypatch)
    try:
        receipt = review(f)
        source = f.root / ARCHIVER
        cache = source.with_suffix('.pyc')
        put(cache, b'fixture retained bytecode', 0o640)
        registry_before = f.registry.read_bytes()
        original = f.scheduler._save_jobs_unlocked
        attempts = []
        def save(candidate):
            attempts.append(copy.deepcopy(candidate))
            data = mw.read_journal(f.journal)
            assert data['source_switch']['commit_started']
            assert all(Path(p).stat().st_mode & 0o777 == 0 for p in data['source_transaction']['after'])
            if fault == 'save_refused': raise OSError('fixture saver refused')
            original(candidate)
            if fault == 'lost_save_ack': raise OSError('fixture saver acknowledgement lost')
            if fault == 'bad_readback':
                value = json.loads(f.registry.read_bytes()); value['jobs'][1]['unknown_optional'] = 'drift'
                put(f.registry, json.dumps(value).encode())
        monkeypatch.setattr(f.scheduler, '_save_jobs_unlocked', save)
        if fault == 'lost_switch_ack':
            real_switch = mw.ActiveWindow.switch_files
            def lost(active, stage, before, **kwargs):
                real_switch(active, stage, before, **kwargs)
                raise OSError('fixture switch acknowledgement lost after commit')
            monkeypatch.setattr(mw.ActiveWindow, 'switch_files', lost)
        with pytest.raises(mw.WindowError, match='adoption_outcome_uncertain'): apply(f, receipt)
        data = mw.read_journal(f.journal)
        assert data['source_transaction']['outcome'] == 'uncertain'
        assert data['source_switch']['state'] == 'commit_uncertain'
        assert data['source_switch']['commit_started']
        for p in data['source_transaction']['after']:
            assert Path(p).stat().st_mode & 0o777 == 0
            with pytest.raises(PermissionError): Path(p).read_bytes()
        assert not cache.exists()
        for name, item in data['source_switch']['files'].items():
            assert pair.sha(Path(item['original']).read_bytes()) == item['before']
            if 'candidate' in item: assert pair.sha(Path(item['candidate']).read_bytes()) == item['after']
        assert len(attempts) == 1
        if fault == 'save_refused': assert f.registry.read_bytes() == registry_before
        elif fault != 'bad_readback': assert f.scheduler.load_jobs() == attempts[0]
        files = {p: Path(p).stat() for p in data['source_transaction']['after']}
        with pytest.raises(RuntimeError): apply(f, receipt)
        assert len(attempts) == 1
        assert {p: Path(p).stat() for p in files} == files
        with pytest.raises(mw.WindowError, match='restore_refused_source_or_adoption_outcome'):
            f.window.exit()
    finally:
        f.window.release_locks()
