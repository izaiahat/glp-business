"""Registered sweep ordering under a finite synthetic clock; all effects denied."""
import json
from pathlib import Path
from types import SimpleNamespace
from test_host_disk_tmp_custody import sweep, cold, sys


def test_scratch_24h_cleanup_precedes_slow_classes_and_reference_drift(tmp_path, monkeypatch):
    """A finite pass must reach idle scratch before other classes poison its generation."""
    import host_disk_reference_guard
    clock = [0.0]
    source = tmp_path / 'lane-generation'
    source.write_text('admitted')
    target = tmp_path / 'idle-14-days'
    report = tmp_path / 'receipt'
    monkeypatch.setattr(sweep.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(sweep, 'BUS', tmp_path)
    monkeypatch.setattr(sweep, 'api', SimpleNamespace(path_is_active_privileged=lambda p: False))
    def references(report):
        cold.write_json(report / 'references.json',
                        {'reference_source_identity': {str(source): cold.signature(source)}})
        return set()
    monkeypatch.setattr(sweep, 'references', references)
    monkeypatch.setattr(sweep, 'load_external_pins', lambda p: set())
    monkeypatch.setattr(sweep, 'idle_targets', lambda *a, **kw: [target])
    order = []
    def idle(api, targets, **kw):
        order.append('scratch')
        assert targets == [target]
        assert clock[0] < kw['deadline']
        assert not api.path_is_active_privileged(target), 'pre-scratch classes invalidated the complete index'
        return [{'rule': 'idle_scratch_tmp', 'status': 'ELIGIBLE', 'bytes_freed': 0}]
    def slow(*a, **kw):
        order.append('other')
        source.write_text('new unrelated lane generation')
        clock[0] = 1801.0
        return []
    monkeypatch.setattr(cold, 'idle_scratch', idle)
    monkeypatch.setattr(cold, 'cleanup_abandoned_archives', slow)
    monkeypatch.setattr(sweep, 'auto_classes', lambda api, report, pins, external, **kw: (pins, []))
    monkeypatch.setattr(sweep, 'discover', lambda *a, **kw: [])
    monkeypatch.setattr(cold, 'node_modules', lambda *a, **kw: [])
    monkeypatch.setattr(cold, 'verified_duplicates', lambda *a, **kw: [])
    monkeypatch.setitem(sys.modules, 'host_disk_completed_dependencies', SimpleNamespace(run=slow))
    monkeypatch.setattr(sweep, 'cold_report_roots', lambda *a, **kw: [])
    monkeypatch.setattr(sweep, 'rank_reports', lambda *a: ([], []))
    sweep.run(report, dry_run=True, budget=1800, external_pins='fixture-only', local_only=True)
    assert order[0] == 'scratch'
    assert order.count('scratch') == 1


def test_site_archive_receives_budget_before_report_backlog(tmp_path, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(sweep.time, 'monotonic', lambda: clock[0])
    report = tmp_path / 'receipt'
    root = tmp_path / 'reports' / 'cold'
    root.mkdir(parents=True)
    monkeypatch.setattr(sweep, 'BUS', tmp_path)
    def references(report):
        cold.write_json(report / 'references.json', {'reference_source_identity': {}})
        return set()
    monkeypatch.setattr(sweep, 'references', references)
    monkeypatch.setattr(sweep, 'load_external_pins', lambda p: set())
    monkeypatch.setattr(sweep, 'auto_classes', lambda api, report, pins, external, **kw: (pins, []))
    monkeypatch.setattr(sweep, 'discover', lambda *a, **kw: [])
    monkeypatch.setattr(sweep, 'idle_targets', lambda *a, **kw: [])
    monkeypatch.setattr(cold, 'cleanup_abandoned_archives', lambda *a, **kw: [])
    monkeypatch.setattr(cold, 'node_modules', lambda *a, **kw: [])
    monkeypatch.setattr(cold, 'idle_scratch', lambda *a, **kw: [])
    monkeypatch.setitem(sys.modules, 'host_disk_completed_dependencies', SimpleNamespace(run=lambda *a, **kw: []))
    monkeypatch.setitem(sys.modules, 'host_disk_r2', SimpleNamespace(migrate_archives=lambda *a, **kw: []))
    monkeypatch.setattr(sweep, 'cold_report_roots', lambda *a, **kw: [root])
    monkeypatch.setattr(sweep, 'rank_reports', lambda *a: ([(1024**3, root)], []))
    order = []
    def archive(*a, **kw):
        order.append('report')
        clock[0] = 1801.0
        return {'bytes_freed': 0, 'status': 'EMPTY'}
    def site(*a, **kw):
        order.append('site')
        assert 0 < kw['deadline'] <= 600
        return []
    monkeypatch.setattr(cold, 'cold_archive', archive)
    import host_disk_auto_classes as auto
    monkeypatch.setattr(auto, 'site_cold_archive', site)
    sweep.run(report, dry_run=True, budget=1800, external_pins='fixture-only')
    assert order == ['site', 'report']
