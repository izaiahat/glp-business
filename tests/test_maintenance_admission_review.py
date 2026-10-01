"""PR109 F4 oracles and bounded-admission tests; scratch files/processes only."""
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'tests')]
import maintenance_window as mw
import maintenance_admission as admission
import maintenance_cohort as cohort
import test_maintenance_window as fixtures

def test_new_reader_after_guard_entry_must_prevent_success(tmp_path, monkeypatch):
    backend = fixtures.FakeBackend()
    backend.root, backend.hermes = tmp_path.resolve(), (tmp_path / 'hermes').resolve()
    window = fixtures.new_window(mw, tmp_path, backend)
    monkeypatch.setattr(mw, 'LinuxBackend', lambda *_: backend)
    monkeypatch.setattr(mw, 'default_locks', lambda *_: window.lock_paths)
    try:
        window.enter()
        with mw.require_active_window(window.journal, backend.root, backend.hermes):
            # Model admission of a direct, nonparticipating reader AFTER pre-write check.
            backend.running = [{'kind': 'process', 'pid': 999999, 'reason': 'new direct source reader'}]
        # Context accepted the adoption; no second inflight/ACTIVE check happened.
        pytest.fail('adoption context succeeded with new direct source reader still inflight')
    except mw.WindowError:
        pass
    finally:
        window.release_locks()


def test_actual_new_reader_observes_partial_cohort_while_full_guard_is_held(tmp_path, monkeypatch):
    backend, window, sources, before, after, original_job, adopted_job, prepare = fixtures.transaction_fixture(mw, tmp_path, monkeypatch)
    monkeypatch.setattr(mw, 'LinuxBackend', lambda *_: backend)
    monkeypatch.setattr(mw, 'default_locks', lambda *_: window.lock_paths)
    try:
        with pytest.raises(mw.WindowError):
            with mw.require_active_window(window.journal, backend.root, backend.hermes) as active:
                prepare(active)
                sources[0].write_bytes(after[0])
                # A real new nonparticipating interpreter starts AFTER the admitted
                # status sample and reads both fixture paths without these flocks.
                reader = subprocess.run([sys.executable, '-c',
                                         'import json,sys;from pathlib import Path;print(json.dumps([Path(p).read_text() for p in sys.argv[1:]]))',
                                         *map(str, sources)], cwd=tmp_path, capture_output=True, text=True, check=True, timeout=10)
                observed = json.loads(reader.stdout)
                sources[1].write_bytes(after[1])
                backend.jobs['enabled'] = dict(adopted_job)
        assert mw.read_journal(window.journal)['source_transaction']['outcome'] == 'uncertain'
        # Exact original scratch reproduction still demonstrates advisory-lock
        # bypass. The repaired guard MUST refuse this unsupported direct writer;
        # production adopters use the fenced switch tested below.
        assert observed == [after[0].decode(), before[1].decode()]
    finally:
        window.release_locks()


def test_fenced_switch_denies_real_mid_cohort_reader(tmp_path, monkeypatch):
    backend, window, sources, before, after, _, adopted_job, prepare = fixtures.transaction_fixture(mw, tmp_path, monkeypatch)
    monkeypatch.setattr(mw, 'LinuxBackend', lambda *_: backend)
    monkeypatch.setattr(mw, 'default_locks', lambda *_: window.lock_paths)
    observed = []
    replace = cohort.os.replace
    def inspect_replace(source, target):
        replace(source, target)
        if Path(target) == sources[0]:
            reader = subprocess.run([sys.executable, '-c',
                'import json,sys;from pathlib import Path\ntry: print(json.dumps([Path(p).read_text() for p in sys.argv[1:]]))\nexcept PermissionError: print("BLOCKED")',
                *map(str, sources)], capture_output=True, text=True, timeout=10, check=True)
            observed.append(reader.stdout.strip())
    monkeypatch.setattr(cohort.os, 'replace', inspect_replace)
    try:
        with mw.require_active_window(window.journal, backend.root, backend.hermes) as active:
            prepare(active)
            active.switch_files(dict(zip(sources, after)), {path: (body, 0o644) for path, body in zip(sources, before)})
            backend.jobs['enabled'] = dict(adopted_job)
        assert observed == ['BLOCKED']
        assert [p.read_bytes() for p in sources] == after
        assert [stat.S_IMODE(p.stat().st_mode) for p in sources] == [0o644, 0o644]
        assert mw.read_journal(window.journal)['source_transaction']['outcome'] == 'applied'
    finally:
        window.release_locks()


def test_cohort_rollback_restores_full_bytes_and_modes(tmp_path, monkeypatch):
    files = [tmp_path / 'a.py', tmp_path / 'b.py']
    old = [b'old a\n', b'old b\n']
    modes = [0o640, 0o750]
    for path, body, mode in zip(files, old, modes):
        path.write_bytes(body)
        path.chmod(mode)
    journal = tmp_path / 'window.json'
    mw.atomic_json(journal, {'schema': mw.SCHEMA})
    replace = cohort.os.replace
    failed = []
    def fail_second(source, target):
        if Path(target) == files[1] and not failed:
            failed.append(True)
            raise OSError('injected rename failure')
        replace(source, target)
    monkeypatch.setattr(cohort.os, 'replace', fail_second)
    with pytest.raises(OSError, match='injected'):
        cohort.switch_files(journal, dict(zip(files, [b'new a\n', b'new b\n'])),
                            {p: (b, m) for p, b, m in zip(files, old, modes)}, lambda: [])
    assert [p.read_bytes() for p in files] == old
    assert [stat.S_IMODE(p.stat().st_mode) for p in files] == modes
    assert mw.read_journal(journal)['source_switch']['state'] == 'rolled_back'


def test_cohort_crash_keeps_all_public_files_unreadable(tmp_path, monkeypatch):
    files = [tmp_path / 'a.py', tmp_path / 'b.py']
    for path in files:
        path.write_bytes(b'old\n')
    journal = tmp_path / 'window.json'
    mw.atomic_json(journal, {'schema': mw.SCHEMA})
    replace = cohort.os.replace
    def lose_power(source, target):
        replace(source, target)
        if Path(target) == files[0]:
            raise fixtures.SimulatedPowerLoss('after first rename')
    monkeypatch.setattr(cohort.os, 'replace', lose_power)
    with pytest.raises(fixtures.SimulatedPowerLoss):
        cohort.switch_files(journal, {p: b'new\n' for p in files}, {p: (b'old\n', 0o644) for p in files}, lambda: [])
    assert all(stat.S_IMODE(p.stat().st_mode) == 0 for p in files)
    assert mw.read_journal(journal)['source_switch']['state'] == 'switching'


def test_cohort_fences_and_invalidates_stale_bytecode(tmp_path, monkeypatch):
    import py_compile
    source = tmp_path / 'target.py'
    source.write_text('VALUE = "old"\n')
    cache = Path(py_compile.compile(str(source), doraise=True))
    journal = tmp_path / 'window.json'
    mw.atomic_json(journal, {'schema': mw.SCHEMA})
    observed = []
    replace = cohort.os.replace
    def inspect_replace(old, new):
        replace(old, new)
        if Path(new) == source:
            assert stat.S_IMODE(cache.stat().st_mode) == 0
            result = subprocess.run([sys.executable, '-c', 'import target;print(target.VALUE)'],
                                    cwd=tmp_path, capture_output=True, text=True, timeout=10)
            observed.append(result.returncode)
    monkeypatch.setattr(cohort.os, 'replace', inspect_replace)
    cohort.switch_files(journal, {source: b'VALUE = "new"\n'}, {source: (source.read_bytes(), 0o644)}, lambda: [])
    assert observed and observed[0] != 0
    assert not cache.exists()
    assert subprocess.check_output([sys.executable, '-c', 'import target;print(target.VALUE)'], cwd=tmp_path, text=True).strip() == 'new'


def proc_entry(proc, pid, ppid, argv, cwd, comm='python'):
    base = proc / str(pid)
    base.mkdir()
    fields = ['S', str(ppid)] + ['0'] * 17 + ['123']
    (base / 'stat').write_text(str(pid) + ' (' + comm + ') ' + ' '.join(fields))
    (base / 'cmdline').write_bytes(b'\0'.join(x.encode() for x in argv) + b'\0')
    (base / 'cwd').symlink_to(cwd, target_is_directory=True)
    (base / 'fd').mkdir()
    (base / 'maps').write_text('')
    return base


def test_bounded_process_admission_blocks_source_and_task_descendants_only(tmp_path):
    proc = tmp_path / 'proc'
    proc.mkdir()
    source = tmp_path / 'adopt.py'
    task = tmp_path / 'wrapper.sh'
    proc_entry(proc, 11, 0, ['python', '/opt/hermes/gateway.py'], tmp_path)
    proc_entry(proc, 12, 0, ['bash'], tmp_path, 'bash')
    proc_entry(proc, 13, 0, ['codex', 'agent session'], tmp_path, 'codex')
    proc_entry(proc, 14, 0, ['python', str(source)], tmp_path)
    proc_entry(proc, 15, 0, ['bash', str(task)], tmp_path, 'bash')
    proc_entry(proc, 16, 15, ['python', '-c', 'pass'], tmp_path)
    fd_reader = proc_entry(proc, 17, 0, ['node', '/tmp/unrelated.js'], tmp_path)
    (fd_reader / 'fd/3').symlink_to(source)
    mmap_reader = proc_entry(proc, 18, 0, ['python', '-c', 'pass'], tmp_path)
    (mmap_reader / 'maps').write_text('000-fff r--p 0 00:00 1 ' + str(source) + '\n')
    rows = admission.process_readers([source], [task], proc=proc)
    assert [row['pid'] for row in rows] == [14, 15, 16, 17, 18]
    assert all(row['start_ticks'] == 123 for row in rows)


def test_unreadable_unrelated_pid_is_ignored_but_identified_source_blocks(tmp_path, monkeypatch):
    proc = tmp_path / 'proc'
    proc.mkdir()
    source = tmp_path / 'adopt.py'
    proc_entry(proc, 21, 0, ['sd-pam'], tmp_path, 'sd-pam')
    proc_entry(proc, 22, 0, ['python', str(source)], tmp_path)
    iterdir = Path.iterdir
    def deny_fd(path):
        if path in {proc / '21/fd', proc / '22/fd'}:
            raise PermissionError('fixture read refusal')
        return iterdir(path)
    monkeypatch.setattr(Path, 'iterdir', deny_fd)
    rows = admission.process_readers([source], proc=proc)
    assert [row['pid'] for row in rows] == [22]
    assert rows[0]['reason'] == 'identified_reader_unreadable'


def test_unreadable_cmdline_does_not_hide_readable_source_descriptor(tmp_path, monkeypatch):
    proc = tmp_path / 'proc'
    proc.mkdir()
    source = tmp_path / 'adopt.py'
    row = proc_entry(proc, 25, 0, ['python', '-c', 'pass'], tmp_path)
    (row / 'fd/3').symlink_to(source)
    original = Path.read_bytes
    def deny_cmdline(path):
        if path == row / 'cmdline':
            raise PermissionError('fixture denied cmdline')
        return original(path)
    monkeypatch.setattr(Path, 'read_bytes', deny_cmdline)
    busy = admission.process_readers([source], proc=proc)
    assert busy[0]['pid'] == 25 and busy[0]['sources'] == [str(source)]


def test_cohort_short_writes_cannot_publish_truncated_bytes(tmp_path, monkeypatch):
    source = tmp_path / 'a.py'
    source.write_bytes(b'old original bytes\n')
    journal = tmp_path / 'window.json'
    mw.atomic_json(journal, {'schema': mw.SCHEMA})
    old_write = cohort.os.write
    def short_write(fd, body):
        return old_write(fd, body[:3])
    monkeypatch.setattr(cohort.os, 'write', short_write)
    body = b'complete staged candidate bytes\n'
    cohort.switch_files(journal, {source: body}, {source: (source.read_bytes(), 0o644)}, lambda: [])
    assert source.read_bytes() == body


def test_removed_cache_is_restored_if_publication_permission_fails(tmp_path, monkeypatch):
    import py_compile
    source = tmp_path / 'a.py'
    old = b'VALUE="old"\n'
    source.write_bytes(old)
    cache = Path(py_compile.compile(str(source), doraise=True))
    old_cache = cache.read_bytes()
    journal = tmp_path / 'window.json'
    mw.atomic_json(journal, {'schema': mw.SCHEMA})
    fchmod = cohort.os.fchmod
    failed = []
    def deny_publish(fd, mode):
        if mode == 0o644 and not cache.exists() and not failed:
            failed.append(True)
            raise OSError('injected publish failure')
        return fchmod(fd, mode)
    monkeypatch.setattr(cohort.os, 'fchmod', deny_publish)
    with pytest.raises(OSError, match='publish failure'):
        cohort.switch_files(journal, {source: b'VALUE="new"\n'}, {source: (old, 0o644)}, lambda: [])
    assert source.read_bytes() == old
    assert cache.read_bytes() == old_cache
    assert mw.read_journal(journal)['source_switch']['state'] == 'rolled_back'


def test_inline_executing_source_is_a_named_blocker(tmp_path):
    proc = tmp_path / 'proc'
    proc.mkdir()
    source = tmp_path / 'adopt.py'
    proc_entry(proc, 28, 0, ['python', '-c', 'exec(open(' + repr(str(source)) + ').read())'], tmp_path)
    rows = admission.process_readers([source], proc=proc)
    assert [r['pid'] for r in rows] == [28]
    assert rows[0]['sources'] == [str(source)]
