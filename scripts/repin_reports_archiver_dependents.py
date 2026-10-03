#!/usr/bin/env python3
"""Fail-closed, review-gated repinning for reports_archiver.py dependents.

The operator must supply the already-reviewed source digest.  The tool then
updates the two wrapper source pins and their scheduler wrapper pins as one
validated closure.  It never derives authority from whatever bytes happen to
be present at the source path.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
from dataclasses import dataclass
from typing import Any, Sequence

SOURCE = Path("/home/ubuntu/business/scripts/reports_archiver.py")
F12_WRAPPER = Path("/home/ubuntu/.hermes/scripts/f12_generator_archiver.py")
F13_WRAPPER = Path("/home/ubuntu/.hermes/scripts/f13_growth_retention.py")
JOBS = Path("/home/ubuntu/.hermes/cron/jobs.json")
LOCK = Path("/tmp/hermes-reports-archiver-repin.lock")
F12_JOB_ID = "f3d2db07d83f"
F13_JOB_ID = "69a28f878ce3"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class RepinError(RuntimeError):
    """The closure was unsafe, malformed, or changed during application."""


@dataclass(frozen=True)
class Snapshot:
    path: Path
    data: bytes
    device: int
    inode: int
    mode: int


@dataclass(frozen=True)
class Plan:
    source: Snapshot
    f12: Snapshot
    f13: Snapshot
    jobs: Snapshot
    expected_source_sha256: str
    f12_candidate: bytes
    f13_candidate: bytes
    jobs_candidate: bytes

    @property
    def candidates(self) -> tuple[tuple[Snapshot, bytes], ...]:
        return (
            (self.f12, self.f12_candidate),
            (self.f13, self.f13_candidate),
            (self.jobs, self.jobs_candidate),
        )

    @property
    def changed_paths(self) -> list[str]:
        return [str(snapshot.path) for snapshot, candidate in self.candidates if snapshot.data != candidate]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_snapshot(path: Path) -> Snapshot:
    path = Path(path)
    before = os.lstat(path)
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise RepinError(f"refusing non-regular or symlink path: {path}")

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise RepinError(f"refusing non-regular opened path: {path}")
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise RepinError(f"path identity changed while reading: {path}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(fd)
        if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise RepinError(f"file changed while reading: {path}")
        return Snapshot(
            path=path,
            data=b"".join(chunks),
            device=opened.st_dev,
            inode=opened.st_ino,
            mode=stat.S_IMODE(opened.st_mode),
        )
    finally:
        os.close(fd)


def _replace_digest_assignment(data: bytes, variable: str, digest: str) -> bytes:
    name = re.escape(variable.encode("ascii"))
    pattern = re.compile(
        rb"(?m)^(" + name + rb"[ \t]*=[ \t]*[\"'])([0-9a-f]{64})([\"'][ \t]*)$"
    )
    matches = list(pattern.finditer(data))
    if len(matches) != 1:
        raise RepinError(f"expected exactly one {variable} digest assignment, found {len(matches)}")
    return pattern.sub(lambda match: match.group(1) + digest.encode("ascii") + match.group(3), data, count=1)


def _compile_wrapper(path: Path, data: bytes) -> None:
    try:
        text = data.decode("utf-8")
        compile(text, str(path), "exec")
    except (UnicodeDecodeError, SyntaxError) as exc:
        raise RepinError(f"prospective wrapper does not compile: {path}: {exc}") from exc


def _load_jobs(data: bytes, path: Path) -> dict[str, Any]:
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RepinError(f"invalid jobs JSON at {path}: {exc}") from exc
    if not isinstance(value, dict) or not isinstance(value.get("jobs"), list):
        raise RepinError(f"jobs JSON lacks a jobs list: {path}")
    return value


def _one_job(root: dict[str, Any], job_id: str) -> dict[str, Any]:
    matches = [job for job in root["jobs"] if isinstance(job, dict) and job.get("id") == job_id]
    if len(matches) != 1:
        raise RepinError(f"expected exactly one scheduler job {job_id}, found {len(matches)}")
    return matches[0]


def _validate_job(job: dict[str, Any], job_id: str, expected_script: Path) -> None:
    if job.get("script") != expected_script.name:
        raise RepinError(
            f"scheduler job {job_id} script mismatch: expected {expected_script.name!r}, got {job.get('script')!r}"
        )
    current_hash = job.get("script_sha256")
    if not isinstance(current_hash, str) or not _HEX64.fullmatch(current_hash):
        raise RepinError(f"scheduler job {job_id} has malformed script_sha256")


def _replace_registered_script_hash(
    data: bytes,
    *,
    job_id: str,
    current_sha256: str,
    desired_sha256: str,
) -> bytes:
    if current_sha256 == desired_sha256:
        return data
    old = b'"script_sha256": "' + current_sha256.encode("ascii") + b'"'
    new = b'"script_sha256": "' + desired_sha256.encode("ascii") + b'"'
    count = data.count(old)
    if count != 1:
        raise RepinError(
            f"expected one byte-exact script_sha256 field for job {job_id}, found {count}"
        )
    return data.replace(old, new, 1)


def build_plan(
    *,
    source_path: Path = SOURCE,
    f12_path: Path = F12_WRAPPER,
    f13_path: Path = F13_WRAPPER,
    jobs_path: Path = JOBS,
    expected_source_sha256: str,
) -> Plan:
    if not _HEX64.fullmatch(expected_source_sha256):
        raise RepinError("--expected-source-sha256 must be exactly 64 lowercase hexadecimal characters")

    source = _read_snapshot(source_path)
    actual_source_sha256 = _sha256(source.data)
    if actual_source_sha256 != expected_source_sha256:
        raise RepinError(
            "reviewed source digest mismatch: "
            f"expected {expected_source_sha256}, observed {actual_source_sha256}"
        )

    f12 = _read_snapshot(f12_path)
    f13 = _read_snapshot(f13_path)
    jobs = _read_snapshot(jobs_path)

    f12_candidate = _replace_digest_assignment(
        f12.data, "EXPECTED_PAYLOAD_SHA256", expected_source_sha256
    )
    f13_candidate = _replace_digest_assignment(
        f13.data, "EXPECTED_REPORTS_ARCHIVER_SHA256", expected_source_sha256
    )
    _compile_wrapper(f12.path, f12_candidate)
    _compile_wrapper(f13.path, f13_candidate)

    jobs_root = _load_jobs(jobs.data, jobs.path)
    f12_job = _one_job(jobs_root, F12_JOB_ID)
    f13_job = _one_job(jobs_root, F13_JOB_ID)
    _validate_job(f12_job, F12_JOB_ID, f12.path)
    _validate_job(f13_job, F13_JOB_ID, f13.path)
    f12_registered = f12_job["script_sha256"]
    f13_registered = f13_job["script_sha256"]
    assert isinstance(f12_registered, str)
    assert isinstance(f13_registered, str)
    f12_desired = _sha256(f12_candidate)
    f13_desired = _sha256(f13_candidate)
    jobs_candidate = _replace_registered_script_hash(
        jobs.data,
        job_id=F12_JOB_ID,
        current_sha256=f12_registered,
        desired_sha256=f12_desired,
    )
    jobs_candidate = _replace_registered_script_hash(
        jobs_candidate,
        job_id=F13_JOB_ID,
        current_sha256=f13_registered,
        desired_sha256=f13_desired,
    )

    # Validate the complete prospective closure rather than only the edits.
    prospective_jobs = _load_jobs(jobs_candidate, jobs.path)
    prospective_f12 = _one_job(prospective_jobs, F12_JOB_ID)
    prospective_f13 = _one_job(prospective_jobs, F13_JOB_ID)
    _validate_job(prospective_f12, F12_JOB_ID, f12.path)
    _validate_job(prospective_f13, F13_JOB_ID, f13.path)
    if prospective_f12["script_sha256"] != _sha256(f12_candidate):
        raise RepinError("prospective F12 scheduler digest does not match prospective wrapper")
    if prospective_f13["script_sha256"] != _sha256(f13_candidate):
        raise RepinError("prospective F13 scheduler digest does not match prospective wrapper")

    return Plan(
        source=source,
        f12=f12,
        f13=f13,
        jobs=jobs,
        expected_source_sha256=expected_source_sha256,
        f12_candidate=f12_candidate,
        f13_candidate=f13_candidate,
        jobs_candidate=jobs_candidate,
    )


def _read_all_fd(fd: int) -> bytes:
    os.lseek(fd, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _write_all_fd(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


def _rewrite_checked(snapshot: Snapshot, expected_current: bytes, replacement: bytes) -> None:
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(snapshot.path, flags)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise RepinError(f"refusing non-regular opened path: {snapshot.path}")
        if (opened.st_dev, opened.st_ino) != (snapshot.device, snapshot.inode):
            raise RepinError(f"path identity changed before write: {snapshot.path}")
        if stat.S_IMODE(opened.st_mode) != snapshot.mode:
            raise RepinError(f"path mode changed before write: {snapshot.path}")
        observed = _read_all_fd(fd)
        if observed != expected_current:
            raise RepinError(f"file bytes changed before write: {snapshot.path}")

        try:
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            _write_all_fd(fd, replacement)
            os.fsync(fd)
        except BaseException:
            # Restore through the already-verified descriptor if our own write
            # was interrupted.  This keeps the closure at its prior state.
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                os.ftruncate(fd, 0)
                _write_all_fd(fd, expected_current)
                os.fsync(fd)
            finally:
                raise
    finally:
        os.close(fd)


def _assert_snapshot_current(snapshot: Snapshot) -> None:
    current = _read_snapshot(snapshot.path)
    if (
        current.device != snapshot.device
        or current.inode != snapshot.inode
        or current.mode != snapshot.mode
        or current.data != snapshot.data
    ):
        raise RepinError(f"file changed after planning: {snapshot.path}")


def _fsync_parent_directories(paths: Sequence[Path]) -> None:
    for parent in sorted({str(path.parent) for path in paths}):
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        fd = os.open(parent, flags)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _custody_path(plan: Plan) -> Path:
    # Reuse the maintained pin-archive custody, not a second registry. Unrelated
    # cron progress must not mint another attempt after an uncertain outcome.
    rows = _load_jobs(plan.jobs.data, plan.jobs.path)
    identity = [plan.expected_source_sha256, _sha256(plan.f12.data), _sha256(plan.f13.data),
                [_one_job(rows, jid) for jid in (F12_JOB_ID, F13_JOB_ID)]]
    key = _sha256(json.dumps(identity, sort_keys=True).encode())
    return plan.source.path.parent.parent / 'reports/ops/pin-archive/reports-archiver' / key


def apply_plan(plan: Plan, *, scheduler=None, source_owner=None, common_lock=None) -> None:
    """Apply once under the owner's continuous source/fire/jobs/common custody.

    The caller obtains independent exact source/runtime acceptance and durable
    receiving custody before calling, and holds these actual locks IN ORDER:
    sync.source_lock(root), both native _fire_job_lock, native
    _jobs_lock(require_cross_process=True), reports_archiver.acquire_common_lock.
    Direct/pre-lock readers and nonparticipating writers must already be drained.
    They remain excluded on any error, interruption or uncertain acknowledgement.
    This function never opens a window, releases a hold, installs source or retries.

    Rebuild a fresh plan under those locks only after comparing the reviewed full
    target records and exact source/wrapper preimages. A stale whole-registry plan
    is refused, never rebased or rolled back here. Native owns registry encoding,
    updated_at, atomic replace, permissions and uncertain-save semantics.
    """
    if scheduler is None or source_owner is None or common_lock is None:
        raise RepinError('reviewed lock-held caller required; unfenced apply refused')
    import copy
    import sqlite3
    import sync_from_repo as sync
    from maintenance_admission import native_runs
    from maxbounty_pin_release import preserve_custody, atomic_write
    from cron.claim_disposition import require_no_pending_disposition

    root = plan.source.path.parent.parent
    store = scheduler._current_cron_store()
    if (plan.jobs.path != store.jobs_file or scheduler._jobs_lock_file() != store.cron_dir / '.jobs.lock'
            or plan.f12.path != store.cron_dir.parent / 'scripts' / F12_WRAPPER.name
            or plan.f13.path != store.cron_dir.parent / 'scripts' / F13_WRAPPER.name):
        raise RepinError('native store or wrapper binding moved')
    # Validate the already-held native leases BEFORE entering a nested context;
    # a raw flock, degraded lease, or acquiring jobs after common is not custody.
    for jid in (F12_JOB_ID, F13_JOB_ID):
        scheduler._require_disposition_lock_custody(jid)
    with sync.source_session(root, source_owner), scheduler._jobs_lock(require_cross_process=True):
        common_lock.assert_owned()
        custody = _custody_path(plan)
        intent = custody / 'intent.json'
        if intent.exists():
            raise RepinError('prior adoption intent: reconcile exact outcome; no replay')
        for snapshot in (plan.source, plan.f12, plan.f13, plan.jobs):
            _assert_snapshot_current(snapshot)
        fresh = build_plan(source_path=plan.source.path, f12_path=plan.f12.path,
                           f13_path=plan.f13.path, jobs_path=plan.jobs.path,
                           expected_source_sha256=plan.expected_source_sha256)
        if fresh != plan:
            raise RepinError('candidate differs from the exact fresh plan')
        before = _load_jobs(plan.jobs.data, plan.jobs.path)
        for jid, wrapper in ((F12_JOB_ID, plan.f12), (F13_JOB_ID, plan.f13)):
            row = _one_job(before, jid)
            if (row.get('enabled') is not False or row.get('state') != 'paused'
                    or row.get('fire_claim') is not None or row.get('run_claim') is not None
                    or row.get('running') or row['script_sha256'] != _sha256(wrapper.data)):
                raise RepinError('paused drained exact wrapper registration required:' + jid)
            require_no_pending_disposition(jid, store.cron_dir)
        entries = [{'kind': 'hermes', 'id': jid} for jid in (F12_JOB_ID, F13_JOB_ID)]
        busy, owners = native_runs(scheduler, entries, lambda jid: _one_job(before, jid))
        if busy or owners:
            raise RepinError('native target executions not drained')
        database = store.cron_dir / 'executions.db'
        if database.exists():
            connection = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)
            try:
                if connection.execute("SELECT 1 FROM executions WHERE job_id IN (?, ?) "
                                      "AND status IN ('claimed', 'running') LIMIT 1",
                                      (F12_JOB_ID, F13_JOB_ID)).fetchone():
                    raise RepinError('active target execution remains')
            finally:
                connection.close()
        if not plan.changed_paths:
            return
        candidate = _load_jobs(plan.jobs_candidate, plan.jobs.path)['jobs']
        observed = {s.path: (s.data, s.mode) for s in (plan.source, plan.f12, plan.f13, plan.jobs)}
        preserve_custody(custody, observed, {s.path: b for s, b in plan.candidates})
        # O_EXCL intent, fsynced before the FIRST wrapper mutation. Process death
        # cannot erase it; even a pre-save interruption requires reconciliation.
        with intent.open('xb') as out:
            out.write(json.dumps({'status': 'intent', 'retry_allowed': False,
                                  'jobs_before_sha256': _sha256(plan.jobs.data),
                                  **_summary(plan, 'apply', 'intent')}, sort_keys=True).encode())
            out.flush()
            os.fsync(out.fileno())
        # Fsync the newly created custody ancestry through the existing source
        # root too: a durable leaf is insufficient if its parent entry is lost.
        lineage = [intent, custody]
        parent = custody.parent
        while parent != root:
            lineage.append(parent)
            parent = parent.parent
        _fsync_parent_directories(lineage)
        changed = []
        save_started = False
        try:
            for snapshot in (plan.source, plan.f12, plan.f13, plan.jobs):
                _assert_snapshot_current(snapshot)
            common_lock.assert_owned()
            for snapshot, replacement in plan.candidates[:2]:
                if snapshot.data != replacement:
                    _rewrite_checked(snapshot, snapshot.data, replacement)
                    changed.append((snapshot, replacement))
            _fsync_parent_directories([s.path for s, _ in changed])
            _assert_snapshot_current(plan.source)
            _assert_snapshot_current(plan.jobs)
            for jid in (F12_JOB_ID, F13_JOB_ID):
                scheduler._require_disposition_lock_custody(jid)
            common_lock.assert_owned()
            save_started = True
            # Never pass replace=True or mutate the readback oracle in the saver.
            scheduler._save_jobs_unlocked(copy.deepcopy(candidate))
            if _load_jobs(_read_snapshot(plan.jobs.path).data, plan.jobs.path)['jobs'] != candidate:
                raise RepinError('native readback mismatch')
            _assert_snapshot_current(plan.source)
            for snapshot, replacement in plan.candidates[:2]:
                current = _read_snapshot(snapshot.path)
                if (current.data, current.device, current.inode, current.mode) != (
                        replacement, snapshot.device, snapshot.inode, snapshot.mode):
                    raise RepinError('wrapper readback mismatch')
            common_lock.assert_owned()
            atomic_write(custody / 'acceptance.json', json.dumps(
                _summary(plan, 'apply', 'applied'), sort_keys=True).encode(), 0o600)
        except BaseException as exc:
            if save_started:
                # Even save-then-raise or a failed acknowledgement must NEVER
                # restore a stale registry or roll wrappers back behind its pins.
                raise RepinError('native commit/ack UNKNOWN; retain custody and holds; no replay') from exc
            for snapshot, replacement in reversed(changed):
                _rewrite_checked(snapshot, replacement, snapshot.data)
            raise


def _summary(plan: Plan, mode: str, status: str) -> dict[str, Any]:
    return {
        "mode": mode,
        "status": status,
        "source": str(plan.source.path),
        "source_sha256": plan.expected_source_sha256,
        "f12_wrapper_sha256": _sha256(plan.f12_candidate),
        "f13_wrapper_sha256": _sha256(plan.f13_candidate),
        "changed_paths": plan.changed_paths,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true", help="validate and report closure drift")
    action.add_argument("--apply", action="store_true", help="legacy spelling; refuses without reviewed lock-held callable custody")
    parser.add_argument("--expected-source-sha256", required=True)
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--f12-wrapper", type=Path, default=F12_WRAPPER)
    parser.add_argument("--f13-wrapper", type=Path, default=F13_WRAPPER)
    parser.add_argument("--jobs", type=Path, default=JOBS)
    parser.add_argument("--lock", type=Path, default=LOCK)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    args.lock.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(args.lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        plan = build_plan(
            source_path=args.source,
            f12_path=args.f12_wrapper,
            f13_path=args.f13_wrapper,
            jobs_path=args.jobs,
            expected_source_sha256=args.expected_source_sha256,
        )
        if args.check:
            drifted = bool(plan.changed_paths)
            print(json.dumps(_summary(plan, "check", "drift" if drifted else "current"), sort_keys=True))
            return 1 if drifted else 0

        # A standalone CLI cannot inherit authenticated in-process source,
        # common and native lock leases. Keep the old spelling fail-closed.
        raise RepinError('reviewed lock-held caller required; use apply_plan, not standalone --apply')
    except (OSError, RepinError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2
    finally:
        os.close(lock_fd)


if __name__ == "__main__":
    raise SystemExit(main())
