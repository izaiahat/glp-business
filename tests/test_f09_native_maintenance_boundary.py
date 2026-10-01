"""F09 single-carrier admission counterexamples, not deployable approval.

All mutations, locks, source files and native scheduler homes are scratch-only.
The carrier body is a harmless stand-in: never execute the real health manager.
The observed gap does not depend on a mixed multi-file cohort or idle assertions.
"""
from contextlib import ExitStack, contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import maintenance_cohort as cohort
import maintenance_window as mw
import scheduled_job_health_carrier_release as f09

NATIVE_ROOT = Path('/home/ubuntu/.hermes/hermes-agent')


def digest(body):
    return hashlib.sha256(body).hexdigest()


@contextmanager
def fixture_locks(root, home):
    """Hold precisely the existing maintenance lock names, in scratch only."""
    handles = []
    with ExitStack() as stack:
        for path in mw.default_locks(root, home):
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            stack.callback(os.close, fd)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            handles.append((path, fd))
        yield handles


def carrier(body_label):
    # Explicitly not the real effectful health-manager body.
    return ('from pathlib import Path\n'
            'def run(marker):\n'
            f'    Path(marker).write_text({body_label!r})\n').encode()


def direct_run(path, marker):
    # A new interpreter's source read/import and callable run() do not take any
    # of the maintained advisory flocks. No socket, send, rerun or live state.
    result = subprocess.run([
        sys.executable, '-I', '-B', '-c',
        'import runpy,sys\n'
        'try:\n'
        '    module=runpy.run_path(sys.argv[1])\n'
        '    module["run"](sys.argv[2])\n'
        '    print("EXECUTED")\n'
        'except PermissionError:\n'
        '    print("DAC_BLOCKED")\n',
        str(path), str(marker)], capture_output=True, text=True, check=True, timeout=15)
    return result.stdout.strip()


@pytest.mark.skipif(os.geteuid() == 0, reason='real unprivileged DAC oracle; root bypass is not this boundary')
def test_single_carrier_switch_fence_ends_before_native_commit(tmp_path, monkeypatch):
    root, home = tmp_path / 'business', tmp_path / 'hermes'
    root.mkdir()
    target = home / 'scripts' / f09.CARRIER
    target.parent.mkdir(parents=True)
    old, new = carrier('old'), carrier('new')
    target.write_bytes(old)
    target.chmod(0o750)
    journal = tmp_path / 'window.json'
    mw.atomic_json(journal, {'schema': mw.SCHEMA})
    marker = tmp_path / 'direct-output'
    observed = []
    replace = cohort.os.replace

    def inspect_sealed_replace(source, destination):
        replace(source, destination)
        if Path(destination) == target:
            assert stat.S_IMODE(target.stat().st_mode) == 0
            observed.append(direct_run(target, marker))
            assert not marker.exists()

    monkeypatch.setattr(cohort.os, 'replace', inspect_sealed_replace)
    with fixture_locks(root, home):
        # Preview/acceptance interval: a direct callable can execute old bytes.
        assert direct_run(target, marker) == 'EXECUTED'
        assert marker.read_text() == 'old'
        marker.unlink()
        cohort.switch_files(journal, {target: new}, {target: (old, 0o750)}, lambda: [])
        # Existing switch has already reopened the carrier. A native pin save
        # and full-record readback have not happened yet.
        assert direct_run(target, marker) == 'EXECUTED'
        assert marker.read_text() == 'new'
    assert observed == ['DAC_BLOCKED']
    assert mw.read_journal(journal)['source_switch']['state'] == 'published'
    assert target.read_bytes() == new
    assert stat.S_IMODE(target.stat().st_mode) == 0o750


def test_scoped_f09_input_importer_does_not_participate_in_window_locks(tmp_path):
    """Real guarded importer, real one-file Git manifest, no invented hold API."""
    root, home, checkout = (tmp_path / name for name in ('business', 'hermes', 'checkout'))
    for path in (root, home, checkout):
        path.mkdir()
    rel = 'ops/n8n_execution_budget.py'
    assert rel in f09.SOURCES
    old, new = b'VALUE = "old"\n', b'VALUE = "new"\n'
    for base, body in ((root, old), (checkout, new)):
        path = base / rel
        path.parent.mkdir(parents=True)
        path.write_bytes(body)
        path.chmod(0o640)
    (root / 'ops/.rebuild-sync-state.json').write_text(json.dumps(
        {'schema': 1, 'hashes': {rel: digest(old)}}))
    manifest = {'schema': 'GLP_BUSINESS_SOURCE_V1', 'tracked_count': 2,
                'files': {rel: {'sha256': digest(new)}}}
    (checkout / 'ops/rebuild-source-manifest.json').write_text(json.dumps(manifest))
    subprocess.run(['git', 'init', '-q', str(checkout)], check=True)
    subprocess.run(['git', '-C', str(checkout), 'add', rel, 'ops/rebuild-source-manifest.json'], check=True)
    command = [sys.executable, str(ROOT / 'scripts/sync_from_repo.py'), '--root', str(root),
               '--checkout', str(checkout), '--only', rel]
    with fixture_locks(root, home):
        result = subprocess.run([*command, '--apply'], capture_output=True, text=True, timeout=20, check=True)
        assert 'APPLY diffs=1 conflicts=0' in result.stdout
        assert (root / rel).read_bytes() == new
        post = subprocess.run([*command, '--dry-run'], capture_output=True, text=True, timeout=20, check=True)
        assert 'DRY_RUN diffs=0 conflicts=0' in post.stdout
    assert stat.S_IMODE((root / rel).stat().st_mode) == 0o640


@pytest.mark.skipif(not (NATIVE_ROOT / 'cron/jobs.py').is_file(),
                    reason='installed unchanged native scheduler required; hosted CI has no native runtime')
@pytest.mark.skipif(os.geteuid() == 0, reason='native fixture includes real unprivileged direct-reader oracle')
@pytest.mark.parametrize('with_due', [False, True])
def test_native_exact_restoration_is_not_the_f09_blocker(tmp_path, monkeypatch, with_due):
    """Native pause/save/restore succeed; direct execution escapes before saver.

    This is NOT F09 preview/accept/apply: the producer correctly refuses the
    paused record. No ACTIVE journal or independent acceptance is fabricated.
    """
    root, home = tmp_path / 'business', tmp_path / 'hermes'
    root.mkdir()
    (home / 'cron').mkdir(parents=True)
    target = home / 'scripts' / f09.CARRIER
    target.parent.mkdir()
    old, new = carrier('old'), carrier('new')
    target.write_bytes(old)
    target.chmod(0o750)
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('HERMES_SKIP_ENV_FILE', '1')
    monkeypatch.syspath_prepend(str(NATIVE_ROOT))
    from cron import jobs as native
    registry = home / 'cron/jobs.json'
    assert native._current_cron_store().jobs_file == registry
    original = {'id': f09.JOB_ID, 'name': 'F09 scratch boundary fixture',
                'prompt': 'fixture only', 'script': f09.CARRIER, 'script_sha256': digest(old),
                'enabled': True, 'state': 'scheduled', 'no_agent': True,
                'schedule': {'kind': 'interval', 'minutes': 15},
                'last_status': 'fixture-prior-status', 'fixture_extension': {'preserve': [1, 2]}}
    if with_due:
        original['next_run_at'] = '2030-01-01T00:07:00Z'
    unrelated = {'id': 'unrelated-fixture', 'enabled': False, 'state': 'paused',
                 'paused_reason': 'existing unrelated hold', 'schedule': {'kind': 'interval', 'minutes': 60}}
    registry.write_text(json.dumps({'jobs': [original, unrelated]}))
    journal = tmp_path / 'window.json'
    backend = mw.LinuxBackend(root, home, journal)
    monkeypatch.setattr(backend, 'scheduler', lambda: native)
    entry = {'kind': 'hermes', 'id': f09.JOB_ID, 'affected': True,
             'command': str(target), 'trace': [str(target)]}
    prior = backend.snapshot(entry)
    # Only durable native control preimages: deliberately not an ACTIVE lease.
    mw.atomic_json(journal, {'schema': mw.SCHEMA, 'entries': [
        {'entry': entry, 'prior': prior, 'intent': True}]})
    marker = tmp_path / 'direct-output'
    from tools import cronjob_tools
    monkeypatch.setattr(cronjob_tools, '_notify_provider_jobs_changed_safe', lambda: None)
    assert Path(native.__file__).resolve() == NATIVE_ROOT / 'cron/jobs.py'
    native_save = native._save_jobs_unlocked
    observed = []

    def saver_with_direct_entry(jobs, **kwargs):
        current = backend.job(f09.JOB_ID)
        assert current['enabled'] is False and current['state'] == 'paused'
        assert current['script_sha256'] == digest(old)
        assert not current.get('fire_claim') and not current.get('run_claim')
        observed.append(direct_run(target, marker))
        assert marker.read_text() == 'new'
        return native_save(jobs, **kwargs)

    with fixture_locks(root, home):
        backend.pause(entry, prior)
        paused = backend.job(f09.JOB_ID)
        try:
            assert backend.paused(entry, prior)
            assert {k: paused[k] for k in mw.CONTROL_FIELDS if k in paused} == prior['paused_fields']
            import maintenance_admission as admission
            assert admission.native_runs(native, [entry], backend.job) == ([], set())
            assert direct_run(target, marker) == 'EXECUTED'
            assert marker.read_text() == 'old'
            marker.unlink()
            cohort.switch_files(journal, {target: new}, {target: (old, 0o750)}, lambda: [])
            pinned = {**paused, 'script_sha256': digest(new)}
            # Exercise the existing hash-only job-update journal and exact
            # native locked save/readback, not a fictional new scheduler API.
            mw.ActiveWindow(journal, backend).accept_job_update(f09.JOB_ID, paused, pinned)
            with monkeypatch.context() as scoped:
                scoped.setattr(native, '_save_jobs_unlocked', saver_with_direct_entry)
                with mw.native_registry_lock(native):
                    jobs = native.load_jobs()
                    matched = [j for j in jobs if j['id'] == f09.JOB_ID]
                    assert len(matched) == 1 and matched[0] == paused
                    matched[0]['script_sha256'] = digest(new)
                    native._save_jobs_unlocked(jobs)
                    assert backend.job(f09.JOB_ID) == pinned
        finally:
            backend.restore(entry, prior)
    assert observed == ['EXECUTED']
    assert backend.job(f09.JOB_ID) == {**original, 'script_sha256': digest(new)}
    assert ('next_run_at' in backend.job(f09.JOB_ID)) is with_due
    assert 'skills' not in backend.job(f09.JOB_ID) and 'skill' not in backend.job(f09.JOB_ID)
    assert backend.job(unrelated['id']) == unrelated
    assert target.read_bytes() == new and stat.S_IMODE(target.stat().st_mode) == 0o750
