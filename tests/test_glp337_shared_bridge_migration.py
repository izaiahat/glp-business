"""Fixed GLP337 source custody only; fake authority is never live acceptance."""
import json
import subprocess
from pathlib import Path

from scripts import cron_bridge_action as cron
import hashlib
import contextlib
import pytest

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = 'scripts/cron_bridge_action.py'
CADENCE = 'scripts/glp_analytics_brain_cadence.py'
BASE = '4e8da708900178d32b2f165e1d18067750b45dabb5a115d3487b4d1a9d2d6bcb'


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args])


def put(root, path, body, mode=0o640):
    dest = root / path
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(body)
    dest.chmod(mode)


def fixture(tmp_path):
    repo, business = tmp_path / 'repo', tmp_path / 'business'
    repo.mkdir(); business.mkdir()
    old = git(ROOT, 'show', 'e2fa6efc6070708132b576e57182f648af0b0038:' + LAUNCHER)
    assert hashlib.sha256(old).hexdigest() == BASE
    resident = old.replace(b'\"gpt-6-sol\"', b'\"gpt-6.1-sol\"')
    assert hashlib.sha256(resident).hexdigest() == 'f00f66759a9bdc3a0f3a4156c5fbe8a6acc73597f5b83732e818b9cc7dc1ec55'
    prior_cadence = git(ROOT, 'show', '84427444a4c84ff66380506d29fb9e0e1fc7d8aa:' + CADENCE)
    git(repo, 'init', '-q'); git(repo, 'config', 'user.email', 'fixture@example.invalid')
    git(repo, 'config', 'user.name', 'Offline fixture')
    put(repo, CADENCE, prior_cadence)
    git(repo, 'add', CADENCE); git(repo, 'commit', '-qm', 'authentic cadence predecessor')
    # This migration accepts one fixed historical pair, not later carrier guards.
    for p, expected in ((LAUNCHER, 'f1055dea298e8354bd61d9ccad084a2b33ab9c028ed7070b54e23d64ad023032'),
                        (CADENCE, '4d51a627d7a5208238ab7ce7622772c118d9b141497942bf9ed8bd6d4587bcb2')):
        body = git(ROOT, 'show', '898b45e903dffcce4046aa5548fc3e6f23b60b78:' + p)
        assert hashlib.sha256(body).hexdigest() == expected
        put(repo, p, body, 0o755)
    manifest = {'schema': 'GLP_BUSINESS_SOURCE_V1', 'tracked_count': 3,
                'files': {p: {'sha256': hashlib.sha256((repo / p).read_bytes()).hexdigest()}
                          for p in (LAUNCHER, CADENCE)}}
    put(repo, 'ops/rebuild-source-manifest.json', json.dumps(manifest).encode())
    git(repo, 'add', '.'); git(repo, 'commit', '-qm', 'fixed reviewed successor')
    put(business, LAUNCHER, resident); put(business, CADENCE, prior_cadence)
    put(business, 'ops/.rebuild-sync-state.json', json.dumps({'hashes': {LAUNCHER: BASE, 'unselected.py': 'retained'}}).encode(), 0o600)
    for p in ('ops/scheduled-source-drift-manifest.json', 'ops/n8n-bridge/actions.json', 'ops/n8n-bridge/bridge.py'):
        put(business, p, b'{"fixture": "registered actions context only"}' if p.endswith('actions.json') else (ROOT / p).read_bytes())
    return repo, business


def approvals(tmp_path, proposal):
    review, authority = tmp_path / 'review.json', tmp_path / 'authority.json'
    review.write_text(json.dumps({'schema': 'glp337-shared-bridge-review-v1',
        'decision': 'ACCEPT_EXACT_MIGRATION', 'prepared_by': 'fixture-author',
        'reviewer': 'fixture-independent-reviewer', 'proposal': proposal}))
    authority.write_text(json.dumps({'schema': 'glp337-shared-bridge-authority-v1',
        'decision': 'AUTHORIZE_EXACT_MIGRATION', 'owner': 'fixture-install-owner',
        'writer_exclusion': 'fixture only, not receiving-host authority',
        'review_sha256': hashlib.sha256(review.read_bytes()).hexdigest(), 'proposal': proposal}))
    return review, authority


def migration():
    path = ROOT / 'scripts/shared_bridge_predecessor_migration.py'
    assert path.is_file(), 'fixed GLP337 migration is missing'
    import importlib.util
    spec = importlib.util.spec_from_file_location('glp337_migration', path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


@contextlib.contextmanager
def excluded():
    # Fixture only: receiving-host exclusion is NOT accepted by these tests.
    yield True


@pytest.mark.parametrize('cadence_baseline_present', [False, True])
def test_authentic_pair_migrates_only_pair_state_under_guarded_importer(tmp_path, cadence_baseline_present):
    module = migration()
    repo, business = fixture(tmp_path)
    if cadence_baseline_present:
        state_path = business / 'ops/.rebuild-sync-state.json'
        state = json.loads(state_path.read_bytes())
        state['hashes'][CADENCE] = module.CADENCE_BEFORE
        state_path.write_text(json.dumps(state))
    observed = {p: (business / p).read_bytes() for p in
                ('ops/scheduled-source-drift-manifest.json', 'ops/n8n-bridge/actions.json', 'ops/n8n-bridge/bridge.py')}
    proposal = module.prepare(repo, business)
    assert proposal['bindings_count'] == 20
    assert proposal['selected'] == [LAUNCHER, CADENCE]
    review, authority = approvals(tmp_path, proposal)
    assert module.apply(repo, business, review, authority, reader_exclusion=excluded)['applied'] is True
    for p in (LAUNCHER, CADENCE):
        assert (business / p).read_bytes() == (repo / p).read_bytes()
        assert (business / p).stat().st_mode & 0o777 == 0o640
    state = json.loads((business / 'ops/.rebuild-sync-state.json').read_bytes())
    assert state['hashes']['unselected.py'] == 'retained'
    assert state['hashes'][LAUNCHER] == hashlib.sha256((repo / LAUNCHER).read_bytes()).hexdigest()
    assert state['hashes'][CADENCE] == hashlib.sha256((repo / CADENCE).read_bytes()).hexdigest()
    assert all((business / p).read_bytes() == b for p, b in observed.items())
    custody = business / 'reports/ops/pin-archive/shared-bridge' / hashlib.sha256(review.read_bytes()).hexdigest()
    assert hashlib.sha256((custody / (module.PREDECESSOR + '.bytes')).read_bytes()).hexdigest() == module.PREDECESSOR
    assert (custody / 'preimages.json').is_file()


@pytest.mark.parametrize('case', ['launcher', 'cadence', 'baseline', 'successor', 'state-mode', 'inventory', 'bridge', 'actions', 'head', 'self-review', 'authority', 'exclusion'])
def test_moved_or_unaccepted_inputs_refuse_without_source_or_state_writes(tmp_path, case):
    module = migration(); repo, business = fixture(tmp_path)
    proposal = module.prepare(repo, business)
    review, authority = approvals(tmp_path, proposal)
    target = {'launcher': business / LAUNCHER, 'cadence': business / CADENCE,
              'baseline': business / 'ops/.rebuild-sync-state.json',
              'successor': repo / LAUNCHER, 'inventory': business / module.CONTEXT[0],
              'bridge': business / module.CONTEXT[2], 'actions': business / module.CONTEXT[1]}.get(case)
    if target:
        target.write_bytes(target.read_bytes() + b'\n# moved')
    elif case == 'state-mode':
        (business / 'ops/.rebuild-sync-state.json').chmod(0o644)
    elif case == 'head':
        git(repo, 'commit', '--allow-empty', '-qm', 'moved head')
    elif case == 'self-review':
        d = json.loads(review.read_text()); d['reviewer'] = d['prepared_by']; review.write_text(json.dumps(d))
    elif case == 'authority':
        d = json.loads(authority.read_text()); d['review_sha256'] = '0' * 64; authority.write_text(json.dumps(d))
    before = {p: p.read_bytes() for p in (business / LAUNCHER, business / CADENCE,
                                        business / 'ops/.rebuild-sync-state.json')}
    with pytest.raises((RuntimeError, ValueError)):
        module.apply(repo, business, review, authority,
                     reader_exclusion=None if case == 'exclusion' else excluded)
    assert all(p.read_bytes() == body for p, body in before.items())
    assert not (business / 'reports/ops/pin-archive').exists()


def test_ordinary_importer_still_refuses_the_authenticated_literal_edit(tmp_path):
    from scripts import sync_from_repo as importer
    repo, business = fixture(tmp_path)
    before = (business / LAUNCHER).read_bytes()
    assert importer.sync(business, repo, True, (CADENCE,), (LAUNCHER, CADENCE)) == 2
    assert (business / LAUNCHER).read_bytes() == before


def test_intervening_preimage_change_under_exclusion_refuses_before_custody(tmp_path):
    module = migration(); repo, business = fixture(tmp_path)
    review, authority = approvals(tmp_path, module.prepare(repo, business))
    @contextlib.contextmanager
    def raced():
        (business / module.CONTEXT[0]).write_bytes(b'{"jobs": []}')
        yield True
    before = (business / LAUNCHER).read_bytes()
    with pytest.raises(RuntimeError, match='preimage_changed'):
        module.apply(repo, business, review, authority, reader_exclusion=raced)
    assert (business / LAUNCHER).read_bytes() == before
    assert not (business / 'reports/ops/pin-archive').exists()


def test_private_handoff_cannot_widen_cadence_successor(tmp_path):
    from scripts import sync_from_repo as importer
    module = migration(); repo, business = fixture(tmp_path)
    put(repo, CADENCE, b'# arbitrary but manifest-pinned cadence\n')
    manifest = json.loads((repo / importer.MANIFEST).read_bytes())
    manifest['files'][CADENCE]['sha256'] = hashlib.sha256((repo / CADENCE).read_bytes()).hexdigest()
    put(repo, importer.MANIFEST, json.dumps(manifest).encode())
    with pytest.raises(ValueError, match='fixed paired'):
        importer._sync_locked(business, repo, False, (CADENCE,), (LAUNCHER, CADENCE),
                              _shared_bridge_preimage=module.PREDECESSOR)


def test_curated_inventory_changes_only_known_shared_source_pins():
    module = migration()
    # This fixed migration's inventory belongs to its immutable approved
    # source, not later independently reviewed native-pin inventory successors.
    inventory = json.loads(git(ROOT, 'show',
        '898b45e903dffcce4046aa5548fc3e6f23b60b78:' + module.CONTEXT[0]))
    predecessor = json.loads(git(ROOT, 'show', '375491444eca0dd5f159eaa09f5379a055e12e3d:' + module.CONTEXT[0]))
    launcher_count = cadence_count = 0
    for row in predecessor['jobs']:
        for source in row.get('sources', []):
            if source.get('source') == LAUNCHER:
                source['reviewed_sha256'] = module.SUCCESSOR; launcher_count += 1
            elif source.get('source') == CADENCE:
                source['reviewed_sha256'] = module.CADENCE_AFTER; cadence_count += 1
    assert launcher_count == 20 and cadence_count > 0
    assert inventory == predecessor


def test_paired_caller_and_child_fences_do_not_self_deadlock(tmp_path, monkeypatch):
    import importlib.util
    import fcntl
    spec = importlib.util.spec_from_file_location('glp337_cadence', ROOT / CADENCE)
    cadence = importlib.util.module_from_spec(spec); spec.loader.exec_module(cadence)
    monkeypatch.setattr(cadence, 'READER_LOCK', tmp_path / 'analytics_brain.lock')
    calls = []
    def admitted(phase, **kwargs):
        with (tmp_path / 'analytics_brain.state.lock').open('a') as contender:
            with pytest.raises(BlockingIOError):
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        calls.append(phase)
        return {'status': 'PASS_FIXTURE', 'output_count': 0}, 0
    monkeypatch.setattr(cadence, '_run_admitted', admitted)
    def bridge(path, payload=None):
        assert path == '/run'
        receipt, code = cadence.run('hourly')
        assert code == 0
        return 200, {'job_id': 'fixture-child', 'status': 'done', 'rc': code}
    result = cron.run_action('analytics_brain', {}, state_dir=tmp_path,
        actions={'analytics_brain': {'timeout_s': 1}}, call=bridge,
        finish=lambda *a: {}, notify=lambda *a: {}, poll_interval=0)
    assert result['rc'] == 0 and calls == ['hourly']


@pytest.mark.parametrize('launch_fails', [False, True])
def test_host_sol_preserves_literal_and_durable_no_replay(tmp_path, monkeypatch, launch_fails):
    import sys
    import types
    # Only the private router's directory constant is consumed by this function;
    # do not import the router's uncurated live action-items/send dependencies.
    router = types.ModuleType('ops.decisions_router')
    router.AGENT_DIR = tmp_path
    monkeypatch.setitem(sys.modules, 'ops.decisions_router', router)
    state, calls = {}, []
    state_path = tmp_path / 'state.json'
    def fake_spawn(command, **kwargs):
        assert json.loads(state_path.read_text())['decisions_jobs']['sol']['status'] == 'launch_unknown'
        calls.append(command)
        assert command[command.index('--model') + 1] == 'gpt-6.1-sol'
        assert command[command.index('--provider') + 1] == 'openai-codex'
        assert command[command.index('--reasoning') + 1] == 'xhigh'
        if launch_fails:
            raise OSError('fixture unconfirmed launch')
        class Process:
            returncode = 0
            def communicate(self, timeout):
                assert timeout == 3300
                return b'fixture', b''
        return Process()
    monkeypatch.setattr(subprocess, 'Popen', fake_spawn)
    args = ('fixture prompt', str(tmp_path / 'receipt.json'), 'fixture', state, state_path)
    if launch_fails:
        with pytest.raises(OSError):
            cron.decisions_host_sol(*args)
        with pytest.raises(RuntimeError, match='unconfirmed_no_replay'):
            cron.decisions_host_sol(*args)
    else:
        assert cron.decisions_host_sol(*args)['rc'] == 0
        assert cron.decisions_host_sol(*args)['rc'] == 0
    assert len(calls) == 1
