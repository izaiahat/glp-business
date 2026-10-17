"""Projected installed caller, real declaration reader and bounded full planning."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
from contextlib import contextmanager

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))


@pytest.mark.parametrize('missing', ['glp_ship_attempts.py', 'glp331_carrier_adoption.py', 'glp345_a4_adoption.py',
                                    'glp44_safe_bridge_activation.py', 'glp334_pin_closure.py',
                                    'glp345_a4_adoption.py:outdated',
                                    'glp331_carrier_adoption.py:preclaim', 'inventory-opaque', None])
def test_actual_caller_authenticates_projected_installer_before_claim(tmp_path, monkeypatch, missing):
    import glp_ship as maintained
    import maintenance_window as mw
    # Real source bytes projected as an installed script directory, never host imports.
    projected = tmp_path / 'installed/scripts'
    shutil.copytree(ROOT / 'scripts', projected)
    if missing and missing.endswith(':outdated'):
        (projected / missing.split(':')[0]).write_text('# outdated declaration\n')
    elif missing and missing != 'inventory-opaque' and not missing.endswith(':preclaim'):
        (projected / missing).unlink()
    loader = importlib.util.spec_from_file_location('projected_ship', projected / 'glp_ship.py')
    ship = importlib.util.module_from_spec(loader)
    loader.loader.exec_module(ship)
    home = tmp_path / 'home'
    home.mkdir()
    source = subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip()
    @contextmanager
    def committed(*args):
        # Bypass only network/clone; executing closure authentication MUST still
        # happen in prepare, not only in the committed_checkout context manager.
        yield ROOT, {}
    monkeypatch.setattr(ship, 'committed_checkout', committed)
    monkeypatch.setattr(ship, 'preview', lambda *a, **k: {})
    monkeypatch.setattr(ship, 'derive_pins', lambda *a: {})
    import glp_ship_registry
    monkeypatch.setattr(glp_ship_registry, 'enrollment_hashes', lambda *a: {})
    monkeypatch.setattr(ship.syncer, 'reviewed_prior_hashes', lambda *a: set())
    # Missing closure must refuse before ANY payload reads or attempts.
    monkeypatch.setattr(ship, 'fingerprint', lambda *a: {'sha256': '', 'mode': 0o644})
    (home / 'cron').mkdir()
    from glp_ship_registry import registered_job
    from dataclasses import replace
    spec = replace(registered_job('aadf6677c7f0'), paths=(), wrappers=())
    monkeypatch.setattr(ship, 'registered_job', lambda *a: spec)
    (home / 'cron/jobs.json').write_text(json.dumps({'jobs': [{'id': j} for j in spec.jobs]}))
    monkeypatch.setattr(ship, 'validate_review', lambda *a: None)
    # Exercise actual MaintenanceWindow.plan and inventory.discover; the only
    # seams are external scheduler command responses and disposable metadata.
    import maintenance_inventory as inventory
    monkeypatch.setattr(inventory, '__file__', str(projected / 'maintenance_inventory.py'))
    actions = tmp_path / 'installed/ops/n8n-bridge/actions.json'
    actions.parent.mkdir(parents=True)
    entry = projected / 'glp_price_index_weekly.py'
    actions.write_text(json.dumps({'reader': {'argv': ['python3', str(entry)]}}))
    probes = []
    def probe(self, argv, payload=None):
        probes.append(argv)
        assert payload is None
        if argv == ['/usr/bin/crontab', '-l']:
            return 'PATH=/unresolved\n' if missing == 'inventory-opaque' else '0 1 * * * python3 ' + str(entry) + '\n'
        if argv[:3] == ['/usr/bin/systemctl', '--user', 'list-unit-files']:
            return 'reader.timer enabled enabled\n'
        if argv[:3] == ['/usr/bin/systemctl', '--user', 'list-units']:
            return 'reader.timer loaded active waiting\n'
        if 'show' in argv:
            if argv[3] == 'reader.timer':
                return 'Id=reader.timer\nLoadState=loaded\nActiveState=active\nUnitFileState=enabled\nTriggers=reader.service\n'
            return 'Id=n8n-bridge.service\nLoadState=loaded\nActiveState=inactive\nExecStart={ path=/usr/bin/true ; argv[]=/usr/bin/true ; }\n'
        raise AssertionError('unexpected scheduler command: ' + repr(argv))
    monkeypatch.setattr(mw.LinuxBackend, 'command', probe)
    plans = []
    real_plan = mw.MaintenanceWindow.plan
    def record_plan(self):
        report = real_plan(self)
        plans.append(report)
        return report
    monkeypatch.setattr(mw.MaintenanceWindow, 'plan', record_plan)
    effects = []
    monkeypatch.setattr(ship, 'apply_generation', lambda *a: effects.append('apply') or {'success': True})
    # Positive test stops at the durable fixture claim, not a real effect lifecycle.
    original_claim = ship.claim_attempt
    def claim(*a, **k):
        if missing and missing.endswith(':preclaim'):
            (projected / missing.split(':')[0]).unlink()
        original_claim(*a, **k)
        raise RuntimeError('fixture_claim_boundary')
    monkeypatch.setattr(ship, 'claim_attempt', claim)
    result = ship.main(['aadf6677c7f0', '--merge-sha', source, '--root', str(tmp_path / 'installed'),
                        '--hermes', str(home), '--checkout', str(ROOT), '--apply'])
    attempts = list((tmp_path / 'installed/reports/ops/glp-ship/attempts').glob('*.json'))
    assert result == 2 and not effects
    if missing:
        assert attempts == [], 'incomplete installed closure reached durable effect claim'
    else:
        assert len(attempts) == 1 and probes, 'bounded full planning did not precede claim'
        assert len(plans) == 2, 'preparation and immediate preclaim both need full fresh planning'
        assert all(set(p['coverage']) == {'hermes', 'crontab', 'systemd', 'n8n'} for p in plans)
        assert all({e['kind'] for e in p['entries']} == {'hermes', 'crontab', 'systemd', 'n8n'} for p in plans)
        assert all(not p['errors'] for p in plans)
