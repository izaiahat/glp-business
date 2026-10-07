"""Fixed F08/A4 pin-chain fixtures. No provider calls or live job execution."""
import importlib.util
import json
from pathlib import Path
import sys
import copy
import subprocess
from types import SimpleNamespace

import pytest


def put(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    path.chmod(0o600)


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    import glp345_a4_adoption as a4
    root, home = tmp_path / 'business', tmp_path / 'hermes'
    m = (ROOT / a4.MAP_SNAPSHOT).read_bytes()
    w = (ROOT / a4.WRAPPER_SNAPSHOT).read_bytes()
    # Only fixture prerequisites are stand-ins. Real maintained pin derivation,
    # preimage checks, registry selection and transaction logic remain intact.
    deps = {**a4.wrapper_pins(w), **{v['path']: v['sha256'] for v in json.loads(m).values()}}
    for path in deps:
        put(root / Path(path).relative_to(a4.BUSINESS), b'# offline retained dependency\n')
    old = subprocess.check_output(['git', '-C', str(ROOT), 'show', a4.SOURCE + '^:' + a4.MONITOR])
    new = subprocess.check_output(['git', '-C', str(ROOT), 'show', a4.SOURCE + ':' + a4.MONITOR])
    assert a4.sha(old) == a4.BEFORE and a4.sha(new) == a4.AFTER
    put(root / a4.MONITOR, old)
    put(root / a4.MAP, m)
    put(home / 'scripts' / a4.WRAPPER, w)
    state = {'schema': 1, 'hashes': {a4.MONITOR: a4.BEFORE, 'untouched': 'a' * 64}}
    put(root / 'ops/.rebuild-sync-state.json', json.dumps(state).encode())
    target = dict(id=a4.JOB, script=a4.WRAPPER, script_sha256=a4.WRAPPER_BEFORE,
                  enabled=False, state='paused', paused_reason='fixture window', no_agent=True,
                  run_claim=None, fire_claim=None, schedule={'kind': 'cron', 'expr': '30 9 * * 1'},
                  next_run_at='2026-10-05T09:30:00-07:00', unusual={'retain': [None]})
    jobs = [target, {'id': 'other', 'enabled': False, 'paused_reason': 'provider hold'}]
    put(home / 'cron/jobs.json', json.dumps({'jobs': jobs}).encode())
    # The Git binding is tested independently against the committed checkout.
    monkeypatch.setattr(a4, 'authenticate', lambda checkout: ('f' * 40, new), raising=False)
    original = copy.deepcopy(target)
    original.update(enabled=True, state='scheduled', paused_reason=None)
    window = SimpleNamespace(journal=tmp_path / 'window.json', original_job=lambda jid: copy.deepcopy(original))
    put(window.journal, json.dumps({'created_at': 1, 'keeper': {'pid': 123}, 'adopter': None}).encode())
    return root, home, window, target


def test_plan_contains_full_pin_chain_and_preserves_unselected_state(isolated):
    import glp345_a4_adoption as a4
    root, home, window, target = isolated
    assert callable(getattr(a4, 'plan', None)), 'missing exact-preimage A4 plan'
    proposal, observed, stage = a4.plan(ROOT, root, home, 'fixture author', window=window)
    assert set(stage) == {root / a4.MONITOR, root / a4.MAP,
                          root / 'ops/.rebuild-sync-state.json', home / 'scripts' / a4.WRAPPER}
    assert proposal['job_before'] == target
    assert proposal['job_original']['enabled'] is True
    state = json.loads(stage[root / 'ops/.rebuild-sync-state.json'])
    assert state == {'schema': 1, 'hashes': {a4.MONITOR: a4.AFTER, 'untouched': 'a' * 64}}
    assert proposal['weekly_residual_drift']  # explicit; never "weekly green"
    for path, value in observed.items():
        assert path.read_bytes() == value[0]  # preview is read-only

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))


def test_monitor_map_wrapper_chain_preserves_every_other_byte():
    spec = importlib.util.find_spec('glp345_a4_adoption')
    assert spec is not None, 'missing maintained monitor -> JSON -> wrapper producer'
    import glp345_a4_adoption as a4
    before = (ROOT / a4.MAP_SNAPSHOT).read_bytes()
    wrapper = (ROOT / a4.WRAPPER_SNAPSHOT).read_bytes()
    emitted, carrier = a4.stage_pins(before, wrapper)
    assert emitted == before.replace(a4.BEFORE.encode(), a4.AFTER.encode(), 1)
    assert carrier == wrapper.replace(a4.MAP_BEFORE.encode(), a4.sha(emitted).encode(), 1)
    original, candidate = json.loads(before), json.loads(emitted)
    assert set(original) == set(candidate) == {'monitor', 'economics', 'formatter', 'microsoft', 'posthog'}
    assert candidate['monitor']['sha256'] == a4.AFTER
    for key in set(original) - {'monitor'}:
        assert candidate[key] == original[key]
    # These are real dependencies of the unchanged live weekly caller, NOT the
    # incompatible reduced wrapper in the old curated snapshot.
    assert b'scripts/result_contract.py' in carrier
    assert b'briefs/plan/A3-CAMPAIGNS-THREE-PLATFORMS.md' in carrier
    assert b'weekly_ads_loop.py' in carrier


def test_maintained_inventory_admits_exact_new_chain(isolated):
    import glp345_a4_adoption as a4
    import maintenance_inventory as inv
    root, home, _, _ = isolated
    assert {root / p for p in a4.PATHS} | {home / 'scripts' / a4.WRAPPER} <= inv.affected_paths(root, home)


def fixture_review(proposal):
    from datetime import datetime, timezone
    import maxbounty_pin_release as p
    return {'schema': 'glp345-a4-review-v1', 'decision': 'ACCEPT_EXACT_BYTES',
            'reviewer': 'fixture independent', 'proposal': copy.deepcopy(proposal),
            'reviewed_at_utc': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'writer_exclusion': 'isolated fixture, no live authority',
            'evidence': ['fixture only'],
            'runtime_review': {'decision': 'ACCEPT_EXACT_BYTES', 'reviewer': 'fixture receiver',
                               'proposal_sha256': p.sha_bytes(p.json_bytes(proposal))}}


@pytest.mark.parametrize('outcome', ['ok', 'lost_ack', 'bad_readback'])
def test_guarded_switch_native_pin_and_no_replay(isolated, tmp_path, monkeypatch, outcome,
                                                *, continuous=False, inspect_phase=lambda _: None,
                                                generation=None):
    import os
    import glp345_a4_adoption as a4
    import maintenance_inventory as inv
    import maintenance_window as mw
    from test_glp333_reporting_generation import PortableScheduler
    root, home, _, target = isolated
    registry = home / 'cron/jobs.json'
    jobs = json.loads(registry.read_bytes())['jobs']
    jobs[0].update(enabled=True, state='scheduled', paused_reason=None)
    put(registry, json.dumps({'jobs': jobs}).encode())
    native = os.environ.get('GLP345_NATIVE_CRON_SOURCE')
    if native:
        assert not any(n == 'cron' or n.startswith('cron.') for n in sys.modules)
        monkeypatch.setenv('HERMES_HOME', str(home))
        monkeypatch.setenv('HERMES_SKIP_ENV_FILE', '1')
        monkeypatch.syspath_prepend(native)
        from cron import jobs as scheduler
        assert scheduler._current_cron_store().jobs_file == registry
        print('NATIVE_SOURCE', scheduler.__file__, a4.sha(Path(scheduler.__file__).read_bytes()))
    else:
        scheduler = PortableScheduler(registry)
    put(root / 'ops/n8n-bridge/actions.json', b'{}\n')
    bridge = {'paused': False}
    def probe(argv):
        if argv == ['crontab', '-l'] or 'list-unit-files' in argv or 'list-units' in argv:
            return ''
        return ('LoadState=loaded\nActiveState=active\nUnitFileState=enabled\nMainPID=123\n'
                'ExecStart={ path=/usr/bin/true ; argv[]=true ; ignore_errors=no ; }\n')
    class Backend(mw.LinuxBackend):
        def scheduler(self): return scheduler
        def inventory(self): return inv.discover(root, home, probe=probe)
        def snapshot(self, entry):
            return {'fixture_paused': bridge['paused']} if entry['kind'] == 'n8n' else super().snapshot(entry)
        def pause(self, entry, prior):
            if entry['kind'] == 'n8n': bridge['paused'] = True
            else: super().pause(entry, prior)
        def paused(self, entry, prior):
            return bridge['paused'] if entry['kind'] == 'n8n' else super().paused(entry, prior)
        def restore(self, entry, prior):
            if entry['kind'] == 'n8n': bridge['paused'] = prior['fixture_paused']
            else: super().restore(entry, prior)
        def cron_tool(self, action, jid):
            assert action == 'pause'
            current = json.loads(registry.read_bytes())['jobs']
            next(j for j in current if j['id'] == jid).update(enabled=False, state='paused')
            scheduler._save_jobs_unlocked(current)
        def inflight(self, entries): return []  # no tasks launched in disposable roots
    journal = tmp_path / 'active.json'
    backend = Backend(root, home, journal)
    locks = [tmp_path / 'consumer.lock', tmp_path / 'writer.lock']
    window = mw.MaintenanceWindow(backend, journal, locks)
    monkeypatch.setattr(mw, 'LinuxBackend', lambda *_: backend)
    monkeypatch.setattr(mw, 'default_locks', lambda *_: locks)
    from contextlib import ExitStack
    session = ExitStack()
    source_args = {}
    if continuous:
        owner = session.enter_context(a4.installation_session(window))
        source_args['_source_owner'] = owner
    window.enter()
    calls = []
    save = scheduler._save_jobs_unlocked
    try:
        with mw.require_active_window(journal, root, home) as active:
            proposal, before, stage = a4.plan(ROOT, root, home, 'fixture author', window=active,
                **({'generation': generation} if generation is not None else {}))
        inspect_phase('proposal_review')
        review = fixture_review(proposal)
        assert callable(getattr(a4, 'adopt', None)), 'missing reviewed paired A4 adoption'
        def apply(receipt=review):
            return a4.adopt(ROOT, root, home, receipt, scheduler=scheduler, window=journal,
                            **source_args)
        for mutation in (lambda r: r.update(reviewer='fixture author'),
                         lambda r: r['runtime_review'].update(proposal_sha256='0' * 64),
                         lambda r: r['proposal']['outputs'].update({'outside': '0' * 64})):
            invalid = copy.deepcopy(review); mutation(invalid)
            with pytest.raises(RuntimeError): apply(invalid)
        # Complete target CAS, but unrelated jobs may advance while review lands.
        current = json.loads(registry.read_bytes())['jobs']
        current[-1]['last_status'] = 'unrelated tick'
        put(registry, json.dumps({'jobs': current}).encode())
        def checked_save(candidate):
            calls.append(copy.deepcopy(candidate))
            data = mw.read_journal(journal)
            assert data['source_transaction']['job_ids'] == [a4.JOB]
            assert data['source_switch']['state'] == 'commit_uncertain'
            for path in stage:
                assert path.stat().st_mode & 0o777 == 0
                with pytest.raises(PermissionError): path.read_bytes()
            save(candidate)
            if outcome == 'lost_ack': raise OSError('fixture lost ack')
            if outcome == 'bad_readback':
                doc = json.loads(registry.read_bytes()); doc['jobs'][-1]['moved'] = True
                put(registry, json.dumps(doc).encode())
        monkeypatch.setattr(scheduler, '_save_jobs_unlocked', checked_save)
        if outcome == 'ok':
            result = apply()
            assert result['applied']
            expected = copy.deepcopy(current)
            expected[0]['script_sha256'] = a4.sha(stage[home / 'scripts' / a4.WRAPPER])
            assert json.loads(registry.read_bytes())['jobs'] == expected
            for path, body in stage.items():
                assert path.read_bytes() == body and path.stat().st_mode & 511 == before[path][1]
            monkeypatch.setattr(scheduler, '_save_jobs_unlocked', save)
            inspect_phase('before_restore')
            if continuous:
                session.close()  # maintained owner restores before releasing source EX
            else:
                window.exit()
            restored = json.loads(registry.read_bytes())['jobs']
            assert restored[0]['enabled'] is True and restored[0]['state'] == 'scheduled'
            assert restored[0]['schedule'] == jobs[0]['schedule']
            assert restored[0]['next_run_at'] == jobs[0]['next_run_at']
        else:
            with pytest.raises(Exception, match='adoption_outcome_uncertain'):
                apply()
            assert mw.read_journal(journal)['source_transaction']['outcome'] == 'uncertain'
            assert all(path.stat().st_mode & 511 == 0 for path in stage)
        with pytest.raises(RuntimeError, match='no_replay'): apply()
        assert len(calls) == 1
    finally:
        try:
            monkeypatch.setattr(scheduler, '_save_jobs_unlocked', save)
            session.close()
        finally:
            window.release_locks()


def test_review_window_excludes_second_supported_importer(isolated, tmp_path, monkeypatch):
    """Real competing CLI apply must not move shared state until restoration."""
    import os
    import glp345_a4_adoption as a4
    import maintenance_window as mw
    import sync_from_repo as syncer
    from test_sync_from_repo import checkout
    (tmp_path / 'importer').mkdir()
    repo, _ = checkout(tmp_path / 'importer')
    root, home, _, _ = isolated
    command = [sys.executable, '-B', str(ROOT / 'scripts/sync_from_repo.py'),
               '--root', str(root), '--checkout', str(repo), '--only',
               'scripts/sample.py', '--apply', '--wait', '0']
    def importer():
        return subprocess.run(command, text=True, capture_output=True, timeout=10,
                              env={**os.environ, 'GLP_SOURCE_LOCK_WAIT_S': '0'})
    phases = []
    def excluded(phase):
        before = (root / syncer.STATE).read_bytes()
        result = importer()
        assert result.returncode == 2 and 'source_import_busy' in result.stderr, (
            phase, result.returncode, result.stdout, result.stderr)
        assert (root / syncer.STATE).read_bytes() == before
        phases.append(phase)
    restore = mw.MaintenanceWindow.exit
    def checked_restore(window, *args, **kwargs):
        excluded('restore')
        result = restore(window, *args, **kwargs)
        excluded('restored_still_owned')
        return result
    monkeypatch.setattr(mw.MaintenanceWindow, 'exit', checked_restore)
    test_guarded_switch_native_pin_and_no_replay(isolated, tmp_path, monkeypatch,
        'ok', continuous=True, inspect_phase=excluded)
    assert phases == ['proposal_review', 'before_restore', 'restore', 'restored_still_owned']
    journal = mw.read_journal(tmp_path / 'active.json')
    assert journal['state'] == 'closed' and all(row['restored'] for row in journal['entries'])
    before = (root / syncer.STATE).read_bytes()
    result = importer()
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert (root / syncer.STATE).read_bytes() != before


@pytest.mark.parametrize('mutation', ['monitor', 'map', 'wrapper', 'state', 'claim', 'schedule'])
def test_moved_preimage_or_claim_refuses_without_writes(isolated, mutation):
    import glp345_a4_adoption as a4
    root, home, window, _ = isolated
    registry = home / 'cron/jobs.json'
    if mutation in ('claim', 'schedule'):
        doc = json.loads(registry.read_bytes())
        if mutation == 'claim': doc['jobs'][0]['fire_claim'] = {'owner': 'fixture inflight'}
        else: doc['jobs'][0]['schedule']['expr'] = '* * * * *'
        put(registry, json.dumps(doc).encode())
    else:
        path = {'monitor': root / a4.MONITOR, 'map': root / a4.MAP,
                'wrapper': home / 'scripts' / a4.WRAPPER,
                'state': root / 'ops/.rebuild-sync-state.json'}[mutation]
        if mutation == 'state':
            doc = json.loads(path.read_bytes()); doc['hashes'][a4.MONITOR] = '0' * 64
            put(path, json.dumps(doc).encode())
        else: put(path, path.read_bytes() + b'\n')
    before = {p: p.read_bytes() for r in (root, home) for p in r.rglob('*') if p.is_file()}
    with pytest.raises(RuntimeError): a4.plan(ROOT, root, home, 'fixture author', window=window)
    assert all(p.read_bytes() == b for p, b in before.items())


def test_new_window_invalidates_old_review(isolated):
    import glp345_a4_adoption as a4
    root, home, window, _ = isolated
    proposal = a4.plan(ROOT, root, home, 'fixture author', window=window)[0]
    review = fixture_review(proposal)
    doc = json.loads(window.journal.read_bytes()); doc['created_at'] = 2
    put(window.journal, json.dumps(doc).encode())
    fresh = a4.plan(ROOT, root, home, 'fixture author', window=window)[0]
    with pytest.raises(RuntimeError): a4.validate_review(review, fresh)


def test_stream_successor_preserves_historical_outputs_and_exact_edges():
    import glp345_a4_adoption as a4
    old = a4.stage_weekly_pins(*a4.stage_pins(
        (ROOT / a4.MAP_SNAPSHOT).read_bytes(),
        (ROOT / a4.WRAPPER_SNAPSHOT).read_bytes()),
        generation=a4.MANUAL_WEEKLY_GENERATION)
    assert tuple(map(a4.sha, old)) == (
        '5d83a280be12cdbf1f7449ac9a7c83d07e66027ad3c8be6eb4b57eaaeb61190a',
        '43532ad8c94c1dde3eac99abe42eb499fe831d1bc1328aa661b9184ea147206d')
    assert callable(getattr(a4, 'stage_stream_pins', None)), 'missing named coherent successor'
    emitted, carrier = a4.stage_stream_pins(*old)
    assert tuple(map(a4.sha, (emitted, carrier))) == (
        '0306e497a344075bcbf9606b09cfca158e263548095823feae13b1497d24cf40',
        '2e49f094fa1801f231e82897178c0f9be7d59e910eaefa5a48c2fe4839a55c4d')
    before, after = map(json.loads, (old[0], emitted))
    assert after['posthog']['sha256'] == a4.sha((ROOT / a4.RAILS).read_bytes())
    assert {k for k in before if before[k] != after[k]} == {'posthog'}
    bp, ap = map(a4.wrapper_pins, (old[1], carrier))
    assert {k for k in bp if bp[k] != ap[k]} == {str(a4.BUSINESS / a4.MAP)}
    for changed in ((old[0] + b' ', old[1]), (old[0], old[1] + b' ')):
        with pytest.raises(RuntimeError, match='unknown_stream_artifact_preimage'):
            a4.stage_stream_pins(*changed)


def test_stream_successor_declares_full_receiving_scope(isolated):
    import glp345_a4_adoption as a4
    import maintenance_inventory as inv
    root, home, _, _ = isolated
    required = {root / p for p in (a4.COLLECTOR, a4.RAILS, a4.WEEKLY_PINS,
                                  a4.WEEKLY_ANALYSIS, a4.MAP)}
    assert required <= inv.affected_paths(root, home)
    assert callable(getattr(a4, 'stream_sources', None)), 'missing coherent source proof'


@pytest.fixture
def stream_isolated(isolated, monkeypatch):
    """Host-only fixture bytes prove guards, never installed host identities."""
    import glp345_a4_adoption as a4
    root, home, window, target = isolated
    manual = a4.stage_weekly_pins(*a4.stage_pins(
        (ROOT / a4.MAP_SNAPSHOT).read_bytes(), (ROOT / a4.WRAPPER_SNAPSHOT).read_bytes()),
        generation=a4.MANUAL_WEEKLY_GENERATION)
    emitted, carrier = a4.stage_stream_pins(*manual)
    old, new = {}, {}
    for rel in (a4.COLLECTOR, a4.RAILS, a4.WEEKLY_PINS, a4.WEEKLY_ANALYSIS):
        old[rel] = subprocess.check_output(['git', '-C', str(ROOT), 'show', a4.STREAM_PREDECESSOR + ':' + rel])
        new[rel] = (ROOT / rel).read_bytes()
    pins = json.loads(new[a4.WEEKLY_PINS])
    for absolute in pins['paths']:
        rel = str(Path(absolute).relative_to(a4.BUSINESS))
        # Real curated bytes are retained; noncurated prerequisites are explicit
        # stand-ins. The unmodified runtime loops still cover every identity.
        body = new.get(rel, (ROOT / rel).read_bytes() if (ROOT / rel).is_file() else b'# host-only fixture\n')
        put(root / rel, old.get(rel, body))
        pins['paths'][absolute] = a4.sha(body)
    new[a4.WEEKLY_PINS] = json.dumps(pins).encode()
    deps = a4.wrapper_pins(carrier)
    m = json.loads(emitted)
    for row in m.values():
        deps[row['path']] = row['sha256']
    for absolute in deps:
        rel = str(Path(absolute).relative_to(a4.BUSINESS))
        if rel == a4.MAP:
            continue
        if rel == a4.RAILS:
            body = new[rel]
        else:
            body = (ROOT / rel).read_bytes() if (ROOT / rel).is_file() else b'# A4 host-only fixture\n'
            put(root / rel, body)
        deps[absolute] = a4.sha(body)
    for row in m.values():
        row['sha256'] = deps[row['path']]
    emitted = json.dumps(m).encode()
    deps[str(a4.BUSINESS / a4.MAP)] = a4.sha(emitted)
    # Pure exact projection is covered above. Relocate only fixture dependency
    # identities here so native/DAC guards can execute without host ingestion.
    carrier = ('PINS = ' + repr(deps) + '\n').encode()
    monkeypatch.setattr(a4, 'stage_stream_pins', lambda *_: (emitted, carrier))
    monkeypatch.setattr(a4, 'stream_sources', lambda _: (old, new))
    monkeypatch.setattr(a4, 'authenticate', lambda checkout, generation=None: ('f' * 40, new[a4.COLLECTOR]))
    for rel, body in old.items():
        put(root / rel, body)
    put(root / a4.MAP, manual[0])
    put(home / 'scripts' / a4.WRAPPER, manual[1])
    state = {'schema': 1, 'hashes': {rel: a4.sha(body) for rel, body in old.items()}}
    state['hashes'].update({a4.MAP: a4.STREAM_MAP_BEFORE, 'untouched': 'a' * 64})
    put(root / 'ops/.rebuild-sync-state.json', json.dumps(state).encode())
    doc = json.loads((home / 'cron/jobs.json').read_bytes())
    doc['jobs'][0]['script_sha256'] = a4.STREAM_WRAPPER_BEFORE
    put(home / 'cron/jobs.json', json.dumps(doc).encode())
    target['script_sha256'] = a4.STREAM_WRAPPER_BEFORE
    original = copy.deepcopy(target)
    original.update(enabled=True, state='scheduled', paused_reason=None)
    window.original_job = lambda jid: copy.deepcopy(original)
    return isolated


def test_stream_plan_seven_members_all_twenty_and_scoped_baselines(stream_isolated):
    import glp345_a4_adoption as a4
    root, home, window, target = stream_isolated
    proposal, observed, stage = a4.plan(ROOT, root, home, 'fixture author', window=window,
                                       generation=a4.STREAM_GENERATION)
    assert set(stage) == {root / p for p in a4.STREAM_PATHS} | {
        root / 'ops/.rebuild-sync-state.json', home / 'scripts' / a4.WRAPPER}
    assert proposal['weekly_residual_drift'] == []
    assert proposal['source_head'] == a4.STREAM_SOURCE
    assert proposal['job_before'] == target
    pins = json.loads(stage[root / a4.WEEKLY_PINS])['paths']
    assert len(pins) == 20
    assert all(root / Path(p).relative_to(a4.BUSINESS) in observed for p in pins)
    state = json.loads(stage[root / 'ops/.rebuild-sync-state.json'])
    assert state['hashes'] == {**{rel: a4.sha(stage[root / rel]) for rel in a4.STREAM_PATHS},
                               'untouched': 'a' * 64}
    assert all(p.read_bytes() == b[0] for p, b in observed.items())


@pytest.mark.parametrize('outcome', ['ok', 'lost_ack', 'bad_readback'])
def test_stream_sealed_switch_native_full_cas_restore_no_replay(
        stream_isolated, tmp_path, monkeypatch, outcome):
    import glp345_a4_adoption as a4
    test_guarded_switch_native_pin_and_no_replay(stream_isolated, tmp_path, monkeypatch,
        outcome, continuous=(outcome == 'ok'), generation=a4.STREAM_GENERATION)


@pytest.mark.parametrize('index', range(20))
def test_stream_plan_rejects_each_of_twenty_changed_prerequisites(stream_isolated, index):
    import glp345_a4_adoption as a4
    root, home, window, _ = stream_isolated
    _, new = a4.stream_sources(ROOT)
    absolute = sorted(json.loads(new[a4.WEEKLY_PINS])['paths'])[index]
    selected = root / Path(absolute).relative_to(a4.BUSINESS)
    selected.write_bytes(selected.read_bytes() + b'\n# drift\n')
    before = {p: p.read_bytes() for r in (root, home) for p in r.rglob('*') if p.is_file()}
    with pytest.raises(RuntimeError, match='weekly_dependency_drift|stream_source_preimage'):
        a4.plan(ROOT, root, home, 'fixture author', window=window, generation=a4.STREAM_GENERATION)
    assert all(p.read_bytes() == b for p, b in before.items())


@pytest.mark.parametrize('rel', ('systems/analytics-brain/collector.py', 'ops/glp/seo_data_rails.py',
                                'ops/glp/seo-weekly-installed-pins.json', 'scripts/glp_weekly_seo_analysis.py',
                                'reports/ops/orchestration-restore-20260907/PLAN/A4/SOURCE-PINS.json'))
def test_stream_plan_does_not_adopt_absent_baseline(stream_isolated, rel):
    import glp345_a4_adoption as a4
    root, home, window, _ = stream_isolated
    path = root / 'ops/.rebuild-sync-state.json'
    doc = json.loads(path.read_bytes()); del doc['hashes'][rel]
    path.write_text(json.dumps(doc))
    with pytest.raises(RuntimeError, match='baseline_moved'):
        a4.plan(ROOT, root, home, 'fixture author', window=window, generation=a4.STREAM_GENERATION)


def test_stream_committed_source_and_historical_generation_binding():
    import glp345_a4_adoption as a4
    head, body = a4.authenticate(ROOT, generation=a4.STREAM_GENERATION)
    assert head == subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD']).decode().strip()
    assert a4.sha(body) == a4.STREAM_COLLECTOR_AFTER
    old, new = a4.stream_sources(ROOT)
    before, after = map(json.loads, (old[a4.WEEKLY_PINS], new[a4.WEEKLY_PINS]))
    assert len(after['paths']) == 20
    assert {p for p in before['paths'] if before['paths'][p] != after['paths'][p]} == {str(a4.BUSINESS / a4.RAILS)}
    # Historical authentication continues refusing the moved raw rail. It does
    # not alias, repin, or silently redirect to the named successor.
    with pytest.raises(RuntimeError, match='approved_weekly_input_moved:ops/glp/seo_data_rails.py'):
        a4.authenticate(ROOT, generation=a4.MANUAL_WEEKLY_GENERATION)


def test_no_window_cannot_apply(isolated):
    import glp345_a4_adoption as a4
    root, home, _, _ = isolated
    with pytest.raises(RuntimeError, match='active_maintenance_window_required'):
        a4.adopt(ROOT, root, home, {}, scheduler=None)
