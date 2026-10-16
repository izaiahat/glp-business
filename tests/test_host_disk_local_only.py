"""Private registered-chain fixture. No live systemd, cleanup, R2 or sends."""
import ast
import fcntl
import json
import socket
import copy
import io
import stat

# Compile only the maintained route singleton, never import live relay modules.
ROUTE_HOLDER = '''
import ast, fcntl, sys
from pathlib import Path
from contextlib import contextmanager
source, route, hold = sys.argv[1:]
node = next(n for n in ast.parse(Path(source).read_text()).body
            if isinstance(n, ast.FunctionDef) and n.name == 'singleton')
ns = dict(contextmanager=contextmanager, fcntl=fcntl, directory=lambda: Path(route))
exec(compile(ast.Module(body=[node], type_ignores=[]), source, 'exec'), ns)
try:
    with ns['singleton']():
        print('OWNED', flush=True)
        if hold == 'hold': sys.stdin.readline()
except BlockingIOError:
    sys.exit(75)
'''


def route_command(route, mode='probe'):
    return [sys.executable, '-c', ROUTE_HOLDER, str(ROOT / 'scripts/pro_route.py'), str(route), mode]


def deny_network(monkeypatch):
    def denied(*a, **k):
        raise AssertionError('private cleanup fixture attempted network')
    monkeypatch.setattr(socket, 'create_connection', denied)
    monkeypatch.setattr(socket.socket, 'connect', denied)
    return denied

from pathlib import Path
import runpy
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ops'))
sys.path.insert(0, str(ROOT / 'scripts'))

import host_disk_large_sweep as sweep
import host_disk_cold_classes as cold
import host_disk_completed_dependencies as dependencies
import host_disk_auto_classes as auto
import host_disk_r2 as r2


def duplicate_deadline_probe(expiry, expected_error=None):
    """Execute candidate definition with memory-only filesystem/custody seams."""
    path = ROOT / 'scripts/host_disk_cold_classes.py'
    node = next(n for n in ast.parse(path.read_text()).body
                if isinstance(n, ast.FunctionDef) and n.name == 'verified_duplicates')
    now, calls, removed, writes = [11 if expiry == 'initial' else 0], [0], [], []
    class MemoryPath(str):
        def is_symlink(self):
            if expiry == 'target' and self == 'b1': now[0] = 11
            return False
        def lstat(self):
            return SimpleNamespace(st_mode=stat.S_IFREG | 0o444, st_size=1,
                                   st_nlink=1, st_mtime=0, st_ctime=0, st_blocks=8)
        def unlink(self):
            assert now[0] <= 10, 'unlink admitted after deadline'
            removed.append(str(self))
            if expiry == 'after_completed_unlink': now[0] = 11
            if expiry == 'ambiguous_unlink' and self == 'b2': raise OSError('ambiguous unlink')
    def custody(pins):
        calls[0] += 1
        if expiry == 'initial_custody': now[0] = 11
        if expiry == 'final_custody' and calls[0] == 3: now[0] = 11
        if expiry == 'unrelated_timeout' and calls[0] == 3: raise TimeoutError('custody failure')
        return ()
    def digest(p):
        if expiry == 'final_hash' and calls[0] == 3 and p == 'b2': now[0] = 11
        return p[0]
    def write(manifest, rows):
        writes.append(copy.deepcopy(rows))
        if (expiry == 'durable_intent' and rows[-1].get('path') == 'b2'
                and rows[-1]['status'] == 'VERIFIED_REMOVAL_INTENT'): now[0] = 11
    namespace = dict(
        Path=MemoryPath, stat=stat,
        time=SimpleNamespace(time=lambda: 10**9, monotonic=lambda: now[0]),
        os=SimpleNamespace(O_RDONLY=0, O_NOFOLLOW=0, O_NOATIME=0,
                           open=lambda *a: 0, fdopen=lambda *a: io.BytesIO(b'plain')),
        custody_pins=custody, protected=lambda *a: False,
        dirty_worktree=lambda *a: False, inactive=lambda *a: True,
        signature=lambda p: [str(p)], sha=digest, write_json=write)
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), namespace)
    def run():
        return namespace['verified_duplicates'](
            None, ['a1', 'a2', 'b1', 'b2', 'c1'], 'memory-only', dry_run=False, deadline=10)
    if expected_error:
        with pytest.raises(expected_error) as error: run()
        rows = error.value
    else:
        rows = run()
    return rows, removed, writes, calls[0]


@pytest.mark.parametrize('expiry', ['after_completed_unlink', 'unrelated_timeout', 'ambiguous_unlink'])
def test_duplicate_completed_effect_survives_cooperative_deadline(expiry):
    error_type = {'unrelated_timeout': TimeoutError, 'ambiguous_unlink': OSError}.get(expiry)
    rows, removed, writes, _ = duplicate_deadline_probe(expiry, error_type)
    if error_type:
        assert isinstance(rows, error_type)
        assert removed == (['a2', 'b2'] if expiry == 'ambiguous_unlink' else ['a2'])
        assert writes[-1][-1]['status'] == 'VERIFIED_REMOVAL_INTENT'
        assert not any(r.get('status') == 'TIME_BUDGET_PARTIAL' for r in writes[-1])
        return
    assert removed == ['a2']
    assert sum(r['bytes_freed'] for r in rows) == 4096
    assert rows[-1]['status'] == 'TIME_BUDGET_PARTIAL'
    assert rows[-1]['partial'] is True and rows[-1]['ok'] is False
    assert rows[-1]['bytes_freed'] == 0
    assert rows == writes[-1]
    assert all(r['status'] != 'VERIFIED_REMOVAL_INTENT' for r in rows)


@pytest.mark.parametrize('expiry', ['initial', 'initial_custody', 'target',
                                   'final_custody', 'final_hash', 'durable_intent'])
def test_duplicate_final_validation_expiry_prevents_late_unlink(expiry):
    rows, removed, writes, calls = duplicate_deadline_probe(expiry)
    expected_removed = [] if expiry in {'initial', 'initial_custody'} else ['a2']
    assert removed == expected_removed
    assert sum(r['bytes_freed'] for r in rows) == 4096 * len(expected_removed)
    if expiry == 'initial': assert calls == 0
    assert rows[-1]['status'] == 'TIME_BUDGET_PARTIAL'
    assert rows[-1]['partial'] is True and rows[-1]['ok'] is False
    assert rows[-1]['bytes_freed'] == 0
    assert rows == writes[-1]
    assert all(r['status'] != 'VERIFIED_REMOVAL_INTENT' for r in rows)


def entrypoint(path, namespace, argv, monkeypatch):
    """Execute the actual CLI tail against private fixture-bound globals."""
    tail = ast.parse(path.read_text()).body[-1]
    assert isinstance(tail, ast.If)
    with monkeypatch.context() as scope:
        scope.setattr(sys, 'argv', argv)
        scope.setitem(namespace, '__name__', '__main__')
        with pytest.raises(SystemExit) as terminal:
            exec(compile(ast.Module(body=[tail], type_ignores=[]), str(path), 'exec'), namespace)
    return terminal.value.code


def fixture_sweep(tmp_path, monkeypatch):
    # A neighboring legacy test reloads these names at collection time. Bind
    # this fixture's imported candidate cohort for dynamic imports as well.
    for module in (sweep, cold, dependencies, auto, r2):
        assert Path(module.__file__).resolve().parent == ROOT / 'scripts'
        monkeypatch.setitem(sys.modules, module.__name__, module)
    bus = tmp_path / 'business'
    (bus / 'ops').mkdir(parents=True)
    (bus / 'ops/disk-retention-policy.json').write_text('{"protected_roots":[]}')
    proof = bus / 'reports/ops/glp347-pallas-20261007/ACK.txt'
    proof.parent.mkdir(parents=True)
    proof.write_text('fixture retained proof')
    monkeypatch.setattr(sweep, 'BUS', bus)
    monkeypatch.setattr(sweep, 'api', SimpleNamespace(path_is_active_privileged=lambda p: False))
    monkeypatch.setattr(cold, 'live_symlink_targets', lambda: set())
    monkeypatch.setattr(__import__('host_disk_custody'), 'collect', lambda: set())
    monkeypatch.setattr(dependencies, 'run', lambda *a, **k: [])
    monkeypatch.setattr(sweep, 'auto_classes', lambda api, report, pins, external, **k: (pins, []))
    monkeypatch.setattr(cold, 'cleanup_abandoned_archives', lambda *a, **k: [])
    monkeypatch.setattr(sweep, 'discover', lambda *a, **k: [])
    monkeypatch.setattr(sweep, 'idle_targets', lambda *a, **k: [])
    monkeypatch.setattr(cold, 'node_modules', lambda *a, **k: [])
    monkeypatch.setattr(cold, 'idle_scratch', lambda *a, **k: [])
    monkeypatch.setattr(cold, 'verified_duplicates', lambda *a, **k: [])
    monkeypatch.setattr(sweep, 'cold_report_roots', lambda *a, **k: [])
    monkeypatch.setattr(sweep, 'rank_reports', lambda *a: ([], []))
    deny_network(monkeypatch)
    return bus, proof


@pytest.mark.parametrize('already_expired', [False, True], ids=['worker-expiry', 'admission-expiry'])
def test_duplicate_caller_checkpoints_partial_accounting_and_failure(tmp_path, monkeypatch, already_expired):
    fixture_sweep(tmp_path, monkeypatch)
    report = tmp_path / 'private-report'
    now = [0]
    monkeypatch.setattr(sweep.time, 'monotonic', lambda: now[0])
    def references(target):
        cold.write_json(target / 'references.json', {'reference_source_identity': {}})
        return ()
    monkeypatch.setattr(sweep, 'references', references)
    def ranked(*args):
        if already_expired: now[0] = 11
        return [], []
    monkeypatch.setattr(sweep, 'rank_reports', ranked)
    expected = ([] if already_expired else [
        {'rule': 'verified_cold_duplicate', 'status': 'REMOVED', 'bytes_freed': 4096}]) + [
        {'rule': 'verified_cold_duplicate', 'status': 'TIME_BUDGET_PARTIAL',
         'bytes_freed': 0, 'partial': True, 'ok': False}]
    calls = []
    def duplicates(api, targets, manifest, **kwargs):
        checkpoint = json.loads((report / 'large-classes.json').read_text())
        assert checkpoint['stage'] == 'verified_duplicates'
        assert kwargs['deadline'] == 10  # no renewed budget at late admission
        cold.write_json(manifest, expected)
        calls.append('duplicates')
        return expected
    monkeypatch.setattr(cold, 'verified_duplicates', duplicates)
    rc = entrypoint(ROOT / 'scripts/host_disk_large_sweep.py', sweep.__dict__,
                    ['sweep', '--local-only', '--budget', '10', '--report', str(report)], monkeypatch)
    assert calls == ['duplicates']
    assert rc == 1
    terminal = json.loads((report / 'large-classes.json').read_text())
    assert terminal['stage'] == 'partial'
    assert terminal['rows'] == expected
    assert terminal['attributable_bytes_freed'] == (0 if already_expired else 4096)
    assert json.loads((report / 'duplicate-manifest.json').read_text()) == expected


@pytest.mark.parametrize('phase_exception', [False, True], ids=['normal-release', 'exception-release'])
def test_registered_local_only_chain_reaches_local_work_without_any_r2(tmp_path, monkeypatch, phase_exception):
    bus, proof = fixture_sweep(tmp_path, monkeypatch)
    deny_network(monkeypatch)
    route = bus / 'ops/gpt6pro-relay/route'
    route.mkdir(parents=True)
    (route / 'queue').mkdir()
    (route / 'queue/original.json').write_text(json.dumps({'pin': str(proof)}))
    route_lock = route / 'dispatcher.lock'
    route_lock.touch()
    original_inode = route_lock.stat().st_ino
    real_run = subprocess.run
    excluded = []
    def assert_excluded(stage):
        assert route_lock.stat().st_ino == original_inode
        cp = real_run(route_command(route), capture_output=True, text=True, timeout=10)
        assert cp.returncode == 75, (stage, cp.stdout, cp.stderr)
        excluded.append(stage)
    import host_disk_relay_custody as custody
    original_collect = custody.collect
    def collect(**kwargs):
        assert_excluded('reference_inventory')
        return original_collect(**kwargs)
    monkeypatch.setattr(custody, 'collect', collect)
    real_unlink = Path.unlink
    def unlink(path, *args, **kwargs):
        if path.name == 'ordinary.txt':
            assert_excluded('final_retirement')
        return real_unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', unlink)

    # No credentials are hidden: every destination/control/credential boundary is
    # forbidden instead. Even an attempted migration with an inert remote fails.
    contacts = []
    def forbidden(*a, **k):
        contacts.append('R2')
        raise AssertionError('local-only reached R2')
    for name in ('from_environment', '__init__', 'store', 'upload', 'readback', 'summary'):
        monkeypatch.setattr(r2.Destination, name, forbidden)
    for name in ('__init__', 'ensure', 'request', 'check_private'):
        monkeypatch.setattr(r2.Control, name, forbidden)
    monkeypatch.setattr(r2, 'credentials', forbidden)
    monkeypatch.setattr(r2, 'migrate_archives', forbidden)
    seen = []
    source = bus / 'reports/local-archive-fixture'
    source.mkdir()
    payload = source / 'ordinary.txt'
    payload.write_bytes(b'private local-only fixture\n' * 4096)
    future = time.time() + 40 * 86400
    def local(api, m, report, **kwargs):
        assert kwargs['dry_run'] is False
        assert kwargs['archive_destination'] is None
        assert not api.path_is_active_privileged(proof)
        with monkeypatch.context() as clock:
            clock.setattr(m.time, 'time', lambda: future)
            row = m.cold_archive(api, source, report / 'fixture.tar.zst',
                pins=kwargs['pins'], dry_run=False, deadline=time.monotonic()+30,
                archive_destination=kwargs['archive_destination'])
        assert row['status'] == 'ARCHIVED_VERIFIED_RETIRED'
        assert not payload.exists()
        assert Path(row['archive']).is_file()
        seen.append('local_site_work')
        return [row]
    monkeypatch.setattr(auto, 'site_cold_archive', local)
    # Import controller candidate-bound before substituting its runtime root.
    controller = runpy.run_path(str(ROOT / 'scripts/host_disk_cleanup_large_unit.py'))
    g = controller['main'].__globals__
    home = tmp_path / 'hermes'; (home / 'runstate').mkdir(parents=True)
    monkeypatch.setitem(g, 'BUS', bus)
    monkeypatch.setitem(g, 'HOME', home)
    monkeypatch.setitem(g, 'STATE', home / 'runstate/last.json')
    # Private pressure fixture, not the host/runner's unrelated free-space state.
    monkeypatch.setitem(g, 'disk', lambda: {'free_bytes': 10 * 1024**3, 'total_bytes': 100 * 1024**3})
    monkeypatch.setattr(auto, 'external_pins', lambda: set())
    monkeypatch.setitem(sys.modules, 'post_discord_message', SimpleNamespace(
        send=lambda *a: {'id': 'fixture'}, readback=lambda *a: {}))
    real_run_path = runpy.run_path
    def candidate_run_path(path, **kwargs):
        path = str(path)
        if path in ('/home/ubuntu/business/scripts/host_disk_pruner.py',
                    '/home/ubuntu/business/scripts/host_disk_pruner_trigger.py'):
            path = str(ROOT / 'scripts' / Path(path).name)
        return real_run_path(path, **kwargs)
    monkeypatch.setattr(runpy, 'run_path', candidate_run_path)
    def process(command, **kwargs):
        if command[:3] == ['systemctl', '--user', 'is-active']:
            return SimpleNamespace(returncode=3, stdout='inactive\n')
        if command[:4] == ['systemctl', '--user', 'start', '--no-block']:
            # Exact existing unit, configured by the reviewed local-only drop-in.
            assert command[4:] == ['host-disk-cleanup.service']
            unit = (ROOT / 'ops/systemd/host-disk-cleanup-local-only.conf.example').read_text()
            assert 'host_disk_cleanup_large_unit.py --local-only\n' in unit
            seen.append('existing_unit')
            rc = entrypoint(ROOT / 'scripts/host_disk_cleanup_large_unit.py', g,
                            ['controller', '--local-only'], monkeypatch)
            assert rc == 0
            return SimpleNamespace(returncode=0)
        script = next(Path(arg).name for arg in command if str(arg).endswith('.py'))
        if script == 'host_disk_large_sweep.py':
            assert '--local-only' in command
            assert command[command.index('--budget') + 1] == '2700'
            offset = next(i for i, arg in enumerate(command) if str(arg).endswith('.py'))
            assert entrypoint(ROOT / 'scripts/host_disk_large_sweep.py', sweep.__dict__,
                              command[offset:], monkeypatch) == 0
            seen.append('sweep')
        elif script == 'host_disk_pruner.py':
            seen.append('safe_rules')
            report = Path(command[command.index('--report-dir') + 1])
            report.mkdir(parents=True)
            (report / 'receipt.json').write_text('{"reclaimed_bytes":0}')
        elif script == 'evidence_preserving_pruner.py':
            assert_excluded('D_A_B_C_final_phase')
            assert command[command.index('--max-seconds') + 1] == '1050'
            if phase_exception:
                raise RuntimeError('private phase exception')
            seen.append('D_A_B_C')
            Path(command[command.index('--report') + 1]).write_text('{"rows":[]}')
        else:
            raise AssertionError(command)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(subprocess, 'run', process)
    monkeypatch.setattr(sys, 'argv', ['host_disk_pruner.py'])
    if phase_exception:
        with pytest.raises(RuntimeError, match='private phase exception'):
            candidate_run_path(str(ROOT / 'ops/hermes-cron-scripts/host_disk_pruner.py'), run_name='__main__')
    else:
        candidate_run_path(str(ROOT / 'ops/hermes-cron-scripts/host_disk_pruner.py'), run_name='__main__')
    assert seen == ['existing_unit', 'local_site_work', 'sweep', 'safe_rules'] + ([] if phase_exception else ['D_A_B_C'])
    assert excluded == ['reference_inventory', 'final_retirement', 'D_A_B_C_final_phase']
    cp = real_run(route_command(route), capture_output=True, text=True, timeout=10)
    assert cp.returncode == 0, cp.stderr
    with (home / 'runstate/host-disk-cleanup.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert route_lock.stat().st_ino == original_inode
    assert contacts == [], 'R2 attempted even if its exception was caught'
    assert proof.read_text() == 'fixture retained proof'
    receipts = list((bus / 'reports/host-disk-prune').glob('*/large/large-classes.json'))
    assert len(receipts) == 1
    data = json.loads(receipts[0].read_text())
    assert data['r2']['status'] == 'DISABLED_LOCAL_ONLY'
    assert data['local_only'] is True
    assert data['rows'][0]['status'] == 'ARCHIVED_VERIFIED_RETIRED'


def test_registered_local_only_busy_dispatcher_defers_before_cleanup(tmp_path, monkeypatch, capsys):
    bus, _ = fixture_sweep(tmp_path, monkeypatch)
    denied = deny_network(monkeypatch)
    controller = runpy.run_path(str(ROOT / 'scripts/host_disk_cleanup_large_unit.py'))
    g = controller['main'].__globals__
    home = tmp_path / 'hermes'; (home / 'runstate').mkdir(parents=True)
    monkeypatch.setitem(g, 'BUS', bus)
    monkeypatch.setitem(g, 'HOME', home)
    monkeypatch.setitem(g, 'STATE', home / 'runstate/last.json')
    monkeypatch.setitem(g, 'disk', denied)
    monkeypatch.setitem(g, 'atomic_json', denied)
    monkeypatch.setattr(auto, 'external_pins', denied)
    route = bus / 'ops/gpt6pro-relay/route'; route.mkdir(parents=True)
    # Real maintained dispatcher singleton owns the private existing inode.
    holder = subprocess.Popen(route_command(route, 'hold'), stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == 'OWNED'
        inode = (route / 'dispatcher.lock').stat().st_ino
        controller_rc = []
        real_run_path = runpy.run_path
        def candidate_run_path(path, **kwargs):
            if str(path) == '/home/ubuntu/business/scripts/host_disk_pruner_trigger.py':
                path = ROOT / 'scripts/host_disk_pruner_trigger.py'
            return real_run_path(str(path), **kwargs)
        monkeypatch.setattr(runpy, 'run_path', candidate_run_path)
        def process(command, **kwargs):
            if command[:3] == ['systemctl', '--user', 'is-active']:
                return SimpleNamespace(returncode=3, stdout='inactive\n')
            if command[:4] == ['systemctl', '--user', 'start', '--no-block']:
                assert command[4:] == ['host-disk-cleanup.service']
                unit = (ROOT / 'ops/systemd/host-disk-cleanup-local-only.conf.example').read_text()
                assert 'host_disk_cleanup_large_unit.py --local-only\n' in unit
                controller_rc.append(entrypoint(ROOT / 'scripts/host_disk_cleanup_large_unit.py', g,
                                                ['controller', '--local-only'], monkeypatch))
                return SimpleNamespace(returncode=0)
            return denied()
        monkeypatch.setattr(subprocess, 'run', process)
        for name in ('from_environment', '__init__', 'store', 'upload', 'readback', 'summary'):
            monkeypatch.setattr(r2.Destination, name, denied)
        started = time.monotonic()
        monkeypatch.setattr(sys, 'argv', ['host_disk_pruner.py'])
        candidate_run_path(str(ROOT / 'ops/hermes-cron-scripts/host_disk_pruner.py'), run_name='__main__')
        assert controller_rc == [75]
        assert time.monotonic() - started < 2
        assert 'deferred' in capsys.readouterr().out.lower()
        assert not (home / 'runstate/host-disk-cleanup.lock').exists()
        assert not (home / 'runstate/last.json').exists()
        assert not (bus / 'reports/host-disk-prune').exists()
        assert (route / 'dispatcher.lock').stat().st_ino == inode
        assert holder.poll() is None
    finally:
        holder.stdin.write('release\n'); holder.stdin.flush()
        assert holder.wait(timeout=10) == 0
        holder.stdin.close(); holder.stdout.close(); holder.stderr.close()


def test_local_only_omits_remote_report_phase_without_lowering_local_threshold(tmp_path, monkeypatch):
    bus, _ = fixture_sweep(tmp_path, monkeypatch)
    root = bus / 'reports/private-ranked-report'; root.mkdir()
    monkeypatch.setattr(sweep, 'cold_report_roots', lambda *a, **k: [root])
    monkeypatch.setattr(sweep, 'rank_reports', lambda *a: ([(256 * 1024**2, root)], []))
    calls = []
    def archive(api, path, destination, **kwargs):
        calls.append((kwargs['cold_days'], kwargs['min_bytes'], kwargs['archive_destination']))
        return {'rule': 'cold_evidence_archive', 'status': 'REFUSED', 'bytes_freed': 0}
    monkeypatch.setattr(cold, 'cold_archive', archive)
    def denied(*a, **k):
        raise AssertionError('local report fixture attempted R2')
    monkeypatch.setattr(r2.Destination, 'from_environment', denied)
    monkeypatch.setattr(r2, 'migrate_archives', denied)
    sweep.run(tmp_path / 'report', budget=120, local_only=True)
    assert calls == [(7, 128 * 1024**2, None)]


def test_default_sweep_still_constructs_existing_destination(tmp_path, monkeypatch):
    bus, _ = fixture_sweep(tmp_path, monkeypatch)
    reached = []
    monkeypatch.setattr(r2.Destination, 'from_environment', lambda: reached.append('default') or object())
    monkeypatch.setattr(r2, 'migrate_archives', lambda *a, **k: [])
    sweep.run(tmp_path / 'report', budget=10)
    assert reached == ['default']


def test_local_only_reference_drift_still_refuses_before_work(tmp_path, monkeypatch):
    bus, proof = fixture_sweep(tmp_path, monkeypatch)
    import host_disk_relay_custody as custody
    queue = bus / 'ops/gpt6pro-relay/route/queue'
    queue.mkdir(parents=True)
    (queue / 'original.json').write_text(json.dumps({'pin': str(proof)}))
    real_lanes = custody.lane_sources
    changed = []
    def churn(*args):
        result = real_lanes(*args)
        # A noncooperative publication after relay discovery, before its final
        # complete-identity validation. The actual collector raises the error.
        if not changed:
            (queue / 'new.json').write_text(json.dumps({'pin': str(proof)}))
            changed.append(str(queue))
        return result
    monkeypatch.setattr(custody, 'lane_sources', churn)
    report = tmp_path / 'report'
    with pytest.raises(ValueError, match='relay/lane discovery changed during collection') as caught:
        sweep.run(report, budget=10, local_only=True)
    assert changed == [str(queue)]
    assert caught.value.changed_source == str(queue)
    data = json.loads((report / 'large-classes.json').read_text())
    assert data['attributable_bytes_freed'] == 0 and data['r2'] is None
    assert data['reference_veto_source'] == str(queue)
    assert proof.read_text() == 'fixture retained proof'


def test_registered_trigger_payload_drift_is_not_clean(tmp_path):
    import copy
    import scheduled_source_drift as drift
    spec = next(row for row in json.loads((ROOT / 'ops/scheduled-source-drift-manifest.json').read_text())['jobs']
                if row['id'] == '075ed81c6b43')
    spec = copy.deepcopy(spec)
    wanted = {'scripts/host_disk_pruner_trigger.py',
              'scripts/host_disk_cleanup_large_unit.py', 'scripts/host_disk_large_sweep.py'}
    assert {pin['path'].removeprefix('/home/ubuntu/business/') for pin in spec['payloads']} == wanted
    bus = tmp_path / 'business'
    home = tmp_path / 'hermes'
    (bus / 'ops/hermes-cron-scripts').mkdir(parents=True)
    (home / 'scripts').mkdir(parents=True)
    (home / 'cron').mkdir()
    source = ROOT / spec['source']
    installed = home / 'scripts/host_disk_pruner.py'
    installed.write_bytes(source.read_bytes())
    (bus / spec['source']).write_bytes(source.read_bytes())
    spec['path'] = str(installed)
    for pin in spec['payloads']:
        rel = pin['path'].removeprefix('/home/ubuntu/business/')
        path = bus / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((ROOT / rel).read_bytes())
        pin.update(path=str(path), owner=str(installed))
    files = {spec['source']: {'sha256': spec['reviewed_sha256']}}
    (bus / 'ops/rebuild-source-manifest.json').write_text(json.dumps({'files': files}))
    (bus / 'ops/scheduled-source-drift-manifest.json').write_text(json.dumps(
        {'schema': 'scheduled-source-drift-v1', 'jobs': [spec]}))
    jobs = {'jobs': [{'id': spec['id'], 'script': installed.name,
                     'script_sha256': spec['reviewed_sha256'], 'enabled': False}]}
    (home / 'cron/jobs.json').write_text(json.dumps(jobs))
    assert drift.audit(bus, home, crontab_ids=[])['status'] == 'CLEAN'
    (bus / 'scripts/host_disk_pruner_trigger.py').write_text('drifted fixture\n')
    result = drift.audit(bus, home, crontab_ids=[])
    assert result['status'] == 'DRIFT'
    assert result['counts']['payload_drift'] == 1
