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
import subprocess

# Fixture effect-denial patches subprocess globally; preserve only this narrow
# scratch-root lease probe transport before those patches. No live CLI entry.
_LEASE_POPEN = subprocess.Popen
_LEASE_PROBE = """
import sys
sys.path.insert(0, sys.argv[1])
from sync_from_repo import source_lock
try:
    with source_lock(sys.argv[2]):
        print('ACQUIRED', flush=True)
        if sys.argv[3] == 'hold': sys.stdin.readline()
except ValueError as exc:
    if str(exc) != 'source_import_busy': raise
    print('BUSY', flush=True)
"""


def lease_process(root, *, hold=False):
    return _LEASE_POPEN([sys.executable, '-I', '-B', '-c', _LEASE_PROBE,
        str(Path(ep.__file__).parent), str(root), 'hold' if hold else 'probe'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env={**ep.os.environ, 'GLP_SOURCE_LOCK_WAIT_S': '0'})


def lease_result(root):
    process = lease_process(root)
    out, err = process.communicate(timeout=10)
    assert process.returncode == 0, err
    return out.strip()


def test_cleanup_c_reference_effects_exclude_real_receiving_process(owner, monkeypatch):
    observed = []
    def check(boundary):
        result = lease_result(ep.BUS)
        observed.append((boundary, result))
        assert result == 'BUSY', 'cleanup lacks the existing workspace lease: '+boundary
    def references(*args):
        check('C_first_reference_read')
        return [owner.source]
    def recovery(*args):
        check('C_retirement_recovery')
        return []
    def candidate(*args, **kwargs):
        check('C_dependent_archive_effect')
        return {'rule': 'C_cold_evidence_archive', 'ok': True, 'status': 'GATED_FIXTURE'}
    monkeypatch.setattr(ep, 'reference_files', references)
    monkeypatch.setattr(ep, 'recover_retirements', recovery)
    monkeypatch.setattr(ep, 'archive_tree', candidate)
    ep.run(owner.api, owner.config, owner.state, dry_run=False,
           deadline=time.monotonic()+60, archive_only=True)
    assert [name for name, _ in observed] == ['C_first_reference_read',
        'C_retirement_recovery', 'C_dependent_archive_effect']
    assert lease_result(ep.BUS) == 'ACQUIRED', 'lease leaked after cleanup return'


@pytest.mark.parametrize('phase', ['D', 'A', 'B'])
def test_cleanup_snapshot_effect_scopes_hold_existing_lease(owner, monkeypatch, phase):
    calls = []
    def effect(*args, **kwargs):
        calls.append(phase)
        assert lease_result(ep.BUS) == 'BUSY', 'snapshot-dependent '+phase+' lacks lease'
        return {'rule': phase+'_fixture', 'ok': True}
    if phase == 'D':
        monkeypatch.setitem(sys.modules, 'screenshot_pruner',
                            SimpleNamespace(run_rules=lambda *a, **k: [effect()]))
    elif phase == 'A':
        monkeypatch.setattr(ep, 'regenerable', lambda *a, **k: [effect()])
    else:
        monkeypatch.setattr(ep, 'dedupe', effect)
    rows, _ = execute(owner)
    assert calls == [phase]
    assert lease_result(ep.BUS) == 'ACQUIRED'


@pytest.mark.parametrize('phase', ['D', 'C'])
def test_receiving_owner_refuses_cleanup_before_snapshot(owner, monkeypatch, phase):
    monkeypatch.setenv('GLP_SOURCE_LOCK_WAIT_S', '0')
    def denied(*args, **kwargs):
        raise AssertionError('reference inventory/effects entered without admission')
    monkeypatch.setattr(ep, 'reference_files', denied)
    monkeypatch.setitem(sys.modules, 'screenshot_pruner', SimpleNamespace(run_rules=denied))
    history = copy.deepcopy(owner.state['C_done'])
    holder = lease_process(ep.BUS, hold=True)
    try:
        assert holder.stdout.readline().strip() == 'ACQUIRED'
        rows = ep.run(owner.api, owner.config, owner.state, dry_run=False,
                      deadline=time.monotonic()+60, archive_only=phase == 'C')
        assert rows[-1]['status'] == 'GATED_SOURCE_BUSY'
        assert rows[-1]['partial'] is True and rows[-1]['bytes_freed'] == 0
        assert owner.state['full_pass_complete'] is False
        assert owner.state['C_done'] == history
    finally:
        out, err = holder.communicate('\n', timeout=10)
        assert holder.returncode == 0, err
    assert lease_result(ep.BUS) == 'ACQUIRED'
    # A new fixture invocation must capture fresh source, not reuse refused pins.
    owner.source.write_text(json.dumps({'current_root': str(owner.root)}))
    monkeypatch.setattr(ep, 'reference_files', lambda *a: [owner.source])
    rows = ep.run(owner.api, owner.config, owner.state, dry_run=False,
                  deadline=time.monotonic()+60, archive_only=True)
    assert next(r for r in rows if r.get('path') == str(owner.root))['status'] == 'GATED_REFERENCED'


@pytest.mark.parametrize('failure', ['inventory', 'effect'])
def test_cleanup_lease_releases_on_gated_inventory_and_exception(owner, monkeypatch, failure):
    def failed(*args, **kwargs):
        assert lease_result(ep.BUS) == 'BUSY'
        if failure == 'inventory': raise ep.ScanBudget('fixture inventory budget')
        raise RuntimeError('fixture dependent effect refused')
    if failure == 'inventory':
        monkeypatch.setattr(ep, 'reference_files', failed)
        rows = ep.run(owner.api, owner.config, owner.state, dry_run=False,
                      deadline=time.monotonic()+60, archive_only=True)
        assert rows[-1]['status'] == 'GATED_REFERENCE_INVENTORY_INCOMPLETE'
    else:
        monkeypatch.setattr(ep, 'archive_tree', failed)
        with pytest.raises(RuntimeError, match='fixture dependent effect refused'):
            ep.run(owner.api, owner.config, owner.state, dry_run=False,
                   deadline=time.monotonic()+60, archive_only=True)
    assert lease_result(ep.BUS) == 'ACQUIRED'


def test_b_slices_rebuild_protection_after_real_receiving_checkpoint(owner, monkeypatch):
    from contextlib import contextmanager
    from sync_from_repo import source_lock
    scopes = 0
    snapshots = []
    policy = ep.BUS / 'ops/disk-retention-policy.json'
    @contextmanager
    def scheduled_receiving(root):
        nonlocal scopes
        scopes += 1
        number = scopes
        with source_lock(root) as acquired:
            yield acquired
        if number == 3:  # D, A, first B slice; custody has actually ended.
            holder = lease_process(root, hold=True)
            try:
                assert holder.stdout.readline().strip() == 'ACQUIRED'
                policy.write_text(json.dumps({'protected_roots': [str(owner.root)]}))
            finally:
                out, err = holder.communicate('\n', timeout=10)
                assert holder.returncode == 0, err
    def dedupe(*args, protected_roots, **kwargs):
        assert lease_result(ep.BUS) == 'BUSY'
        snapshots.append(protected_roots)
        return {'rule': 'B_fixture', 'ok': True,
                'status': 'TIME_BUDGET_PARTIAL' if len(snapshots) == 1 else 'COMPLETE'}
    monkeypatch.setattr(ep, 'source_lock', scheduled_receiving)
    monkeypatch.setattr(ep, 'dedupe', dedupe)
    execute(owner)
    assert snapshots == [[], [str(owner.root)]]
    assert snapshots[0] is not snapshots[1]
    assert lease_result(ep.BUS) == 'ACQUIRED'


def test_receiving_between_b_and_c_refuses_before_reference_inventory(owner, monkeypatch):
    from contextlib import contextmanager
    from sync_from_repo import source_lock
    monkeypatch.setenv('GLP_SOURCE_LOCK_WAIT_S', '0')
    scopes = 0
    holders = []
    @contextmanager
    def receiving_after_b(root):
        nonlocal scopes
        scopes += 1
        number = scopes
        with source_lock(root) as acquired:
            yield acquired
        if number == 3:
            holder = lease_process(root, hold=True)
            holders.append(holder)
            assert holder.stdout.readline().strip() == 'ACQUIRED'
    def denied(*args):
        raise AssertionError('C inventoried after losing receiving admission')
    monkeypatch.setattr(ep, 'source_lock', receiving_after_b)
    monkeypatch.setattr(ep, 'reference_files', denied)
    try:
        rows, candidates = execute(owner)
        assert candidates[-1]['status'] == 'GATED_SOURCE_BUSY'
        assert owner.state['full_pass_complete'] is False
        assert rows[-1]['rule'] == 'C_cold_evidence_archive'
    finally:
        for holder in holders:
            out, err = holder.communicate('\n', timeout=10)
            assert holder.returncode == 0, err
    assert lease_result(ep.BUS) == 'ACQUIRED'


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


def scheduler_heartbeat(owner):
    """Nine-field fixture from engine.py:642-652; instants are fixture labels."""
    path = owner.source.parent / 'email_flows' / 'scheduler-heartbeat.json'
    path.parent.mkdir()
    payload = dict(monitor='shared_email_flow_scheduler', ran_at='2026-10-04T07:07:28Z',
                   upstream_ok=True, auth_valid=True, items_seen=0, sent_count=0,
                   draft_count=0, cadence_s=1800, secret_printed=False)
    path.write_text(json.dumps(payload))
    return path, payload


def replace_heartbeat(path, payload):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(payload, sort_keys=True, separators=(',', ':'))+'\n')
    temporary.replace(path)  # same atomic replacement shape as the maintained writer


@pytest.mark.parametrize('rule_D', [False, True])
def test_scheduler_telemetry_change_does_not_invalidate_consumers(owner, rule_D):
    path, payload = scheduler_heartbeat(owner)
    assert ep.reference_source_allowed(path), 'keep heartbeat inventory and identity guards'
    index = ep.ReferenceIndex([path], ep.BUS,
                              rule_D_qa_root=owner.root.parent if rule_D else None)
    assert not index.references(owner.root)
    payload.update(ran_at='2026-10-04T07:37:28Z', upstream_ok=False,
                   items_seen=3, sent_count=1, draft_count=2)
    replace_heartbeat(path, payload)
    assert not index.references(owner.root), 'telemetry-only replacement gated every scope'
    assert not hasattr(index, 'last_changed_source')
    assert index.references(path.parent), 'reference-source ancestor guard must survive'


def test_scheduler_telemetry_reaches_owning_fresh_admission(owner, monkeypatch):
    path, payload = scheduler_heartbeat(owner)
    monkeypatch.setattr(ep, 'reference_files', lambda *a: [path])
    def telemetry_discovery(*args, **kwargs):
        result = owner.discovery(*args, **kwargs)
        payload['ran_at'] = '2026-10-04T07:37:28Z'
        replace_heartbeat(path, payload)
        return result
    monkeypatch.setattr(ep, 'archive_candidates', telemetry_discovery)
    rows, candidates = execute(owner)
    assert candidates[0]['status'] == 'GATED_YOUNG_FILE', 'telemetry blocked real age admission'
    assert rows[-1]['archive_progress']['evaluated'] == 1
    assert (owner.root / 'evidence.txt').exists()


@pytest.mark.parametrize('field', ['script', 'workdir', 'prompt', 'policy', 'pin',
                                   'live_root', 'deploy_root', 'protected_roots', 'unknown'])
def test_scheduler_consumer_addition_still_refuses(owner, field):
    path, payload = scheduler_heartbeat(owner)
    index = ep.ReferenceIndex([path], ep.BUS)
    payload[field] = str(owner.root)
    replace_heartbeat(path, payload)
    assert index.references(owner.root)
    assert index.last_changed_source == str(path)
    # A fresh index must also recognize actual references, not merely drift.
    assert ep.ReferenceIndex([path], ep.BUS).references(owner.root)


@pytest.mark.parametrize('change', ['malformed', 'duplicate', 'missing', 'monitor',
                                    'timestamp', 'boolean_count', 'string_count',
                                    'nested_count', 'cadence', 'auth', 'secret', 'deleted', 'symlink'])
def test_scheduler_unknown_or_changed_schema_still_refuses(owner, change):
    path, payload = scheduler_heartbeat(owner)
    index = ep.ReferenceIndex([path], ep.BUS)
    if change == 'malformed':
        path.write_text('{broken')
    elif change == 'duplicate':
        path.write_text(json.dumps(payload)[:-1]+',"ran_at":"other"}')
    elif change == 'missing':
        payload.pop('draft_count')
        replace_heartbeat(path, payload)
    elif change == 'deleted':
        path.unlink()
    elif change == 'symlink':
        path.unlink()
        path.symlink_to(owner.source)
    else:
        key, value = {'monitor': ('monitor', 'other'), 'timestamp': ('ran_at', str(owner.root)),
                      'boolean_count': ('items_seen', True), 'string_count': ('items_seen', str(owner.root)),
                      'nested_count': ('items_seen', {'live_root': str(owner.root)}),
                      'cadence': ('cadence_s', 3600), 'auth': ('auth_valid', False),
                      'secret': ('secret_printed', True)}[change]
        payload[key] = value
        replace_heartbeat(path, payload)
    assert index.references(owner.root)
    assert index.last_changed_source == str(path)


def test_scheduler_named_unrelated_file_remains_raw_consumer(owner):
    path, payload = scheduler_heartbeat(owner)
    other = owner.source.parent / path.name
    other.write_text(json.dumps(payload))
    index = ep.ReferenceIndex([other], ep.BUS)
    payload['ran_at'] = '2026-10-04T07:37:28Z'
    replace_heartbeat(other, payload)
    assert index.references(owner.root)
    assert index.last_changed_source == str(other)


@pytest.mark.parametrize('field', ['script', 'workdir', 'prompt', 'policy', 'pin',
                                   'live_root', 'deploy_root', 'protected_roots'])
def test_real_consumer_change_still_refuses(owner, field):
    index = ep.ReferenceIndex([owner.source], ep.BUS)
    owner.source.write_text(json.dumps({field: str(owner.root)}))
    assert index.references(owner.root)
    assert index.last_changed_source == str(owner.source)
    assert ep.ReferenceIndex([owner.source], ep.BUS).references(owner.root)


def test_safety_oracles_equal_authenticated_installed_predecessor():
    """Content/metadata/hardlink/retention/lifecycle guards are byte-unchanged.

    This is a source invariance assertion, not a live effect verification.
    Digest derives from authenticated predecessor commit 8f30fd7, not a mock.
    The one new exact-path telemetry branch is separately regression-tested;
    project only that branch out, retaining byte protection for jobs projection,
    reference matching/inventory and all other authenticated safety oracles.
    """
    import ast
    import hashlib
    source = (Path(__file__).resolve().parents[1] / 'scripts' /
              'evidence_preserving_pruner.py').read_text()
    index = next(n for n in ast.parse(source).body if isinstance(n, ast.ClassDef)
                 and n.name == 'ReferenceIndex')
    consumer = next(n for n in index.body if isinstance(n, ast.FunctionDef)
                    and n.name == 'consumer_text')
    branch = consumer.body[1]
    assert isinstance(branch, ast.If)
    assert ast.unparse(branch.test) == "p == self.business / 'ops/email_flows/scheduler-heartbeat.json'"
    lines = source.splitlines(keepends=True)
    source = ''.join(lines[:branch.lineno-1] + lines[branch.end_lineno:])
    # D's subclass defers initial collection. The default constructor still
    # calls the exact same collector; project only this additive seam out.
    initial_seam = """    def initial_bindings(self,collector):
        return collector(bus=self.business)

"""
    assert source.count(initial_seam) == 1
    assert source.count('        bindings=self.initial_bindings(relay_custody)') == 1
    source = source.replace(initial_seam, '').replace(
        '        bindings=self.initial_bindings(relay_custody)',
        '        bindings=relay_custody(bus=self.business)')
    # Project ONLY the newly tested additive relay binding branch out of the
    # authenticated predecessor comparison; do not rebaseline any old oracle.
    added_init = """        from host_disk_relay_custody import collect as relay_custody
        bindings=relay_custody(bus=self.business)
        self.relay_source_identities=bindings.identities
        self.ancestor_pins=list(bindings)
"""
    added_check = """        from host_disk_relay_custody import collect as relay_custody, matches as binding_matches
        for source,expected in self.relay_source_identities.items():
            if not binding_matches(source,expected):
                self.last_changed_source=source;return True
        # Never cache live issue descriptions or lane/JOB discovery across gates.
        # Preserve the admitted pins; a fresh complete generation can only add
        # protection here, not erase an earlier hold or replay a refused effect.
        try:
            fresh=relay_custody(bus=self.business)
        except (OSError,ValueError,RuntimeError,subprocess.SubprocessError):
            self.last_changed_source='relay_lane_linear_refresh_incomplete';return True
        for source,expected in self.relay_source_identities.items():
            if not binding_matches(source,expected):
                self.last_changed_source=source;return True
        live_pins=tuple(fresh)
        if any(pin==p or pin in p.parents or p in pin.parents for pin in live_pins): return True
"""
    added_commit_check = """        # Hashing a large verified archive can outlive the preceding reference
        # check. Re-read live custody at the actual namespace-commit boundary.
        if pins.references(root):
            return dict(row,status='GATED_REFERENCE_BEFORE_COMMIT')
"""
    assert source.count(added_init) == source.count(added_check) == source.count(added_commit_check) == 1
    source = source.replace(added_init, '        self.ancestor_pins=[]\n').replace(added_check, '').replace(added_commit_check, '')
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


def test_state_backup_successful_steps_are_paced(tmp_path):
    """A WAL-readable snapshot must not treat backup(sleep=...) as a rate limit.

    That argument only sleeps on SQLITE_BUSY/LOCKED. Exercise the registered
    producer with successful SQLite page copies and highly compressible data
    so wall time is governed by source copying rather than zstd/host disk.
    """
    import os
    import sqlite3
    import time
    home = tmp_path / 'hermes'
    home.mkdir()
    (home / 'config.yaml').write_text('fixture: true\n')
    with sqlite3.connect(home / 'state.db') as db:
        db.execute('pragma journal_mode=wal')
        db.execute('create table payload(data blob)')
        for _ in range(40):
            db.execute('insert into payload values (zeroblob(?))', (1024**2,))
        db.commit()
    producer = Path(__file__).resolve().parents[1] / 'scripts/hermes-state-backup.sh'
    begin = time.monotonic()
    run = subprocess.run(['bash', str(producer)], env={**os.environ,
        'HERMES_HOME': str(home), 'HERMES_BACKUP_ROOT': str(tmp_path / 'backups'),
        'TMPDIR': str(tmp_path)}, capture_output=True, text=True, timeout=90)
    elapsed = time.monotonic() - begin
    assert run.returncode == 0, run.stdout + run.stderr
    assert elapsed >= 3.8, ('successful page copies bypassed 10MiB/s pacing', elapsed)


def test_daily_backup_large_synthetic_page_stream_fits_pacing_budget():
    """19.35 GB synthetic SQLite page stream, virtual clock, exact producer block.

    No 19 GB allocation or 31-minute sleep: drive the actual progress callback
    with SQLite's 1 MiB successful-step shape. The old 1800s cap fails here.
    Real SQLite backup/quick_check/archive/retention are covered below.
    """
    import ast
    producer = Path(__file__).resolve().parents[1] / 'scripts/hermes-state-backup.sh'
    body = producer.read_text().split("<<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
    tree = ast.parse(body)
    statements = next(n.body for n in ast.walk(tree) if isinstance(n, ast.Try)
                      and any(isinstance(s, ast.Assign) and any(
                          isinstance(t, ast.Name) and t.id == 'copy_started'
                          for t in s.targets) for s in n.body))
    first = next(i for i, n in enumerate(statements) if isinstance(n, ast.Assign)
                 and any(isinstance(t, ast.Name) and t.id == 'copy_started' for t in n.targets))
    last = next(i for i, n in enumerate(statements) if isinstance(n, ast.Expr)
                and isinstance(n.value, ast.Call) and isinstance(n.value.func, ast.Attribute)
                and n.value.func.attr == 'backup')
    code = compile(ast.Module(body=statements[first:last+1], type_ignores=[]), str(producer), 'exec')
    clock = [0.0]
    def sleep(delay):
        clock[0] += delay
    logical_bytes = 19_385_290_752
    page_size = 4096
    pages_total = logical_bytes // page_size
    def backup(dst, *, pages, progress, sleep):
        for copied in range(pages, pages_total + pages, pages):
            remaining = max(0, pages_total - copied)
            progress(0 if remaining else 101, remaining, pages_total)
    logs = []
    scope = dict(time=SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep),
                 src=SimpleNamespace(backup=backup), dst=None, is_state=True,
                 logical_bytes=logical_bytes, page_size=page_size,
                 limit=logical_bytes + 1024**3, rel='state.db',
                 free_bytes=lambda: 100 * 1024**3, MIN_FREE_BYTES=2 * 1024**3,
                 wal=SimpleNamespace(exists=lambda: False), wal_before=0,
                 sqlite3=SimpleNamespace(SQLITE_OK=0, SQLITE_DONE=101), log=logs.append)
    exec(code, scope)
    assert clock[0] == pytest.approx(logical_bytes / (10 * 1024**2))
    assert scope['deadline'] >= clock[0] + 900
    assert len(logs) >= 30, 'long snapshot must publish progress heartbeats'
    # Size-scaled time must not relax growth/headroom/WAL refusal guards.
    for reason, change in (
            ('SIZE', {'limit': logical_bytes - 1}),
            ('HEADROOM', {'free_bytes': lambda: 0}),
            ('WAL_GROWTH', {'wal': SimpleNamespace(exists=lambda: True, stat=lambda:
                SimpleNamespace(st_size=2 * 1024**3))})):
        guarded = dict(scope, **change)
        clock[0] = 0.0
        with pytest.raises(TimeoutError, match='reason='+reason):
            exec(code, guarded)
    clock[0] = scope['deadline'] + 1
    with pytest.raises(TimeoutError, match='reason=DEADLINE'):
        scope['progress'](0, pages_total - 256, pages_total)


def test_daily_backup_restores_committed_wal_ledgers_privately_and_fails_closed(tmp_path):
    """Real registered producer + SQLite restore, scratch only; not live coverage."""
    import hashlib
    import os
    import sqlite3
    import tarfile
    home = tmp_path / 'hermes'
    (home / 'cron').mkdir(parents=True)
    (home / 'memories').mkdir()
    payloads = {'config.yaml': b'fixture: true\n', '.env': b'FIXTURE_ONLY=yes\n',
                'auth.json': b'{}\n', 'cron/jobs.json': b'[]\n',
                'cron/cron.db': b'', 'SOUL.md': b'fixture\n',
                'memories/MEMORY.md': b'fixture memory\n'}
    for name, data in payloads.items():
        (home / name).write_bytes(data)
    ledgers = ('executions.db', 'deliveries.db', 'notepad.db')
    connections = []
    try:
        state = sqlite3.connect(home / 'state.db')
        connections.append(state)
        state.execute('pragma journal_mode=wal')
        state.execute('pragma wal_autocheckpoint=0')
        state.execute('create table messages(id integer primary key, content text)')
        state.execute("create virtual table messages_fts using fts5(content, content='messages', content_rowid='id')")
        state.execute("create trigger messages_ai after insert on messages begin insert into messages_fts(rowid, content) values(new.id, new.content); end")
        state.execute("insert into messages(content) values('committed recovery fixture')")
        state.commit()
        state.execute("insert into messages(content) values('uncommitted fixture must not restore')")
        for name in ledgers:
            conn = sqlite3.connect(home / 'cron' / name)
            connections.append(conn)
            assert conn.execute('pragma journal_mode=wal').fetchone()[0] == 'wal'
            conn.execute('pragma wal_autocheckpoint=0')
            conn.execute('create table proof(value text)')
            conn.execute('insert into proof values (?)', ('committed-in-wal-'+name,))
            conn.commit()
            assert (home / 'cron' / (name+'-wal')).stat().st_size > 0
        producer = Path(__file__).resolve().parents[1] / 'scripts/hermes-state-backup.sh'
        backup_root = tmp_path / 'backups'
        env = {**os.environ, 'HERMES_HOME': str(home),
               'HERMES_BACKUP_ROOT': str(backup_root), 'TMPDIR': str(tmp_path)}
        run = subprocess.run(['bash', str(producer)], env=env, capture_output=True,
                             text=True, timeout=90)
        assert run.returncode == 0, run.stdout + run.stderr
        artifacts = list(backup_root.glob('hermes-state-????????.tar.gz'))
        assert len(artifacts) == 1
        artifact = artifacts[0]
        assert artifact.stat().st_mode & 0o777 == 0o600
        assert backup_root.stat().st_mode & 0o777 == 0o700
        with tarfile.open(artifact) as tar:
            members = {m.name: m for m in tar.getmembers()}
            assert len(members) == len(tar.getmembers())
            assert 'state.db' not in members  # published separately as zstd
            assert not any(n.endswith(('-wal', '-shm')) for n in members)
            for name, payload in payloads.items():
                member_file = tar.extractfile(name)
                assert member_file is not None
                assert member_file.read() == payload
            coverage_file = tar.extractfile('backup-coverage.json')
            assert coverage_file is not None
            manifest = json.load(coverage_file)
            assert manifest['session_store']['status'] == 'SNAPSHOT_OK'
            state_copies = list(backup_root.glob('hermes-state-db-????????.sqlite.zst'))
            assert [p.name for p in state_copies] == [manifest['session_store']['artifact']]
            assert state_copies[0].stat().st_mode & 0o777 == 0o600
            snapshot = tmp_path / 'state-snapshot.db'
            subprocess.run(['zstd', '-q', '-d', str(state_copies[0]), '-o', str(snapshot)], check=True)
            restored = tmp_path / 'state-restored.db'
            with restored.open('xb') as target:
                os.fchmod(target.fileno(), 0o600)
            with sqlite3.connect(snapshot) as src, sqlite3.connect(restored) as dst:
                src.backup(dst)
                assert dst.execute('pragma quick_check').fetchall() == [('ok',)]
                assert dst.execute('select count(*) from messages').fetchone()[0] == 1
                assert dst.execute("select count(*) from messages_fts where messages_fts match 'recovery'").fetchone()[0] == 1
                assert dst.execute("select count(*) from messages_fts where messages_fts match 'uncommitted'").fetchone()[0] == 0
                dst.execute("insert into messages_fts(messages_fts, rank) values('integrity-check', 1)")
            assert manifest['session_store']['sha256'] == hashlib.sha256(snapshot.read_bytes()).hexdigest()
            assert manifest['producer_sha256'] == hashlib.sha256(producer.read_bytes()).hexdigest()
            for name in ledgers:
                member = 'cron/' + name
                assert member in members, 'scheduled backup omits modern cron ledger: '+name
                assert members[member].mode == 0o600
                snapshot = tmp_path / ('snapshot-'+name)
                member_file = tar.extractfile(member)
                assert member_file is not None
                snapshot.write_bytes(member_file.read())
                os.chmod(snapshot, 0o600)
                restored = tmp_path / ('restored-'+name)
                src = sqlite3.connect('file:'+str(snapshot)+'?mode=ro', uri=True)
                dst = sqlite3.connect(restored)
                src.backup(dst)
                src.close()
                assert dst.execute('pragma quick_check').fetchone()[0] == 'ok'
                assert dst.execute('select value from proof').fetchall() == [('committed-in-wal-'+name,)]
                dst.close()
                assert manifest['ledgers'][member]['sha256'] == hashlib.sha256(snapshot.read_bytes()).hexdigest()
        # Retention: only 2 compressed state.db copies survive a new run.
        for day in ('20000101', '20000102', '20000103'):
            (backup_root / ('hermes-state-db-'+day+'.sqlite.zst')).write_bytes(b'old')
        again = subprocess.run(['bash', str(producer)], env=env, capture_output=True, text=True, timeout=90)
        assert again.returncode == 0, again.stdout + again.stderr
        kept = sorted(p.name for p in backup_root.glob('hermes-state-db-*.sqlite.zst'))
        assert kept == ['hermes-state-db-20000103.sqlite.zst', state_copies[0].name]
        prior = artifact.read_bytes()
        # Exact producer with scratch-only statvfs simulation, no live pressure.
        injection = tmp_path / 'low-space-python'
        injection.mkdir()
        (injection / 'sitecustomize.py').write_text(
            'import os\nfrom types import SimpleNamespace\n'
            'os.statvfs = lambda p: SimpleNamespace(f_bavail=1, f_frsize=1)\n')
        refused = subprocess.run(['bash', str(producer)],
            env={**env, 'PYTHONPATH': str(injection)}, capture_output=True, timeout=90)
        assert refused.returncode != 0
        assert b'insufficient private state snapshot/archive headroom' in refused.stderr
        assert artifact.read_bytes() == prior
        assert not list(backup_root.glob('.snapshot-*'))
        # Corrupt a required present ledger. Prior good archive survives; no partial publish.
        for conn in connections:
            conn.close()
        connections.clear()
        (home / 'cron' / 'deliveries.db').write_bytes(b'not a sqlite database')
        failed = subprocess.run(['bash', str(producer)], env=env, capture_output=True, timeout=90)
        assert failed.returncode != 0
        assert artifact.read_bytes() == prior
        assert not list(backup_root.glob('*.partial.*'))
        assert not list(backup_root.glob('.snapshot-*'))
        # An outside symlink cannot be chmodded or truncated by output publication.
        outside = tmp_path / 'outside'
        outside.write_bytes(b'untouched')
        os.chmod(outside, 0o644)
        artifact.unlink()
        artifact.symlink_to(outside)
        refused = subprocess.run(['bash', str(producer)], env=env, capture_output=True, timeout=90)
        assert refused.returncode != 0
        assert outside.read_bytes() == b'untouched'
        assert outside.stat().st_mode & 0o777 == 0o644
    finally:
        for conn in connections:
            conn.close()


@pytest.mark.parametrize('full_pass', [False, True])
def test_glp349_d_cannot_starve_later_classes(owner, monkeypatch, full_pass):
    """Retained D=1049.947/1050 timing shape, synthetic clock; no effects.

    C's authentic reference-walk refusal remains a refusal, not eligible work.
    Images-only qualification must retain its entire original deadline.
    """
    clock = [0.0]
    deadlines = {}
    original_done = copy.deepcopy(owner.state['C_done'])
    owner.state.update(A_cursor=['fixture unfinished'], A_pending=['fixture held'])
    owner.state['B_scan'] = {'cursors': {'fixture': ['unfinished']}}
    monkeypatch.setattr(ep.time, 'monotonic', lambda: clock[0])
    def images(*args, deadline, **kwargs):
        deadlines['D'] = deadline
        clock[0] = deadline - 0.053
        return [{'rule': 'D_cold_run_images_summary', 'ok': True,
                 'status': 'TIME_BUDGET_PARTIAL', 'partial': True, 'bytes_freed': 0}]
    def regenerable(*args, deadline, **kwargs):
        deadlines['A'] = deadlines.get('A', 0) + deadline - clock[0]
        clock[0] = deadline
        return [{'rule': 'A_regenerable', 'ok': True,
                 'status': 'TIME_BUDGET_PARTIAL', 'bytes_freed': 0}]
    def dedupe(*args, deadline, **kwargs):
        deadlines['B'] = deadlines.get('B', 0) + deadline - clock[0]
        clock[0] = deadline
        return {'rule': 'B_readonly_evidence_dedupe', 'ok': True,
                'status': 'TIME_BUDGET_PARTIAL', 'bytes_freed': 0}
    def references(config, deadline):
        deadlines['C'] = deadline - clock[0]
        raise ep.ScanBudget('reference_walk:/home/ubuntu/business/ops selected=0')
    monkeypatch.setitem(sys.modules, 'screenshot_pruner', SimpleNamespace(run_rules=images))
    monkeypatch.setattr(ep, 'regenerable', regenerable)
    monkeypatch.setattr(ep, 'dedupe', dedupe)
    monkeypatch.setattr(ep, 'reference_files', references)
    rows = ep.run(owner.api, owner.config, owner.state, dry_run=False,
                  deadline=1050, full_pass=full_pass)
    assert deadlines['D'] == pytest.approx(262.5), deadlines
    assert deadlines['A'] >= 180 and deadlines['B'] >= 150, deadlines
    assert deadlines['C'] > 0 and clock[0] <= 1050
    assert rows[-1]['status'] == 'GATED_REFERENCE_INVENTORY_INCOMPLETE'
    assert owner.state['C_done'] == original_done
    assert owner.state['A_cursor'] == ['fixture unfinished']
    assert owner.state['B_scan']['cursors'] == {'fixture': ['unfinished']}
    assert (owner.root / 'evidence.txt').read_text() == 'fixture evidence must stay intact'
    clock[0] = 0
    deadlines.clear()
    ep.run(owner.api, owner.config, owner.state, dry_run=True, deadline=1050, images_only=True)
    assert deadlines == {'D': 1050}, 'qualification-only scope lost its original cap'


@pytest.mark.parametrize('large_rc,safe_rc,evidence_rc,safe_present,evidence_partial', [
    (0, 0, 2, True, True), (0, 1, 0, False, False),
    (1, 0, 0, True, False), (0, 2, 0, True, False),
    (0, 0, 0, False, False), (0, 0, 0, True, True)])
def test_glp349_controller_preserves_each_phase_result(tmp_path, monkeypatch,
        large_rc, safe_rc, evidence_rc, safe_present, evidence_partial):
    """Exact maintained controller AST; scratch reports and captured sink only.

    Dependencies are inert fixtures: no installed import, sudo, cleanup, native
    state, network or send. The retained coarse-error shape is evidence rc2 with
    only ok=true partial/gated rows. A missing safe receipt must not hide rc1.
    """
    import ast
    import collections
    import datetime
    import os
    from typing import Any
    scripts_root = Path(__file__).resolve().parents[1] / 'scripts'
    assert ep.__file__ and Path(ep.__file__).resolve() == scripts_root / 'evidence_preserving_pruner.py'
    source = (scripts_root / 'host_disk_cleanup_large_unit.py').read_text()
    functions: list[ast.stmt] = [n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef)]
    home = tmp_path / 'hermes'
    (home / 'runstate').mkdir(parents=True)
    capture = []
    phases = []
    evidence_rows = [
        {'rule': 'D_cold_run_images_summary', 'ok': True, 'partial': True,
         'status': 'TIME_BUDGET_PARTIAL', 'bytes_freed': 0},
        {'rule': 'C_cold_evidence_archive', 'ok': True,
         'status': 'GATED_REFERENCE_INVENTORY_INCOMPLETE', 'bytes_freed': 0}]
    def run(command, **kwargs):
        if '--budget' in command:
            report = Path(command[command.index('--report') + 1])
            ep.atomic_json(report / 'large-classes.json', {'rows': []})
            phases.append('large_classes')
            return SimpleNamespace(returncode=large_rc)
        if '--safe-rules-only' in command:
            report = Path(command[command.index('--report-dir') + 1])
            if safe_present:
                ep.atomic_json(report / 'receipt.json', {'reclaimed_bytes': 0})
            phases.append('legacy_safe_rules')
            return SimpleNamespace(returncode=safe_rc)
        assert '--max-seconds' in command and command[command.index('--max-seconds')+1] == '1050'
        report = Path(command[command.index('--report') + 1])
        ep.atomic_json(report, {'rows': evidence_rows if evidence_partial else []})
        phases.append('D_A_B_C')
        return SimpleNamespace(returncode=evidence_rc)
    monkeypatch.setitem(sys.modules, 'host_disk_auto_classes',
                        SimpleNamespace(external_pins=lambda: []))
    monkeypatch.setitem(sys.modules, 'post_discord_message', SimpleNamespace(
        send=lambda channel, line: capture.append(line) or {'id': 'fixture'},
        readback=lambda channel, mid: {'content': capture[-1], 'channel_id': channel}))
    namespace: dict[str, Any] = dict(Path=Path, collections=collections, datetime=datetime, os=os,
                     sys=sys, time=time, json=json, atomic_json=ep.atomic_json,
                     fcntl=SimpleNamespace(LOCK_EX=1, LOCK_NB=2, flock=lambda *a: None),
                     subprocess=SimpleNamespace(run=run), BUS=tmp_path, HOME=home,
                     STATE=home/'runstate/host-disk-cleanup-last.json', UNIT='fixture.service')
    exec(compile(ast.Module(body=functions, type_ignores=[]), '<candidate-controller-AST>', 'exec'), namespace)
    namespace['disk'] = lambda: {'free_bytes': 100, 'total_bytes': 100}
    rc = namespace['main']()
    receipts = list((tmp_path / 'reports/host-disk-prune').glob('*/completion.json'))
    assert len(receipts) == 1 and phases == ['large_classes', 'legacy_safe_rules', 'D_A_B_C']
    receipt = json.loads(receipts[0].read_text())
    actual = receipt['phase_results']
    assert {key: result['returncode'] for key, result in actual.items()} == dict(
        large_classes=large_rc, legacy_safe_rules=safe_rc, D_A_B_C=evidence_rc)
    assert actual['legacy_safe_rules']['report_present'] is safe_present
    if evidence_partial:
        assert actual['D_A_B_C']['status'] == 'partial_or_gated'
        assert actual['D_A_B_C']['status_counts'] == dict(
            TIME_BUDGET_PARTIAL=1, GATED_REFERENCE_INVENTORY_INCOMPLETE=1)
        assert actual['D_A_B_C']['partial_rows'] == 1
        assert actual['D_A_B_C']['gated_rows'] == 1
    assert rc == (0 if (large_rc == 0 and safe_rc in (0, 2) and evidence_rc == 0
                       and safe_present and not evidence_partial) else 1)
    assert receipt['status'] == ('completed' if rc == 0 else 'completed_with_errors')
    assert receipt['reclaimed_bytes'] == 0 and len(capture) == 1


def test_glp349_native_ranking_incomplete_is_not_controller_completion(tmp_path, monkeypatch):
    """Real expired ranker branch -> controller main; private reports, no effects.

    UNKNOWN is an explicit fixture; COMPLETE/REMOVED and status-less summaries
    remain complete, including the safe phase's accepted rc2. No live imports.
    """
    import ast
    import collections
    import datetime
    import os
    from typing import Any
    scripts = Path(__file__).resolve().parents[1] / 'scripts'
    def functions(name, selected=None) -> list[ast.stmt]:
        return [n for n in ast.parse((scripts / name).read_text()).body
                if isinstance(n, ast.FunctionDef) and (selected is None or n.name in selected)]
    def denied(*args, **kwargs):
        raise AssertionError('ranker subprocess/effect boundary denied')
    ranker: dict[str, Any] = dict(time=SimpleNamespace(monotonic=lambda: 2),
                  subprocess=SimpleNamespace(run=denied, TimeoutExpired=TimeoutError))
    exec(compile(ast.Module(body=functions('host_disk_large_sweep.py', {'rank_reports'}),
                           type_ignores=[]), '<native-ranker-AST>', 'exec'), ranker)
    ranking, incomplete = ranker['rank_reports']([tmp_path / 'unranked-report'], 1)
    assert ranking == [] and incomplete == [dict(rule='cold_report_ranking',
        path=str(tmp_path / 'unranked-report'), bytes_freed=0, status='INCOMPLETE', reason='budget')]
    assert not any(not r.get('ok', True) for r in incomplete), 'native large-sweep rc0 shape'
    complete = [dict(rule='D_cold_run_images_summary', status='COMPLETE', ok=True, bytes_freed=0),
                dict(rule='A_regenerable', status='REMOVED', ok=True, bytes_freed=0),
                dict(rule='B_readonly_evidence_dedupe', ok=True, bytes_freed=0)]
    cases = [('native-INCOMPLETE', incomplete, [], 1, 'large_classes', {'INCOMPLETE': 1}),
             ('explicit-UNKNOWN', [], [dict(rule='evidence_preserving_summary',
                 status='UNKNOWN', ok=True, bytes_freed=0)], 1, 'D_A_B_C', {'UNKNOWN': 1}),
             ('complete', [], complete, 0, 'D_A_B_C', {'COMPLETE': 1, 'REMOVED': 1, 'UNSPECIFIED': 1})]
    for name, large_rows, evidence_rows, expected_rc, phase, counts in cases:
        root = tmp_path / name
        home = root / 'hermes'
        (home / 'runstate').mkdir(parents=True)
        capture = []
        def run(argv, **kwargs):
            if '--budget' in argv:
                report = Path(argv[argv.index('--report') + 1]) / 'large-classes.json'
                ep.atomic_json(report, {'rows': large_rows})
                return SimpleNamespace(returncode=0)
            if '--safe-rules-only' in argv:
                report = Path(argv[argv.index('--report-dir') + 1]) / 'receipt.json'
                ep.atomic_json(report, {'reclaimed_bytes': 0})
                return SimpleNamespace(returncode=2)
            assert argv[argv.index('--max-seconds') + 1] == '1050'
            ep.atomic_json(Path(argv[argv.index('--report') + 1]), {'rows': evidence_rows})
            return SimpleNamespace(returncode=0)
        monkeypatch.setitem(sys.modules, 'host_disk_auto_classes', SimpleNamespace(external_pins=lambda: []))
        monkeypatch.setitem(sys.modules, 'post_discord_message', SimpleNamespace(
            send=lambda channel, line: capture.append(line) or {'id': 'fixture'},
            readback=lambda channel, mid: {'content': capture[-1], 'channel_id': channel}))
        ns: dict[str, Any] = dict(Path=Path, collections=collections, datetime=datetime, os=os, sys=sys,
                  time=time, json=json, atomic_json=ep.atomic_json,
                  fcntl=SimpleNamespace(LOCK_EX=1, LOCK_NB=2, flock=lambda *a: None),
                  subprocess=SimpleNamespace(run=run), BUS=root, HOME=home,
                  STATE=home/'runstate/state.json', UNIT='fixture.service')
        exec(compile(ast.Module(body=functions('host_disk_cleanup_large_unit.py'),
                               type_ignores=[]), '<candidate-controller-AST>', 'exec'), ns)
        ns['disk'] = lambda: {'free_bytes': 100, 'total_bytes': 100}
        rc = ns['main']()
        receipt, = (root / 'reports/host-disk-prune').glob('*/completion.json')
        receipt = json.loads(receipt.read_text())
        assert rc == expected_rc, (name, receipt['phase_results'])
        assert receipt['status'] == ('completed' if rc == 0 else 'completed_with_errors')
        actual = receipt['phase_results'][phase]
        assert actual['status'] == ('completed' if rc == 0 else 'partial_or_gated')
        assert actual['partial_rows'] == expected_rc and actual['status_counts'] == counts
        assert receipt['rows'] == large_rows + [dict(rule='legacy_safe_rules', bytes_freed=0, ok=True)] + evidence_rows
        assert receipt['phase_results']['legacy_safe_rules']['status'] == 'completed'
        assert actual['returncode'] == 0 and len(capture) == 1
