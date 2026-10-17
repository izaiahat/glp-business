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


def fixture_audit(tmp_path, monkeypatch, reservations):
    """Only synthetic home/ledger; never open or mutate the live session DB."""
    spec = importlib.util.spec_from_file_location('host_audit', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'HOME', tmp_path)
    (tmp_path / 'state.db').write_bytes(b'fixture, not SQLite')
    (tmp_path / 'cron').mkdir()
    (tmp_path / 'cron/jobs.json').write_text('{"jobs": []}')
    (tmp_path / 'cache').mkdir()
    (tmp_path / 'cache/delegation-host-budget.json').write_text(json.dumps(reservations))
    return module


@pytest.mark.parametrize('available', [500_000_000, 99_999_999_999, 100_000_000_000, 100_000_000_001])
def test_fleet_disk_floor_and_backup_headroom_boundaries(tmp_path, monkeypatch, available):
    from types import SimpleNamespace
    module = fixture_audit(tmp_path, monkeypatch, [])
    monkeypatch.setattr(module.shutil, 'disk_usage', lambda _: SimpleNamespace(
        total=622_000_000_000, free=available))
    report = module.snapshot()
    assert report['root_disk']['fleet_floor_bytes'] == 100_000_000_000
    assert report['root_disk']['available_bytes'] == available
    assert ('root_disk_below_fleet_floor' in report['findings']) == (available < 100_000_000_000)
    assert ('insufficient_state_backup_compaction_headroom' in report['findings']) == (
        available < 1024**3 + (tmp_path / 'state.db').stat().st_size)


@pytest.mark.parametrize('bad_row', [None, [], {'pid': None},
    {'pid': True, 'start': '1', 'token': 'bad'},
    {'pid': 1, 'start': [], 'token': 'bad'},
    {'pid': 1, 'start': '1', 'token': ''}])
def test_invalid_capacity_row_is_unknown_not_zero_or_crash(tmp_path, monkeypatch, bad_row):
    module = fixture_audit(tmp_path, monkeypatch, [bad_row])
    report = module.snapshot()
    assert report['capacity']['active_reservations'] is None
    assert 'capacity_ledger_unverifiable' in report['findings']


def healthy_fixture_audit(tmp_path, monkeypatch, reservations):
    """Make capacity the only possible finding; keep real owner stat reads."""
    from types import SimpleNamespace
    module = fixture_audit(tmp_path, monkeypatch, reservations)
    original_read = Path.read_text
    original_iterdir = Path.iterdir
    def read_text(path, *args, **kwargs):
        if path == Path('/proc/meminfo'):
            return 'MemTotal: 32000000 kB\nMemAvailable: 24000000 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n'
        if path == Path('/proc/pressure/memory'):
            return 'some avg10=0.00 avg60=0.00 avg300=0.00 total=0\n'
        return original_read(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', read_text)
    monkeypatch.setattr(Path, 'iterdir', lambda path: iter(()) if path == Path('/proc') else original_iterdir(path))
    monkeypatch.setattr(module.shutil, 'disk_usage', lambda _: SimpleNamespace(
        total=622_000_000_000, free=200_000_000_000))
    return module


@pytest.mark.parametrize('start_form', ['leading-zero', 'empty', 'plus', 'minus',
    'whitespace', 'unicode', 'decimal-point', 'overflow', 'huge'])
def test_noncanonical_live_owner_start_is_unknown_never_healthy(tmp_path, monkeypatch, start_form):
    import os
    pid = os.getpid()
    tick = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
    start = {'leading-zero': '0' + tick, 'empty': '', 'plus': '+' + tick,
             'minus': '-' + tick, 'whitespace': tick + ' ', 'unicode': '１２３',
             'decimal-point': tick + '.0', 'overflow': str(2**64), 'huge': '9' * 5000}[start_form]
    module = healthy_fixture_audit(tmp_path, monkeypatch,
        [{'pid': pid, 'start': start, 'token': 'review-5555902144'}])
    report = module.snapshot()
    observed = {key: report[key] for key in ('capacity', 'findings', 'assessment')}
    assert report['capacity']['active_reservations'] is None, observed
    assert report['findings'] == ['capacity_ledger_unverifiable']
    assert report['assessment'] == 'degraded'


@pytest.mark.parametrize('owner_state', ['live', 'absent', 'reused', 'zero', 'uint64-max', 'malformed-proc'])
def test_canonical_capacity_owner_identity_controls(tmp_path, monkeypatch, owner_state):
    import os
    pid = os.getpid()
    proc_stat = Path(f'/proc/{pid}/stat')
    stat_text = proc_stat.read_text()
    prefix, rest = stat_text.rsplit(')', 1)
    fields = rest.split()
    tick = fields[19]
    module = healthy_fixture_audit(tmp_path, monkeypatch,
        [{'pid': pid, 'start': tick, 'token': 'owner-control'}])
    original_read = Path.read_text
    def owner_read(path, *args, **kwargs):
        if path == proc_stat:
            if owner_state == 'absent':
                raise FileNotFoundError('fixture owner absent')
            current_fields = fields.copy()
            current_fields[19] = {'reused': str(int(tick) + 1), 'zero': '0',
                'uint64-max': str(2**64 - 1), 'malformed-proc': '0' + tick}.get(owner_state, tick)
            return prefix + ') ' + ' '.join(current_fields)
        return original_read(path, *args, **kwargs)
    if owner_state in {'zero', 'uint64-max'}:
        ledger = tmp_path / 'cache/delegation-host-budget.json'
        ledger.write_text(json.dumps([{'pid': pid, 'start': '0' if owner_state == 'zero' else str(2**64 - 1),
                                      'token': 'owner-control'}]))
    monkeypatch.setattr(Path, 'read_text', owner_read)
    report = module.snapshot()
    if owner_state == 'malformed-proc':
        assert report['capacity']['active_reservations'] is None
        assert report['findings'] == ['capacity_ledger_unverifiable']
        assert report['assessment'] == 'degraded'
    else:
        assert report['capacity']['active_reservations'] == (0 if owner_state in {'absent', 'reused'} else 1)
        assert report['findings'] == []
        assert report['assessment'] == 'healthy'


def test_duplicate_capacity_tokens_are_unknown(tmp_path, monkeypatch):
    import os
    pid = os.getpid()
    tick = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
    row = {'pid': pid, 'start': tick, 'token': 'same-token'}
    module = fixture_audit(tmp_path, monkeypatch, [row, row])
    report = module.snapshot()
    assert report['capacity']['active_reservations'] is None
    assert 'capacity_ledger_unverifiable' in report['findings']


def test_unreadable_capacity_owner_is_unknown_but_dead_owner_is_stale(tmp_path, monkeypatch):
    import os
    pid = os.getpid()
    tick = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
    rows = [{'pid': pid, 'start': tick, 'token': 'live'},
            {'pid': 99999999, 'start': '1', 'token': 'dead'}]
    module = fixture_audit(tmp_path, monkeypatch, rows)
    assert module.snapshot()['capacity']['active_reservations'] == 1
    original = Path.read_text
    def denied(path, *args, **kwargs):
        if path == Path(f'/proc/{pid}/stat'):
            raise PermissionError('fixture owner inspection denied')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', denied)
    report = module.snapshot()
    assert report['capacity']['active_reservations'] is None
    assert 'capacity_ledger_unverifiable' in report['findings']
