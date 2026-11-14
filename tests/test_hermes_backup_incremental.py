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


def backup_command(tmp_path, home, injection='', backup_root=None):
    # Exercise real admission, but only against healthy scratch state/df.
    import runpy
    scripts = tmp_path / 'producer/scripts'
    scripts.mkdir(parents=True, exist_ok=True)
    producer = scripts / PRODUCER.name
    producer.write_bytes(PRODUCER.read_bytes())
    module = scripts / 'disk_alarm.py'
    module.write_text((PRODUCER.parent / module.name).read_text().replace(
        "BUSINESS = Path('/home/ubuntu/business')", f'BUSINESS = Path({str(scripts.parent)!r})').replace(
        "if __name__ == '__main__':", "def free_bytes(): return 70 * 2**30\n\nif __name__ == '__main__':"))
    alarm = runpy.run_path(str(module))
    with alarm['lock'](alarm['STATE']):
        alarm['tick'](alarm['STATE'], 70 * 2**30)
    # Model backup space independently of hosted runner capacity, not a production knob.
    hook = tmp_path / 'hook'
    hook.mkdir(exist_ok=True)
    (hook / 'sitecustomize.py').write_text(
        'import os\nfrom types import SimpleNamespace\n'
        'os.statvfs = lambda p: SimpleNamespace(f_bavail=100*1024**3, f_frsize=1)\n'
        + injection)
    return ['bash', str(producer)], {**os.environ, 'HERMES_HOME': str(home),
        'HERMES_BACKUP_ROOT': str(backup_root or tmp_path / 'backups'), 'TMPDIR': str(tmp_path),
        'PYTHONPATH': str(hook), 'PYTHONDONTWRITEBYTECODE': '1'}


def run_backup(tmp_path, home, injection='', backup_root=None):
    argv, env = backup_command(tmp_path, home, injection, backup_root)
    return subprocess.run(argv, env=env, capture_output=True, text=True, timeout=90)


def complete(tmp_path):
    return sorted(p for p in (tmp_path / 'backups').glob('snapshot-*')
                  if (p / 'COMPLETE').is_file())


@pytest.mark.parametrize('stop', ['SIGKILL', 'critical-alarm'])
def test_wrapper_death_and_critical_alarm_stop_real_rsync_tree(tmp_path, stop):
    import signal
    import time
    import importlib.util
    home = tmp_path / 'home'
    home.mkdir()
    (home / 'config.yaml').write_bytes(b'x' * 4_000_000)
    root = tmp_path / 'backups/hermes-state'
    argv, env = backup_command(tmp_path, home,
        'import subprocess, pathlib\n'
        '_popen = subprocess.Popen\n'
        'def popen(argv, **kw):\n'
        '    argv = ["--bwlimit=128" if a.startswith("--bwlimit=") else a for a in argv]\n'
        '    p = _popen(argv, **kw)\n'
        '    pathlib.Path(os.environ["TMPDIR"], "rsync-pid").write_text(str(p.pid))\n'
        '    return p\n'
        'subprocess.Popen = popen\n', root)
    wrapper = subprocess.Popen(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    child = None
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            marker = tmp_path / 'rsync-pid'
            if marker.exists():
                child = int(marker.read_text())
                partials = list(root.glob('.snapshot-*/data/.*'))
                if any(p.is_file() and p.stat().st_size for p in partials):
                    break
            assert wrapper.poll() is None, wrapper.communicate()
            time.sleep(0.02)
        else:
            pytest.fail('real rsync did not reach mid-copy')
        # Capture all current local-transfer workers, not only rsync's leader.
        def descendants(pid):
            children = Path(f'/proc/{pid}/task/{pid}/children')
            ids = list(map(int, children.read_text().split())) if children.exists() else []
            return [pid] + [p for c in ids for p in descendants(c)]
        workers = descendants(child)
        assert len(workers) >= 2, 'exercise the local rsync process tree'
        if stop == 'SIGKILL':
            wrapper.kill()
        else:
            spec = importlib.util.spec_from_file_location('alarm', PRODUCER.parent / 'disk_alarm.py')
            alarm = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(alarm)
            alarm.BUSINESS = tmp_path
            state = tmp_path / 'alarm-state'
            state.mkdir()
            result = alarm.tick(state, 39 * GIB, lambda *a: {'message_id': 'scratch-only'})
            assert result['backup_stop'] == 'stopped'
            assert (state / 'backup-paused').exists()
        out, err = wrapper.communicate(timeout=10)
        assert wrapper.returncode != 0, out + err
        def running(pid):
            stat_path = Path(f'/proc/{pid}/stat')
            try:
                return stat_path.read_text().rsplit(')', 1)[1].split()[0] != 'Z'
            except FileNotFoundError:
                return False
        deadline = time.monotonic() + 5
        while any(running(p) for p in workers) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not any(running(p) for p in workers), 'orphan rsync workers survived guard death'
    finally:
        if wrapper.poll() is None:
            wrapper.kill()
        wrapper.communicate(timeout=10)
        if child:
            # Only this test's separately-created copy group; never a host job.
            try:
                os.killpg(child, signal.SIGKILL)
            except ProcessLookupError:
                pass
            # Pre-fix rsync did not own a group: also reap its captured workers.
            for pid in locals().get('workers', [child]):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


def test_headroom_precedes_metadata_and_listing_stays_on_backup_filesystem(tmp_path):
    home = tmp_path / 'home'
    (home / 'memories').mkdir(parents=True)
    for n in range(12):
        (home / f'memories/empty-{n}').mkdir()
    refused = run_backup(tmp_path, home,
        'os.statvfs = lambda p: SimpleNamespace(f_bavail=49*1024**3, f_frsize=1)\n')
    assert refused.returncode == 75
    assert not (tmp_path / 'backups').exists(), 'low-space preflight must precede root/lock writes'
    # Account directory blocks with a virtual nearly-full filesystem: a tiny
    # fixture exercises the same incremental allocation as a million empty dirs.
    probe = (
        'import tempfile\n'
        '_mkdir, _temporary = os.mkdir, tempfile.TemporaryFile\n'
        '_available = 51*1024**3 + 4*4096\n'
        'def vfs(p): return SimpleNamespace(f_bavail=_available, f_frsize=1)\n'
        'os.statvfs = vfs\n'
        'def mkdir(p, *a, **k):\n'
        '    global _available\n'
        '    assert _available >= 51*1024**3 + 4096, "metadata crossed reserve"\n'
        '    result = _mkdir(p, *a, **k)\n'
        '    _available -= 4096\n'
        '    return result\n'
        'os.mkdir = mkdir\n'
        'def temporary(*a, **k):\n'
        '    assert os.path.realpath(k.get("dir", "")) == os.path.realpath(os.environ["HERMES_BACKUP_ROOT"]), "listing escaped backup filesystem"\n'
        '    return _temporary(*a, **k)\n'
        'tempfile.TemporaryFile = temporary\n')
    refused = run_backup(tmp_path, home, probe)
    assert refused.returncode == 75, refused.stdout + refused.stderr
    # With ample space, execute the actual temporary-list check as well.
    (home / 'config.yaml').write_text('fixture')
    succeeded = run_backup(tmp_path, home, probe[probe.index('def temporary'):].replace(
        'def temporary', 'import tempfile\n_temporary = tempfile.TemporaryFile\ndef temporary', 1))
    assert succeeded.returncode == 0, succeeded.stdout + succeeded.stderr


def test_reclaims_abandoned_stage_and_retires_corrupt_oldest_complete(tmp_path):
    home = tmp_path / 'home'
    home.mkdir()
    (home / 'config.yaml').write_text('fixture')
    for _ in range(3):
        assert run_backup(tmp_path, home).returncode == 0
    oldest = complete(tmp_path)[0]
    (oldest / 'COMPLETE').write_text('corrupt seal')
    stale = tmp_path / 'backups/.snapshot-deadbeef'
    stale.mkdir()
    (stale / 'leftover').write_text('abandoned by killed writer')
    run = run_backup(tmp_path, home)
    assert run.returncode == 0, run.stdout + run.stderr
    assert not stale.exists()
    assert not oldest.exists()
    assert len(complete(tmp_path)) == 3
    assert 'reclaimed stale staging=' in run.stdout
    assert 'retiring corrupt COMPLETE snapshot=' in run.stdout


def test_dotdot_destination_inside_source_is_rejected_before_writes(tmp_path):
    # Exact reported shape, entirely inside scratch; no live .hermes is read.
    user = tmp_path / 'user'
    home = user / '.hermes'
    (home / 'memories').mkdir(parents=True)
    (user / 'business').mkdir()
    (home / 'config.yaml').write_text('fixture')
    destination = user / 'business/../.hermes/memories/backups'
    run = run_backup(tmp_path, home, backup_root=destination)
    assert run.returncode != 0
    assert 'source and destination must be disjoint' in run.stderr
    assert not (home / 'memories/backups').exists()


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
