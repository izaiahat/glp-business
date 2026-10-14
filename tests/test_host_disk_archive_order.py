"""Registered sweep ordering under a finite synthetic clock; all effects denied."""
import json
from pathlib import Path
from types import SimpleNamespace
from test_host_disk_tmp_custody import sweep, cold, sys


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
