"""Narrow fixed GLP80 successor; offline, no resident/network/restart effects."""
import hashlib
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import pytest

ROOT = Path(__file__).resolve().parents[1]

@pytest.fixture
def activation():
    spec = importlib.util.spec_from_file_location('glp80_activation_fixture', ROOT / 'scripts/glp44_safe_bridge_activation.py')
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def test_fixed_successor_preserves_glp338_edge_and_real_drain(activation, tmp_path, monkeypatch):
    assert hasattr(activation, 'PricingRegistryHost'), 'missing maintained GLP80 successor'
    assert activation.REGISTRY_BEFORE == '335c4f0cc2b2933ac6ddda4a3f9eb674e8699a08534a71ea54038ee8ceb401c7'
    assert activation.REGISTRY_AFTER == '32d9d635a43761cc425f5275b682cc8929f7c4916536bc169358a3023cff2a47'
    host = activation.PricingRegistryHost()
    assert host.identity.__func__ is activation.RegistryHost.identity
    assert host.quiet.__func__ is activation.LinuxHost.quiet
    assert host.exclusive_source.__func__ is activation.RegistryHost.exclusive_source
    assert host.readback.__func__ is activation.RegistryHost.readback
    assert host.importer().__file__ == str(ROOT / 'scripts/sync_from_repo.py')
    monkeypatch.setattr(activation, 'BUSINESS', tmp_path)
    monkeypatch.setattr(activation, 'BRIDGE', tmp_path / 'bridge.py')
    monkeypatch.setattr(activation, 'REGISTRY_BRIDGE_SHA256', hashlib.sha256(b'bridge').hexdigest())
    (tmp_path / 'bridge.py').write_bytes(b'bridge')
    monkeypatch.setattr(activation, 'PRICING_BEFORE', {'a': hashlib.sha256(b'old').hexdigest(), 'b': None})
    monkeypatch.setattr(activation, 'PRICING_AFTER', {'a': hashlib.sha256(b'new').hexdigest(), 'b': hashlib.sha256(b'payload').hexdigest()})
    (tmp_path / 'a').write_bytes(b'old')
    host.source()
    (tmp_path / 'b').write_bytes(b'extra')
    with pytest.raises(activation.Refused, match='pricing_generation_mismatch'): host.source()
    (tmp_path / 'b').unlink()
    (tmp_path / 'a').write_bytes(b'new')
    (tmp_path / 'b').write_bytes(b'payload')
    host.imported = True
    host.source()
    (tmp_path / 'b').write_bytes(b'drift')
    with pytest.raises(activation.Refused, match='pricing_generation_mismatch'): host.source()


def test_successor_source_plan_and_apply_guard_are_read_only(activation, monkeypatch, capsys):
    monkeypatch.setattr(activation, 'read_regular', lambda *a: pytest.fail('plan host read'))
    monkeypatch.setattr(activation.LinuxHost, 'command', lambda *a, **k: pytest.fail('plan command'))
    control = activation.CONTROL
    assert activation.main(['pricing-registry-plan']) == 0
    import json
    value = json.loads(capsys.readouterr().out)
    assert value['after'][activation.REGISTRY_REL].startswith('0593f4c6')
    assert value['runtime_authorization'] is False
    assert activation.main(['activate-pricing-registry', '--pid', '100', '--start-ticks', '1000', '--wait', '900']) == 2
    assert 'apply_required' in capsys.readouterr().out
    assert activation.CONTROL == control

@pytest.mark.parametrize('failure', [None, 'preview', 'apply', 'zero', 'drain', 'restart'])
def test_fixed_successor_orders_one_scoped_transaction(activation, monkeypatch, failure):
    assert hasattr(activation, 'PricingRegistryHost'), 'missing maintained GLP80 successor'
    host = activation.PricingRegistryHost()
    host.owner = object()
    host.resident_identity = (100, 1000)
    events = []
    def sync(apply, adoption):
        assert adoption == ()
        phase = ['preview', 'apply', 'zero'][sum(x in ['preview', 'apply', 'zero'] for x in events)]
        events.append(phase)
        return 2 if failure == phase else 0
    monkeypatch.setattr(host, 'scoped_sync', sync)
    monkeypatch.setattr(host, 'source', lambda: events.append('source'))
    monkeypatch.setattr(host, 'journal', lambda **kw: events.append(kw['phase']))
    def drained(*args):
        events.append('drain')
        return failure != 'drain'
    monkeypatch.setattr(activation, 'drained', drained)
    def restart(self):
        events.append('restart')
        if failure == 'restart': raise activation.Refused('fixture restart uncertainty')
    monkeypatch.setattr(activation.LinuxHost, 'restart', restart)
    if failure:
        with pytest.raises(activation.Refused): host.restart()
    else:
        host.restart()
        assert host.imported
        assert events.index('preview') < events.index('apply') < events.index('zero') < events.index('restart')
    assert events.count('apply') <= 1 and events.count('restart') <= 1
    if failure in ['preview', 'drain']: assert 'apply' not in events

def test_fixed_successor_selection_and_privilege_restore(activation, monkeypatch):
    assert hasattr(activation, 'PricingRegistryHost'), 'missing maintained GLP80 successor'
    host = activation.PricingRegistryHost(); host.owner = object()
    uid = {'value': 0}
    monkeypatch.setattr(activation.os, 'geteuid', lambda: uid['value'])
    monkeypatch.setattr(activation.os, 'getegid', lambda: 0)
    monkeypatch.setattr(activation.os, 'seteuid', lambda value: uid.update(value=value))
    monkeypatch.setattr(activation.os, 'setegid', lambda value: None)
    def sync(root, checkout, apply, adoption, only, **kw):
        assert uid['value'] == 1000 and adoption == ()
        assert tuple(only) == tuple(activation.PRICING_AFTER)
        assert kw['_source_owner'] is host.owner
        raise OSError('fixture unknown acknowledgement')
    host.syncer = SimpleNamespace(sync=sync)
    with pytest.raises(OSError): host.scoped_sync(True, ())
    assert uid['value'] == 0


def test_successor_actual_importer_five_paths_preserves_unselected(activation, tmp_path, monkeypatch, capsys):
    import json
    host = activation.PricingRegistryHost()
    host.syncer = host.importer()
    root = tmp_path / 'receiving'; root.mkdir()
    previous = {'unrelated': 'retained-baseline'}
    for rel in activation.PRICING_AFTER:
        path = root / rel; path.parent.mkdir(parents=True, exist_ok=True)
        if rel != 'scripts/glp_price_freshness.py':
            path.write_bytes(b'offline predecessor fixture\n')
            path.chmod(0o640)
            previous[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    (root / 'unrelated').write_text('live unrelated bytes')
    state = root / 'ops/.rebuild-sync-state.json'
    state.write_text(json.dumps({'schema': 1, 'hashes': previous}))
    monkeypatch.setattr(activation, 'BUSINESS', root)
    monkeypatch.setattr(activation.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(activation.os, 'seteuid', lambda value: None)
    monkeypatch.setattr(activation.os, 'setegid', lambda value: None)
    with host.syncer.source_lock(root) as owner:
        host.owner = owner
        assert host.scoped_sync(False, ()) == 0
        assert 'DRY_RUN diffs=5 conflicts=0' in capsys.readouterr().out
        assert host.scoped_sync(True, ()) == 0
        capsys.readouterr()
        assert host.scoped_sync(False, ()) == 0
        assert 'DRY_RUN diffs=0 conflicts=0' in capsys.readouterr().out
    for rel, expected in activation.PRICING_AFTER.items():
        assert hashlib.sha256((root / rel).read_bytes()).hexdigest() == expected
        if rel in previous: assert (root / rel).stat().st_mode & 0o777 == 0o640
    assert (root / 'unrelated').read_text() == 'live unrelated bytes'
    assert json.loads(state.read_text()) == {'schema': 1, 'hashes': {
        **previous, **activation.PRICING_AFTER}}
    assert not (root / 'ops/rebuild-source-manifest.json').exists()
