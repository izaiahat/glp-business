from __future__ import annotations

import hashlib
import importlib.util
import json
import stat
import sys
from pathlib import Path

import pytest


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "repin_reports_archiver_dependents.py"
)
SPEC = importlib.util.spec_from_file_location(
    "repin_reports_archiver_dependents_tested", SCRIPT
)
assert SPEC is not None and SPEC.loader is not None
repin = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = repin
SPEC.loader.exec_module(repin)


def make_fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path, str]:
    source = tmp_path / "reports_archiver.py"
    f12 = tmp_path / "f12_generator_archiver.py"
    f13 = tmp_path / "f13_growth_retention.py"
    jobs = tmp_path / "jobs.json"

    source.write_bytes(b"print('source')\n")
    f12.write_text(
        'EXPECTED_PAYLOAD_SHA256 = "' + "0" * 64 + '"\n', encoding="utf-8"
    )
    f13.write_text(
        'EXPECTED_REPORTS_ARCHIVER_SHA256 = "' + "1" * 64 + '"\n',
        encoding="utf-8",
    )
    f12.chmod(0o751)
    f13.chmod(0o750)

    root = {
        "jobs": [
            {
                "id": repin.F12_JOB_ID,
                "script": f12.name,
                "script_sha256": "2" * 64,
            },
            {
                "id": repin.F13_JOB_ID,
                "script": f13.name,
                "script_sha256": "3" * 64,
            },
            {
                "id": "unrelated",
                "script": "unrelated.py",
                "script_sha256": "4" * 64,
                "chat_name": "Kael HQ / #📈・investment-ops",
                "prompt": "arrow →",
            },
        ]
    }
    jobs.write_text(
        json.dumps(root, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    jobs.chmod(0o600)

    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    return source, f12, f13, jobs, digest


def build_fixture_plan(tmp_path: Path):
    source, f12, f13, jobs, digest = make_fixture(tmp_path)
    plan = repin.build_plan(
        source_path=source,
        f12_path=f12,
        f13_path=f13,
        jobs_path=jobs,
        expected_source_sha256=digest,
    )
    return source, f12, f13, jobs, digest, plan


def test_unfenced_apply_refuses_before_crash_can_truncate_registry(tmp_path: Path) -> None:
    """A process death bypasses the old caught-error rollback entirely."""
    import os
    _source, _f12, _f13, jobs, _digest, plan = build_fixture_plan(tmp_path)
    before = jobs.read_bytes()
    inode = jobs.stat().st_ino
    pid = os.fork()
    if pid == 0:
        truncate = os.ftruncate
        def die_after_registry_truncate(fd, size):
            truncate(fd, size)
            if os.fstat(fd).st_ino == inode:
                os._exit(91)
        os.ftruncate = die_after_registry_truncate
        try:
            repin.apply_plan(plan)
        except repin.RepinError:
            os._exit(0)
        os._exit(92)
    _, status = os.waitpid(pid, 0)
    assert jobs.read_bytes() == before, 'process death left jobs.json truncated'
    assert os.waitstatus_to_exitcode(status) == 0, 'unfenced apply was not refused'


def test_plan_updates_full_dependency_closure(tmp_path: Path) -> None:
    source, f12, f13, jobs, digest, plan = build_fixture_plan(tmp_path)

    assert set(plan.changed_paths) == {str(f12), str(f13), str(jobs)}
    assert digest.encode("ascii") in plan.f12_candidate
    assert digest.encode("ascii") in plan.f13_candidate

    candidate_jobs = json.loads(plan.jobs_candidate)
    jobs_by_id = {job["id"]: job for job in candidate_jobs["jobs"]}
    assert jobs_by_id[repin.F12_JOB_ID]["script_sha256"] == hashlib.sha256(
        plan.f12_candidate
    ).hexdigest()
    assert jobs_by_id[repin.F13_JOB_ID]["script_sha256"] == hashlib.sha256(
        plan.f13_candidate
    ).hexdigest()
    assert "Kael HQ / #📈・investment-ops".encode() in plan.jobs_candidate
    assert "arrow →".encode() in plan.jobs_candidate
    desired_f12 = hashlib.sha256(plan.f12_candidate).hexdigest().encode()
    desired_f13 = hashlib.sha256(plan.f13_candidate).hexdigest().encode()
    restored = plan.jobs_candidate.replace(desired_f12, b"2" * 64, 1).replace(
        desired_f13, b"3" * 64, 1
    )
    assert restored == plan.jobs.data


def test_reviewed_source_mismatch_refuses_without_writes(tmp_path: Path) -> None:
    source, f12, f13, jobs, _digest = make_fixture(tmp_path)
    before = {path: path.read_bytes() for path in (source, f12, f13, jobs)}

    with pytest.raises(repin.RepinError, match="reviewed source digest mismatch"):
        repin.build_plan(
            source_path=source,
            f12_path=f12,
            f13_path=f13,
            jobs_path=jobs,
            expected_source_sha256="f" * 64,
        )

    assert {path: path.read_bytes() for path in before} == before


def test_duplicate_pin_assignment_refuses(tmp_path: Path) -> None:
    source, f12, f13, jobs, digest = make_fixture(tmp_path)
    f13.write_text(
        f13.read_text(encoding="utf-8")
        + 'EXPECTED_REPORTS_ARCHIVER_SHA256 = "' + "4" * 64 + '"\n',
        encoding="utf-8",
    )

    with pytest.raises(repin.RepinError, match="exactly one"):
        repin.build_plan(
            source_path=source,
            f12_path=f12,
            f13_path=f13,
            jobs_path=jobs,
            expected_source_sha256=digest,
        )


def test_wrong_registered_script_refuses(tmp_path: Path) -> None:
    source, f12, f13, jobs, digest = make_fixture(tmp_path)
    root = json.loads(jobs.read_text(encoding="utf-8"))
    root["jobs"][1]["script"] = "wrong.py"
    jobs.write_text(json.dumps(root, indent=2) + "\n", encoding="utf-8")

    with pytest.raises(repin.RepinError, match="script mismatch"):
        repin.build_plan(
            source_path=source,
            f12_path=f12,
            f13_path=f13,
            jobs_path=jobs,
            expected_source_sha256=digest,
        )


@pytest.fixture
def native_fixture(tmp_path, monkeypatch):
    from contextlib import contextmanager, ExitStack
    import types
    native_root = Path('/home/ubuntu/.hermes/hermes-agent')
    if not (native_root / 'cron/jobs.py').is_file():
        pytest.skip('real installed native scheduler unavailable; no fake native success')
    base = tmp_path / 'input'
    base.mkdir()
    source0, f120, f130, jobs0, digest = make_fixture(base)
    root, home = tmp_path / 'business', tmp_path / 'hermes'
    for directory in (root / 'scripts', home / 'scripts', home / 'cron'):
        directory.mkdir(parents=True)
    source, f12, f13, registry = (root / 'scripts' / source0.name,
        home / 'scripts' / f120.name, home / 'scripts' / f130.name, home / 'cron/jobs.json')
    import shutil
    for old, new in ((source0, source), (f120, f12), (f130, f13), (jobs0, registry)):
        shutil.copy2(old, new)
    rows = json.loads(registry.read_bytes())['jobs']
    for row, wrapper in zip(rows[:2], (f12, f13)):
        row.update(enabled=False, state='paused', fire_claim=None, run_claim=None,
                   paused_reason='retained UNKNOWN; no retry', next_run_at='2026-10-01T00:00:00Z',
                   fixture_extension={'keep': ['all', 'fields']},
                   script_sha256=hashlib.sha256(wrapper.read_bytes()).hexdigest())
    registry.write_text(json.dumps({'jobs': rows}, indent=2, ensure_ascii=False))
    (tmp_path / 'common').mkdir()
    (tmp_path / 'common/retention.lock').write_bytes(b'fixture')
    (tmp_path / 'common/retention.lock').chmod(0o600)
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('HERMES_SKIP_ENV_FILE', '1')
    monkeypatch.syspath_prepend(str(native_root))
    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    from cron import jobs as native
    assert Path(native.__file__).resolve() == native_root / 'cron/jobs.py'
    assert native._current_cron_store().jobs_file == registry
    import sync_from_repo as sync
    # The archiver's offhost adapter is uncurated; never exercise it in this test.
    if 'r2_offload' not in sys.modules:
        monkeypatch.setitem(sys.modules, 'r2_offload', types.ModuleType('r2_offload'))
    import reports_archiver as ra
    monkeypatch.setattr(ra, 'COMMON_LOCK_ABSTRACT_SOCKET_NAME',
                        b'\0repin-test-' + hashlib.sha256(str(tmp_path).encode()).hexdigest().encode())

    def plan():
        return repin.build_plan(source_path=source, f12_path=f12, f13_path=f13,
                                jobs_path=registry, expected_source_sha256=digest)

    @contextmanager
    def fences(*, jobs_lock=True):
        with ExitStack() as stack:
            owner = stack.enter_context(sync.source_lock(root))
            for jid in (repin.F12_JOB_ID, repin.F13_JOB_ID):
                assert stack.enter_context(native._fire_job_lock(jid))
            if jobs_lock:
                stack.enter_context(native._jobs_lock(require_cross_process=True))
            common = stack.enter_context(ra.acquire_common_lock(tmp_path / 'common/retention.lock'))
            yield dict(scheduler=native, source_owner=owner, common_lock=common)

    return types.SimpleNamespace(plan=plan, fences=fences, native=native, root=root,
                                 source=source, f12=f12, f13=f13, jobs=registry, rows=rows)


def test_native_apply_preserves_wrappers_but_atomically_replaces_registry(native_fixture) -> None:
    f = native_fixture
    f12, f13, jobs, plan = f.f12, f.f13, f.jobs, f.plan()
    digest = plan.expected_source_sha256
    before = {
        path: (path.stat().st_ino, stat.S_IMODE(path.stat().st_mode))
        for path in (f12, f13, jobs)
    }

    with f.fences() as guards:
        repin.apply_plan(plan, **guards)

    after = {
        path: (path.stat().st_ino, stat.S_IMODE(path.stat().st_mode))
        for path in (f12, f13, jobs)
    }
    assert after[f12] == before[f12] and after[f13] == before[f13]
    assert after[jobs][0] != before[jobs][0]  # native staged atomic replacement
    assert after[jobs][1] == 0o600
    assert json.loads(jobs.read_bytes())['jobs'] == json.loads(plan.jobs_candidate)['jobs']
    assert json.loads(jobs.read_bytes())['jobs'][2] == f.rows[2]
    assert (repin._custody_path(plan) / 'acceptance.json').is_file()
    assert json.loads((repin._custody_path(plan) / 'intent.json').read_bytes())['retry_allowed'] is False

    closed = repin.build_plan(
        source_path=plan.source.path,
        f12_path=f12,
        f13_path=f13,
        jobs_path=jobs,
        expected_source_sha256=digest,
    )
    assert closed.changed_paths == []


def test_cli_check_reports_drift_and_unfenced_apply_refuses(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source, f12, f13, jobs, digest = make_fixture(tmp_path)
    lock = tmp_path / "repin.lock"
    paths = [
        "--expected-source-sha256",
        digest,
        "--source",
        str(source),
        "--f12-wrapper",
        str(f12),
        "--f13-wrapper",
        str(f13),
        "--jobs",
        str(jobs),
        "--lock",
        str(lock),
    ]

    assert repin.main(["--check", *paths]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "drift"

    before = {p: p.read_bytes() for p in (source, f12, f13, jobs)}
    assert repin.main(["--apply", *paths]) == 2
    assert 'lock-held caller' in json.loads(capsys.readouterr().err)['error']
    assert {p: p.read_bytes() for p in before} == before

    assert repin.main(["--check", *paths]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "drift"


def test_partial_f12_only_repin_closes_f13_and_scheduler(native_fixture) -> None:
    f = native_fixture
    source, f12, f13, jobs = f.source, f.f12, f.f13, f.jobs
    source.write_bytes(b"print('reviewed replacement source')\n")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()

    f12.write_text(
        'EXPECTED_PAYLOAD_SHA256 = "' + digest + '"\n', encoding="utf-8"
    )
    root = json.loads(jobs.read_text(encoding="utf-8"))
    jobs_by_id = {job["id"]: job for job in root["jobs"]}
    jobs_by_id[repin.F12_JOB_ID]["script_sha256"] = hashlib.sha256(
        f12.read_bytes()
    ).hexdigest()
    jobs_by_id[repin.F13_JOB_ID]["script_sha256"] = hashlib.sha256(
        f13.read_bytes()
    ).hexdigest()
    jobs.write_text(
        json.dumps(root, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    plan = repin.build_plan(
        source_path=source,
        f12_path=f12,
        f13_path=f13,
        jobs_path=jobs,
        expected_source_sha256=digest,
    )

    assert plan.changed_paths == [str(f13), str(jobs)]
    with f.fences() as guards:
        repin.apply_plan(plan, **guards)
    closed = repin.build_plan(
        source_path=source,
        f12_path=f12,
        f13_path=f13,
        jobs_path=jobs,
        expected_source_sha256=digest,
    )
    assert closed.changed_paths == []


def test_native_raw_flock_is_not_a_native_lease(native_fixture):
    import fcntl
    f = native_fixture
    plan = f.plan()
    before = {s.path: s.data for s in (plan.source, plan.f12, plan.f13, plan.jobs)}
    with f.fences(jobs_lock=False) as guards:
        with f.native._jobs_lock_file().open('a+') as fd:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with pytest.raises(RuntimeError, match='cross-process lock required'):
                repin.apply_plan(plan, **guards)
    assert {p: p.read_bytes() for p in before} == before
    assert not repin._custody_path(plan).exists()


def test_native_degraded_lease_refuses(native_fixture, monkeypatch):
    f = native_fixture
    plan = f.plan()
    with f.fences(jobs_lock=False) as guards:
        monkeypatch.setattr(f.native, '_acquire_flock', lambda *_: False)
        with f.native._jobs_lock():  # actual read-only degraded context
            with pytest.raises(RuntimeError, match='cross-process lock required'):
                repin.apply_plan(plan, **guards)
    assert f.jobs.read_bytes() == plan.jobs.data
    assert f.f12.read_bytes() == plan.f12.data
    assert not repin._custody_path(plan).exists()


@pytest.mark.parametrize('drift', ['source', 'wrapper', 'target_claim'])
def test_native_preimage_or_claim_drift_refuses_without_writes(native_fixture, drift):
    f = native_fixture
    plan = f.plan()
    with f.fences() as guards:
        if drift == 'source':
            f.source.write_bytes(b'changed unreviewed source')
        elif drift == 'wrapper':
            f.f12.write_bytes(plan.f12.data + b'# unreviewed\n')
        else:
            rows = json.loads(f.jobs.read_bytes())['jobs']
            rows[0]['fire_claim'] = {'by': 'fixture-owner', 'at': '2026-10-03T00:00:00Z'}
            f.native._save_jobs_unlocked(rows)
            plan = f.plan()  # even a fresh plan cannot admit a claimed target
        before = {p: p.read_bytes() for p in (f.source, f.f12, f.f13, f.jobs)}
        with pytest.raises(repin.RepinError, match='changed after planning|paused drained'):
            repin.apply_plan(plan, **guards)
        assert {p: p.read_bytes() for p in before} == before
        assert not repin._custody_path(plan).exists()


def test_fresh_unrelated_rows_survive_and_stale_plan_is_not_replayed(native_fixture):
    f = native_fixture
    reviewed = f.plan()
    with f.fences() as guards:
        rows = json.loads(f.jobs.read_bytes())['jobs']
        rows[2]['last_status'] = 'fresh unrelated completion'
        rows.append({'id': 'new-unrelated', 'state': 'paused', 'opaque': {'keep': 42}})
        f.native._save_jobs_unlocked(rows)
        with pytest.raises(repin.RepinError, match='changed after planning'):
            repin.apply_plan(reviewed, **guards)
        fresh = f.plan()
        assert (fresh.source, fresh.f12, fresh.f13) == (reviewed.source, reviewed.f12, reviewed.f13)
        assert json.loads(fresh.jobs.data)['jobs'][:2] == json.loads(reviewed.jobs.data)['jobs'][:2]
        repin.apply_plan(fresh, **guards)
        assert json.loads(f.jobs.read_bytes())['jobs'][2:] == rows[2:]


def test_native_save_then_raise_never_rolls_registry_or_wrappers_back(native_fixture, monkeypatch):
    f = native_fixture
    plan = f.plan()
    save = f.native._save_jobs_unlocked
    calls = []
    def save_then_raise(rows):
        calls.append(rows)
        save(rows)
        raise OSError('lost acknowledgement after atomic commit')
    monkeypatch.setattr(f.native, '_save_jobs_unlocked', save_then_raise)
    with f.fences() as guards:
        with pytest.raises(repin.RepinError, match='UNKNOWN.*no replay'):
            repin.apply_plan(plan, **guards)
        assert f.f12.read_bytes() == plan.f12_candidate
        assert f.f13.read_bytes() == plan.f13_candidate
        assert json.loads(f.jobs.read_bytes())['jobs'] == json.loads(plan.jobs_candidate)['jobs']
        with pytest.raises(repin.RepinError, match='prior adoption intent.*no replay'):
            repin.apply_plan(plan, **guards)
    assert len(calls) == 1
    assert not (repin._custody_path(plan) / 'acceptance.json').exists()


def test_process_death_at_native_commit_leaves_complete_json_and_no_replay(native_fixture):
    import os
    f = native_fixture
    plan = f.plan()
    pid = os.fork()
    if pid == 0:
        replace = f.native.atomic_replace
        def die_after_atomic_commit(src, dst):
            replace(src, dst)
            os._exit(94)
        f.native.atomic_replace = die_after_atomic_commit
        with f.fences() as guards:
            repin.apply_plan(plan, **guards)
        os._exit(95)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 94
    assert json.loads(f.jobs.read_bytes())['jobs'] == json.loads(plan.jobs_candidate)['jobs']
    assert f.jobs.stat().st_ino != plan.jobs.inode
    assert f.f12.read_bytes() == plan.f12_candidate
    assert f.f13.read_bytes() == plan.f13_candidate
    with f.fences() as guards:
        with pytest.raises(repin.RepinError, match='prior adoption intent.*no replay'):
            repin.apply_plan(plan, **guards)
    assert not (repin._custody_path(plan) / 'acceptance.json').exists()
