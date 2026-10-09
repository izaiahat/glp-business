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
