"""F09 short cutover: scratch-only, harmless carriers, no live controls."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from test_f09_native_maintenance_boundary import direct_run, carrier
from test_scheduled_job_health_carrier_release import fixture, accept

ROOT = Path(__file__).resolve().parents[1]
NATIVE = Path('/home/ubuntu/.hermes/hermes-agent')


def test_importer_refuses_before_first_manifest_read_under_source_fence(tmp_path):
    """Lock the directory inode: no new lockfile or preview mutation needed."""
    import fcntl
    root = tmp_path / 'root'
    root.mkdir()
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/sync_from_repo.py'),
            '--root', str(root), '--checkout', str(root), '--apply'],
            capture_output=True, text=True, timeout=15)
        assert result.returncode == 2
        assert 'source_import_busy' in result.stderr  # Not missing manifest: admission is first.
        assert list(root.iterdir()) == []
    finally:
        os.close(fd)
    result = subprocess.run([sys.executable, str(ROOT / 'scripts/sync_from_repo.py'),
        '--root', str(root), '--checkout', str(root), '--apply'],
        capture_output=True, text=True, timeout=15)
    assert 'source_import_busy' not in result.stderr
    assert 'rebuild-source-manifest.json' in result.stderr


@pytest.mark.skipif(os.geteuid() == 0, reason='unprivileged DAC boundary')
def test_cohort_commit_runs_sealed_and_uncertain_commit_never_reopens(tmp_path):
    import maintenance_cohort as cohort
    import maintenance_window as mw
    target = tmp_path / 'carrier.py'
    old, new = carrier('old'), carrier('new')
    target.write_bytes(old)
    target.chmod(0o750)
    journal = tmp_path / 'journal.json'
    mw.atomic_json(journal, {'schema': mw.SCHEMA})
    marker = tmp_path / 'marker'
    calls = []

    def commit():
        calls.append('save')
        assert direct_run(target, marker) == 'DAC_BLOCKED'
        assert not marker.exists()
        raise RuntimeError('uncertain native save')

    with pytest.raises(RuntimeError, match='uncertain native save'):
        cohort.switch_files(journal, {target: new}, {target: (old, 0o750)}, lambda: [], commit=commit)
    assert calls == ['save']
    assert target.stat().st_mode & 0o777 == 0
    assert direct_run(target, marker) == 'DAC_BLOCKED'
    assert mw.read_journal(journal)['source_switch']['state'] == 'commit_uncertain'
    with pytest.raises(RuntimeError, match='external_commit'):
        cohort.recovery_proposal(journal)


@pytest.mark.skipif(not (NATIVE / 'cron/jobs.py').is_file(), reason='local unchanged native scheduler required')
@pytest.mark.skipif(os.geteuid() == 0, reason='unprivileged DAC boundary')
@pytest.mark.parametrize('with_due', [False, True])
@pytest.mark.parametrize('save_outcome', ['ok', 'raise_before', 'raise_after', 'bad_readback'])
def test_native_preview_apply_restores_admission_without_pause(tmp_path, monkeypatch, with_due, save_outcome):
    """Native saver, registry/fire locks, census; harmless single-carrier body."""
    f = list(fixture(tmp_path))
    p, repo, root, home, _, receipt, review = f
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('HERMES_SKIP_ENV_FILE', '1')
    monkeypatch.syspath_prepend(str(NATIVE))
    from cron import jobs as native
    assert Path(native.__file__).resolve() == NATIVE / 'cron/jobs.py'
    registry = home / 'cron/jobs.json'
    assert native._current_cron_store().jobs_file == registry
    f[4] = native
    old, new = carrier('old'), carrier('new')
    target = home / 'scripts' / p.CARRIER
    target.write_bytes(old)
    p.PREDECESSOR_SHA256 = p.sha_bytes(old)
    for base in (repo, root):
        (base / p.SNAPSHOT).write_bytes(new)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Fixture', '-c',
        'user.email=fixture@example.invalid', 'commit', '-qam', 'harmless carrier'], check=True)
    head = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
    review.update(producer_head=head, source_head=head, predecessor_sha256=p.sha_bytes(old))
    review['source_sha256'][p.SNAPSHOT] = p.sha_bytes(new)
    doc = json.loads(registry.read_text())
    job = doc['jobs'][0]
    job.update(script_sha256=p.sha_bytes(old), last_status='fixture prior', extension={'keep': [1, 2]})
    if not with_due:
        job.pop('next_run_at')
    review['expected_job'] = dict(job)
    registry.write_text(json.dumps(doc))
    receipt.write_text(json.dumps(review))
    before_sources = {n: p.read_regular(root / n) for n in p.SOURCES}
    preview = accept(f)
    marker = tmp_path / 'marker'
    # No exclusion is claimed during independent review. Old bytes may run.
    assert direct_run(target, marker) == 'EXECUTED'
    marker.unlink()
    calls = []
    save = native._save_jobs_unlocked

    def sealed_save(jobs, **kw):
        calls.append('save')
        assert direct_run(target, marker) == 'DAC_BLOCKED'
        assert not marker.exists()
        # The native target has never been paused; controls remain original.
        assert json.loads(registry.read_text())['jobs'] == doc['jobs']
        imported = subprocess.run([sys.executable, str(ROOT / 'scripts/sync_from_repo.py'),
            '--root', str(root), '--checkout', str(repo), '--apply', '--only', 'ops/n8n_execution_budget.py'],
            capture_output=True, text=True, timeout=15)
        assert imported.returncode == 2 and 'source_import_busy' in imported.stderr
        # Real native fire fence: another interpreter cannot claim or read/run.
        probe = subprocess.run([sys.executable, '-c',
            'import sys;sys.path.insert(0,sys.argv[1]);from cron import jobs;'
            'jobs._JOBS_LOCK_TIMEOUT_SECONDS=0.05;'
            'print(jobs.claim_job_for_fire(sys.argv[2],manual=True))', str(NATIVE), p.JOB_ID],
            capture_output=True, text=True, timeout=15)
        assert probe.returncode == 0 and probe.stdout.strip() == 'False'
        if save_outcome == 'raise_before':
            raise RuntimeError('injected native save failure')
        if save_outcome == 'bad_readback':
            jobs[0]['extension'] = 'unexpected native postimage'
        save(jobs, **kw)
        assert direct_run(target, marker) == 'DAC_BLOCKED'
        if save_outcome == 'raise_after':
            raise RuntimeError('injected native save failure')

    monkeypatch.setattr(native, '_save_jobs_unlocked', sealed_save)
    if save_outcome != 'ok':
        reason = 'readback_mismatch' if save_outcome == 'bad_readback' else 'injected native save failure'
        with pytest.raises(RuntimeError, match=reason):
            p.release(receipt, repo, business=root, hermes=home, apply=True, scheduler=native)
        assert calls == ['save'] and target.stat().st_mode & 0o777 == 0
        assert direct_run(target, marker) == 'DAC_BLOCKED'
        with pytest.raises((RuntimeError, OSError)):
            p.release(receipt, repo, business=root, hermes=home, apply=True, scheduler=native)
        assert calls == ['save']
        assert not list((root / 'reports').rglob('acceptance.json'))
        current = json.loads(registry.read_text())['jobs']
        assert current[1] == doc['jobs'][1]
        assert current[0]['script_sha256'] == p.sha_bytes(old if save_outcome == 'raise_before' else new)
        assert current[0].get('next_run_at') == job.get('next_run_at')
        assert ('next_run_at' in current[0]) is with_due
        assert {n: p.read_regular(root / n) for n in p.SOURCES} == before_sources
        return
    result = p.release(receipt, repo, business=root, hermes=home, apply=True, scheduler=native)
    assert calls == ['save'] and result['applied']
    expected = [{**job, 'script_sha256': preview['proposed_sha256']}, doc['jobs'][1]]
    assert json.loads(registry.read_text())['jobs'] == expected
    assert 'skills' not in expected[0] and 'skill' not in expected[0]
    assert ('next_run_at' in expected[0]) is with_due
    assert {n: p.read_regular(root / n) for n in p.SOURCES} == before_sources
    assert target.stat().st_mode & 0o777 == 0o750
    assert direct_run(target, marker) == 'EXECUTED'
    assert marker.read_text() == 'new'
    assert result['admission_restored'] is True


def test_late_bytecode_cannot_escape_sealed_commit(tmp_path):
    import maintenance_cohort as cohort
    import maintenance_window as mw
    target = tmp_path / 'carrier.py'
    target.write_bytes(carrier('old'))
    target.chmod(0o750)
    before = (target.read_bytes(), 0o750)
    journal = tmp_path / 'journal.json'
    mw.atomic_json(journal, {'schema': mw.SCHEMA})
    calls = []
    late = target.with_suffix('.pyc')

    def drain():
        # A pre-seal importer can finish writing bytecode after enumeration.
        if not late.exists():
            late.write_bytes(b'late fixture cache')
        return []

    with pytest.raises(RuntimeError, match='cohort_cache_set_changed'):
        cohort.switch_files(journal, {target: carrier('new')}, {target: before}, drain,
                            commit=lambda: calls.append('save'))
    assert not calls
    assert target.read_bytes() == before[0]


def test_loaded_direct_callable_blocks_before_custody(tmp_path):
    f = fixture(tmp_path)
    p, repo, root, home, scheduler, receipt, review = f
    target = home / 'scripts' / p.CARRIER
    target.write_bytes(carrier('old'))
    p.PREDECESSOR_SHA256 = p.sha_bytes(target.read_bytes())
    review['predecessor_sha256'] = p.PREDECESSOR_SHA256
    review['expected_job']['script_sha256'] = p.PREDECESSOR_SHA256
    registry = home / 'cron/jobs.json'
    doc = json.loads(registry.read_text())
    doc['jobs'][0]['script_sha256'] = p.PREDECESSOR_SHA256
    registry.write_text(json.dumps(doc))
    receipt.write_text(json.dumps(review))
    accept(f)
    marker = tmp_path / 'old-callable-marker'
    # Retain the imported callable after all source descriptors have closed.
    child = subprocess.Popen([sys.executable, '-B', '-c',
        'import sys,runpy; m=runpy.run_path(sys.argv[1]); print("ready",flush=True);'
        'sys.stdin.readline(); m["run"](sys.argv[2])', str(target), str(marker)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == 'ready'
        before = p.read_regular(target)
        with pytest.raises(RuntimeError, match='carrier_not_drained'):
            p.release(receipt, repo, business=root, hermes=home, apply=True, scheduler=scheduler)
        assert p.read_regular(target) == before and not (root / 'reports').exists()
    finally:
        child.communicate('\n', timeout=15)  # Natural completion; never kill work.
    assert marker.read_text() == 'old' and child.returncode == 0
    assert p.release(receipt, repo, business=root, hermes=home, apply=True, scheduler=scheduler)['applied']


def test_old_resident_importer_requires_real_bootstrap_drain(tmp_path):
    """Regression/documentation of the bootstrap, NOT an old-code lock claim.

    An already-loaded old sync can write under the new flock. Retire it before
    adopting admission; an on-disk replacement is not resident-code adoption.
    """
    import fcntl
    from test_sync_from_repo import checkout
    repo, root = checkout(tmp_path)
    old_script = tmp_path / 'old_sync.py'
    # Retained counterexample source, not a synthesized lock-free importer.
    old_script.write_bytes(subprocess.check_output(['git', '-C', str(ROOT), 'show',
        '02ebdc90206ef00405b04d780ab0b030d60ffed3:scripts/sync_from_repo.py']))
    child = subprocess.Popen([sys.executable, '-B', '-c',
        'import runpy,sys;from pathlib import Path;m=runpy.run_path(sys.argv[1]);'
        'print("ready",flush=True);sys.stdin.readline();'
        'raise SystemExit(m["sync"](Path(sys.argv[2]),Path(sys.argv[3]),True))',
        str(old_script), str(root), str(repo)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == 'ready'
        # Installing new bytes does not alter m["sync"] in the resident child.
        old_script.write_bytes((ROOT / 'scripts/sync_from_repo.py').read_bytes())
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            output, _ = child.communicate('\n', timeout=15)
            assert child.returncode == 0 and 'APPLY diffs=2' in output
            assert (root / 'scripts/sample.py').read_bytes() == b'old\n'
            new = subprocess.run([sys.executable, str(old_script), '--root', str(root),
                '--checkout', str(repo), '--dry-run'], capture_output=True, text=True, timeout=15)
            assert new.returncode == 2 and 'source_import_busy' in new.stderr
        finally:
            os.close(fd)
    finally:
        if child.poll() is None:
            child.communicate('\n', timeout=15)
    # Old owner has naturally exited. The fresh maintained entry now honors
    # exclusion and remains operational once admitted.
    new = subprocess.run([sys.executable, str(old_script), '--root', str(root),
        '--checkout', str(repo), '--dry-run'], capture_output=True, text=True, timeout=15)
    assert new.returncode == 0 and 'DRY_RUN diffs=0' in new.stdout
