"""Scratch-only real registered producer; no installed state/credentials."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import pytest
import fcntl
import hashlib


PRODUCER = Path(__file__).resolve().parents[1] / 'scripts/hermes-state-backup.sh'
GIB = 1024**3


def run_backup(tmp_path, home, injection=''):
    # Model space independently of hosted runner capacity, not a production knob.
    hook = tmp_path / 'hook'
    hook.mkdir(exist_ok=True)
    (hook / 'sitecustomize.py').write_text(
        'import os\nfrom types import SimpleNamespace\n'
        'os.statvfs = lambda p: SimpleNamespace(f_bavail=100*1024**3, f_frsize=1)\n'
        + injection)
    return subprocess.run(['bash', str(PRODUCER)], capture_output=True, text=True,
        timeout=90, env={**os.environ, 'HERMES_HOME': str(home),
        'HERMES_BACKUP_ROOT': str(tmp_path / 'backups'), 'TMPDIR': str(tmp_path),
        'PYTHONPATH': str(hook), 'PYTHONDONTWRITEBYTECODE': '1'})


def complete(tmp_path):
    return sorted(p for p in (tmp_path / 'backups').glob('snapshot-*')
                  if (p / 'COMPLETE').is_file())


def test_real_second_incremental_run_links_only_backup_inodes(tmp_path):
    home = tmp_path / 'home'
    (home / 'memories').mkdir(parents=True)
    (home / 'cron').mkdir()
    (home / 'config.yaml').write_text('synthetic: true\n')
    (home / 'memories' / 'MEMORY.md').write_bytes(b'fixture' * 200000)
    (home / 'cron' / 'jobs.json').write_text('[]')
    (home / 'cache').mkdir()
    (home / 'cache' / 'rebuildable').write_text('excluded')
    first = run_backup(tmp_path, home)
    assert first.returncode == 0, first.stdout + first.stderr
    snapshots = complete(tmp_path)
    assert len(snapshots) == 1, first.stdout
    second = run_backup(tmp_path, home)
    assert second.returncode == 0, second.stdout + second.stderr
    a, b = complete(tmp_path)
    for rel in ('config.yaml', 'memories/MEMORY.md', 'cron/jobs.json'):
        old, new, live = a / 'data' / rel, b / 'data' / rel, home / rel
        assert old.stat().st_ino == new.stat().st_ino
        assert new.stat().st_ino != live.stat().st_ino
        assert new.read_bytes() == live.read_bytes()
        assert new.stat().st_mode & 0o777 == 0o600
    assert not (b / 'data' / 'cache').exists()
    manifest = json.loads((b / 'backup-coverage.json').read_text())
    assert manifest['status'] == 'COMPLETE'
    assert manifest['ordinary']['count'] == 3
    assert manifest['ordinary']['bytes'] == sum((home / r).stat().st_size for r in manifest['ordinary']['files'])
    assert manifest['ordinary']['new_bytes'] == 0
    assert manifest['minimum_free_bytes'] == 50 * GIB
    (home / 'config.yaml').write_text('synthetic: changed\n')
    third = run_backup(tmp_path, home)
    assert third.returncode == 0, third.stdout + third.stderr
    c = complete(tmp_path)[-1]
    assert (a / 'data/config.yaml').read_text() == 'synthetic: true\n'
    assert (c / 'data/config.yaml').stat().st_ino != (a / 'data/config.yaml').stat().st_ino
    # A previously covered selected top-level item disappearing is not a new
    # COMPLETE baseline; otherwise retention could silently erase its last copy.
    (home / 'config.yaml').unlink()
    refused = run_backup(tmp_path, home)
    assert refused.returncode != 0
    assert complete(tmp_path) == [a, b, c]


def test_headroom_refusal_preserves_all_prior_and_no_partial(tmp_path):
    home = tmp_path / 'home'
    home.mkdir()
    (home / 'config.yaml').write_text('fixture')
    assert run_backup(tmp_path, home).returncode == 0
    old = complete(tmp_path)[0]
    before = {p.relative_to(old): p.read_bytes() for p in old.rglob('*') if p.is_file()}
    refused = run_backup(tmp_path, home,
        'os.statvfs = lambda p: SimpleNamespace(f_bavail=50*1024**3-1, f_frsize=1)\n')
    assert refused.returncode == 75
    assert 'SKIP insufficient headroom' in refused.stdout
    assert complete(tmp_path) == [old]
    assert not list((tmp_path / 'backups').glob('.snapshot-*'))
    assert before == {p.relative_to(old): p.read_bytes() for p in old.rglob('*') if p.is_file()}


def test_runtime_rsync_headroom_refusal_reaps_child(tmp_path):
    home = tmp_path / 'home'
    home.mkdir()
    (home / 'config.yaml').write_bytes(b'a' * 2_000_000)
    # Real inherited permissive default ACL, without requiring acl CLI packages.
    import struct
    root = tmp_path / 'backups'
    root.mkdir()
    os.setxattr(root, 'system.posix_acl_default', struct.pack('<I', 2) + b''.join(
        struct.pack('<HHI', tag, 7, 0xffffffff) for tag in (1, 4, 32)))
    permission_probe = (
        'import subprocess, pathlib\n'
        '_popen = subprocess.Popen\n'
        'def popen(*a, **k):\n'
        '    p = _popen(*a, **k)\n'
        '    original_poll = p.poll\n'
        '    def poll():\n'
        '        for f in pathlib.Path(os.environ["HERMES_BACKUP_ROOT"]).glob(".snapshot-*/data/**/.*"):\n'
        '            if f.is_file() and f.stat().st_size > 0:\n'
        '                assert f.stat().st_mode & 0o777 == 0o600, "non-private first bytes"\n'
        '                pathlib.Path(os.environ["TMPDIR"], "first-byte-observed").touch()\n'
        '        return original_poll()\n'
        '    p.poll = poll\n'
        '    return p\n'
        'subprocess.Popen = popen\n')
    private = run_backup(tmp_path, home, permission_probe)
    assert private.returncode == 0, private.stdout + private.stderr
    assert (tmp_path / 'first-byte-observed').exists()
    prior = complete(tmp_path)
    # Drop reported space only after the real rsync child starts.
    refused = run_backup(tmp_path, home,
        'import subprocess\n'
        '_popen = subprocess.Popen\n'
        'def popen(*a, **k):\n'
        '    p = _popen(*a, **k)\n'
        '    os.statvfs = lambda p: SimpleNamespace(f_bavail=49*1024**3, f_frsize=1)\n'
        '    return p\n'
        'subprocess.Popen = popen\n')
    assert refused.returncode == 75, refused.stdout + refused.stderr
    assert complete(tmp_path) == prior
    assert not list((tmp_path / 'backups').glob('.snapshot-*'))


def test_retention_newest_three_complete_only_and_failure_keeps_old(tmp_path):
    home = tmp_path / 'home'
    home.mkdir()
    (home / 'config.yaml').write_text('fixture')
    for _ in range(3):
        run = run_backup(tmp_path, home)
        assert run.returncode == 0, run.stdout + run.stderr
    originals = complete(tmp_path)
    legacy = tmp_path / 'backups/hermes-state-db-20000101.sqlite.zst'
    legacy.write_bytes(b'historical retain')
    incomplete = tmp_path / 'backups/snapshot-20000101T000000000000Z-abcdefgh'
    incomplete.mkdir()
    (incomplete / 'unsealed').write_text('do not count or prune')
    (home / 'state.db').write_text('corrupt database')
    failed = run_backup(tmp_path, home)
    assert failed.returncode != 0
    assert complete(tmp_path) == originals
    (home / 'state.db').unlink()
    run = run_backup(tmp_path, home)
    assert run.returncode == 0, run.stdout + run.stderr
    kept = complete(tmp_path)
    assert len(kept) == 3
    assert kept[:2] == originals[1:]
    assert not originals[0].exists()
    assert legacy.read_bytes() == b'historical retain'
    assert (incomplete / 'unsealed').read_text() == 'do not count or prune'


@pytest.mark.parametrize('kind', ['symlink', 'sqlite-header', 'sqlite-sidecar', 'fifo'])
def test_unknown_or_unsafe_ordinary_data_refuses_not_silently_skipped(tmp_path, kind):
    home = tmp_path / 'home'
    (home / 'memories').mkdir(parents=True)
    p = home / 'memories/item'
    if kind == 'symlink':
        target = tmp_path / 'outside'
        target.write_text('untouched')
        p.symlink_to(target)
    elif kind == 'sqlite-header':
        with sqlite3.connect(p) as db:
            db.execute('create table proof(id)')
    elif kind == 'sqlite-sidecar':
        p.with_name('item-wal').write_bytes(b'wal')
    else:
        os.mkfifo(p)
    run = run_backup(tmp_path, home)
    assert run.returncode != 0
    assert not complete(tmp_path)


def test_lock_contention_and_unsafe_lock_do_not_touch_sources(tmp_path):
    home = tmp_path / 'home'
    home.mkdir()
    (home / 'config.yaml').write_text('fixture')
    root = tmp_path / 'backups'
    root.mkdir()
    lock = root / '.backup.lock'
    with lock.open('w') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        run = run_backup(tmp_path, home)
        assert run.returncode == 75
        assert 'owner busy' in run.stdout
    link = tmp_path / 'lock-alias'
    os.link(lock, link)
    lock.chmod(0o644)
    run = run_backup(tmp_path, home)
    assert run.returncode != 0
    assert link.stat().st_mode & 0o777 == 0o644
    assert not complete(tmp_path)


def test_tampered_basis_is_never_linked_or_retired(tmp_path):
    home = tmp_path / 'home'
    home.mkdir()
    (home / 'config.yaml').write_text('fixture')
    assert run_backup(tmp_path, home).returncode == 0
    old = complete(tmp_path)[0]
    run = run_backup(tmp_path, home,
        'def refuse_rename(*a, **k): raise OSError("synthetic prepublication failure")\n'
        'os.rename = refuse_rename\n')
    assert run.returncode != 0
    assert complete(tmp_path) == [old]
    assert not list((tmp_path / 'backups').glob('.snapshot-*'))
    (old / 'data/config.yaml').write_text('corrupt')
    run = run_backup(tmp_path, home)
    assert run.returncode != 0
    assert complete(tmp_path) == [old]


def test_sqlite_runtime_refusal_and_pinned_wal_generation(tmp_path):
    home = tmp_path / 'home'
    home.mkdir()
    with sqlite3.connect(home / 'state.db') as db:
        db.execute('pragma journal_mode=wal')
        db.execute('create table payload(value)')
        db.execute("insert into payload values('committed')")
        db.commit()
        # A real competing writer commits after the source generation is pinned.
        # The captured online image must retain the original generation.
        hook = (
            'import sqlite3\n'
            '_connect = sqlite3.connect\n'
            'class Source(sqlite3.Connection):\n'
            '    def backup(self, dst, **kw):\n'
            '        with _connect(os.environ["HERMES_HOME"]+"/state.db") as writer:\n'
            '            writer.execute("insert into payload values (\'later\')")\n'
            '        return super().backup(dst, **kw)\n'
            'def connect(*a, **k):\n'
            '    if "mode=ro" in str(a[0]): k["factory"] = Source\n'
            '    return _connect(*a, **k)\n'
            'sqlite3.connect = connect\n')
        run = run_backup(tmp_path, home, hook)
        assert run.returncode == 0, run.stdout + run.stderr
        with sqlite3.connect(complete(tmp_path)[0] / 'data/state.db') as snap:
            assert snap.execute('select value from payload').fetchall() == [('committed',)]
        assert db.execute('select count(*) from payload').fetchone()[0] == 2
        prior = complete(tmp_path)
        low_space = hook.replace('return super().backup(dst, **kw)',
            'os.statvfs = lambda p: SimpleNamespace(f_bavail=49*1024**3, f_frsize=1)\n'
            '        return super().backup(dst, **kw)')
        run = run_backup(tmp_path, home, low_space)
        assert run.returncode == 75, run.stdout + run.stderr
        assert 'HEADROOM' in run.stdout
        assert complete(tmp_path) == prior
        assert not list((tmp_path / 'backups').glob('.snapshot-*'))
        # Full online image must fit even when current free space exceeds 50GiB.
        # A synthetic 19GiB page count avoids allocating a giant fixture.
        capacity = (
            'import sqlite3\n'
            '_connect = sqlite3.connect\n'
            'class S(sqlite3.Connection):\n'
            '    def execute(self, sql, *a):\n'
            '        if sql == "pragma page_count":\n'
            '            return SimpleNamespace(fetchone=lambda: (19*1024**3//4096,))\n'
            '        return super().execute(sql, *a)\n'
            'sqlite3.connect = lambda *a, **k: _connect(*a, **dict(k, factory=S))\n'
            'os.statvfs = lambda p: SimpleNamespace(f_bavail=53*1024**3, f_frsize=1)\n')
        run = run_backup(tmp_path, home, capacity)
        assert run.returncode == 75, run.stdout + run.stderr
        assert 'insufficient headroom' in run.stdout
        assert complete(tmp_path) == prior
        assert not list((tmp_path / 'backups').glob('.snapshot-*'))
