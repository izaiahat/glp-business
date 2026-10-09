"""Regression for the old kael audit's unbounded LLM/autofix/send pipeline."""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'host_health_audit.py'


import importlib.util
import pytest


@pytest.mark.skipif(not Path('/home/ubuntu/.hermes/state.db').exists(), reason='live-host integration')
def test_audit_produces_fresh_local_report_without_external_effects(tmp_path):
    assert SCRIPT.exists(), 'replace legacy Opus/autofix/email with a local audit producer'
    result = subprocess.run([sys.executable, str(SCRIPT), '--report-dir', str(tmp_path)],
                            text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
    latest = json.loads((tmp_path / 'LATEST.json').read_text())
    assert latest['schema'] == 'glp-host-health-audit-v1'
    assert latest['effects'] == {'process_signals': 0, 'db_writes': 0,
                                'external_sends': 0, 'inference_calls': 0}
    assert latest['memory']['MemTotal'] > 0
    assert latest['memory']['MemAvailable'] > 0
    assert latest['state_db']['main_bytes'] > 0
    assert latest['state_db']['fleet_threshold_bytes'] == 8_000_000_000
    assert latest['root_disk']['free_bytes'] > 0
    assert latest['capacity']['active_reservations'] >= 0
    assert latest['assessment'] in {'healthy', 'degraded'}
    assert len(list(tmp_path.glob('audit-*.json'))) == 1
    assert str(tmp_path / 'LATEST.json') in result.stdout
    text = (tmp_path / 'LATEST.json').read_text()
    for forbidden in ('DISCORD_BOT_TOKEN', 'cmdline', 'environ', 'prompt', 'password'):
        assert forbidden not in text


def test_missing_store_is_unknown_and_reported_not_healthy(tmp_path):
    spec = importlib.util.spec_from_file_location('host_audit', SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    setattr(module, 'HOME', tmp_path)
    report = module.snapshot()
    assert report['state_db']['main_bytes'] is None
    assert report['capacity']['active_reservations'] is None
    assert report['assessment'] == 'degraded'
    assert 'state_db_missing' in report['findings']
    assert 'housekeeping_registry_unavailable' in report['findings']
    assert 'capacity_ledger_unavailable' in report['findings']


@pytest.mark.parametrize('alias_kind', ['symlink', 'hardlink'])
def test_main_never_reuses_temp_alias_or_overwrites_outside_inode(tmp_path, monkeypatch, alias_kind):
    import os
    import stat
    spec = importlib.util.spec_from_file_location('host_audit', SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    report_dir = tmp_path / 'reports'
    report_dir.mkdir()
    outside = tmp_path / 'outside'
    outside.write_bytes(b'outside sentinel\n')
    outside.chmod(0o640)
    for alias in (report_dir / 'LATEST.json.tmp', report_dir / 'LATEST.json'):
        if alias_kind == 'symlink':
            alias.symlink_to(outside)
        else:
            os.link(outside, alias)
    before = outside.stat()
    monkeypatch.setattr(module, 'snapshot', lambda: {'assessment': 'healthy', 'findings': []})
    monkeypatch.setattr(sys, 'argv', [str(SCRIPT), '--report-dir', str(report_dir)])
    old_umask = os.umask(0o077)
    try:
        assert module.main() == 0
    finally:
        os.umask(old_umask)
    after = outside.stat()
    assert outside.read_bytes() == b'outside sentinel\n'
    assert stat.S_IMODE(after.st_mode) == 0o640
    assert after.st_ino == before.st_ino
    latest = report_dir / 'LATEST.json'
    assert not latest.is_symlink()
    assert latest.stat().st_ino != outside.stat().st_ino
    assert stat.S_IMODE(latest.stat().st_mode) == 0o600
    assert json.loads(latest.read_text()) == {'assessment': 'healthy', 'findings': []}
    assert len(list(report_dir.glob('audit-*.json'))) == 1
    assert not list(report_dir.glob('.LATEST.json.*.tmp'))


def test_report_writer_cleans_exclusive_temp_on_failure(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('host_audit', SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    path = tmp_path / 'LATEST.json'
    path.write_bytes(b'previous report\n')
    def fail_replace(*args):
        raise OSError('fixture rename failure')
    monkeypatch.setattr(module.os, 'replace', fail_replace)
    with pytest.raises(OSError, match='fixture rename failure'):
        module.write_json(path, {'new': True})
    assert path.read_bytes() == b'previous report\n'
    assert sorted(p.name for p in tmp_path.iterdir()) == ['LATEST.json']
