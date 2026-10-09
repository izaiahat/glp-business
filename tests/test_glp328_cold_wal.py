"""Real SQLite cold-WAL regression, with only disposable synthetic records."""
import importlib
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from test_glp328_readonly_evidence import fixture, NOW


def test_closed_wal_without_sidecars_reads_controls_without_source_mutation(tmp_path):
    fixture(tmp_path)
    path = tmp_path/'outreach.sqlite'
    connection = sqlite3.connect(path)
    assert connection.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
    connection.execute("insert into suppressions values ('cold','','','cold.example','domain',1)")
    connection.commit()
    connection.close()
    assert path.read_bytes()[18:20] == b'\x02\x02'
    assert not Path(str(path)+'-wal').exists()
    assert not Path(str(path)+'-shm').exists()
    before = {p.name:(p.read_bytes(),p.stat().st_mtime_ns,p.stat().st_ctime_ns) for p in tmp_path.iterdir()}
    adapter = importlib.import_module('glp328_readonly_evidence')
    result = adapter.read_local_sources(tmp_path,NOW)
    assert result['suppression']['complete'], result['faults']
    assert {r['domain'] for r in result['suppression']['entries']} == {'alpha.example','beta.example','cold.example'}
    assert {p.name:(p.read_bytes(),p.stat().st_mtime_ns,p.stat().st_ctime_ns) for p in tmp_path.iterdir()} == before


def test_cold_snapshot_excludes_real_sqlite_writer_for_entire_copy(tmp_path,monkeypatch):
    import os
    import subprocess
    fixture(tmp_path)
    path = tmp_path/'outreach.sqlite'
    connection = sqlite3.connect(path)
    connection.execute('PRAGMA journal_mode=WAL')
    connection.close()
    assert not Path(str(path)+'-shm').exists()
    adapter = importlib.import_module('glp328_readonly_evidence')
    before = path.read_bytes()
    original = os.read
    attempts = []
    def attempt(fd,size):
        if not attempts:
            attempts.append('running')  # subprocess itself reads an error pipe
            probe = subprocess.run([sys.executable,'-c',
                "import sqlite3,sys;c=sqlite3.connect(sys.argv[1],timeout=0)\n"
                "try:\n c.execute('BEGIN IMMEDIATE');print('acquired')\n"
                "except sqlite3.OperationalError as e:\n print('busy' if e.sqlite_errorcode==sqlite3.SQLITE_BUSY else 'unexpected')\n"
                "finally:\n c.close()",str(path)],capture_output=True,text=True,check=True,timeout=10)
            attempts[0] = probe.stdout.strip()
        return original(fd,size)
    private = tmp_path/'private'
    private.mkdir()
    monkeypatch.setattr(adapter.os,'read',attempt)
    assert adapter._capture_sqlite_snapshot(path,private)
    assert attempts == ['busy']
    assert path.read_bytes() == before
    assert not Path(str(path)+'-shm').exists()
    assert not Path(str(path)+'-wal').exists()


def test_missing_shm_never_discards_even_empty_existing_wal(tmp_path):
    fixture(tmp_path)
    path = tmp_path/'outreach.sqlite'
    connection = sqlite3.connect(path)
    connection.execute('PRAGMA journal_mode=WAL')
    connection.close()
    Path(str(path)+'-wal').write_bytes(b'')
    adapter = importlib.import_module('glp328_readonly_evidence')
    result = adapter.read_local_sources(tmp_path,NOW)
    assert not result['suppression']['complete']
    assert not Path(str(path)+'-shm').exists()
